import csv
from datetime import timedelta
from html import unescape
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.utils import timezone

from .csv_export import safe_csv_cell
from .models import AuditEvent, Booking, BookingCreationRequest, Room


class CSVExportTests(TestCase):
    def setUp(self):
        self.root = get_user_model().objects.create_superuser(username="csv-root", email="", password="test-only")
        self.user = get_user_model().objects.create_user(username="csv-owner", email="owner@example.invalid")
        self.room = Room.objects.create(name="Alpha", is_vip=True)
        self.other_room = Room.objects.create(name="Beta")
        start = (timezone.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.booking = Booking.objects.create(room=self.room, user=self.user, start_time=start, end_time=start + timedelta(hours=1))
        self.other = Booking.objects.create(room=self.other_room, user=self.user, start_time=start, end_time=start + timedelta(hours=1))
        self.client.force_login(self.root)

    def admin_export(self, ids=None, query="", **extra):
        return self.client.post("/admin/rooms/booking/" + query, {
            "action": "export_bookings_csv",
            "_selected_action": ids if ids is not None else [self.booking.pk], **extra,
        })

    def rows(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(response.content.count(b"\xef\xbb\xbf"), 1)
        return list(csv.reader(StringIO(response.content.decode("utf-8-sig"), newline=""), delimiter=";"))

    def ids(self, response):
        return {int(row[0]) for row in self.rows(response)[1:]}

    def assert_text_roundtrip(self, value, expected):
        Room.objects.filter(pk=self.room.pk).update(name=value)
        get_user_model().objects.filter(pk=self.user.pk).update(username=value, email=value)
        for response in [self.admin_export(), self.client.get("/management/export.csv", {"status": "pending"})]:
            with self.subTest(interface=response.wsgi_request.path, value=repr(value)):
                row = self.rows(response)[1]
                self.assertEqual(row[1:4], [expected] * 3)
        self.assertEqual(Room.objects.get(pk=self.room.pk).name, value)
        current = get_user_model().objects.get(pk=self.user.pk)
        self.assertEqual((current.username, current.email), (value, value))

    def test_equal_formula(self):
        self.assert_text_roundtrip("=1+1", "'=1+1")

    def test_plus_formula(self):
        self.assert_text_roundtrip("+1+1", "'+1+1")

    def test_minus_formula(self):
        self.assert_text_roundtrip("-1+1", "'-1+1")

    def test_at_formula(self):
        self.assert_text_roundtrip("@SUM(1)", "'@SUM(1)")

    def test_whitespace_and_control_prefixes(self):
        for value in ["\t=1+1", "\n=1+1", "\r=1+1", "  =1+1", " \t\r\n+1+1", "\u00a0@SUM(1)", "\ttext"]:
            with self.subTest(value=repr(value)):
                self.assert_text_roundtrip(value, "'" + value)

    def test_bom_and_control_characters_cannot_hide_formula(self):
        for value in ["\ufeff=1", " \ufeff \t-1", "\x00=1"]:
            self.assertEqual(safe_csv_cell(value), "'" + value)

    def test_quotes_delimiters_and_multiline_russian(self):
        value = 'Зал "Альфа"; первый этаж\nвторая строка\r\nтретья'
        self.assert_text_roundtrip(value, value)

    def test_empty_and_regular_text(self):
        for value in ["", "Обычная переговорная", "  Обычный текст", "user@example.invalid", "00123"]:
            self.assert_text_roundtrip(value, value)

    def test_bom_headers_dates_and_existing_column_contract(self):
        admin_rows = self.rows(self.admin_export([self.booking.pk, self.other.pk]))
        management_rows = self.rows(self.client.get("/management/export.csv"))
        expected = ["ID", "Переговорная", "Пользователь", "Email", "Начало", "Окончание", "Статус"]
        self.assertEqual(admin_rows[0], expected + ["Создано"])
        self.assertEqual(management_rows[0], expected)
        self.assertEqual(len(admin_rows), 3)
        self.assertEqual(len(management_rows), 3)
        self.assertEqual(admin_rows[1][4], timezone.localtime(self.booking.start_time).strftime("%d.%m.%Y %H:%M"))
        self.assertEqual(admin_rows[1][6], "Ожидает подтверждения")

    def test_management_search_and_status_match_list(self):
        for params in [{"q": "Alpha"}, {"q": "csv-owner", "status": "pending"},
                       {"q": "owner@example.invalid", "status": "confirmed"},
                       {"q": "  Beta  ", "status": "invalid"}, {"q": "no-match"}]:
            with self.subTest(params=params):
                response = self.client.get("/management/bookings/", params)
                expected = {item.pk for item in response.context["items"]}
                self.assertEqual(self.ids(self.client.get("/management/export.csv", params)), expected)

    def test_management_link_preserves_urlencoded_search(self):
        query = "Альфа & + ?"
        response = self.client.get("/management/bookings/", {"q": query, "status": "pending"})
        # Inspect the rendered URL, not a duplicate of the filtering implementation.
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    links.append(dict(attrs).get("href", ""))
        links = []
        Links().feed(response.content.decode())
        from urllib.parse import parse_qs, urlsplit
        export_links = [url for url in links if url.startswith("/management/export.csv?")]
        self.assertEqual(len(export_links), 1)
        self.assertEqual(parse_qs(urlsplit(unescape(export_links[0])).query), {"q": [query], "status": ["pending"]})

    def test_admin_exports_only_selected_records(self):
        self.assertEqual(self.ids(self.admin_export([self.other.pk])), {self.other.pk})

    def test_admin_selection_intersects_search_and_filters(self):
        for query, expected in [("?q=Alpha", {self.booking.pk}), ("?status__exact=confirmed", {self.other.pk}),
                                ("?room__id__exact=" + str(self.other_room.pk), {self.other.pk})]:
            self.assertEqual(self.ids(self.admin_export([self.booking.pk, self.other.pk], query)), expected)

    def test_admin_missing_selection_never_exports_everything(self):
        response = self.admin_export([])
        self.assertNotIn("text/csv", response["Content-Type"])

    def test_admin_select_across_cannot_expand_export(self):
        for ids in [[], [self.booking.pk]]:
            response = self.admin_export(ids, select_across="1")
            self.assertNotIn("text/csv", response["Content-Type"])
            self.assertEqual(Booking.objects.count(), 2)

    def test_export_permission_is_independent_of_view_and_change(self):
        staff = get_user_model().objects.create_user(username="csv-staff", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_booking", "change_booking"]))
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/management/export.csv").status_code, 403)
        self.assertNotIn("text/csv", self.admin_export()["Content-Type"])
        staff.user_permissions.add(Permission.objects.get(codename="export_booking"))
        self.assertEqual(self.ids(self.client.get("/management/export.csv")), {self.booking.pk, self.other.pk})
        self.assertEqual(self.ids(self.admin_export()), {self.booking.pk})

    def test_export_only_permission_does_not_grant_admin_list_access(self):
        staff = get_user_model().objects.create_user(username="csv-export-only", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="export_booking"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/management/export.csv").status_code, 200)
        self.assertEqual(self.admin_export().status_code, 403)

    def test_anonymous_and_nonstaff_cannot_export(self):
        self.client.logout()
        self.assertEqual(self.client.get("/management/export.csv").status_code, 302)
        self.assertEqual(self.admin_export().status_code, 302)
        self.user.user_permissions.add(Permission.objects.get(codename="export_booking"))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/management/export.csv").status_code, 302)
        self.assertEqual(self.admin_export().status_code, 302)

    def test_direct_management_post_cannot_bypass_permission_or_export_all(self):
        self.assertEqual(self.client.post("/management/export.csv", {"_selected_action": self.booking.pk}).status_code, 405)
        staff = get_user_model().objects.create_user(username="csv-no-export", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post("/management/export.csv").status_code, 403)

    def test_admin_direct_post_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post("/admin/rooms/booking/", {
            "action": "export_bookings_csv", "_selected_action": [self.booking.pk],
        }).status_code, 403)

    def test_exports_have_no_business_side_effects(self):
        before = (list(Booking.objects.values()), AuditEvent.objects.count(), BookingCreationRequest.objects.count())
        self.admin_export()
        self.client.get("/management/export.csv")
        self.assertEqual((list(Booking.objects.values()), AuditEvent.objects.count(), BookingCreationRequest.objects.count()), before)
