from uuid import uuid4
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.signals import post_save
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import booking_operations as operations
from .models import Booking, Room
from . import test_concurrency


class RevisionFixtures:
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="revision-owner")
        self.staff = User.objects.create_user(username="revision-staff", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="rooms", codename__in=["view_booking", "change_booking", "add_booking"]))
        self.room = Room.objects.create(name="Revision VIP", is_vip=True)
        self.start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.booking = Booking.objects.create(room=self.room, user=self.user, start_time=self.start, end_time=self.end)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)
        self.url = f"/api/bookings/{self.booking.pk}/"

    def html_data(self, **extra):
        return {"room": self.room.pk, "revision": self.booking.revision,
                "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                "end_time_0": self.end.date().isoformat(), "end_time_1": "12:00", **extra}

    def admin_data(self, **extra):
        data = self.html_data()
        data.pop("revision")
        return {**data, "expected_revision": self.booking.revision, "user": self.user.pk,
                "status": "pending", "end_time_1": "11:00", **extra}

    def current_state(self):
        row = Booking.objects.get(pk=self.booking.pk)
        return operations.snapshot(row)

    def advance(self):
        operations.edit(self.booking, actor=self.user, revision=self.booking.revision,
                        end_time=self.end + timedelta(hours=1))

    def management(self, data):
        self.client.force_login(self.staff)
        return self.client.post(f"/management/bookings/{self.booking.pk}/status/", data)

    def action(self, data):
        self.client.force_login(self.staff)
        return self.client.post("/admin/rooms/booking/", {
            "action": "confirm_bookings", "_selected_action": [self.booking.pk], **data})


class RevisionTests(RevisionFixtures, TestCase):
    def test_api_invalid_or_missing_revision_for_patch_put_cancel(self):
        before = self.current_state()
        for method in ["patch", "put", "cancel"]:
            for value in [None, "", -1, True, False, 1.0, 1.5, [], {}, "1.0", "1e0", "²", "9" * 100]:
                with self.subTest(method=method, revision=value):
                    data = {"room": self.room.pk, "start_time": self.start.isoformat(), "end_time": self.end.isoformat()}
                    if value is not None:
                        data["revision"] = value
                    response = (self.api.post(self.url + "cancel/", data, format="json") if method == "cancel"
                                else getattr(self.api, method)(self.url, data, format="json"))
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(self.current_state(), before)
        self.assertEqual(self.api.patch(self.url, {"revision": None}, format="json").status_code, 400)

    def test_api_stale_and_future_revision(self):
        self.advance()
        before = self.current_state()
        for revision in [1, 3]:
            for suffix in ["", "cancel/"]:
                method = self.api.post if suffix else self.api.patch
                response = method(self.url + suffix, {"revision": revision}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.current_state(), before)

    def test_api_cancel_rejects_nonobject_json(self):
        before = self.current_state()
        for data in [[], [1], "1", 1, True, None]:
            response = self.api.post(self.url + "cancel/", data, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.current_state(), before)

    def test_api_correct_revision_increments_and_is_server_owned(self):
        response = self.api.patch(self.url, {"revision": 1, "end_time": (self.end + timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["revision"], 2)
        self.assertEqual(response.data["status"], "pending")

    def test_create_does_not_require_revision(self):
        response = self.api.post("/api/bookings/", {"room": self.room.pk,
            "start_time": self.end.isoformat(), "end_time": (self.end + timedelta(hours=1)).isoformat()}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["revision"], 1)

    def test_api_cancellation_replay_does_not_save_or_increment(self):
        self.assertEqual(self.api.post(self.url + "cancel/", {"revision": 1}, format="json").status_code, 200)
        events = []
        def receiver(**kwargs):
            events.append(kwargs["instance"].pk)
        post_save.connect(receiver, sender=Booking)
        try:
            for revision in [0, 1, 2]:
                response = self.api.post(self.url + "cancel/", {"revision": revision}, format="json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["revision"], 2)
            for data in [{}, {"revision": ""}, {"revision": 3}]:
                self.assertEqual(self.api.post(self.url + "cancel/", data, format="json").status_code, 400)
        finally:
            post_save.disconnect(receiver, sender=Booking)
        self.assertEqual(events, [])

    def test_html_edit_missing_and_invalid_revision(self):
        before = self.current_state()
        for value in [None, "", "true", "1.0", "1e0", "-1", "²"]:
            data = self.html_data(revision=value)
            if value is None:
                data.pop("revision")
            response = self.client.post(f"/my-bookings/{self.booking.pk}/edit/", data)
            self.assertEqual(response.status_code, 200)
            self.assertIn("revision", response.context["form"].errors)
            self.assertEqual(self.current_state(), before)

    def test_html_stale_form_does_not_overwrite_or_replace_revision(self):
        data = self.html_data()
        self.advance()
        before = self.current_state()
        response = self.client.post(f"/my-bookings/{self.booking.pk}/edit/", data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(str(response.context["form"]["revision"].value()), "1")
        self.assertEqual(self.current_state(), before)

    def test_html_cancel_requires_current_revision_and_repeats(self):
        self.advance()
        before = self.current_state()
        for data in [{}, {"revision": "bad"}, {"revision": 1}]:
            self.client.post(f"/cancel/{self.booking.pk}/", data)
            self.assertEqual(self.current_state(), before)
        self.client.post(f"/cancel/{self.booking.pk}/", {"revision": 2})
        cancelled = self.current_state()
        self.client.post(f"/cancel/{self.booking.pk}/", {"revision": 2})
        self.assertEqual(self.current_state(), cancelled)
        self.assertEqual(cancelled[-2:], ("cancelled", 3))

    def test_html_pages_render_revision_in_actions(self):
        for url in ["/my-bookings/", f"/my-bookings/{self.booking.pk}/", f"/my-bookings/{self.booking.pk}/edit/"]:
            response = self.client.get(url)
            self.assertContains(response, 'name="revision"')
            self.assertContains(response, 'value="1"')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get("/management/bookings/"), 'name="revision"')
        response = self.client.get("/admin/rooms/booking/")
        self.assertContains(response, f'name="revision_{self.booking.pk}" value="1"')

    def test_management_rejects_missing_invalid_and_stale_confirmation(self):
        self.advance()
        before = self.current_state()
        for revision in [None, "", "1.0", "bad", 1, 3]:
            data = {"status": "confirmed"}
            if revision is not None:
                data["revision"] = revision
            self.management(data)
            self.assertEqual(self.current_state(), before)
        self.management({"status": "confirmed", "revision": 2})
        self.assertEqual(self.current_state()[-2:], ("confirmed", 3))

    def test_management_cancel_current_and_replay(self):
        self.advance()
        self.management({"status": "cancelled", "revision": 1})
        self.assertEqual(self.current_state()[-2:], ("pending", 2))
        self.management({"status": "cancelled", "revision": 2})
        self.management({"status": "cancelled", "revision": 2})
        self.assertEqual(self.current_state()[-2:], ("cancelled", 3))

    def test_admin_form_missing_invalid_and_stale_version(self):
        self.client.force_login(self.staff)
        self.advance()
        before = self.current_state()
        for revision in [None, "", "bad", "1.0", 1, 3]:
            data = self.admin_data(expected_revision=revision, status="confirmed", end_time_1="12:00")
            if revision is None:
                data.pop("expected_revision")
            self.client.post(f"/admin/rooms/booking/{self.booking.pk}/change/", data)
            self.assertEqual(self.current_state(), before)
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_admin_form_correct_version_confirms_viewed_booking(self):
        self.client.force_login(self.staff)
        response = self.client.post(f"/admin/rooms/booking/{self.booking.pk}/change/", self.admin_data(status="confirmed"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.current_state()[-2:], ("confirmed", 2))

    def test_admin_add_does_not_require_revision(self):
        self.client.force_login(self.staff)
        data = self.admin_data(start_time_1="11:00", end_time_1="12:00")
        data["idempotency_key"] = "admin-add-without-revision"
        data.pop("expected_revision")
        response = self.client.post("/admin/rooms/booking/add/", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Booking.objects.count(), 2)

    def test_admin_repeated_cancel_does_not_add_log_entry(self):
        self.client.force_login(self.staff)
        data = self.admin_data(status="cancelled")
        url = f"/admin/rooms/booking/{self.booking.pk}/change/"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        count = LogEntry.objects.count()
        self.assertEqual(count, 1)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.current_state()[-2:], ("cancelled", 2))
        self.assertEqual(LogEntry.objects.count(), count)

    def test_admin_cannot_cancel_and_modify_parameters_together(self):
        self.client.force_login(self.staff)
        before = self.current_state()
        self.client.post(f"/admin/rooms/booking/{self.booking.pk}/change/",
                         self.admin_data(status="cancelled", end_time_1="12:00"))
        self.assertEqual(self.current_state(), before)

    def test_admin_bulk_missing_invalid_and_stale_versions(self):
        self.advance()
        before = self.current_state()
        for action in ["confirm_bookings", "reject_bookings"]:
            for value in [None, "bad", 1, 3]:
                data = {"action": action}
                if value is not None:
                    data[f"revision_{self.booking.pk}"] = value
                self.action(data)
                self.assertEqual(self.current_state(), before)

    def test_admin_bulk_reports_partial_results_without_refreshing_versions(self):
        other = Booking.objects.create(room=self.room, user=self.user, start_time=self.end, end_time=self.end + timedelta(hours=1))
        # Advance only the first record without changing its interval.
        operations.edit(self.booking, actor=self.user, revision=1)
        self.action({"_selected_action": [self.booking.pk, other.pk],
                     f"revision_{self.booking.pk}": 1, f"revision_{other.pk}": 1})
        self.assertEqual(self.current_state()[-2:], ("pending", 2))
        other.refresh_from_db()
        self.assertEqual((other.status, other.revision), ("confirmed", 2))

    def test_admin_select_across_cannot_approve_unviewed_results(self):
        self.action({"select_across": "1", f"revision_{self.booking.pk}": 1})
        self.assertEqual(self.current_state()[-2:], ("pending", 1))

    def test_revision_never_grants_access(self):
        other = get_user_model().objects.create_user(username="revision-stranger", is_staff=True)
        self.api.force_authenticate(other)
        self.assertEqual(self.api.patch(self.url, {"revision": 1}, format="json").status_code, 404)
        self.assertEqual(self.api.post(self.url + "cancel/", {"revision": 1}, format="json").status_code, 404)
        self.client.force_login(other)
        self.assertEqual(self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": 1}).status_code, 403)
        self.assertEqual(self.current_state()[-2:], ("pending", 1))

    def test_services_and_model_require_explicit_revision(self):
        for operation in [operations.edit, operations.cancel, operations.confirm, operations.reject]:
            actor = self.staff if operation in [operations.confirm, operations.reject] else self.user
            with self.assertRaises(ValidationError):
                operation(Booking.objects.get(pk=self.booking.pk), actor=actor)
        with self.assertRaises(ValidationError):
            self.booking.save()
        with self.assertRaises(ValidationError):
            operations.persist(self.booking)
        self.assertEqual(self.current_state()[-2:], ("pending", 1))

    def test_revision_comparison_occurs_after_room_locks(self):
        events = []
        lock, parse = operations._lock_rooms, operations.parse_revision
        def locked(*args):
            events.append("lock")
            return lock(*args)
        def parsed(value):
            events.append("revision")
            self.assertTrue(connection.in_atomic_block)
            return parse(value)
        with patch.object(operations, "_lock_rooms", side_effect=locked), patch.object(operations, "parse_revision", side_effect=parsed):
            with self.assertRaises(ValidationError):
                operations.cancel(self.booking, actor=self.user, revision=0)
        self.assertEqual(events, ["lock", "revision"])

    def test_model_snapshot_still_blocks_forged_current_revision(self):
        stale = Booking.objects.get(pk=self.booking.pk)
        self.advance()
        stale.revision = 2
        stale.end_time = self.end + timedelta(hours=2)
        with self.assertRaises(ValidationError):
            stale.save(revision=2)
        self.assertEqual(self.current_state(), operations.snapshot(self.booking))

    def test_reconstructed_instance_cannot_overwrite_existing_primary_key(self):
        clone = Booking(pk=self.booking.pk, room=self.room, user=self.user, start_time=self.start, end_time=self.end)
        with self.assertRaises(ValidationError):
            clone.save(revision=1)
        self.assertEqual(self.current_state()[-2:], ("pending", 1))

    def test_zero_legacy_revision_and_counter_limit(self):
        # Isolated fixture only: emulate a pre-revision row / exhausted counter.
        for revision in [0, operations.MAX_REVISION]:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE rooms_booking SET revision = %s WHERE id = %s", [revision, self.booking.pk])
            response = self.api.patch(self.url, {"revision": revision}, format="json")
            self.assertEqual(response.status_code, 200 if revision == 0 else 400)
            self.assertEqual(self.current_state()[-1], 1 if revision == 0 else revision)

    def test_bulk_operations_cannot_bypass_revision_on_either_manager(self):
        for manager in [Booking.objects, Booking._base_manager]:
            for operation in [
                lambda: manager.filter(pk=self.booking.pk).update(status="confirmed"),
                lambda: manager.bulk_update([self.booking], ["status", "revision"]),
                lambda: manager.bulk_create([self.booking], update_conflicts=True, update_fields=["status"], unique_fields=["pk"]),
                lambda: manager.update_or_create(pk=self.booking.pk, defaults={"status": "confirmed"}),
            ]:
                with self.assertRaises(TypeError):
                    operation()
        self.assertEqual(self.current_state()[-2:], ("pending", 1))


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL row locks.")
class RevisionConcurrencyTests(RevisionFixtures, TransactionTestCase):
    race = test_concurrency.PostgreSQLConcurrencyTests.race

    def test_two_api_cancellations_have_one_state_change(self):
        def cancel():
            api = APIClient()
            api.force_authenticate(self.user)
            response = api.post(self.url + "cancel/", {"revision": 1}, format="json")
            return response.status_code
        self.assertEqual(self.race(cancel, cancel), [200, 200])
        self.assertEqual(self.current_state()[-2:], ("cancelled", 2))

    def test_management_confirmation_against_api_edit(self):
        client = Client()
        client.force_login(self.staff)
        def confirm():
            return client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": 1}).status_code
        def edit():
            api = APIClient()
            api.force_authenticate(self.user)
            return api.patch(self.url, {"revision": 1, "end_time": (self.end + timedelta(hours=1)).isoformat()}, format="json").status_code
        results = self.race(confirm, edit)
        state = self.current_state()
        self.assertEqual(state[-1], 2)
        if results[1] == 200:
            self.assertEqual(state[-2], "pending")
            self.assertEqual(state[3], self.end + timedelta(hours=1))
        else:
            self.assertEqual(results[1], 400)
            self.assertEqual(state[-2], "confirmed")
            self.assertEqual(state[3], self.end)

    def test_admin_bulk_confirmation_against_api_cancel(self):
        client = Client()
        client.force_login(self.staff)
        def confirm():
            return client.post("/admin/rooms/booking/", {"action": "confirm_bookings",
                "_selected_action": [self.booking.pk], f"revision_{self.booking.pk}": 1}).status_code
        def cancel():
            api = APIClient()
            api.force_authenticate(self.user)
            return api.post(self.url + "cancel/", {"revision": 1}, format="json").status_code
        results = self.race(confirm, cancel)
        self.assertEqual(self.current_state()[-2:], ("cancelled" if results[1] == 200 else "confirmed", 2))
        self.assertIn(results[1], [200, 400])

    def test_html_edit_against_api_cancel(self):
        client = Client()
        client.force_login(self.user)
        def edit():
            return client.post(f"/my-bookings/{self.booking.pk}/edit/", self.html_data()).status_code
        def cancel():
            api = APIClient()
            api.force_authenticate(self.user)
            return api.post(self.url + "cancel/", {"revision": 1}, format="json").status_code
        results = self.race(edit, cancel)
        self.assertIn(results, [[302, 400], [200, 200]])
        self.assertEqual(self.current_state()[-2:], ("pending" if results[0] == 302 else "cancelled", 2))
