from datetime import timedelta
from io import StringIO
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rooms.models import Role, Room, Booking
from rooms.management.commands.setup_roles import ROLE_PERMISSIONS


class RoleTests(TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=StringIO())
        self.member = get_user_model().objects.create_user(username="member")
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.room = Room.objects.create(name="VIP", is_vip=True)
        start = (timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.booking = Booking.objects.create(room=self.room, user=self.member, start_time=start, end_time=start + timedelta(hours=1))
        self.api = APIClient()

    def test_role_model_is_proxy_of_django_group(self):
        role = Role.objects.get(name="Booking Viewers")
        self.assertTrue(Role._meta.proxy)
        self.assertIs(Role._meta.concrete_model, Group)
        self.assertEqual(Role._meta.db_table, Group._meta.db_table)
        self.assertEqual(role.pk, Group.objects.get(name=role.name).pk)

    def role(self, name=None):
        if name:
            self.staff.groups.add(Group.objects.get(name=name))
        self.client.force_login(self.staff)
        self.api.force_authenticate(self.staff)

    def action(self, name):
        return self.client.post("/admin/rooms/booking/", {"action": name, "_selected_action": [self.booking.pk], f"revision_{self.booking.pk}": self.booking.revision})

    def test_anonymous_and_member(self):
        self.assertEqual(self.client.get("/management/").status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "cancelled", "revision": self.booking.revision}).status_code, 302)
        self.booking.user = self.staff
        self.booking.save(revision=self.booking.revision)
        self.api.force_authenticate(self.member)
        for method, suffix in [("get", ""), ("patch", ""), ("post", "cancel/")]:
            self.assertEqual(getattr(self.api, method)(f"/api/bookings/{self.booking.pk}/{suffix}").status_code, 404)

    def test_staff_without_permissions(self):
        self.role()
        for url in ["/management/", "/management/bookings/", "/management/rooms/", "/management/users/", "/management/analytics/", "/management/export.csv"]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision}).status_code, 403)

    def test_viewer_actions_denied(self):
        self.role("Booking Viewers")
        for url in ["/management/bookings/", "/admin/rooms/booking/"]:
            self.assertEqual(self.client.get(url).status_code, 200)
        for action in ["confirm_bookings", "reject_bookings", "export_bookings_csv"]:
            self.assertNotIn("text/csv", self.action(action).get("Content-Type", ""))
            self.booking.refresh_from_db()
            self.assertEqual(self.booking.status, "pending")
        self.assertEqual(self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "confirmed", "revision": self.booking.revision}).status_code, 403)
        self.assertEqual(self.client.get("/management/export.csv").status_code, 403)

    def test_booking_manager_scope(self):
        self.role("Booking Managers")
        self.assertEqual(self.client.get("/management/analytics/").status_code, 200)
        self.action("confirm_bookings")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")
        self.assertEqual(self.client.post(f"/management/bookings/{self.booking.pk}/status/", {"status": "cancelled", "revision": self.booking.revision}).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "cancelled")
        self.assertEqual(self.client.get("/management/users/").status_code, 403)
        self.assertEqual(self.api.post("/api/rooms/", {"name": "blocked"}).status_code, 403)

    def test_room_manager_scope(self):
        self.role("Room Managers")
        r = self.api.post("/api/rooms/", {"name": "created", "capacity": 3}, format="json")
        self.assertEqual(r.status_code, 201)
        pk = r.data["id"]
        self.assertEqual(self.api.delete(f"/api/rooms/{pk}/").status_code, 204)
        self.assertEqual(self.api.get(f"/api/rooms/{pk}/?include_archived=true").status_code, 200)
        self.assertEqual(self.api.patch(f"/api/rooms/{pk}/", {"is_active": True}, format="json").status_code, 200)
        self.assertEqual(self.client.get("/management/bookings/").status_code, 403)
        self.assertEqual(self.client.get("/management/users/").status_code, 403)

    def test_room_writes_denied(self):
        self.role("Booking Viewers")
        for method, url in [("post", "/api/rooms/"), ("patch", f"/api/rooms/{self.room.pk}/"), ("delete", f"/api/rooms/{self.room.pk}/")]:
            self.assertEqual(getattr(self.api, method)(url, {"name": "blocked"}, format="json").status_code, 403)
        for url in ["/management/rooms/add/", f"/management/rooms/{self.room.pk}/edit/", f"/management/rooms/{self.room.pk}/toggle/"]:
            self.assertEqual(self.client.post(url, {"name": "blocked"}).status_code, 403)
        self.room.is_active = False
        self.room.save()
        self.assertEqual(self.api.patch(f"/api/rooms/{self.room.pk}/", {"is_active": True}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/management/rooms/{self.room.pk}/toggle/").status_code, 403)
        self.assertEqual(self.client.post("/admin/rooms/room/add/", {"name": "blocked"}).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/rooms/room/{self.room.pk}/change/", {}).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/rooms/room/{self.room.pk}/delete/", {"post": "yes"}).status_code, 403)

    def test_user_manager_cannot_escalate(self):
        self.role("User Managers")
        self.assertEqual(self.client.get("/management/users/").status_code, 200)
        for url in [f"/management/users/{self.staff.pk}/edit/", f"/admin/auth/user/{self.staff.pk}/change/"]:
            self.assertEqual(self.client.post(url, {"is_superuser": "on"}).status_code, 403)
        payload = {"username": self.member.username, "first_name": "Edited", "last_name": "", "email": "", "is_active": "on", "is_staff": "on", "is_superuser": "on", "groups": [Group.objects.get(name="Operations Administrators").pk]}
        for url in [f"/management/users/{self.member.pk}/edit/", f"/admin/auth/user/{self.member.pk}/change/"]:
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            self.member.refresh_from_db()
            self.assertEqual(self.member.first_name, "Edited")
            self.assertFalse(self.member.is_staff)
            self.assertFalse(self.member.is_superuser)
            self.assertFalse(self.member.groups.exists())
        self.assertEqual(self.client.post(f"/admin/auth/user/{self.member.pk}/password/", {}).status_code, 403)
        self.assertEqual(self.client.get("/management/bookings/").status_code, 403)

    def test_operations_and_superuser(self):
        self.role("Operations Administrators")
        for url in ["/management/", "/management/bookings/", "/management/rooms/", "/management/users/", "/management/analytics/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertFalse(self.staff.is_superuser)
        self.assertEqual(self.client.get("/management/export.csv").status_code, 403)
        self.staff.is_superuser = True
        self.staff.save()
        self.assertEqual(self.client.get("/management/export.csv").status_code, 200)
        self.assertIn("text/csv", self.action("export_bookings_csv")["Content-Type"])

    def test_explicit_export_permission(self):
        self.role("Booking Viewers")
        self.staff.user_permissions.add(Permission.objects.get(codename="export_booking"))
        self.assertEqual(self.client.get("/management/export.csv").status_code, 200)
        self.assertIn("text/csv", self.action("export_bookings_csv")["Content-Type"])

    def test_nonstaff_permissions_insufficient(self):
        self.role("Operations Administrators")
        self.staff.is_staff = False
        self.staff.save()
        self.api.force_authenticate(self.staff)
        self.assertEqual(self.client.get("/management/rooms/").status_code, 302)
        self.assertEqual(self.client.get("/admin/rooms/room/").status_code, 302)
        self.assertEqual(self.api.post("/api/rooms/", {"name": "blocked"}).status_code, 403)
        self.assertEqual(self.api.get("/api/rooms/?include_archived=true").status_code, 403)

    def test_setup_preserves_existing_settings(self):
        group = Group.objects.get(name="Booking Viewers")
        extra = Permission.objects.get(codename="export_booking")
        group.permissions.add(extra)
        group.user_set.add(self.staff)
        call_command("setup_roles", stdout=StringIO())
        self.assertEqual(Group.objects.filter(name__in=ROLE_PERMISSIONS).count(), 5)
        self.assertTrue(group.permissions.filter(pk=extra.pk).exists())
        self.assertTrue(group.user_set.filter(pk=self.staff.pk).exists())
        for name, expected in ROLE_PERMISSIONS.items():
            actual = {f"{p.content_type.app_label}.{p.codename}" for p in Group.objects.get(name=name).permissions.select_related("content_type")}
            self.assertTrue(expected <= actual)

    def test_view_booking_alone_blocks_actions(self):
        self.role()
        self.staff.user_permissions.add(Permission.objects.get(codename="view_booking"))
        self.assertEqual(self.client.get("/admin/rooms/booking/").status_code, 200)
        for action in ["confirm_bookings", "reject_bookings", "export_bookings_csv"]:
            response = self.action(action)
            self.assertNotIn("text/csv", response.get("Content-Type", ""))
            self.booking.refresh_from_db()
            self.assertEqual(self.booking.status, "pending")

    def test_user_manager_cannot_edit_other_staff_or_superuser(self):
        self.role("User Managers")
        for index, superuser in enumerate([False, True]):
            target = get_user_model().objects.create_user(username=f"protected{index}", is_staff=True, is_superuser=superuser)
            for url in [f"/management/users/{target.pk}/edit/", f"/admin/auth/user/{target.pk}/change/"]:
                self.assertEqual(self.client.post(url, {"is_active": ""}).status_code, 403)
            target.refresh_from_db()
            self.assertTrue(target.is_active)

    def test_room_manager_html_operations(self):
        self.role("Room Managers")
        self.assertEqual(self.client.post("/management/rooms/add/", {"name": "html", "capacity": 5, "is_active": "on"}).status_code, 302)
        room = Room.objects.get(name="html")
        self.assertEqual(self.client.post(f"/management/rooms/{room.pk}/edit/", {"name": "updated", "capacity": 6, "is_active": "on"}).status_code, 302)
        for expected in [False, True]:
            self.assertEqual(self.client.post(f"/management/rooms/{room.pk}/toggle/", {"is_active": str(expected).lower()}).status_code, 302)
            room.refresh_from_db()
            self.assertEqual(room.is_active, expected)
        self.assertEqual(self.client.post(f"/admin/rooms/room/{room.pk}/delete/", {"post": "yes"}).status_code, 403)

    def test_permission_groups_not_assigned_to_users(self):
        self.assertFalse(self.member.groups.exists())
        self.assertFalse(self.staff.groups.exists())
        self.assertFalse(self.staff.is_superuser)
