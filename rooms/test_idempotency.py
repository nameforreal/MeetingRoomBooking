from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone as datetime_timezone
from threading import Barrier, Event, Lock
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.signals import post_save
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import booking_idempotency as idempotency, booking_operations as operations
from .models import Booking, BookingCreationRequest, Room


class CreationFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="creation-owner")
        self.other = get_user_model().objects.create_user(username="creation-other")
        self.staff = get_user_model().objects.create_user(username="creation-staff", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="rooms", codename__in=["add_booking", "change_booking", "view_booking"]))
        self.room = Room.objects.create(name="Creation room")
        self.start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.key = str(uuid4())
        self.api = self.api_client()
        self.client.force_login(self.user)

    def api_client(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.user)
        return client

    def payload(self, **changes):
        return {"room": self.room.pk, "start_time": self.start.isoformat(), "end_time": self.end.isoformat(), **changes}

    def html_payload(self, **changes):
        return {"idempotency_key": self.key, "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00", **changes}

    def admin_payload(self, **changes):
        return self.html_payload(**{"room": self.room.pk, "user": self.user.pk, "status": "confirmed", **changes})

    def post_api(self, *, client=None, key=None, **changes):
        return (client or self.api).post("/api/bookings/", self.payload(**changes), format="json",
                                         HTTP_IDEMPOTENCY_KEY=self.key if key is None else key)

    def create_once(self, **changes):
        data = dict(actor=self.user, key=self.key, room_id=self.room.pk, start_time=self.start, end_time=self.end)
        data.update(changes)
        return operations.create_once(**data)

    def assert_one_result(self):
        self.assertEqual(Booking.objects.count(), 1)
        receipt = BookingCreationRequest.objects.get()
        booking = Booking.objects.get()
        self.assertEqual(receipt.booking_id, booking.pk)
        self.assertIsNotNone(receipt.completed_at)
        self.assertEqual(receipt.response_payload["id"], booking.pk)
        return booking


class CreationIdempotencyTests(CreationFixtures, TestCase):
    def test_identical_api_request_returns_original_snapshot(self):
        first, second = self.post_api(), self.post_api()
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.data, second.data)
        self.assertEqual(first["Idempotency-Replayed"], "false")
        self.assertEqual(second["Idempotency-Replayed"], "true")
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_different_payload_is_conflict(self):
        self.post_api()
        for changes in [{"end_time": (self.end + timedelta(hours=1)).isoformat()},
                        {"start_time": (self.start + timedelta(minutes=1)).isoformat()}, {"room": 999999}]:
            with self.subTest(changes=changes):
                response = self.post_api(**changes)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "idempotency_key_reused")
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_missing_api_key_is_explicit_error(self):
        response = self.api.post("/api/bookings/", self.payload(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "idempotency_key_required")
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())

    def test_invalid_keys_rejected(self):
        for key in ["", " ", "a b", "я", "a" * 129, "key\n", "key/other"]:
            with self.subTest(key=key):
                self.assertEqual(self.post_api(key=key).status_code, 400)
        for key in [True, 12, [], {}]:
            with self.assertRaises(ValidationError):
                self.create_once(key=key)
        self.assertFalse(BookingCreationRequest.objects.exists())

    def test_key_length_boundaries_and_case_sensitive(self):
        for index, key in enumerate(["a", "A", "x" * 128]):
            start = self.start + timedelta(days=index)
            self.assertEqual(self.post_api(key=key, start_time=start.isoformat(),
                                          end_time=(start + timedelta(hours=1)).isoformat()).status_code, 201)
        self.assertEqual(BookingCreationRequest.objects.count(), 3)

    def test_old_key_does_not_expire(self):
        first = self.post_api()
        with patch("django.utils.timezone.now", return_value=self.start + timedelta(days=3650)):
            replay = self.post_api()
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.data, first.data)
        self.assert_one_result()

    def test_different_users_can_use_same_key_without_sharing_results(self):
        first = self.post_api()
        other = self.api_client(self.other)
        conflict = self.post_api(client=other)
        self.assertEqual(conflict.status_code, 400)  # Interval conflict, never another user's replay.
        second = self.post_api(client=other, start_time=self.end.isoformat(),
                               end_time=(self.end + timedelta(hours=1)).isoformat())
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["id"], second.data["id"])
        self.assertEqual(BookingCreationRequest.objects.filter(key=self.key).count(), 2)

    def test_equivalent_offsets_replay(self):
        first = self.post_api()
        offset = datetime_timezone(timedelta(hours=3))
        second = self.post_api(start_time=self.start.astimezone(offset).isoformat(),
                               end_time=self.end.astimezone(offset).isoformat())
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.data, second.data)

    def test_microseconds_are_not_rounded(self):
        self.post_api()
        response = self.post_api(end_time=(self.end + timedelta(microseconds=1)).isoformat())
        self.assertEqual(response.status_code, 409)

    def test_readonly_fields_do_not_change_effective_request(self):
        first = self.post_api()
        second = self.post_api(status="pending", user=self.other.pk, revision=99, id=999)
        self.assertEqual(first.data, second.data)
        self.assertEqual(self.assert_one_result().user_id, self.user.pk)

    def test_snapshot_has_no_user_personal_fields(self):
        self.post_api()
        receipt = BookingCreationRequest.objects.get()
        self.assertEqual(set(receipt.canonical_payload), {"room", "user", "start_time", "end_time"})
        self.assertEqual(set(receipt.response_payload), {"id", "room", "room_name", "start_time", "end_time",
                                                        "status", "status_display", "created_at", "revision"})

    def test_html_double_post(self):
        for _ in range(2):
            response = self.client.post(f"/book/{self.room.pk}/", self.html_payload())
            self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_html_missing_and_bad_keys_are_form_errors(self):
        for key in ["", "bad key", "x" * 129]:
            response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(idempotency_key=key))
            self.assertEqual(response.status_code, 200)
            self.assertIn("idempotency_key", response.context["form"].errors)
        self.assertFalse(Booking.objects.exists())

    def test_html_get_provides_new_key_without_reserving(self):
        first = self.client.get(f"/book/{self.room.pk}/")
        second = self.client.get(f"/book/{self.room.pk}/")
        keys = [str(r.context["form"]["idempotency_key"].value()) for r in [first, second]]
        self.assertNotEqual(*keys)
        self.assertContains(first, 'type="hidden" name="idempotency_key"')
        self.assertFalse(BookingCreationRequest.objects.exists())

    def test_html_conflict_retains_submitted_key(self):
        self.post_api()
        response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(end_time_1="12:00"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context["form"]["idempotency_key"].value(), self.key)
        self.assert_one_result()

    def test_html_busy_response_keeps_key_and_retry_header(self):
        error = idempotency.IdempotencyError("Запрос ещё выполняется.", "idempotency_in_progress", 503)
        with patch("rooms.booking_idempotency._claim", side_effect=error):
            response = self.client.post(f"/book/{self.room.pk}/", self.html_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "1")
        self.assertEqual(response.context["form"]["idempotency_key"].value(), self.key)
        self.assertFalse(Booking.objects.exists())

    def test_html_validation_error_retains_unbound_key(self):
        response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(start_time_1="07:00"))
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["form"]["idempotency_key"].value(), self.key)
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code, 302)

    def test_html_and_api_share_receipt_in_both_directions(self):
        first = self.post_api()
        self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code, 302)
        self.assertEqual(self.post_api().data, first.data)
        self.assert_one_result()
        self.key = str(uuid4())
        self.start += timedelta(days=1)
        self.end += timedelta(days=1)
        self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code, 302)
        self.assertEqual(self.post_api()["Idempotency-Replayed"], "true")
        self.assertEqual(Booking.objects.count(), 2)

    def test_replay_after_edit_and_cancel_returns_original_without_restoring(self):
        first = self.post_api()
        booking = Booking.objects.get()
        other_room = Room.objects.create(name="Transferred")
        operations.edit(booking, actor=self.user, revision=booking.revision, room=other_room,
                        start_time=self.start + timedelta(hours=1), end_time=self.end + timedelta(hours=1))
        operations.cancel(booking, actor=self.user, revision=booking.revision)
        self.assertEqual(self.post_api().data, first.data)
        booking.refresh_from_db()
        self.assertEqual((booking.room_id, booking.status, booking.revision), (other_room.pk, "cancelled", 3))
        self.assert_one_result()

    def test_replay_after_archive_and_time_passes(self):
        first = self.post_api()
        self.room.is_active = False
        self.room.save()
        with patch("django.utils.timezone.now", return_value=self.end + timedelta(days=1)):
            self.assertEqual(self.post_api().data, first.data)
            self.assertEqual(self.client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code, 302)
        self.assert_one_result()

    def test_room_rename_does_not_change_snapshot(self):
        first = self.post_api()
        self.room.name = "Renamed"
        self.room.save()
        self.assertEqual(self.post_api().data, first.data)

    def test_vip_creation_and_replay_after_approval(self):
        self.room.is_vip = True
        self.room.save()
        first = self.post_api(status="confirmed")
        self.assertEqual(first.data["status"], "pending")
        booking = Booking.objects.get()
        operations.confirm(booking, actor=self.staff, revision=booking.revision)
        self.assertEqual(self.post_api().data, first.data)
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.revision), ("confirmed", 2))

    def test_room_type_change_preserves_actual_approval_and_revision(self):
        first = self.post_api()
        self.room.is_vip = True
        self.room.save()
        self.assertEqual(self.post_api().data, first.data)
        booking = self.assert_one_result()
        self.assertEqual((booking.status, booking.revision), ("pending", 2))

    def test_initial_revision_from_replay_cannot_edit_newer_booking(self):
        first = self.post_api()
        booking = Booking.objects.get()
        operations.edit(booking, actor=self.user, revision=booking.revision, end_time=self.end + timedelta(hours=1))
        replay = self.post_api()
        self.assertEqual(first.data["revision"], replay.data["revision"])
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": replay.data["revision"],
                                                                   "end_time": self.end.isoformat()}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_anonymous_and_inactive_replay_denied(self):
        self.post_api()
        self.assertIn(self.post_api(client=APIClient()).status_code, (401, 403))
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.post_api().status_code, 403)
        self.assert_one_result()

    def test_service_rechecks_staff_permission_on_replay(self):
        self.create_once(actor=self.staff, user=self.user)
        self.staff.user_permissions.clear()
        with self.assertRaises(PermissionDenied):
            self.create_once(actor=self.staff, user=self.user)
        self.assert_one_result()

    def test_changed_owner_does_not_leak_replay(self):
        self.post_api()
        booking = Booking.objects.get()
        booking.user = self.other
        operations.persist(booking, actor=self.staff, revision=booking.revision)
        self.assertEqual(self.post_api().status_code, 403)

    def test_csrf_is_required_on_html_and_session_api_replay(self):
        self.post_api()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code, 403)
        api = APIClient(enforce_csrf_checks=True)
        api.force_login(self.user)
        self.assertEqual(self.post_api(client=api).status_code, 403)

    def test_business_error_leaves_no_receipt_and_allows_corrected_retry(self):
        self.assertEqual(self.post_api(end_time=self.start.isoformat()).status_code, 400)
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(self.post_api().status_code, 201)
        self.assert_one_result()

    def test_missing_or_archived_room_leaves_no_receipt(self):
        self.assertEqual(self.post_api(room=999999).status_code, 400)
        self.room.is_active = False
        self.room.save()
        self.assertEqual(self.post_api().status_code, 400)
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())

    def test_out_of_range_room_id_is_validation_error(self):
        self.assertEqual(self.post_api(room=9223372036854775808).status_code, 400)
        self.assertFalse(BookingCreationRequest.objects.exists())

    def test_failure_after_claim_rolls_back_reservation(self):
        with patch("rooms.booking_operations.create", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.create_once()
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.create_once()
        self.assert_one_result()

    def test_failure_after_booking_insert_rolls_back_both(self):
        with patch("rooms.booking_idempotency._complete", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.create_once()
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.create_once()
        self.assert_one_result()

    def test_outer_rollback_removes_completed_receipt_and_booking(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.create_once()
                raise RuntimeError("outer rollback")
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.create_once()
        self.assert_one_result()

    def test_repeat_does_not_repeat_save_signals(self):
        events = []
        def record(sender, instance, created, **kwargs):
            events.append((instance.pk, created))
        post_save.connect(record, sender=Booking)
        try:
            self.post_api()
            self.post_api()
        finally:
            post_save.disconnect(record, sender=Booking)
        self.assertEqual(events, [(Booking.objects.get().pk, True)])

    def test_new_protected_links_preserve_receipt(self):
        self.post_api()
        for obj in [Booking.objects.get(), self.user, self.room]:
            with self.assertRaises(ProtectedError):
                obj.delete()
        self.assert_one_result()

    def test_receipts_cannot_be_rewritten_or_deleted_through_normal_orm(self):
        self.create_once()
        receipt = BookingCreationRequest.objects.get()
        for mutation in [
            lambda: receipt.save(), lambda: receipt.delete(),
            lambda: BookingCreationRequest.objects.create(actor=self.user, key="new", canonical_payload={}, payload_hash="x"),
            lambda: BookingCreationRequest.objects.update(key="new"),
            lambda: BookingCreationRequest._base_manager.update(key="new"),
            lambda: BookingCreationRequest.objects.all().delete(),
            lambda: BookingCreationRequest.objects.bulk_create([receipt]),
            lambda: BookingCreationRequest.objects.bulk_update([receipt], ["key"]),
            lambda: BookingCreationRequest.objects.update_or_create(pk=receipt.pk, defaults={"key": "new"}),
        ]:
            with self.assertRaises(TypeError):
                mutation()
        self.assertEqual(BookingCreationRequest.objects.get().key, self.key)
        self.assert_one_result()

    def test_admin_replay_has_one_booking_and_one_log_entry(self):
        self.client.force_login(self.staff)
        for _ in range(2):
            response = self.client.post("/admin/rooms/booking/add/", self.admin_payload())
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assert_one_result().revision, 1)
        self.assertEqual(LogEntry.objects.filter(action_flag=1).count(), 1)

    def test_admin_requires_key_and_preserves_invalid_key(self):
        self.client.force_login(self.staff)
        for key in ["", "invalid key"]:
            response = self.client.post("/admin/rooms/booking/add/", self.admin_payload(idempotency_key=key))
            self.assertEqual(response.status_code, 200)
            self.assertIn("idempotency_key", response.context["adminform"].form.errors)
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_admin_get_generates_hidden_key_without_reservation(self):
        self.client.force_login(self.staff)
        response = self.client.get("/admin/rooms/booking/add/")
        key = str(response.context["adminform"].form["idempotency_key"].value())
        self.assertEqual(idempotency.validate_key(key), key)
        self.assertContains(response, 'type="hidden" name="idempotency_key"')
        self.assertFalse(BookingCreationRequest.objects.exists())

    def test_admin_busy_response_keeps_key_and_retry_header(self):
        self.client.force_login(self.staff)
        error = idempotency.IdempotencyError("Запрос ещё выполняется.", "idempotency_in_progress", 503)
        with patch("rooms.booking_idempotency._claim", side_effect=error):
            response = self.client.post("/admin/rooms/booking/add/", self.admin_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "1")
        self.assertEqual(response.context["adminform"].form["idempotency_key"].value(), self.key)
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_admin_and_api_share_scope_for_same_actor_and_owner(self):
        self.client.force_login(self.staff)
        response = self.client.post("/admin/rooms/booking/add/", self.admin_payload(user=self.staff.pk))
        self.assertEqual(response.status_code, 302)
        replay = self.post_api(client=self.api_client(self.staff))
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assert_one_result()
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_api_then_admin_replay_does_not_add_creation_log(self):
        first = self.post_api(client=self.api_client(self.staff))
        self.assertEqual(first.status_code, 201)
        self.client.force_login(self.staff)
        response = self.client.post("/admin/rooms/booking/add/", self.admin_payload(user=self.staff.pk))
        self.assertEqual(response.status_code, 302)
        self.assert_one_result()
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_admin_cannot_confirm_vip_during_creation_or_replay(self):
        self.room.is_vip = True
        self.room.save()
        self.client.force_login(self.staff)
        for _ in range(2):
            response = self.client.post("/admin/rooms/booking/add/", self.admin_payload(status="confirmed"))
            self.assertEqual(response.status_code, 302)
        booking = self.assert_one_result()
        self.assertEqual((booking.status, booking.revision), ("pending", 1))
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_admin_conflict_and_business_error_keep_original_form(self):
        self.client.force_login(self.staff)
        invalid = self.client.post("/admin/rooms/booking/add/", self.admin_payload(start_time_1="07:00"))
        self.assertTrue(invalid.context["adminform"].form.errors)
        self.assertEqual(invalid.context["adminform"].form["idempotency_key"].value(), self.key)
        self.assertEqual(self.client.post("/admin/rooms/booking/add/", self.admin_payload()).status_code, 302)
        conflict = self.client.post("/admin/rooms/booking/add/", self.admin_payload(end_time_1="12:00"))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.context["adminform"].form["idempotency_key"].value(), self.key)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_admin_replay_after_cancel_archive_and_time_passes(self):
        self.client.force_login(self.staff)
        self.client.post("/admin/rooms/booking/add/", self.admin_payload())
        booking = Booking.objects.get()
        operations.cancel(booking, actor=self.user, revision=booking.revision)
        self.room.is_active = False
        self.room.save()
        with patch("django.utils.timezone.now", return_value=self.end + timedelta(days=2)):
            self.assertEqual(self.client.post("/admin/rooms/booking/add/", self.admin_payload()).status_code, 302)
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.revision), ("cancelled", 2))
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_admin_permission_revocation_blocks_replay(self):
        self.client.force_login(self.staff)
        self.client.post("/admin/rooms/booking/add/", self.admin_payload())
        self.staff.user_permissions.remove(Permission.objects.get(content_type__app_label="rooms", codename="add_booking"))
        response = self.client.post("/admin/rooms/booking/add/", self.admin_payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_admin_log_failure_rolls_back_receipt_and_booking(self):
        self.client.force_login(self.staff)
        with patch("rooms.admin.BookingAdmin.log_addition", side_effect=RuntimeError("log failed")):
            with self.assertRaises(RuntimeError):
                self.client.post("/admin/rooms/booking/add/", self.admin_payload())
        self.assertFalse(Booking.objects.exists())
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertEqual(self.client.post("/admin/rooms/booking/add/", self.admin_payload()).status_code, 302)
        self.assert_one_result()


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL 17; SQLite cannot prove locking.")
class PostgreSQLCreationTests(CreationFixtures, TransactionTestCase):
    def race(self, *jobs):
        # Synchronize BEFORE the unique INSERT, never after the first key lock.
        barrier = Barrier(len(jobs), timeout=10)
        original = idempotency._claim
        def claim(**kwargs):
            barrier.wait()
            return original(**kwargs)
        def run(job):
            connections.close_all()
            try:
                return job()
            finally:
                connections.close_all()
        with patch("rooms.booking_idempotency._claim", side_effect=claim):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(run, job) for job in jobs]
                return [future.result(timeout=20) for future in futures]

    def api_job(self, **changes):
        response = self.post_api(client=self.api_client(), **changes)
        return response.status_code, response.data

    def test_two_same_key_api_requests(self):
        results = self.race(self.api_job, self.api_job)
        self.assertEqual([r[0] for r in results], [201, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_same_key_different_payload_concurrently(self):
        results = self.race(self.api_job, lambda: self.api_job(end_time=(self.end + timedelta(hours=1)).isoformat()))
        self.assertCountEqual([r[0] for r in results], [201, 409])
        self.assert_one_result()

    def test_html_and_api_concurrent_same_key(self):
        client = Client()
        client.force_login(self.user)
        results = self.race(lambda: client.post(f"/book/{self.room.pk}/", self.html_payload()).status_code,
                            lambda: self.api_job()[0])
        self.assertEqual(results, [302, 201])
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_two_html_requests_same_key(self):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.user)
        self.assertEqual(self.race(*[lambda c=c: c.post(f"/book/{self.room.pk}/", self.html_payload()).status_code
                                     for c in clients]), [302, 302])
        self.assert_one_result()

    def test_two_admin_requests_have_one_log(self):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.staff)
        self.assertEqual(self.race(*[lambda c=c: c.post("/admin/rooms/booking/add/", self.admin_payload()).status_code
                                     for c in clients]), [302, 302])
        self.assert_one_result()
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_waiter_can_create_after_first_rolls_back(self):
        original = idempotency._complete
        mutex = Lock()
        fail = [True]
        def complete(*args):
            with mutex:
                should_fail, fail[0] = fail[0], False
            if should_fail:
                raise RuntimeError("rollback first creator")
            return original(*args)
        def job():
            try:
                self.create_once()
                return "created"
            except RuntimeError:
                return "rolled back"
        with patch("rooms.booking_idempotency._complete", side_effect=complete):
            self.assertCountEqual(self.race(job, job), ["created", "rolled back"])
        self.assert_one_result()
        self.assertTrue(self.create_once().replayed)

    def test_lost_response_after_real_commit_replays(self):
        with patch("rooms.api.Response", side_effect=ConnectionError("response lost after commit")):
            with self.assertRaises(ConnectionError):
                self.post_api()
        # Request's atomic has exited; this is a real committed transaction.
        connections.close_all()
        original = BookingCreationRequest.objects.get().response_payload
        response = self.post_api()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, original)
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_failed_transaction_then_retry_on_new_connection(self):
        with patch("rooms.booking_idempotency._complete", side_effect=ConnectionError("lost before commit")):
            with self.assertRaises(ConnectionError):
                self.post_api()
        connections.close_all()
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(self.post_api().status_code, 201)
        self.assert_one_result()

    def test_commit_succeeded_but_acknowledgement_was_lost(self):
        real_commit = connection.commit
        def commit_then_fail():
            real_commit()
            raise OperationalError("injected lost COMMIT acknowledgement")
        with patch.object(connection, "commit", side_effect=commit_then_fail):
            with self.assertRaises(OperationalError):
                self.create_once()
        connections.close_all()
        original = BookingCreationRequest.objects.get().response_payload
        result = self.create_once()
        self.assertTrue(result.replayed)
        self.assertEqual(result.payload, original)
        self.assertEqual(self.assert_one_result().revision, 1)

    def test_commit_did_not_happen_and_retry_creates_once(self):
        with patch.object(connection, "commit", side_effect=OperationalError("injected before COMMIT")):
            with self.assertRaises(OperationalError):
                self.create_once()
        connections.close_all()
        self.assertFalse(BookingCreationRequest.objects.exists())
        self.assertFalse(Booking.objects.exists())
        self.assertFalse(self.create_once().replayed)
        self.assertTrue(self.create_once().replayed)
        self.assert_one_result()

    def test_busy_key_returns_503_after_five_seconds_and_retry_succeeds(self):
        entered, release = Event(), Event()
        original = idempotency._complete
        def complete(*args):
            entered.set()
            if not release.wait(12):
                raise RuntimeError("test failed to release creator")
            return original(*args)
        def creator():
            connections.close_all()
            try:
                return self.api_job()[0]
            finally:
                connections.close_all()
        import time
        with patch("rooms.booking_idempotency._complete", side_effect=complete):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(creator)
                try:
                    self.assertTrue(entered.wait(5))
                    started = time.monotonic()
                    response = self.post_api()
                    elapsed = time.monotonic() - started
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.data["code"], "idempotency_in_progress")
                    self.assertEqual(response["Retry-After"], "1")
                    self.assertGreaterEqual(elapsed, 4.5)
                    self.assertLess(elapsed, 8)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=10), 201)
        self.assertEqual(self.post_api().status_code, 201)
        self.assert_one_result()

    def test_local_lock_timeout_is_restored_on_success_and_conflict(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', '1700ms', true)")
            self.create_once()
            self.create_once()
            with connection.cursor() as cursor:
                cursor.execute("SHOW lock_timeout")
                self.assertEqual(cursor.fetchone()[0], "1700ms")
