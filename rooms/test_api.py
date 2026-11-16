from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from .models import Room, Booking


class ApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="apiuser", password="StrongPass123!")
        self.other = get_user_model().objects.create_user(username="other", password="StrongPass123!")
        self.room = Room.objects.create(name="Room A", capacity=4)
        self.vip = Room.objects.create(name="VIP", capacity=6, is_vip=True)
        self.client = APIClient()
        self.start = timezone.now() + timedelta(days=2)
        self.end = self.start + timedelta(hours=2)

    def payload(self, room=None, start=None, end=None):
        return {"room": (room or self.room).pk, "start_time": (start or self.start).isoformat(), "end_time": (end or self.end).isoformat()}

    def test_rooms_public_and_only_active(self):
        Room.objects.create(name="Hidden", is_active=False)
        response = self.client.get("/api/rooms/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_booking_requires_login(self):
        self.assertIn(self.client.post("/api/bookings/", self.payload()).status_code, (401, 403))

    def test_create_and_list_own_booking(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/bookings/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(self.client.get("/api/bookings/").data), 1)
        self.assertEqual(Booking.objects.get().status, "confirmed")

    def test_vip_is_pending(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/bookings/", self.payload(room=self.vip), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "pending")

    def test_overlap_rejected(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/bookings/", self.payload(), format="json").status_code, 201)
        response = self.client.post("/api/bookings/", self.payload(), format="json")
        self.assertEqual(response.status_code, 400)

    def test_cannot_read_or_cancel_other_users_booking(self):
        booking = Booking.objects.create(room=self.room, user=self.other, start_time=self.start, end_time=self.end)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(f"/api/bookings/{booking.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/bookings/{booking.pk}/cancel/").status_code, 404)

    def test_owner_can_cancel(self):
        booking = Booking.objects.create(room=self.room, user=self.user, start_time=self.start, end_time=self.end)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(f"/api/bookings/{booking.pk}/cancel/").status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_cannot_change_status_directly(self):
        self.client.force_authenticate(self.user)
        data = self.payload()
        data["status"] = "confirmed"
        data["user"] = self.other.pk
        response = self.client.post("/api/bookings/", data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().user, self.user)
