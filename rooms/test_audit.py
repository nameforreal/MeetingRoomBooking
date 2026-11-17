from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, connections, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import audit, booking_operations as operations
from .account_operations import save_account, set_active
from .models import AuditEvent, Booking, BookingCreationRequest, Room
from .retention import delete_empty


class AuditFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audit-owner")
        self.other = get_user_model().objects.create_user(username="audit-other")
        self.staff = get_user_model().objects.create_user(username="audit-staff", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label__in=["rooms", "auth"],
            codename__in=["add_booking", "change_booking", "view_booking", "change_room", "view_room", "change_user", "view_user"]))
        self.root = get_user_model().objects.create_superuser(username="audit-root", password="test-only", email="")
        self.room = Room.objects.create(name="Audit room")
        self.other_room = Room.objects.create(name="Other audit room")
        self.start = (timezone.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.key = str(uuid4())

    def booking(self, **changes):
        return Booking.objects.create(**{"room": self.room, "user": self.user,
                                         "start_time": self.start, "end_time": self.end, **changes})

    def create_api(self, **changes):
        return self.api.post("/api/bookings/", {"room": self.room.pk, "start_time": self.start.isoformat(),
                                               "end_time": self.end.isoformat(), **changes},
                             format="json", HTTP_IDEMPOTENCY_KEY=self.key)

    def html_data(self, **changes):
        return {"idempotency_key": self.key, "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00", **changes}

    def event(self, action=None):
        qs = AuditEvent.objects.all()
        return qs.filter(action=action).get() if action else qs.latest("id")


class RetentionTests(AuditFixtures, TestCase):
    def test_booking_model_queryset_and_base_manager_cannot_delete(self):
        booking = self.booking()
        for delete in [booking.delete, lambda: Booking.objects.filter(pk=booking.pk).delete(),
                       lambda: Booking._base_manager.filter(pk=booking.pk).delete()]:
            with self.assertRaises(ProtectedError):
                delete()
        self.assertEqual(Booking.objects.count(), 1)

    def test_legacy_booking_fks_protect_room_and_user(self):
        # Legacy fixture without an audit event or a creation receipt.
        booking = Booking(room=self.room, user=self.user, start_time=self.start, end_time=self.end, revision=0)
        models.Model.save(booking, force_insert=True)
        self.assertEqual(AuditEvent.objects.count(), 0)
        for target in [self.room, self.user]:
            with self.assertRaises(ProtectedError):
                models.Model.delete(target)
        self.assertEqual(Booking.objects.count(), 1)

    def test_cancelled_and_completed_history_cannot_be_deleted(self):
        booking = self.booking()
        operations.cancel(booking, actor=self.user, revision=booking.revision)
        with patch("django.utils.timezone.now", return_value=self.end + timedelta(days=1)):
            for target in [booking, self.room, self.user]:
                with self.assertRaises(ProtectedError):
                    target.delete()
        self.assertEqual(Booking.objects.get().status, "cancelled")

    def test_room_and_user_ordinary_bulk_deletion_is_forbidden(self):
        for delete in [lambda: Room.objects.all().delete(), lambda: Room._base_manager.all().delete(),
                       lambda: get_user_model().objects.filter(pk=self.other.pk).delete()]:
            with self.assertRaises(ProtectedError):
                with transaction.atomic():
                    delete()
        self.assertTrue(Room.objects.filter(pk=self.other_room.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.other.pk).exists())

    def test_superuser_cannot_delete_through_admin(self):
        booking = self.booking()
        self.client.force_login(self.root)
        for model, pk in [("rooms/booking", booking.pk), ("rooms/room", self.room.pk), ("auth/user", self.user.pk)]:
            response = self.client.post(f"/admin/{model}/{pk}/delete/", {"post": "yes"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(Booking.objects.count(), 1)

    def test_admin_mass_delete_is_not_available_or_executable(self):
        booking = self.booking()
        self.client.force_login(self.root)
        for model, pk in [("rooms/booking", booking.pk), ("rooms/room", self.room.pk), ("auth/user", self.user.pk)]:
            response = self.client.get(f"/admin/{model}/")
            self.assertNotContains(response, 'value="delete_selected"')
            self.client.post(f"/admin/{model}/", {"action": "delete_selected", "_selected_action": [pk], "post": "yes"})
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_api_delete_booking_forbidden_room_archived(self):
        booking = self.booking()
        self.assertEqual(self.api.delete(f"/api/bookings/{booking.pk}/").status_code, 405)
        self.api.force_authenticate(self.staff)
        self.assertEqual(self.api.delete(f"/api/rooms/{self.room.pk}/").status_code, 204)
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_active)
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())

    def test_controlled_empty_deletion_records_tombstone(self):
        pk = self.other_room.pk
        delete_empty(self.other_room, actor=self.root)
        self.assertFalse(Room.objects.filter(pk=pk).exists())
        event = self.event("room.deleted")
        self.assertEqual((event.object_id, event.room_id, event.actor_id), (pk, None, self.root.pk))
        user_pk = self.other.pk
        delete_empty(self.other, actor=self.root)
        self.assertFalse(get_user_model().objects.filter(pk=user_pk).exists())
        self.assertEqual(self.event("user.deleted").object_id, user_pk)

    def test_controlled_delete_requires_superuser_and_no_history(self):
        with self.assertRaises(PermissionDenied):
            delete_empty(self.other_room, actor=self.staff)
        booking = self.booking()
        for obj in [booking, self.room, self.user]:
            with self.assertRaises((PermissionDenied, ProtectedError)):
                delete_empty(obj, actor=self.root)
        with self.assertRaises(PermissionDenied):
            delete_empty(self.root, actor=self.root)

    def test_former_room_and_owner_are_protected(self):
        booking = self.booking()
        operations.edit(booking, actor=self.user, revision=booking.revision, room=self.other_room)
        booking.user = self.other
        operations.persist(booking, actor=self.staff, revision=booking.revision)
        for old in [self.room, self.user]:
            with self.assertRaises(ProtectedError):
                delete_empty(old, actor=self.root)
        self.assertEqual(self.event("booking.owner_changed").previous_owner_id, self.user.pk)

    def test_legacy_admin_log_author_is_protected(self):
        LogEntry.objects.log_actions(user_id=self.staff.pk, queryset=[self.room], action_flag=2, change_message="fixture")
        with self.assertRaises(ProtectedError):
            delete_empty(self.staff, actor=self.root)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_empty_delete_event_failure_rolls_back(self):
        with patch("rooms.audit.record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                delete_empty(self.other_room, actor=self.root)
        self.assertTrue(Room.objects.filter(pk=self.other_room.pk).exists())


class AuditEventTests(AuditFixtures, TestCase):
    def test_api_creation_actor_source_and_values(self):
        self.assertEqual(self.create_api().status_code, 201)
        event = self.event("booking.created")
        self.assertEqual((event.actor_id, event.source, event.revision_before, event.revision_after), (self.user.pk, "api", None, 1))
        self.assertEqual(event.before, {})
        self.assertEqual(event.after, audit.booking_state(Booking.objects.get()))
        self.assertEqual((event.new_room_id, event.new_owner_id), (self.room.pk, self.user.pk))

    def test_system_model_operation_has_null_actor(self):
        self.booking()
        event = self.event()
        self.assertEqual((event.source, event.actor_id), ("system", None))

    def test_html_creation_and_edit_source(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_data()).status_code, 302)
        self.assertEqual(self.event().source, "html")
        booking = Booking.objects.get()
        self.assertEqual(self.client.post(f"/my-bookings/{booking.pk}/edit/",
                                         self.html_data(revision=1, end_time_1="12:00")).status_code, 302)
        event = self.event()
        self.assertEqual((event.action, event.source, event.actor_id), ("booking.time_changed", "html", self.user.pk))
        self.assertEqual(event.before["revision"], 1)
        self.assertEqual(event.after["revision"], 2)

    def test_admin_creation_source(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post("/admin/rooms/booking/add/", self.html_data(
            room=self.room.pk, user=self.user.pk, status="confirmed")).status_code, 302)
        event = self.event()
        self.assertEqual((event.actor_id, event.source), (self.staff.pk, "django_admin"))

    def test_time_room_and_owner_changes_preserve_before_after(self):
        booking = self.booking()
        before = audit.booking_state(booking)
        operations.edit(booking, actor=self.user, revision=1, room=self.other_room,
                        start_time=self.start + timedelta(hours=1), end_time=self.end + timedelta(hours=1))
        event = self.event()
        self.assertEqual(event.before, before)
        self.assertEqual(event.after, audit.booking_state(booking))
        self.assertEqual((event.previous_room_id, event.new_room_id), (self.room.pk, self.other_room.pk))
        self.assertEqual(set(event.changed_fields), {"room_id", "start_time", "end_time", "revision"})
        booking.user = self.other
        operations.persist(booking, actor=self.staff, revision=2)
        self.assertEqual(self.event().action, "booking.owner_changed")
        self.assertEqual((self.event().previous_owner_id, self.event().new_owner_id), (self.user.pk, self.other.pk))

    def test_management_confirmation_and_cancellation_are_distinct(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()
        self.client.force_login(self.staff)
        url = f"/management/bookings/{booking.pk}/status/"
        self.assertEqual(self.client.post(url, {"status": "confirmed", "revision": 1}).status_code, 302)
        event = self.event("booking.confirmed")
        self.assertEqual((event.actor_id, event.source, event.before["status"], event.after["status"]),
                         (self.staff.pk, "management", "pending", "confirmed"))
        self.client.post(url, {"status": "cancelled", "revision": 2})
        self.assertEqual(self.event().action, "booking.cancelled")

    def test_admin_rejection_records_explicit_intent(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()
        self.client.force_login(self.staff)
        self.client.post("/admin/rooms/booking/", {"action": "reject_bookings", "_selected_action": [booking.pk],
                                                 f"revision_{booking.pk}": 1})
        event = self.event("booking.vip_rejected")
        self.assertEqual((event.source, event.after["status"]), ("django_admin", "cancelled"))

    def test_mass_action_correlation_and_partial_failure(self):
        self.room.is_vip = True
        self.room.save()
        bookings = [self.booking(start_time=self.start + timedelta(hours=i), end_time=self.end + timedelta(hours=i)) for i in range(3)]
        self.client.force_login(self.staff)
        self.client.post("/admin/rooms/booking/", {"action": "confirm_bookings", "_selected_action": [b.pk for b in bookings],
                                                  **{f"revision_{b.pk}": 1 if i < 2 else 0 for i, b in enumerate(bookings)}})
        events = list(AuditEvent.objects.filter(action="booking.confirmed"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].correlation_id, events[1].correlation_id)
        self.assertEqual({e.actor_id for e in events}, {self.staff.pk})
        self.assertFalse(AuditEvent.objects.filter(booking=bookings[2], revision_after=2).exists())

    def test_room_type_change_records_all_resets_atomically(self):
        booking = self.booking()
        self.api.force_authenticate(self.staff)
        self.assertEqual(self.api.patch(f"/api/rooms/{self.room.pk}/", {"is_vip": True}, format="json").status_code, 200)
        room_event = self.event("room.type_changed")
        booking_event = self.event("booking.approval_reset")
        self.assertEqual(room_event.correlation_id, booking_event.correlation_id)
        self.assertEqual((booking_event.actor_id, booking_event.source, booking_event.revision_after), (self.staff.pk, "api", 2))
        self.assertEqual(booking_event.before["status"], "confirmed")
        self.assertEqual(booking_event.after["status"], "pending")

    def test_room_type_audit_error_rolls_back_room_and_bookings(self):
        booking = self.booking()
        original = audit.record
        def fail_reset(**kwargs):
            if kwargs["action"] == "booking.approval_reset":
                raise RuntimeError("audit failure")
            return original(**kwargs)
        self.room.is_vip = True
        with patch("rooms.audit.record", side_effect=fail_reset):
            with self.assertRaises(RuntimeError):
                self.room.save()
        self.room.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(self.room.is_vip)
        self.assertEqual((booking.status, booking.revision), ("confirmed", 1))
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_creation_audit_failure_rolls_back_receipt_and_booking(self):
        with patch("rooms.audit.record", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                self.create_api()
        self.assertFalse(Booking.objects.exists())
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_edit_audit_failure_rolls_back_revision_and_data(self):
        booking = self.booking()
        with patch("rooms.audit.record", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                operations.edit(booking, actor=self.user, revision=1, end_time=self.end + timedelta(hours=1))
        booking.refresh_from_db()
        self.assertEqual((booking.revision, booking.end_time), (1, self.end))
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_error_after_event_insert_rolls_back_event_too(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.create_api()
                raise RuntimeError("outer failure")
        self.assertEqual((Booking.objects.count(), BookingCreationRequest.objects.count(), AuditEvent.objects.count()), (0, 0, 0))

    def test_creation_replay_after_cancel_has_no_new_event(self):
        self.create_api()
        booking = Booking.objects.get()
        operations.cancel(booking, actor=self.user, revision=1)
        before = AuditEvent.objects.count()
        self.assertEqual(self.create_api().status_code, 201)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_data()).status_code, 302)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_repeated_cancel_has_no_event_but_ordinary_save_keeps_revision_contract(self):
        booking = self.booking()
        operations.edit(booking, actor=self.user, revision=1)
        self.assertEqual((self.event().action, self.event().revision_after), ("booking.saved", 2))
        operations.cancel(booking, actor=self.user, revision=2)
        operations.cancel(booking, actor=self.user, revision=2)
        self.assertEqual(AuditEvent.objects.filter(booking=booking).count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="booking.cancelled").count(), 1)

    def test_repeated_archive_and_explicit_restore(self):
        self.client.force_login(self.staff)
        url = f"/management/rooms/{self.room.pk}/toggle/"
        for _ in range(2):
            self.assertEqual(self.client.post(url, {"is_active": "false"}).status_code, 302)
        self.assertEqual(AuditEvent.objects.count(), 1)
        event = self.event("room.archived")
        self.assertEqual((event.actor_id, event.source), (self.staff.pk, "management"))
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(self.client.post(url, {"is_active": "true"}).status_code, 302)
        self.assertEqual(AuditEvent.objects.count(), 2)
        self.assertEqual(self.event().action, "room.restored")

    def test_repeated_api_archive_has_one_event(self):
        self.api.force_authenticate(self.staff)
        for _ in range(2):
            self.assertEqual(self.api.delete(f"/api/rooms/{self.room.pk}/").status_code, 204)
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual(self.event().source, "api")

    def test_deactivation_keeps_bookings_and_repeats_without_event(self):
        booking = self.booking()
        self.client.force_login(self.staff)
        for _ in range(2):
            response = self.client.post(f"/management/users/{self.user.pk}/edit/", {"first_name": "", "last_name": "", "email": ""})
            self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual((booking.status, booking.revision), ("confirmed", 1))
        event = self.event("user.deactivated")
        self.assertEqual((event.before, event.after, event.actor_id, event.source),
                         ({"is_active": True}, {"is_active": False}, self.staff.pk, "management"))

    def test_admin_deactivation_and_reactivation(self):
        self.client.force_login(self.staff)
        data = {"username": self.user.username, "first_name": "", "last_name": "", "email": ""}
        url = f"/admin/auth/user/{self.user.pk}/change/"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.event().source, "django_admin")
        self.assertEqual(self.client.post(url, {**data, "is_active": "on"}).status_code, 302)
        self.assertEqual(self.event().action, "user.activated")

    def test_deactivation_audit_failure_rolls_back_user(self):
        with patch("rooms.audit.record", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                set_active(self.user, False, actor=self.staff)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(AuditEvent.objects.exists())

    def test_account_service_preserves_flags_not_changed_by_form(self):
        stale = get_user_model().objects.get(pk=self.user.pk)
        set_active(self.user, False, actor=self.staff)
        stale.first_name = "new"
        save_account(stale, actor=self.staff, fields=["first_name"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_account_service_permissions(self):
        with self.assertRaises(PermissionDenied):
            set_active(self.other, False, actor=self.user)
        with self.assertRaises(ValidationError):
            set_active(self.root, False, actor=self.root)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_account_service_cannot_escalate_roles(self):
        self.user.is_superuser = True
        with self.assertRaises(PermissionDenied):
            save_account(self.user, actor=self.staff, fields=["is_superuser"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)

    def test_deactivated_user_retains_history_authorship(self):
        self.create_api()
        set_active(self.user, False, actor=self.staff)
        self.assertEqual(self.event("booking.created").actor_id, self.user.pk)
        with self.assertRaises(ProtectedError):
            delete_empty(self.user, actor=self.root)

    def test_rejected_revision_has_no_event(self):
        booking = self.booking()
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": 0, "end_time": self.end.isoformat()}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_admin_idempotent_replay_has_one_event_and_log_entry(self):
        self.client.force_login(self.staff)
        data = self.html_data(room=self.room.pk, user=self.user.pk, status="confirmed")
        for _ in range(2):
            self.assertEqual(self.client.post("/admin/rooms/booking/add/", data).status_code, 302)
        self.assertEqual(AuditEvent.objects.filter(action="booking.created").count(), 1)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_source_is_server_owned_and_payload_contains_no_secrets(self):
        self.create_api(source="system", actor=self.other.pk, password="do-not-store", token="secret")
        event = self.event()
        self.assertEqual((event.actor_id, event.source), (self.user.pk, "api"))
        text = str(event.before) + str(event.after)
        for secret in [self.key, "password", "token", "do-not-store", "email"]:
            self.assertNotIn(secret, text)

    def test_failed_operation_only_technical_log_no_secret(self):
        with self.assertLogs("rooms.operations", level="WARNING") as captured:
            self.assertEqual(self.create_api(end_time=self.start.isoformat(), password="do-not-store").status_code, 400)
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertNotIn(self.key, str(captured.output))
        self.assertNotIn("do-not-store", str(captured.output))

    def test_event_orm_mutations_forbidden(self):
        self.booking()
        event = self.event()
        for change in [lambda: event.save(), lambda: event.delete(), lambda: AuditEvent.objects.all().delete(),
                       lambda: AuditEvent.objects.update(action="fake"), lambda: AuditEvent._base_manager.update(action="fake"),
                       lambda: AuditEvent.objects.bulk_update([event], ["action"]), lambda: AuditEvent.objects.bulk_create([event]),
                       lambda: AuditEvent.objects.create(action="fake"),
                       lambda: AuditEvent.objects.update_or_create(pk=event.pk, defaults={"action": "fake"})]:
            with self.assertRaises((TypeError, ProtectedError)):
                change()
        self.assertEqual(self.event().action, "booking.created")

    def test_journal_view_requires_explicit_permission(self):
        self.booking()
        self.client.force_login(self.staff)
        url = "/admin/rooms/auditevent/"
        self.assertEqual(self.client.get(url).status_code, 403)
        self.staff.user_permissions.add(Permission.objects.get(codename="view_auditevent"))
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(f"{url}{self.event().pk}/change/").status_code, 200)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_superuser_cannot_write_journal_through_admin(self):
        self.booking()
        self.client.force_login(self.root)
        url = "/admin/rooms/auditevent/"
        for target in ["add/", f"{self.event().pk}/change/", f"{self.event().pk}/delete/"]:
            self.assertEqual(self.client.post(url + target, {"action": "fake", "post": "yes"}).status_code, 403)
        self.client.post(url, {"action": "delete_selected", "_selected_action": [self.event().pk], "post": "yes"})
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_history_link_targets_filtered_journal(self):
        booking = self.booking()
        self.client.force_login(self.root)
        response = self.client.get(f"/admin/rooms/booking/{booking.pk}/history/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"object_id__exact={booking.pk}", response.url)
        self.assertIn("object_type__exact=booking", response.url)

    def test_no_audit_permission_automatically_assigned_to_groups(self):
        from django.core.management import call_command
        from io import StringIO
        call_command("setup_roles", stdout=StringIO())
        self.assertFalse(Group.objects.filter(permissions__codename="view_auditevent").exists())

    def test_context_resets_between_requests_and_internal_operations(self):
        self.create_api()
        self.booking(room=self.other_room)
        self.assertEqual((self.event().source, self.event().actor_id), ("system", None))

    def test_baseline_schema_rollback_refuses_to_drop_history(self):
        self.booking()
        migration = import_module("rooms.migrations.0007_audit_history")
        from django.apps import apps
        with self.assertRaises(IrreversibleError):
            migration.preserve_history_on_reverse(apps, connection.schema_editor())


class BaselineMigrationTests(TransactionTestCase):
    def test_existing_rows_get_honest_idempotent_baselines(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("rooms", "0007_audit_history")])
        try:
            old_apps = executor.loader.project_state([("rooms", "0007_audit_history")]).apps
            User = old_apps.get_model("auth", "User")
            OldRoom = old_apps.get_model("rooms", "Room")
            OldBooking = old_apps.get_model("rooms", "Booking")
            user = User.objects.create(username="baseline-user")
            room = OldRoom.objects.create(name="baseline-room")
            start = timezone.now() - timedelta(days=10)
            for i, status in enumerate(["confirmed", "pending", "cancelled"]):
                OldBooking.objects.create(room=room, user=user, start_time=start + timedelta(days=i),
                                          end_time=start + timedelta(days=i, hours=1), status=status, revision=i)
            captured_after = timezone.now()
            executor = MigrationExecutor(connection)
            executor.migrate([("rooms", "0009_user_activity_guard")])
            self.assertEqual(AuditEvent.objects.count(), 3)
            for event in AuditEvent.objects.all():
                self.assertEqual((event.action, event.source, event.actor_id, event.before), ("baseline", "system", None, {}))
                self.assertGreaterEqual(event.occurred_at, captured_after)
                self.assertIsNone(event.revision_before)
                self.assertEqual(event.revision_after, event.booking.revision)
                self.assertEqual(event.after, audit.booking_state(event.booking))
                self.assertEqual((event.new_room_id, event.new_owner_id), (room.pk, user.pk))
            # Repeat data migration with historical model state; do not rewrite history.
            historical = executor.loader.project_state([("rooms", "0008_booking_baselines")]).apps
            migration = import_module("rooms.migrations.0008_booking_baselines")
            migration.snapshot_existing(historical, connection.schema_editor())
            self.assertEqual(AuditEvent.objects.count(), 3)
        finally:
            MigrationExecutor(connection).migrate([("rooms", "0009_user_activity_guard")])


@skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger and row lock tests require isolated PostgreSQL 17.")
class PostgreSQLAuditTests(AuditFixtures, TransactionTestCase):
    def test_direct_user_update_model_save_and_bulk_update_blocked(self):
        for attempt in [
            lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=False),
            lambda: self.user.save(update_fields=["is_active"]),
            lambda: get_user_model().objects.bulk_update([self.user], ["is_active"]),
        ]:
            self.user.is_active = False
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    attempt()
            self.user.refresh_from_db()
            self.assertTrue(self.user.is_active)
        self.assertFalse(AuditEvent.objects.exists())

    def test_service_works_and_permit_does_not_leak_inside_outer_transaction(self):
        with transaction.atomic():
            set_active(self.user, False, actor=self.staff)
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('meetingroom.user_activity', true)")
                self.assertIn(cursor.fetchone()[0], (None, ""))
        self.assertEqual(self.event().action, "user.deactivated")

    def test_permit_bound_to_transaction_target_and_value(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT txid_current()")
                txid = cursor.fetchone()[0]
            for permit in [f"{txid - 1}:{self.user.pk}:false", f"{txid}:{self.other.pk}:false", f"{txid}:{self.user.pk}:true", "on"]:
                with self.assertRaises(DatabaseError):
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [permit])
                        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_new_transaction_cannot_reuse_old_permit(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT txid_current()")
                permit = f"{cursor.fetchone()[0]}:{self.user.pk}:false"
                cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [permit])
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [permit])
                get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)

    def test_audit_failure_rolls_back_activity_and_clears_permit(self):
        with patch("rooms.audit.record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                set_active(self.user, False, actor=self.staff)
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(AuditEvent.objects.exists())

    def test_trigger_reverse_and_reinstall(self):
        migration = import_module("rooms.migrations.0009_user_activity_guard")
        from django.apps import apps
        # DDL and temporary bypass roll back together, keeping the installed guard.
        with transaction.atomic():
            with connection.schema_editor() as editor:
                migration.remove_guard(apps, editor)
            get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
            with connection.schema_editor() as editor:
                migration.install_guard(apps, editor)
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
            transaction.set_rollback(True)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_bulk_update_cannot_extend_single_row_permit(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT txid_current()")
                permit = f"{cursor.fetchone()[0]}:{self.user.pk}:false"
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [permit])
                    get_user_model().objects.filter(pk__in=[self.user.pk, self.other.pk]).update(is_active=False)
        self.assertEqual(get_user_model().objects.filter(pk__in=[self.user.pk, self.other.pk], is_active=True).count(), 2)

    def test_other_user_fields_and_same_activity_value_are_unaffected(self):
        get_user_model().objects.filter(pk=self.user.pk).update(first_name="safe", is_active=True)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "safe")
        self.assertTrue(self.user.is_active)

    def race(self, *jobs, hook="rooms.booking_operations._lock_rooms"):
        barrier = Barrier(len(jobs), timeout=10)
        original = operations._lock_rooms if hook.endswith("_lock_rooms") else import_module("rooms.booking_idempotency")._claim
        def synchronized(*args, **kwargs):
            barrier.wait()
            return original(*args, **kwargs)
        def run(job):
            connections.close_all()
            try:
                return job()
            except ValidationError:
                return "rejected"
            finally:
                connections.close_all()
        with patch(hook, side_effect=synchronized):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(run, job) for job in jobs]
                return [f.result(timeout=20) for f in futures]

    def test_concurrent_replay_records_one_creation(self):
        def create():
            client = APIClient()
            client.force_authenticate(self.user)
            return client.post("/api/bookings/", {"room": self.room.pk, "start_time": self.start.isoformat(),
                                                "end_time": self.end.isoformat()}, format="json",
                               HTTP_IDEMPOTENCY_KEY=self.key).status_code
        self.assertEqual(self.race(create, create, hook="rooms.booking_idempotency._claim"), [201, 201])
        self.assertEqual((Booking.objects.count(), AuditEvent.objects.count()), (1, 1))

    def test_concurrent_edit_cancel_has_one_revision_event(self):
        booking = self.booking()
        def edit():
            operations.edit(Booking.objects.get(pk=booking.pk), actor=self.user, revision=1, end_time=self.end + timedelta(hours=1))
            return "edited"
        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=1)
            return "cancelled"
        results = self.race(edit, cancel)
        self.assertIn("rejected", results)
        self.assertEqual(AuditEvent.objects.filter(booking=booking).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(booking=booking, revision_after=2).count(), 1)

    def test_concurrent_confirm_cancel_logs_only_winner(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()
        def confirm():
            operations.confirm(Booking.objects.get(pk=booking.pk), actor=self.staff, revision=1)
            return "confirmed"
        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=1)
            return "cancelled"
        results = self.race(confirm, cancel)
        self.assertIn("rejected", results)
        self.assertEqual(AuditEvent.objects.filter(booking=booking, revision_after=2).count(), 1)

    def test_concurrent_archive_and_creation_preserve_history(self):
        def archive():
            room = Room.objects.get(pk=self.room.pk)
            room.is_active = False
            room.save(update_fields=["is_active"])
            return "archived"
        def create():
            operations.create(actor=self.user, room=self.room, start_time=self.start, end_time=self.end)
            return "created"
        results = self.race(archive, create)
        self.assertEqual(results[0], "archived")
        self.assertEqual(AuditEvent.objects.filter(action="room.archived").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="booking.created").count(), Booking.objects.count())

    def test_parallel_deactivation_produces_single_event(self):
        barrier = Barrier(2, timeout=10)
        def job():
            connections.close_all()
            try:
                row = get_user_model().objects.get(pk=self.user.pk)
                barrier.wait()
                set_active(row, False, actor=self.staff)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(job) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        self.assertEqual(AuditEvent.objects.filter(action="user.deactivated").count(), 1)

    def test_concurrent_empty_room_deletion_and_creation(self):
        def delete():
            try:
                delete_empty(self.room, actor=self.root)
                return "deleted"
            except ProtectedError:
                return "protected"
        def create():
            operations.create(actor=self.user, room=self.room, start_time=self.start, end_time=self.end)
            return "created"
        results = self.race(delete, create)
        self.assertIn(results, [["deleted", "rejected"], ["protected", "created"]])
        if results[0] == "deleted":
            self.assertFalse(Booking.objects.exists())
            self.assertEqual(AuditEvent.objects.filter(action="room.deleted").count(), 1)
        else:
            self.assertEqual(Booking.objects.count(), 1)
            self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())
            self.assertEqual(AuditEvent.objects.filter(action="room.deleted").count(), 0)

    def test_two_users_deactivating_each_other_do_not_deadlock(self):
        # NO KEY UPDATE is compatible with the actor FK's KEY SHARE check.
        second = get_user_model().objects.create_superuser(username="audit-second-root", email="", password="test-only")
        barrier = Barrier(2, timeout=10)
        def job(target, actor):
            connections.close_all()
            try:
                barrier.wait()
                set_active(target, False, actor=actor)
                return "changed"
            except PermissionDenied:
                return "denied"
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(job, second, self.root)
            other = pool.submit(job, self.root, second)
            results = [first.result(timeout=15), other.result(timeout=15)]
        self.assertIn("changed", results)
        self.assertEqual(AuditEvent.objects.filter(action="user.deactivated").count(), results.count("changed"))

    def test_unknown_commit_result_does_not_duplicate_audit(self):
        real_commit = connection.commit
        def lost_ack():
            real_commit()
            raise DatabaseError("injected unknown commit result")
        with patch.object(connection, "commit", side_effect=lost_ack):
            with self.assertRaises(DatabaseError):
                operations.create_once(actor=self.user, key=self.key, room_id=self.room.pk,
                                       start_time=self.start, end_time=self.end)
        connections.close_all()
        result = operations.create_once(actor=self.user, key=self.key, room_id=self.room.pk,
                                        start_time=self.start, end_time=self.end)
        self.assertTrue(result.replayed)
        self.assertEqual(AuditEvent.objects.filter(action="booking.created").count(), 1)
