from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import booking_operations as operations
from .models import Booking, Room


class OperationFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operations-owner")
        self.staff = get_user_model().objects.create_user(username="operations-staff", is_staff=True)
        self.staff.user_permissions.add(Permission.objects.get(codename="change_booking"))
        self.room = Room.objects.create(name="Concurrent room")
        self.start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)

    def booking(self, **changes):
        data = dict(user=self.user, room=self.room, start_time=self.start, end_time=self.end)
        data.update(changes)
        return Booking.objects.create(**data)


class BookingOperationTests(OperationFixtures, TestCase):
    def test_stale_edit_does_not_overwrite_new_values(self):
        first = self.booking()
        stale = Booking.objects.get(pk=first.pk)
        operations.edit(first, revision=first.revision, actor=self.user, start_time=self.start + timedelta(hours=1), end_time=self.end + timedelta(hours=1))
        with self.assertRaises(ValidationError):
            operations.edit(stale, revision=stale.revision, actor=self.user, start_time=self.start + timedelta(hours=2), end_time=self.end + timedelta(hours=2))
        stale.refresh_from_db()
        self.assertEqual(stale.start_time, self.start + timedelta(hours=1))

    def test_stale_model_cannot_restore_cancelled_booking(self):
        booking = self.booking()
        stale = Booking.objects.get(pk=booking.pk)
        operations.cancel(booking, revision=booking.revision, actor=self.user)
        with self.assertRaises(ValidationError):
            stale.save(revision=stale.revision)
        self.assertEqual(Booking.objects.get(pk=booking.pk).status, "cancelled")

    def test_mass_writes_are_blocked(self):
        booking = self.booking()
        for write in [
            lambda: Booking.objects.filter(pk=booking.pk).update(status="cancelled"),
            lambda: Booking._base_manager.filter(pk=booking.pk).update(status="cancelled"),
            lambda: Booking.objects.update_or_create(pk=booking.pk, defaults={"status": "cancelled"}),
            lambda: Booking.objects.bulk_update([booking], ["status"]),
            lambda: Booking.objects.bulk_create([Booking(user=self.user, room=self.room, start_time=self.start, end_time=self.end)]),
        ]:
            with self.assertRaises(TypeError):
                write()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")

    def test_explicit_api_revision_rejects_old_page(self):
        booking = self.booking()
        old_revision = booking.revision
        operations.edit(booking, revision=booking.revision, actor=self.user, end_time=self.end + timedelta(hours=1))
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(f"/api/bookings/{booking.pk}/", {
            "revision": old_revision, "end_time": self.end.isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.end_time, self.end + timedelta(hours=1))

    def test_html_revision_rejects_old_page(self):
        booking = self.booking()
        revision = booking.revision
        operations.edit(booking, revision=booking.revision, actor=self.user, end_time=self.end + timedelta(hours=1))
        self.client.force_login(self.user)
        response = self.client.post(f"/my-bookings/{booking.pk}/edit/", {
            "revision": revision, "room": self.room.pk,
            "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
            "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        booking.refresh_from_db()
        self.assertEqual(booking.end_time, self.end + timedelta(hours=1))

    def test_cancel_is_idempotent_and_wins_over_confirmation(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()
        stale_confirmation = Booking.objects.get(pk=booking.pk)
        operations.cancel(booking, revision=booking.revision, actor=self.user)
        operations.cancel(booking, revision=booking.revision, actor=self.user)
        with self.assertRaises(ValidationError):
            operations.confirm(stale_confirmation, revision=stale_confirmation.revision, actor=self.staff)

    def test_adjacent_and_cancelled_intervals(self):
        first = self.booking()
        second = self.booking(start_time=self.end, end_time=self.end + timedelta(hours=1))
        operations.cancel(first, revision=first.revision, actor=self.user)
        replacement = self.booking()
        self.assertEqual(replacement.status, "confirmed")
        self.assertEqual(second.status, "confirmed")

    def test_admin_reject_action_uses_controlled_operation(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()
        self.client.force_login(self.staff)
        response = self.client.post("/admin/rooms/booking/", {
            "action": "reject_bookings", "_selected_action": [booking.pk], f"revision_{booking.pk}": booking.revision,
        })
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.revision, 2)

    def test_admin_late_validation_error_is_handled(self):
        booking = self.booking()
        self.client.force_login(self.staff)
        with patch("rooms.admin.operations.persist", side_effect=ValidationError(operations.STALE)):
            response = self.client.post(f"/admin/rooms/booking/{booking.pk}/change/", {
                "expected_revision": booking.revision, "room": self.room.pk, "user": self.user.pk, "status": "confirmed",
                "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00",
            })
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.revision, 1)

    def test_completed_cancellation_policy_is_shared(self):
        with patch("django.utils.timezone.now", return_value=self.start - timedelta(days=1)):
            booking = self.booking()
        with patch("django.utils.timezone.now", return_value=self.end + timedelta(days=1)):
            client = APIClient()
            client.force_authenticate(self.user)
            self.assertEqual(client.post(f"/api/bookings/{booking.pk}/cancel/", {"revision": booking.revision}).status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_partial_save_preserves_untouched_fields(self):
        booking = self.booking()
        booking.end_time = self.end + timedelta(hours=2)
        booking.status = "cancelled"
        booking.save(revision=booking.revision, update_fields=["status"])
        booking.refresh_from_db()
        self.assertEqual(booking.end_time, self.end)
        self.assertEqual(booking.status, "cancelled")


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL; SQLite cannot test row locks.")
class PostgreSQLConcurrencyTests(OperationFixtures, TransactionTestCase):
    def race(self, *jobs):
        barrier = Barrier(len(jobs), timeout=10)
        real_lock = operations._lock_rooms

        def synchronized_lock(alias, ids):
            barrier.wait()
            return real_lock(alias, ids)

        def run(job):
            connections.close_all()
            try:
                return job()
            except ValidationError:
                return "rejected"
            finally:
                connections.close_all()

        with patch("rooms.booking_operations._lock_rooms", side_effect=synchronized_lock):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(run, job) for job in jobs]
                return [future.result(timeout=20) for future in futures]

    def test_two_api_requests_same_room(self):
        def create():
            client = APIClient()
            client.force_authenticate(self.user)
            return client.post("/api/bookings/", {
                "room": self.room.pk, "start_time": self.start.isoformat(), "end_time": self.end.isoformat(),
            }, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code
        self.assertCountEqual(self.race(create, create), [201, 400])
        self.assertEqual(Booking.objects.count(), 1)

    def test_html_and_api_same_room(self):
        client = Client()
        client.force_login(self.user)

        def html():
            return client.post(f"/book/{self.room.pk}/", {"idempotency_key": str(uuid4()),
                "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00",
            }).status_code

        def api():
            client = APIClient()
            client.force_authenticate(self.user)
            return client.post("/api/bookings/", {
                "room": self.room.pk, "start_time": self.start.isoformat(), "end_time": self.end.isoformat(),
            }, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code
        results = self.race(html, api)
        self.assertIn(results, [[302, 400], [200, 201]])
        self.assertEqual(Booking.objects.count(), 1)

    def test_edit_and_cancel(self):
        booking = self.booking()

        def edit():
            row = Booking.objects.get(pk=booking.pk)
            operations.edit(row, revision=row.revision, actor=self.user, end_time=self.end + timedelta(hours=1))
            return "edited"

        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=booking.revision)
            return "cancelled"
        results = self.race(edit, cancel)
        self.assertIn(results, [["edited", "rejected"], ["rejected", "cancelled"]])
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed" if results[0] == "edited" else "cancelled")
        self.assertEqual(booking.revision, 2)

    def test_confirm_and_cancel(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.booking()

        def confirm():
            operations.confirm(Booking.objects.get(pk=booking.pk), actor=self.staff, revision=booking.revision)
            return "confirmed"

        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=booking.revision)
            return "cancelled"
        results = self.race(confirm, cancel)
        self.assertIn(results, [["confirmed", "rejected"], ["rejected", "cancelled"]])
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed" if results[0] == "confirmed" else "cancelled")
        self.assertEqual(booking.revision, 2)

    def test_two_edits_reject_one_stale_writer(self):
        booking = self.booking()

        def edit(hours):
            row = Booking.objects.get(pk=booking.pk)
            operations.edit(row, revision=row.revision, actor=self.user, end_time=self.end + timedelta(hours=hours))
            return "edited"
        self.assertCountEqual(self.race(lambda: edit(1), lambda: edit(2)), ["edited", "rejected"])
        booking.refresh_from_db()
        self.assertEqual(booking.revision, 2)

    def test_opposite_room_transfers_do_not_deadlock(self):
        other = Room.objects.create(name="Other room")
        first = self.booking()
        second = self.booking(room=other, start_time=self.end, end_time=self.end + timedelta(hours=1))

        def move(pk, room):
            operations.edit(Booking.objects.get(pk=pk), actor=self.user, room=room, revision=1)
            return "moved"
        self.assertEqual(self.race(lambda: move(first.pk, other), lambda: move(second.pk, self.room)), ["moved", "moved"])

    def test_concurrent_adjacent_intervals_allowed(self):
        def create(start, end):
            operations.create(actor=self.user, room=self.room, start_time=start, end_time=end)
            return "created"
        self.assertEqual(self.race(lambda: create(self.start, self.end), lambda: create(self.end, self.end + timedelta(hours=1))), ["created", "created"])

    def test_database_constraint_rejects_raw_sql_overlap(self):
        booking = self.booking()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO rooms_booking (room_id, user_id, start_time, end_time, status, created_at, revision)
                        VALUES (%s, %s, %s, %s, 'pending', %s, 1)
                    """, [self.room.pk, self.user.pk, self.start, self.end, timezone.now()])
        operations.cancel(booking, revision=booking.revision, actor=self.user)
        self.booking()  # Cancelled rows do not occupy the interval.

    def test_migration_preflight_rejects_existing_overlap(self):
        from importlib import import_module
        migration = import_module("rooms.migrations.0005_booking_concurrency")
        self.booking()
        # Everything, including DROP CONSTRAINT, rolls back in this test DB.
        with self.assertRaisesRegex(RuntimeError, "Overlapping active bookings"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("ALTER TABLE rooms_booking DROP CONSTRAINT booking_no_active_overlap")
                    cursor.execute("""
                        INSERT INTO rooms_booking (room_id, user_id, start_time, end_time, status, created_at, revision)
                        VALUES (%s, %s, %s, %s, 'confirmed', %s, 1)
                    """, [self.room.pk, self.user.pk, self.start, self.end, timezone.now()])
                with connection.schema_editor() as editor:
                    migration.add_overlap_constraint(None, editor)
        self.assertEqual(Booking.objects.count(), 1)
