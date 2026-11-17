from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import booking_operations as operations, booking_rules as rules
from .forms import BookingForm
from .models import Booking, Room
from .test_concurrency import PostgreSQLConcurrencyTests


class BusinessFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="rules-owner")
        self.staff = get_user_model().objects.create_user(username="rules-admin", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="rooms", codename__in=["change_room", "view_room", "change_booking", "view_booking"],
        ))
        self.room = Room.objects.create(name="Business rules")
        self.start = (timezone.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)

    def create(self, **changes):
        data = dict(room=self.room, start_time=self.start, end_time=self.end, user=self.user)
        data.update(changes)
        return Booking.objects.create(**data)

    def payload(self, start=None, end=None, **changes):
        data = {"room": self.room.pk, "start_time": (start or self.start).isoformat(),
                "end_time": (end or self.end).isoformat()}
        data.update(changes)
        return data

    def html_payload(self, start=None, end=None, **changes):
        start, end = start or self.start, end or self.end
        data = {"room": self.room.pk, "start_time_0": start.date().isoformat(),
                "start_time_1": start.strftime("%H:%M:%S"), "end_time_0": end.date().isoformat(),
                "end_time_1": end.strftime("%H:%M:%S")}
        data.update(changes)
        return data


class CalendarTests(BusinessFixtures, TestCase):
    bad_dates = ["2026-02-30", "2026-13-01", "", "garbage", "2026-2-3", "20261116",
                 "0001-01-01", "9999-12-31", "10000-01-01"]

    def test_invalid_dates_in_api_lists_and_availability(self):
        for value in self.bad_dates:
            for url, key in [("/api/bookings/", "date_from"), ("/api/bookings/", "date_to"),
                             (f"/api/rooms/{self.room.pk}/availability/", "date")]:
                with self.subTest(value=value, url=url, key=key):
                    response = self.api.get(url, {key: value})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(key, response.data)

    def test_html_invalid_dates_show_messages(self):
        for value in self.bad_dates:
            for url in ["/schedule/", f"/book/{self.room.pk}/"]:
                with self.subTest(value=value, url=url):
                    response = self.client.get(url, {"date": value})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(list(response.context["messages"]))

    def test_analytics_invalid_dates_show_messages(self):
        self.client.force_login(self.staff)
        for value in self.bad_dates:
            response = self.client.get("/management/analytics/", {"from": value})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(list(response.context["messages"]))

    def test_missing_dates_and_reversed_ranges(self):
        self.assertEqual(self.api.get("/api/bookings/").status_code, 200)
        self.assertEqual(self.api.get(f"/api/rooms/{self.room.pk}/availability/").status_code, 400)
        self.assertEqual(self.client.get("/schedule/").status_code, 200)
        self.assertEqual(self.api.get("/api/bookings/", {"date_from": "2026-11-20", "date_to": "2026-11-19"}).status_code, 400)
        self.client.force_login(self.staff)
        for params in [{"from": "2026-11-20", "to": "2026-11-19"}, {"from": "2020-01-01", "to": "2026-01-01"}]:
            response = self.client.get("/management/analytics/", params)
            self.assertTrue(list(response.context["messages"]))

    def test_schedule_and_availability_share_horizon(self):
        beyond = rules.last_booking_date() + timedelta(days=1)
        for day in [rules.site_today() - timedelta(days=1), beyond]:
            self.assertEqual(self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": day.isoformat()}).status_code, 400)
        response = self.client.get("/schedule/", {"date": beyond.isoformat()})
        self.assertTrue(list(response.context["messages"]))
        self.assertEqual(response.context["max_date"], rules.last_booking_date().isoformat())

    def test_extreme_datetimes_and_bad_timezones_return_400(self):
        values = ["9999-12-31T23:59:59Z", "0001-01-01T00:00:00+14:00",
                  "9999-12-31T23:59:59-14:00", "2026-02-30T10:00:00Z", "",
                  "2026-11-20T10:00:00+25:00", "2026-11-20T10:00:00[Not/AZone]", "bad"]
        values += ["2026-11-20T10:00:00+02:99", "2026-11-20T10:00:00+01:00:99"]
        values += ["2026-11-20T10:00:00+02:99.5", "2026-11-20T10:00:00+01:00:99.5"]
        for value in values:
            for field in ["start_time", "end_time"]:
                with self.subTest(value=value, field=field):
                    response = self.api.post("/api/bookings/", self.payload(**{field: value}), format="json")
                    self.assertEqual(response.status_code, 400)

    def test_html_bad_date_fields_are_validation_errors(self):
        for day in self.bad_dates:
            response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(start_time_0=day))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)

    def test_date_widget_uses_iso_value(self):
        form = BookingForm(initial={"start_time": self.start, "end_time": self.end})
        self.assertIn(f'value="{self.start.date().isoformat()}"', str(form["start_time"]))

    def test_admin_invalid_dates_are_form_errors(self):
        booking = self.create()
        self.client.force_login(self.staff)
        for value in ["2026-02-30", "9999-12-31", ""]:
            response = self.client.post(f"/admin/rooms/booking/{booking.pk}/change/", {
                "expected_revision": booking.revision, "room": self.room.pk, "user": self.user.pk, "status": "confirmed",
                "start_time_0": value, "start_time_1": "10:00",
                "end_time_0": value, "end_time_1": "11:00",
            })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["adminform"].form.errors)

    def test_day_bounds_maximum_does_not_overflow(self):
        start, end = rules.day_bounds(rules.MAX_DATE)
        self.assertEqual(end - start, timedelta(days=1))
        with self.assertRaises(ValidationError):
            rules.day_bounds(datetime.max.date())

    def test_leap_day_and_year_boundary(self):
        leap_now = datetime(2028, 2, 20, 9, tzinfo=datetime_timezone.utc)
        with patch("django.utils.timezone.now", return_value=leap_now):
            response = self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": "2028-02-29"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": "2028-02-30"}).status_code, 400)
        year_end = datetime(2026, 12, 20, 9, tzinfo=datetime_timezone.utc)
        with patch("django.utils.timezone.now", return_value=year_end):
            self.assertEqual(rules.last_booking_date().isoformat(), "2027-01-19")
            self.assertEqual(self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": "2027-01-19"}).status_code, 200)
            self.assertEqual(self.api.get(f"/api/rooms/{self.room.pk}/availability/", {"date": "2027-01-20"}).status_code, 400)

    def test_history_filter_accepts_safe_calendar_extremes(self):
        for day in [rules.MIN_DATE, rules.MAX_DATE]:
            self.assertEqual(self.api.get("/api/bookings/", {"date_from": day.isoformat(), "date_to": day.isoformat()}).status_code, 200)


class TimePolicyTests(BusinessFixtures, TestCase):
    def test_outside_hours_rejected_by_html_api_and_model(self):
        for start, end in [(self.start.replace(hour=7), self.end.replace(hour=9)),
                           (self.start.replace(hour=19), self.end.replace(hour=21)),
                           (self.start.replace(hour=23), self.end + timedelta(days=1))]:
            with self.subTest(start=start, end=end):
                self.assertEqual(self.api.post("/api/bookings/", self.payload(start, end), format="json").status_code, 400)
                response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(start, end))
                self.assertTrue(response.context["form"].errors)
                with self.assertRaises(ValidationError):
                    self.create(start_time=start, end_time=end)

    def test_horizon_inclusive_and_beyond_rejected(self):
        start = datetime.combine(rules.last_booking_date(), self.start.timetz())
        self.assertEqual(self.api.post("/api/bookings/", self.payload(start, start + timedelta(hours=1)), format="json").status_code, 201)
        start += timedelta(days=1)
        self.assertEqual(self.api.post("/api/bookings/", self.payload(start, start + timedelta(hours=1)), format="json").status_code, 400)
        response = self.client.post(f"/book/{self.room.pk}/", self.html_payload(start, start + timedelta(hours=1)))
        self.assertTrue(response.context["form"].errors)

    def test_open_close_and_duration_boundaries(self):
        for start, end in [(self.start.replace(hour=8), self.end.replace(hour=9)),
                           (self.start.replace(hour=16), self.end.replace(hour=20))]:
            self.assertEqual(self.api.post("/api/bookings/", self.payload(start, end), format="json").status_code, 201)
        for duration in [timedelta(minutes=59), timedelta(hours=4, seconds=1)]:
            self.assertEqual(self.api.post("/api/bookings/", self.payload(self.start, self.start + duration), format="json").status_code, 400)

    def test_one_second_outside_workday_and_duration(self):
        opening = self.start.replace(hour=8)
        closing = self.start.replace(hour=20)
        for start, end in [
            (opening - timedelta(seconds=1), opening + timedelta(hours=1)),
            (closing - timedelta(hours=1), closing + timedelta(seconds=1)),
            (self.start, self.start + timedelta(hours=1) - timedelta(seconds=1)),
            (self.start, self.start + timedelta(hours=4, microseconds=1)),
        ]:
            with self.subTest(start=start, end=end):
                self.assertEqual(self.api.post("/api/bookings/", self.payload(start, end), format="json").status_code, 400)

    def test_naive_api_and_html_times_use_site_timezone(self):
        with timezone.override("Asia/Tokyo"):
            form = BookingForm(self.html_payload())
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["start_time"], self.start)
            response = self.api.post("/api/bookings/", self.payload(
                self.start.replace(tzinfo=None), self.end.replace(tzinfo=None),
            ), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().start_time, self.start)

    def test_slots_do_not_round_or_forbid_partial_hours(self):
        start = self.start + timedelta(minutes=15)
        response = self.api.post("/api/bookings/", self.payload(start, start + timedelta(minutes=90)), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().start_time, start)

    def test_valid_offset_converts_to_site_timezone(self):
        offset = datetime_timezone(timedelta(hours=3))
        response = self.api.post("/api/bookings/", self.payload(self.start.astimezone(offset), self.end.astimezone(offset)), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().start_time, self.start)

    def test_offset_cannot_bypass_working_hours(self):
        # 08:00+03:00 is 05:00 UTC, outside the site's opening hours.
        start = self.start.replace(hour=8, tzinfo=datetime_timezone(timedelta(hours=3)))
        self.assertEqual(self.api.post("/api/bookings/", self.payload(start, start + timedelta(hours=1)), format="json").status_code, 400)

    def test_naive_model_datetime_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(start_time=self.start.replace(tzinfo=None), end_time=self.end.replace(tzinfo=None))

    def test_request_timezone_does_not_change_site_working_hours(self):
        # All entry points use settings.TIME_ZONE, not a per-request override.
        with timezone.override("Asia/Tokyo"):
            response = self.api.post("/api/bookings/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().start_time, self.start)

    def test_past_start_rejected(self):
        start = self.start - timedelta(days=5)
        self.assertEqual(self.api.post("/api/bookings/", self.payload(start, start + timedelta(hours=1)), format="json").status_code, 400)

    def test_edit_archived_room_is_rejected_without_deleting_booking(self):
        booking = self.create()
        self.room.is_active = False
        self.room.save()
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": booking.revision, "end_time": (self.end + timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"/my-bookings/{booking.pk}/edit/", self.html_payload())
        self.assertTrue(response.context["form"].errors)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(booking.end_time, self.end)


class RoomTypeAndCancellationTests(BusinessFixtures, TestCase):
    def test_type_change_resets_only_noncompleted_confirmed(self):
        active = self.create()
        cancelled = self.create(start_time=self.start + timedelta(hours=1), end_time=self.end + timedelta(hours=1))
        operations.cancel(cancelled, revision=cancelled.revision, actor=self.user)
        with patch("django.utils.timezone.now", return_value=self.start - timedelta(days=5)):
            historical = self.create(start_time=self.start - timedelta(days=4), end_time=self.end - timedelta(days=4))
        historical_revision = historical.revision
        self.room.is_vip = True
        self.room.save()
        active.refresh_from_db()
        cancelled.refresh_from_db()
        historical.refresh_from_db()
        self.assertEqual((active.status, active.revision), ("pending", 2))
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual((historical.status, historical.revision), ("confirmed", historical_revision))

    def test_ongoing_meeting_requires_approval_after_type_change(self):
        booking = self.create()
        with patch("django.utils.timezone.now", return_value=self.start + timedelta(minutes=15)):
            self.room.is_vip = True
            self.room.save()
            booking.refresh_from_db()
            self.assertEqual(booking.status, "pending")
            operations.confirm(booking, revision=booking.revision, actor=self.staff)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")

    def test_vip_to_standard_keeps_pending(self):
        self.room.is_vip = True
        self.room.save()
        booking = self.create()
        self.room.is_vip = False
        self.room.save()
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.revision), ("pending", 1))

    def test_room_type_change_via_api_management_and_admin(self):
        self.api.force_authenticate(self.staff)
        self.client.force_login(self.staff)
        for interface in ["api", "management", "admin"]:
            room = Room.objects.create(name=interface)
            booking = self.create(room=room)
            if interface == "api":
                response = self.api.patch(f"/api/rooms/{room.pk}/", {"is_vip": True}, format="json")
                self.assertEqual(response.status_code, 200)
            else:
                url = f"/management/rooms/{room.pk}/edit/" if interface == "management" else f"/admin/rooms/room/{room.pk}/change/"
                response = self.client.post(url, {"name": interface, "capacity": 4, "is_vip": "on", "is_active": "on"})
                self.assertEqual(response.status_code, 302)
            booking.refresh_from_db()
            self.assertEqual(booking.status, "pending")

    def test_type_change_invalidates_revision(self):
        booking = self.create()
        revision = booking.revision
        self.room.is_vip = True
        self.room.save()
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": revision, "end_time": (self.end + timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.end_time, self.end)

    def test_revision_values_and_noop(self):
        booking = self.create()
        for revision in ["", -1, "bad", booking.revision + 1]:
            response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": revision}, format="json")
            self.assertEqual(response.status_code, 400)
        response = self.api.patch(f"/api/bookings/{booking.pk}/", {"revision": booking.revision}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["revision"], booking.revision + 1)

    def test_archived_room_can_be_cancelled_and_does_not_lose_history(self):
        booking = self.create()
        self.room.is_active = False
        self.room.save()
        operations.cancel(booking, revision=booking.revision, actor=self.user)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(Booking.objects.count(), 1)

    def test_unrelated_stale_room_save_preserves_type(self):
        stale = Room.objects.get(pk=self.room.pk)
        self.room.is_vip = True
        self.room.save()
        stale.name = "Renamed"
        stale.save()
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_vip)

    def test_bulk_type_changes_cannot_bypass_reapproval(self):
        with self.assertRaises(TypeError):
            Room.objects.filter(pk=self.room.pk).update(is_vip=True)
        with self.assertRaises(TypeError):
            Room.objects.bulk_update([self.room], ["is_vip"])
        with self.assertRaises(TypeError):
            Room.objects.bulk_create([self.room], update_conflicts=True,
                                     update_fields=["is_vip"], unique_fields=["pk"])

    def test_room_flag_update_fields_do_not_apply_other_changes(self):
        booking = self.create()
        self.room.is_vip = True
        self.room.is_active = False
        self.room.save(update_fields=["is_active"])
        self.room.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(self.room.is_vip)
        self.assertFalse(self.room.is_active)
        self.assertEqual((booking.status, booking.revision), ("confirmed", 1))

    def test_room_type_change_rolls_back_all_records_on_failure(self):
        booking = self.create()
        from django.db import models
        original_save = models.Model.save

        def failing_save(instance, *args, **kwargs):
            if isinstance(instance, Booking):
                raise ValidationError("Simulated write failure")
            return original_save(instance, *args, **kwargs)

        self.room.is_vip = True
        with patch("django.db.models.Model.save", new=failing_save):
            with self.assertRaises(ValidationError):
                self.room.save()
        self.room.refresh_from_db()
        booking.refresh_from_db()
        self.assertFalse(self.room.is_vip)
        self.assertEqual((booking.status, booking.revision), ("confirmed", 1))

    def test_completed_cancel_supported_in_all_interfaces(self):
        for interface in ["html", "api", "management", "admin"]:
            room = Room.objects.create(name=interface)
            booking = self.create(room=room)
            with patch("django.utils.timezone.now", return_value=self.end + timedelta(days=1)):
                if interface == "html":
                    response = self.client.post(f"/cancel/{booking.pk}/", {"revision": booking.revision})
                    self.assertEqual(response.status_code, 302)
                elif interface == "api":
                    self.assertEqual(self.api.post(f"/api/bookings/{booking.pk}/cancel/", {"revision": booking.revision}).status_code, 200)
                else:
                    self.client.force_login(self.staff)
                    if interface == "management":
                        response = self.client.post(f"/management/bookings/{booking.pk}/status/", {"status": "cancelled", "revision": booking.revision})
                    else:
                        response = self.client.post(f"/admin/rooms/booking/{booking.pk}/change/", {
                            "expected_revision": booking.revision, "room": room.pk, "user": self.user.pk, "status": "cancelled",
                            "start_time_0": self.start.date().isoformat(), "start_time_1": "10:00",
                            "end_time_0": self.end.date().isoformat(), "end_time_1": "11:00",
                        })
                    self.assertEqual(response.status_code, 302)
            booking.refresh_from_db()
            self.assertEqual(booking.status, "cancelled")


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL.")
class RoomTypeConcurrencyTests(BusinessFixtures, TransactionTestCase):
    race = PostgreSQLConcurrencyTests.race

    def test_type_change_and_create(self):
        def create():
            operations.create(actor=self.user, room=self.room, start_time=self.start, end_time=self.end)
            return "created"

        def change_type():
            room = Room.objects.get(pk=self.room.pk)
            room.is_vip = True
            room.save()
            return "changed"
        self.assertCountEqual(self.race(create, change_type), ["created", "changed"])
        self.assertEqual(Booking.objects.get().status, "pending")

    def test_type_change_and_edit(self):
        booking = self.create()

        def edit():
            operations.edit(Booking.objects.get(pk=booking.pk), actor=self.user, revision=booking.revision, end_time=self.end + timedelta(hours=1))
            return "edited"

        def change_type():
            room = Room.objects.get(pk=self.room.pk)
            room.is_vip = True
            room.save()
            return "changed"
        self.assertIn("changed", self.race(edit, change_type))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")

    def test_archive_and_create(self):
        def create():
            operations.create(actor=self.user, room=self.room, start_time=self.start, end_time=self.end)
            return "created"

        def archive():
            room = Room.objects.get(pk=self.room.pk)
            room.is_active = False
            room.save()
            return "archived"
        results = self.race(create, archive)
        self.assertIn("archived", results)
        self.assertIn(results[0], ["created", "rejected"])
        self.assertEqual(Booking.objects.count(), 1 if results[0] == "created" else 0)

    def test_type_change_and_cancellation(self):
        booking = self.create()

        def cancel():
            operations.cancel(Booking.objects.get(pk=booking.pk), actor=self.user, revision=booking.revision)
            return "cancelled"

        def change_type():
            room = Room.objects.get(pk=self.room.pk)
            room.is_vip = True
            room.save()
            return "changed"
        results = self.race(cancel, change_type)
        self.assertIn(results, [["cancelled", "changed"], ["rejected", "changed"]])
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled" if results[0] == "cancelled" else "pending")
        self.assertEqual(booking.revision, 2)

    def test_type_change_and_transfer_into_room(self):
        other = Room.objects.create(name="Source room")
        booking = self.create(room=other)

        def transfer():
            operations.edit(Booking.objects.get(pk=booking.pk), actor=self.user, revision=booking.revision, room=self.room)
            return "transferred"

        def change_type():
            room = Room.objects.get(pk=self.room.pk)
            room.is_vip = True
            room.save()
            return "changed"
        self.assertCountEqual(self.race(transfer, change_type), ["transferred", "changed"])
        booking.refresh_from_db()
        self.assertEqual((booking.room_id, booking.status), (self.room.pk, "pending"))
