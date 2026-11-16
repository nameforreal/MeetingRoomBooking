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
        self.start = timezone.now() + timedelta(days=2)
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
        booking.save(update_fields=["status"])
        self.create_booking()
        self.assertEqual(Booking.objects.count(), 2)

    def test_cancel_old_booking(self):
        booking = self.create_booking()
        Booking.objects.filter(pk=booking.pk).update(start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(days=2, hours=-2))
        booking.refresh_from_db()
        booking.status = "cancelled"
        booking.save(update_fields=["status"])
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
