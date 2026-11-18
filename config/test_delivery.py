from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError

from rooms.models import Room, Booking


class DeliveryTests(TestCase):
    def test_metrics_require_token_and_hide_request_data(self):
        self.assertEqual(self.client.get("/metrics/").status_code, 404)
        with override_settings(METRICS_TOKEN="test-token"):
            self.client.get("/no-such-route/private-user/?secret=do-not-record")
            response = self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer test-token")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mrb_http_requests_total")
        self.assertContains(response, "mrb_database_up 1")
        self.assertNotContains(response, "private-user")
        self.assertNotContains(response, "do-not-record")

    @override_settings(METRICS_TOKEN="test-token")
    def test_cache_failure_is_exported(self):
        with patch("config.metrics.cache.get", side_effect=RedisConnectionError()):
            response = self.client.get("/metrics/", HTTP_AUTHORIZATION="Bearer test-token")
        self.assertContains(response, "mrb_cache_up 0")

    def test_openapi_exposes_required_concurrency_contracts(self):
        spec = self.client.get("/api/schema/").json()
        self.assertEqual(spec["openapi"], "3.0.3")
        create = spec["paths"]["/api/bookings/"]["post"]
        self.assertEqual(create["parameters"][0]["name"], "Idempotency-Key")
        self.assertTrue(create["parameters"][0]["required"])
        self.assertIn("409", create["responses"])
        self.assertIn("revision", spec["components"]["schemas"]["BookingPatch"]["required"])
        self.assertNotIn("delete", spec["paths"]["/api/bookings/{id}/"])
        self.assertContains(self.client.get("/api/docs/"), "OpenAPI")

    def test_container_settings_do_not_mutate_host_settings(self):
        from config import test_container_settings, test_postgres_settings

        self.assertEqual(test_container_settings.DATABASES["default"]["HOST"], "db")
        self.assertEqual(test_postgres_settings.DATABASES["default"]["HOST"], "127.0.0.1")


class CSVDateRoomTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="date-room-admin", password="test"
        )
        self.room = Room.objects.create(name="Filter room")
        self.other = Room.objects.create(name="Other room")
        self.start = (timezone.now() + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        self.booking = Booking.objects.create(
            user=self.user,
            room=self.room,
            start_time=self.start,
            end_time=self.start + timedelta(hours=1),
        )
        Booking.objects.create(
            user=self.user,
            room=self.other,
            start_time=self.start,
            end_time=self.start + timedelta(hours=1),
        )
        self.client.force_login(self.user)

    def test_room_and_inclusive_dates_match_list_and_csv(self):
        params = {
            "room": self.room.pk,
            "date_from": self.start.date().isoformat(),
            "date_to": self.start.date().isoformat(),
        }
        listed = self.client.get("/management/bookings/", params)
        self.assertEqual([b.pk for b in listed.context["items"]], [self.booking.pk])
        exported = self.client.get("/management/export.csv", params)
        self.assertContains(exported, "Filter room")
        self.assertNotContains(exported, "Other room")
        self.assertContains(listed, 'name="date_from"')
        self.assertContains(listed, "date_to=")

    def test_invalid_filters_reject_instead_of_exporting_all(self):
        for params in [
            {"room": "bad"},
            {"room": "9" * 30},
            {"date_from": "2026-02-30"},
            {"date_to": "9999-12-31"},
            {"date_from": "2026-11-20", "date_to": "2026-11-19"},
        ]:
            for path in ["/management/bookings/", "/management/export.csv"]:
                with self.subTest(params=params, path=path):
                    self.assertEqual(self.client.get(path, params).status_code, 400)

    def test_admin_selected_records_respect_explicit_date_range(self):
        date = self.start.date().isoformat()
        response = self.client.post(
            f"/admin/rooms/booking/?room__id__exact={self.room.pk}&start_time__gte={date}&start_time__lt={(self.start + timedelta(days=1)).date().isoformat()}",
            {"action": "export_bookings_csv", "_selected_action": [self.booking.pk]},
        )
        self.assertContains(response, "Filter room")
        self.assertNotContains(response, "Other room")
