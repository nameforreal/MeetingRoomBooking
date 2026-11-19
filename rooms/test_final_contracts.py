"""Regression coverage for final API, room, navigation and VIP contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import booking_operations as operations
from .models import AuditEvent, Booking, BookingCreationRequest, Room


class ContractFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user("final-owner")
        self.root = get_user_model().objects.create_superuser("final-admin", "", "Final-admin-827!")
        self.room = Room.objects.create(name="Standard")
        self.vip = Room.objects.create(name="VIP", is_vip=True)
        self.start = (timezone.now() + timedelta(days=3)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self.end = self.start + timedelta(hours=1)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)

    def booking(self, room=None):
        return operations.create(
            actor=self.user, room=room or self.vip, start_time=self.start, end_time=self.end
        )

    def approved(self):
        booking = self.booking()
        return operations.confirm(booking, actor=self.root, revision=booking.revision)

    def html(self, booking, room):
        return {
            "room": room.pk,
            "revision": booking.revision,
            "start_time_0": self.start.date().isoformat(),
            "start_time_1": "10:00",
            "end_time_0": self.end.date().isoformat(),
            "end_time_1": "11:00",
        }


class RoomFilterTests(ContractFixtures, TestCase):
    def test_valid_and_unknown_ids_keep_array_response(self):
        booking = self.booking(self.room)
        for value in (str(self.room.pk), "0" + str(self.room.pk)):
            result = self.api.get("/api/bookings/", {"room": value})
            self.assertEqual(result.status_code, 200)
            self.assertIsInstance(result.data, list)
            self.assertEqual([b["id"] for b in result.data], [booking.pk])
        for value in ("9223372036854775807", "999999999"):
            response = self.api.get("/api/bookings/", {"room": value})
            self.assertEqual((response.status_code, response.data), (200, []))

    def test_invalid_room_filters_are_400(self):
        for value in (
            "²",
            "abc",
            "",
            "9" * 5000,
            "-1",
            "0",
            "1.0",
            " 1",
            "١",
            "9223372036854775808",
        ):
            with self.subTest(value=value[:30]):
                response = self.api.get("/api/bookings/", {"room": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("room", response.data)

    def test_absent_filter_and_owner_scope_are_preserved(self):
        booking = self.booking(self.room)
        self.assertEqual([b["id"] for b in self.api.get("/api/bookings/").data], [booking.pk])
        self.api.force_authenticate(self.root)
        self.assertEqual(self.api.get("/api/bookings/", {"room": self.room.pk}).data, [])


class CapacityTests(ContractFixtures, TestCase):
    def test_api_create_and_update_boundaries(self):
        self.api.force_authenticate(self.root)
        for value in (1, 200):
            response = self.api.post(
                "/api/rooms/", {"name": "Capacity", "capacity": value}, format="json"
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["capacity"], value)
            response = self.api.patch(
                f"/api/rooms/{self.room.pk}/", {"capacity": value}, format="json"
            )
            self.assertEqual(response.status_code, 200)
        for value in (0, 201, -1, "invalid", True, None, [], {}, 1.5):
            with self.subTest(value=value):
                for method, url, data in [
                    (self.api.post, "/api/rooms/", {"name": "Invalid", "capacity": value}),
                    (self.api.patch, f"/api/rooms/{self.room.pk}/", {"capacity": value}),
                ]:
                    response = method(url, data, format="json")
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("capacity", response.data)

    def test_both_admin_forms_validate_capacity(self):
        self.client.force_login(self.root)
        for url in ("/management/rooms/add/", "/admin/rooms/room/add/"):
            for value in (0, 201, -1, "bad", "1.5"):
                with self.subTest(url=url, value=value):
                    before = Room.objects.count()
                    response = self.client.post(
                        url, {"name": "Invalid", "capacity": value, "is_active": "on"}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(Room.objects.count(), before)
                    form = (
                        response.context["form"]
                        if url.startswith("/management")
                        else response.context["adminform"].form
                    )
                    self.assertIn("capacity", form.errors)
            for value in (1, 200):
                response = self.client.post(
                    url, {"name": f"Accepted {value}", "capacity": value, "is_active": "on"}
                )
                self.assertEqual(response.status_code, 302)

    def test_model_clean_and_save_validate_capacity(self):
        for value in (0, 201, -1, True, "bad", 1.5):
            room = Room(name="Invalid", capacity=value)
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    room.clean()
                with self.assertRaises(ValidationError):
                    room.save()
        for value in (1, 200):
            Room(name="Valid", capacity=value).full_clean()

    def test_legacy_capacity_does_not_prevent_archival_or_change_history(self):
        booking = self.booking(self.room)
        # Simulate legacy data ONLY in the disposable test database.
        Room.objects.filter(pk=self.room.pk).update(capacity=0)
        self.api.force_authenticate(self.root)
        self.assertEqual(self.api.delete(f"/api/rooms/{self.room.pk}/").status_code, 204)
        self.room.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(
            (self.room.capacity, self.room.is_active, booking.status), (0, False, "confirmed")
        )

    def test_room_permissions_unchanged(self):
        self.assertEqual(
            self.api.post(
                "/api/rooms/", {"name": "Forbidden", "capacity": 1}, format="json"
            ).status_code,
            403,
        )


class NavigationTests(ContractFixtures, TestCase):
    def test_role_navigation_targets_only_allowed_sections(self):
        call_command("setup_roles", stdout=StringIO())
        cases = [
            (None, None),
            ("Room Managers", "/management/rooms/"),
            ("User Managers", "/management/users/"),
            ("Booking Managers", "/management/"),
        ]
        for index, (group, expected) in enumerate(cases):
            with self.subTest(group=group):
                user = get_user_model().objects.create_user(f"nav-{index}", is_staff=bool(group))
                if group:
                    user.groups.add(Group.objects.get(name=group))
                self.client.force_login(user)
                response = self.client.get("/")
                if expected:
                    self.assertContains(response, f'href="{expected}"')
                    self.assertEqual(self.client.get(expected).status_code, 200)
                else:
                    self.assertNotContains(response, "Управление")
                for forbidden in {"/management/", "/management/rooms/", "/management/users/"} - {
                    expected
                }:
                    self.assertNotContains(response, f'href="{forbidden}"')
                if group == "Room Managers":
                    self.assertNotContains(self.client.get(expected), 'href="/management/users/"')
                if group == "Booking Managers":
                    self.assertContains(self.client.get("/management/bookings/"), "Бронирования")

    def test_root_navigation_unchanged_and_staff_without_permissions_hidden(self):
        self.client.force_login(self.root)
        self.assertContains(self.client.get("/"), 'href="/management/"')
        self.assertEqual(self.client.get("/management/").status_code, 200)
        user = get_user_model().objects.create_user("no-permissions", is_staff=True)
        self.client.force_login(user)
        self.assertNotContains(self.client.get("/"), "Управление")

    def test_view_only_staff_does_not_see_forbidden_links_or_actions(self):
        booking = self.booking()
        viewer = get_user_model().objects.create_user("view-only", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_booking"))
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get("/management/"), 'href="/management/rooms/"')
        response = self.client.get("/management/bookings/")
        self.assertNotContains(response, f'action="/management/bookings/{booking.pk}/status/"')
        self.assertNotContains(response, 'href="/management/export.csv')

    def test_user_manager_cannot_see_edit_links_for_staff(self):
        call_command("setup_roles", stdout=StringIO())
        manager = get_user_model().objects.create_user("user-manager", is_staff=True)
        manager.groups.add(Group.objects.get(name="User Managers"))
        self.client.force_login(manager)
        response = self.client.get("/management/users/")
        self.assertContains(response, f'href="/management/users/{self.user.pk}/edit/"')
        self.assertNotContains(response, f'href="/management/users/{self.root.pk}/edit/"')
        self.assertNotContains(response, f'href="/management/users/{manager.pk}/edit/"')


class VIPTransferTests(ContractFixtures, TestCase):
    def assert_transfer(self, booking, source):
        booking.refresh_from_db()
        self.assertEqual(
            (booking.room_id, booking.status, booking.revision), (self.room.pk, "pending", 3)
        )
        event = AuditEvent.objects.filter(booking=booking).latest("id")
        self.assertEqual(
            (event.before["status"], event.after["status"], event.source),
            ("confirmed", "pending", source),
        )
        self.assertEqual((event.previous_room_id, event.new_room_id), (self.vip.pk, self.room.pk))
        self.assertEqual((event.revision_before, event.revision_after), (2, 3))

    def test_api_transfer_audit_revision_and_reconfirmation(self):
        booking = self.approved()
        response = self.api.patch(
            f"/api/bookings/{booking.pk}/",
            {"room": self.room.pk, "revision": 2, "status": "confirmed"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assert_transfer(booking, "api")
        response = self.api.patch(
            f"/api/bookings/{booking.pk}/", {"revision": 3, "status": "confirmed"}, format="json"
        )
        self.assertEqual(response.data["status"], "pending")
        booking.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            operations.confirm(booking, actor=self.user, revision=booking.revision)
        self.client.force_login(self.root)
        self.assertEqual(
            self.client.post(
                f"/management/bookings/{booking.pk}/status/",
                {"status": "confirmed", "revision": booking.revision},
            ).status_code,
            302,
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")

    def test_html_transfer(self):
        booking = self.approved()
        response = self.client.post(
            f"/my-bookings/{booking.pk}/edit/", self.html(booking, self.room)
        )
        self.assertEqual(response.status_code, 302)
        self.assert_transfer(booking, "html")

    def test_admin_cannot_transfer_and_keep_confirmation(self):
        booking = self.approved()
        self.client.force_login(self.root)
        response = self.client.post(
            f"/admin/rooms/booking/{booking.pk}/change/",
            {
                "room": self.room.pk,
                "user": self.user.pk,
                "status": "confirmed",
                "expected_revision": 2,
                "start_time_0": self.start.date().isoformat(),
                "start_time_1": "10:00:00",
                "end_time_0": self.end.date().isoformat(),
                "end_time_1": "11:00:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assert_transfer(booking, "django_admin")

    def test_idempotent_creation_replay_keeps_snapshot_and_current_pending(self):
        key = str(uuid4())
        payload = {
            "room": self.vip.pk,
            "start_time": self.start.isoformat(),
            "end_time": self.end.isoformat(),
        }
        first = self.api.post("/api/bookings/", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
        booking = Booking.objects.get(pk=first.data["id"])
        booking = operations.confirm(booking, actor=self.root, revision=1)
        operations.edit(booking, actor=self.user, revision=2, room=self.room)
        count = AuditEvent.objects.count()
        replay = self.api.post("/api/bookings/", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.data, first.data)
        booking.refresh_from_db()
        self.assertEqual(
            (booking.status, booking.revision, booking.room_id), ("pending", 3, self.room.pk)
        )
        self.assertEqual(
            (
                AuditEvent.objects.count(),
                Booking.objects.count(),
                BookingCreationRequest.objects.count(),
            ),
            (count, 1, 1),
        )

    def test_pending_destination_blocks_overlap_and_stale_approval(self):
        booking = self.approved()
        operations.edit(booking, actor=self.user, revision=2, room=self.room)
        with self.assertRaises(ValidationError):
            operations.create(
                actor=self.user, room=self.room, start_time=self.start, end_time=self.end
            )
        with self.assertRaises(ValidationError):
            operations.confirm(booking, actor=self.root, revision=2)
        self.assertEqual(Booking.objects.count(), 1)

    def test_unchanged_vip_and_ordinary_transfer_contracts_preserved(self):
        booking = self.approved()
        operations.edit(booking, actor=self.user, revision=2)
        self.assertEqual(booking.status, "confirmed")
        operations.edit(
            booking, actor=self.user, revision=3, end_time=self.end + timedelta(hours=1)
        )
        self.assertEqual(booking.status, "pending")
        ordinary = self.booking(self.room)
        another = Room.objects.create(name="Another standard")
        operations.edit(ordinary, actor=self.user, revision=1, room=another)
        self.assertEqual(ordinary.status, "confirmed")


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL booking locks.")
class VIPTransferConcurrencyTests(ContractFixtures, TransactionTestCase):
    def race(self, *jobs):
        barrier = Barrier(len(jobs), timeout=8)
        original = operations._lock_rooms

        def lock(alias, ids):
            barrier.wait()
            return original(alias, ids)

        def run(job):
            connections.close_all()
            try:
                return job()
            except ValidationError:
                return "rejected"
            finally:
                connections.close_all()

        with patch("rooms.booking_operations._lock_rooms", side_effect=lock):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(run, job) for job in jobs]
                return [future.result(timeout=15) for future in futures]

    def test_transfer_competes_with_destination_creation(self):
        booking = self.approved()

        def transfer():
            operations.edit(
                Booking.objects.get(pk=booking.pk), actor=self.user, revision=2, room=self.room
            )
            return "transferred"

        def create():
            operations.create(
                actor=self.user, room=self.room, start_time=self.start, end_time=self.end
            )
            return "created"

        results = self.race(transfer, create)
        self.assertIn(results, [["transferred", "rejected"], ["rejected", "created"]])
        self.assertEqual(Booking.objects.filter(room=self.room).count(), 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending" if results[0] == "transferred" else "confirmed")

    def test_transfer_competes_with_cancellation(self):
        booking = self.approved()

        def transfer():
            operations.edit(
                Booking.objects.get(pk=booking.pk), actor=self.user, revision=2, room=self.room
            )
            return "transferred"

        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=2)
            return "cancelled"

        results = self.race(transfer, cancel)
        self.assertIn(results, [["transferred", "rejected"], ["rejected", "cancelled"]])
        booking.refresh_from_db()
        self.assertEqual(booking.revision, 3)
        self.assertEqual(booking.status, "pending" if results[0] == "transferred" else "cancelled")
        self.assertEqual(AuditEvent.objects.filter(booking=booking).count(), 3)

    def test_pending_transfer_competes_with_approval_of_viewed_version(self):
        booking = self.booking()

        def transfer():
            operations.edit(
                Booking.objects.get(pk=booking.pk), actor=self.user, revision=1, room=self.room
            )
            return "transferred"

        def confirm():
            operations.confirm(Booking.objects.get(pk=booking.pk), actor=self.root, revision=1)
            return "confirmed"

        results = self.race(transfer, confirm)
        self.assertIn(results, [["transferred", "rejected"], ["rejected", "confirmed"]])
        booking.refresh_from_db()
        self.assertEqual(
            (booking.room_id, booking.status),
            (self.room.pk, "pending")
            if results[0] == "transferred"
            else (self.vip.pk, "confirmed"),
        )
        self.assertEqual(booking.revision, 2)
