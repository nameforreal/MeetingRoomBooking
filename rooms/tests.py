from uuid import uuid4
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rooms.models import Room, Booking


class BookingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tester", password="StrongPass123!")
        self.other = get_user_model().objects.create_user(username="other", password="StrongPass123!")
        self.room = Room.objects.create(name="Test room", capacity=4)
        self.start = (timezone.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=2)

    def create_booking(self, **changes):
        values = dict(room=self.room, user=self.user, start_time=self.start, end_time=self.end)
        values.update(changes)
        return Booking.objects.create(**values)

    def test_normal_room_confirmed(self):
        self.assertEqual(self.create_booking().status, "confirmed")

    def test_vip_room_pending(self):
        self.room.is_vip = True
        self.room.save()
        self.assertEqual(self.create_booking().status, "pending")

    def test_overlap_rejected(self):
        self.create_booking()
        with self.assertRaises(ValidationError):
            self.create_booking(start_time=self.start + timedelta(hours=1), end_time=self.end + timedelta(hours=1))

    def test_adjacent_booking_allowed(self):
        self.create_booking()
        self.create_booking(start_time=self.end, end_time=self.end + timedelta(hours=1))
        self.assertEqual(Booking.objects.count(), 2)

    def test_duration_too_short(self):
        with self.assertRaises(ValidationError):
            self.create_booking(end_time=self.start + timedelta(minutes=30))

    def test_duration_too_long(self):
        with self.assertRaises(ValidationError):
            self.create_booking(end_time=self.start + timedelta(hours=5))

    def test_past_booking_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_booking(start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() - timedelta(hours=23))

    def test_cancel_frees_slot(self):
        booking = self.create_booking()
        booking.status = "cancelled"
        booking.save(revision=booking.revision, update_fields=["status"])
        self.create_booking()
        self.assertEqual(Booking.objects.count(), 2)

    def test_cancel_old_booking(self):
        from unittest.mock import patch
        past = (timezone.now() - timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0)
        with patch("django.utils.timezone.now", return_value=past):
            booking = self.create_booking(start_time=past + timedelta(hours=1),
                                          end_time=past + timedelta(hours=3))
        booking.status = "cancelled"
        booking.save(revision=booking.revision, update_fields=["status"])
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_other_user_cannot_cancel(self):
        booking = self.create_booking()
        self.client.force_login(self.other)
        response = self.client.post(reverse("cancel_booking", args=[booking.pk]))
        self.assertEqual(response.status_code, 404)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")

    def test_cancel_requires_post(self):
        booking = self.create_booking()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("cancel_booking", args=[booking.pk])).status_code, 405)

    def test_anonymous_cannot_book(self):
        response = self.client.get(reverse("book_room", args=[self.room.pk]))
        self.assertEqual(response.status_code, 302)

    def test_inactive_room_not_bookable(self):
        self.room.is_active = False
        self.room.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("book_room", args=[self.room.pk])).status_code, 404)

    def test_my_bookings_excludes_other_user(self):
        self.create_booking(user=self.other)
        self.client.force_login(self.user)
        response = self.client.get(reverse("my_bookings"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["bookings"]), 0)


class BookingApiTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        self.api = APIClient()
        self.user = get_user_model().objects.create_user(username="api_user", password="StrongPass123!")
        self.other = get_user_model().objects.create_user(username="api_other", password="StrongPass123!")
        self.room = Room.objects.create(name="API room", capacity=8)
        self.start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)

    def booking(self, **kwargs):
        values = dict(room=self.room, user=self.user, start_time=self.start, end_time=self.end)
        values.update(kwargs)
        return Booking.objects.create(**values)

    def test_room_list_is_public(self):
        response = self.api.get("/api/rooms/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["name"], "API room")

    def test_booking_list_requires_authentication(self):
        self.assertEqual(self.api.get("/api/bookings/").status_code, 403)

    def test_user_sees_only_own_bookings(self):
        own = self.booking()
        self.booking(user=self.other, start_time=self.start + timedelta(days=1), end_time=self.end + timedelta(days=1))
        self.api.force_authenticate(self.user)
        response = self.api.get("/api/bookings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [own.pk])

    def test_create_booking(self):
        self.api.force_authenticate(self.user)
        response = self.api.post("/api/bookings/", {
            "room": self.room.pk,
            "start_time": (self.start + timedelta(days=1)).isoformat(),
            "end_time": (self.end + timedelta(days=1)).isoformat(),
        }, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Booking.objects.filter(user=self.user).count(), 1)

    def test_patch_booking_time(self):
        booking = self.booking()
        self.api.force_authenticate(self.user)
        new_start = self.start + timedelta(days=2)
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": booking.revision,
            "start_time": new_start.isoformat(),
            "end_time": (new_start + timedelta(hours=2)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        booking.refresh_from_db()
        self.assertEqual(booking.start_time, new_start)

    def test_cannot_update_another_users_booking(self):
        booking = self.booking()
        self.api.force_authenticate(self.other)
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": booking.revision,"start_time": (self.start + timedelta(days=2)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_patch_rejects_overlap(self):
        first = self.booking()
        second_start = self.start + timedelta(days=1)
        second = self.booking(start_time=second_start, end_time=second_start + timedelta(hours=1))
        self.api.force_authenticate(self.user)
        response = self.api.patch(f"/api/bookings/{second.pk}/", {"revision": second.revision,
            "start_time": (self.start + timedelta(minutes=15)).isoformat(),
            "end_time": (self.start + timedelta(hours=1, minutes=15)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 400)
        second.refresh_from_db()
        self.assertEqual(second.start_time, second_start)

    def test_cancel_booking(self):
        booking = self.booking()
        self.api.force_authenticate(self.user)
        response = self.api.post(f"/api/bookings/{booking.pk}/cancel/", {"revision": booking.revision})
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_filter_by_status(self):
        self.booking()
        self.api.force_authenticate(self.user)
        response = self.api.get("/api/bookings/?status=confirmed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_room_availability_endpoint(self):
        self.booking()
        response = self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": self.start.date().isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["busy_intervals"]), 1)


    def test_room_write_requires_staff(self):
        self.api.force_authenticate(self.user)
        response = self.api.post("/api/rooms/", {"name": "No permission", "capacity": 5}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_create_and_archive_room_via_api(self):
        staff = get_user_model().objects.create_user(username="api_staff", password="StrongPass123!", is_staff=True)
        from django.contrib.auth.models import Permission
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="rooms", codename__in=["add_room", "change_room"]))
        self.api.force_authenticate(staff)
        response = self.api.post("/api/rooms/", {"name": "Staff room", "capacity": 12, "is_vip": True}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        room_id = response.data["id"]
        response = self.api.delete(f"/api/rooms/{room_id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Room.objects.get(pk=room_id).is_active)


class AdminWorkflowTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(username="siteadmin", email="admin@example.com", password="StrongPass123!")
        self.user = get_user_model().objects.create_user(username="admin_target", password="StrongPass123!")
        self.room = Room.objects.create(name="Admin room", capacity=10, is_vip=True)
        start = (timezone.now() + timedelta(days=4)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.booking = Booking.objects.create(room=self.room, user=self.user, start_time=start, end_time=start + timedelta(hours=1))
        self.client.force_login(self.admin_user)

    def test_admin_can_open_room_and_booking_lists(self):
        self.assertEqual(self.client.get("/admin/rooms/room/").status_code, 200)
        self.assertEqual(self.client.get("/admin/rooms/booking/").status_code, 200)

    def test_admin_csv_action_exports_selected_bookings(self):
        response = self.client.post("/admin/rooms/booking/", {
            "action": "export_bookings_csv",
            "_selected_action": [str(self.booking.pk)],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("Admin room", response.content.decode("utf-8-sig"))
