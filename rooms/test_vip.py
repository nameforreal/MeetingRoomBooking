from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from rooms.models import Booking, Room


class VipApprovalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="vip-owner")
        self.staff = get_user_model().objects.create_user(username="vip-reviewer", is_staff=True)
        self.standard = Room.objects.create(name="Standard")
        self.vip = Room.objects.create(name="VIP", is_vip=True)
        self.start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.booking = Booking.objects.create(
            user=self.user, room=self.standard, start_time=self.start, end_time=self.end,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)
        self.api_url = f"/api/bookings/{self.booking.pk}/"
        self.html_url = f"/my-bookings/{self.booking.pk}/edit/"

    def html_data(self, room=None):
        return {
            "revision": self.booking.revision,
            "room": (room or self.vip).pk,
            "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
            "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00",
            "status": "confirmed",
        }

    def transfer(self):
        response = self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.vip.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def authorize_staff(self):
        self.staff.user_permissions.add(Permission.objects.get(
            content_type__app_label="rooms", codename="change_booking",
        ))
        self.client.force_login(self.staff)

    def confirm(self):
        self.authorize_staff()
        response = self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision})
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_api_patch_standard_to_vip(self):
        self.transfer()
        self.assertEqual(self.booking.room_id, self.vip.pk)

    def test_api_put_standard_to_vip_ignores_status(self):
        response = self.api.put(self.api_url, {"revision": self.booking.revision, 
            "room": self.vip.pk, "start_time": self.start.isoformat(),
            "end_time": self.end.isoformat(), "status": "confirmed",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_html_transfer_ignores_status(self):
        response = self.client.post(self.html_url, self.html_data())
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual((self.booking.room_id, self.booking.status), (self.vip.pk, "pending"))

    def test_creation_cannot_inject_confirmed(self):
        response = self.api.post("/api/bookings/", {
            "room": self.vip.pk, "start_time": self.start.isoformat(),
            "end_time": self.end.isoformat(), "status": "confirmed",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        created = Booking.objects.get(pk=response.data["id"])
        self.assertEqual(created.status, "pending")
        created.status = "cancelled"
        created.save(revision=created.revision)
        response = self.client.post(f"/book/{self.vip.pk}/", self.html_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Booking.objects.filter(room=self.vip).latest("pk").status, "pending")

    def test_owner_cannot_confirm_pending_vip(self):
        self.transfer()
        response = self.api.patch(self.api_url, {"revision": self.booking.revision, "status": "confirmed"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.client.post(self.html_url, self.html_data()).status_code, 302)
        self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_authorized_confirmation(self):
        self.transfer()
        self.confirm()

    def test_unauthorized_staff_cannot_confirm(self):
        self.transfer()
        self.staff.user_permissions.add(Permission.objects.get(codename="view_booking"))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(
            f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision},
        ).status_code, 403)
        self.client.post("/admin/rooms/booking/", {
            "action": "confirm_bookings", "_selected_action": [self.booking.pk], f"revision_{self.booking.pk}": self.booking.revision,
        })
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_authorized_admin_action_confirmation(self):
        self.transfer()
        self.authorize_staff()
        self.client.post("/admin/rooms/booking/", {
            "action": "confirm_bookings", "_selected_action": [self.booking.pk], f"revision_{self.booking.pk}": self.booking.revision,
        })
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_confirmed_vip_reschedule_requires_new_approval_api(self):
        self.transfer()
        self.confirm()
        response = self.api.patch(self.api_url, {"revision": self.booking.revision, 
            "start_time": (self.start + timedelta(days=1)).isoformat(),
            "end_time": (self.end + timedelta(days=1)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_confirmed_vip_reschedule_requires_new_approval_html(self):
        self.transfer()
        self.confirm()
        self.client.force_login(self.user)
        data = self.html_data()
        data.update(start_time_1="11:00", end_time_1="12:00")
        self.assertEqual(self.client.post(self.html_url, data).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_unchanged_confirmed_vip_keeps_approval(self):
        self.transfer()
        self.confirm()
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.vip.pk}, format="json").status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_cancelled_vip_cannot_be_reactivated(self):
        self.transfer()
        self.api.post(self.api_url + "cancel/", {"revision": self.booking.revision})
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.standard.pk, "status": "confirmed"}, format="json").status_code, 400)
        self.client.post(self.html_url, self.html_data())
        self.authorize_staff()
        self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "cancelled")
        self.booking.status = "confirmed"
        with self.assertRaises(ValidationError):
            self.booking.save(revision=self.booking.revision)

    def test_regular_room_transfer_stays_confirmed(self):
        other = Room.objects.create(name="Other standard")
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": other.pk}, format="json").status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.client.post(self.html_url, self.html_data(self.standard)).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_partial_model_save_persists_pending(self):
        self.booking.room = self.vip
        self.booking.save(revision=self.booking.revision, update_fields=["room"])
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_admin_form_transfer_requires_separate_confirmation(self):
        self.authorize_staff()
        payload = {
            "expected_revision": self.booking.revision,
            "room": self.vip.pk, "user": self.user.pk, "status": "confirmed",
            "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00:00",
            "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00:00",
            "_save": "Save",
        }
        url = f"/admin/rooms/booking/{self.booking.pk}/change/"
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")
        payload["expected_revision"] = self.booking.revision
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_transfer_between_vip_rooms_requires_approval(self):
        self.transfer()
        self.confirm()
        other = Room.objects.create(name="Other VIP", is_vip=True)
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": other.pk}, format="json").status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "pending")

    def test_time_only_html_post_remains_supported(self):
        data = self.html_data()
        data.pop("room")
        self.assertEqual(self.client.post(self.html_url, data).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual((self.booking.room_id, self.booking.status), (self.standard.pk, "confirmed"))

    def test_transfer_conflict_keeps_original_booking(self):
        Booking.objects.create(room=self.vip, user=self.staff, start_time=self.start, end_time=self.end)
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.vip.pk}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.html_url, self.html_data()).status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual((self.booking.room_id, self.booking.status), (self.standard.pk, "confirmed"))

    def test_inactive_and_foreign_booking_transfer_denied(self):
        self.vip.is_active = False
        self.vip.save()
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.vip.pk}, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.html_url, self.html_data()).status_code, 200)
        self.booking.user = self.staff
        self.booking.save(revision=self.booking.revision)
        self.assertEqual(self.api.patch(self.api_url, {"revision": self.booking.revision, "room": self.vip.pk}, format="json").status_code, 404)
        self.assertEqual(self.client.post(self.html_url, self.html_data()).status_code, 404)
