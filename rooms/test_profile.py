from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.db import DatabaseError, connection, connections, models, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from .account_operations import ProfileUnavailable, save_account, save_profile, set_active
from .forms import ProfileForm
from .models import AuditEvent


class ProfileFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="profile-user", password="original-password", first_name="Before",
            last_name="Original", email="before@example.invalid", is_staff=True, is_superuser=True,
        )
        self.root = get_user_model().objects.create_superuser(
            username="profile-admin", email="", password="admin-password",
        )
        self.payload = {"first_name": "After", "last_name": "Updated", "email": "after@example.invalid"}
        self.client.force_login(self.user)

    def row(self):
        return get_user_model().objects.values().get(pk=self.user.pk)

    def form(self):
        form = ProfileForm(data=self.payload, instance=get_user_model().objects.get(pk=self.user.pk))
        self.assertTrue(form.is_valid(), form.errors)
        return form

    def assert_profile(self, expected):
        actual = self.row()
        self.assertEqual({name: actual[name] for name in self.payload}, expected)

    def assert_session_ended(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/login/?next=/profile/")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertTrue(any("Сеанс" in str(message) or "сеанс" in str(message)
                            for message in get_messages(response.wsgi_request)))


class ProfileTests(ProfileFixtures, TestCase):
    def test_http_updates_only_profile_fields_and_preserves_permissions(self):
        group = Group.objects.create(name="profile-existing-role")
        permission = Permission.objects.get(codename="view_auditevent")
        self.user.groups.add(group)
        self.user.user_permissions.add(permission)
        before = self.row()
        response = self.client.post("/profile/", {
            **self.payload, "is_staff": "false", "is_superuser": "false", "is_active": "false",
            "password": "forged", "username": "forged", "id": self.root.pk,
            "groups": "", "user_permissions": "",
        })
        self.assertRedirects(response, "/profile/", fetch_redirect_response=False)
        self.assertEqual(self.row(), {**before, **self.payload})
        self.assertEqual(list(self.user.groups.all()), [group])
        self.assertEqual(list(self.user.user_permissions.all()), [permission])
        self.assertFalse(AuditEvent.objects.exists())  # Profile PII is not an activity event.

    def test_stale_form_does_not_restore_staff(self):
        form = self.form()
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=False)
        form.save()
        self.assertFalse(self.row()["is_staff"])
        self.assert_profile(self.payload)

    def test_stale_form_does_not_restore_superuser(self):
        form = self.form()
        get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=False)
        form.save()
        self.assertFalse(self.row()["is_superuser"])

    def test_stale_form_cannot_restore_activity_or_change_inactive_profile(self):
        form = self.form()
        set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root)
        before = self.row()
        with self.assertRaises(ProfileUnavailable):
            form.save()
        self.assertEqual(self.row(), before)
        self.assertEqual(AuditEvent.objects.get().action, "user.deactivated")

    def test_stale_form_does_not_restore_old_password(self):
        form = self.form()
        current = get_user_model().objects.get(pk=self.user.pk)
        current.set_password("replacement-password")
        current.save(update_fields=["password"])
        form.save()
        current.refresh_from_db()
        self.assertTrue(current.check_password("replacement-password"))
        self.assertFalse(current.check_password("original-password"))

    def test_stale_form_preserves_other_concurrently_changed_fields(self):
        form = self.form()
        now = timezone.now()
        get_user_model().objects.filter(pk=self.user.pk).update(
            username="renamed-profile-user", last_login=now, date_joined=now,
        )
        before = self.row()
        form.save()
        self.assertEqual(self.row(), {**before, **self.payload})

    def test_inactive_session_get_is_flushed_and_cannot_resume_after_reactivation(self):
        set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root)
        self.assert_session_ended(self.client.get("/profile/"))
        set_active(get_user_model().objects.get(pk=self.user.pk), True, actor=self.root)
        self.assertRedirects(self.client.get("/profile/"), "/login/?next=/profile/",
                             fetch_redirect_response=False)

    def test_inactive_session_post_is_flushed_without_saving(self):
        set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root)
        before = self.row()
        self.assert_session_ended(self.client.post("/profile/", self.payload))
        self.assertEqual(self.row(), before)

    def test_deactivation_after_authentication_ends_session_without_500(self):
        def deactivate_then_save(**kwargs):
            set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root)
            return save_profile(**kwargs)

        with patch("rooms.forms.save_profile", side_effect=deactivate_then_save):
            response = self.client.post("/profile/", self.payload)
        self.assert_session_ended(response)
        self.assertFalse(self.row()["is_active"])
        self.assertEqual(self.row()["first_name"], "Before")
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_failed_save_rolls_back_all_profile_fields(self):
        form = self.form()
        before = self.row()
        original = models.Model.save

        def fail_after_write(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            if isinstance(instance, get_user_model()):
                raise RuntimeError("injected failure after profile UPDATE")
            return result

        with patch.object(models.Model, "save", fail_after_write):
            with self.assertRaises(RuntimeError):
                form.save()
        self.assertEqual(self.row(), before)
        self.assertFalse(AuditEvent.objects.exists())

    def test_invalid_form_does_not_write(self):
        before = self.row()
        response = self.client.post("/profile/", {**self.payload, "email": "not-an-email"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(self.row(), before)

    def test_commit_false_does_not_write(self):
        before = self.row()
        self.form().save(commit=False)
        self.assertEqual(self.row(), before)

    def test_profile_service_cannot_accept_administrative_fields(self):
        before = self.row()
        with self.assertRaises(TypeError):
            save_profile(actor=self.user, **self.payload, is_staff=False)
        self.assertEqual(self.row(), before)

    def test_service_rejects_unsaved_account(self):
        with self.assertRaises(ProfileUnavailable):
            save_profile(actor=get_user_model()(username="unsaved"), **self.payload)

    def test_anonymous_profile_requires_login(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            self.assertRedirects(method("/profile/"), "/login/?next=/profile/",
                                 fetch_redirect_response=False)

    def test_profile_post_still_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        before = self.row()
        self.assertEqual(client.post("/profile/", self.payload).status_code, 403)
        self.assertEqual(self.row(), before)

    def test_registration_login_and_regular_user_profile(self):
        self.client.logout()
        password = "Registration-example-only-739!"
        response = self.client.post("/register/", {
            "username": "new-profile-user", "email": "new@example.invalid",
            "password1": password, "password2": password,
        })
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username="new-profile-user")
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(self.client.post("/profile/", self.payload).status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.first_name, self.payload["first_name"])
        self.client.post("/logout/")
        self.assertEqual(self.client.post("/login/", {"username": user.username, "password": password}).status_code, 302)
        self.assertEqual(self.client.get("/profile/").status_code, 200)


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locks and activity trigger.")
class PostgreSQLProfileTests(ProfileFixtures, TransactionTestCase):
    def run_connected(self, job):
        connections.close_all()
        try:
            return job()
        finally:
            connections.close_all()

    def assert_blocked(self, pid):
        deadline = monotonic() + 3
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                if cursor.fetchone()[0]:
                    return
            Event().wait(0.01)
        self.fail("Expected a real PostgreSQL row lock wait")

    def profile_after_locked_change(self, change):
        ready, state = Event(), {}
        original = ProfileForm.save

        def observed_save(form, *args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                state["pid"] = cursor.fetchone()[0]
            ready.set()
            return original(form, *args, **kwargs)

        with patch.object(ProfileForm, "save", observed_save):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    change()
                    future = pool.submit(self.run_connected, lambda: self.client.post("/profile/", self.payload))
                    self.assertTrue(ready.wait(5), "Profile never reached save after authentication")
                    self.assert_blocked(state["pid"])
                return future.result(timeout=10)

    def test_concurrent_role_revocation_cannot_be_undone(self):
        def revoke():
            user = get_user_model().objects.get(pk=self.user.pk)
            user.is_superuser = False
            save_account(user, actor=self.root, fields=["is_superuser"])
            user.is_staff = False
            save_account(user, actor=self.root, fields=["is_staff"])

        response = self.profile_after_locked_change(revoke)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.row()["is_staff"] or self.row()["is_superuser"])
        self.assert_profile(self.payload)

    def test_concurrent_password_change_is_preserved(self):
        def change_password():
            user = get_user_model().objects.get(pk=self.user.pk)
            user.set_password("concurrent-replacement-password")
            user.save(update_fields=["password"])

        response = self.profile_after_locked_change(change_password)
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password("concurrent-replacement-password"))
        self.assertFalse(user.check_password("original-password"))
        self.assert_profile(self.payload)

    def test_concurrent_deactivation_first_rejects_profile_and_ends_session(self):
        response = self.profile_after_locked_change(
            lambda: set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root),
        )
        self.assert_session_ended(response)
        self.assertFalse(self.row()["is_active"])
        self.assertEqual(self.row()["first_name"], "Before")
        self.assertEqual(AuditEvent.objects.get().action, "user.deactivated")

    def test_profile_first_then_deactivation_preserves_both_operations(self):
        saved, release, deactivation_started = Event(), Event(), Event()
        state = {}
        original = models.Model.save

        def hold_profile_transaction(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            if isinstance(instance, get_user_model()) and set(kwargs.get("update_fields") or ()) == set(self.payload):
                saved.set()
                if not release.wait(5):
                    raise RuntimeError("Profile transaction was not released")
            return result

        def deactivate():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                state["pid"] = cursor.fetchone()[0]
            deactivation_started.set()
            set_active(get_user_model().objects.get(pk=self.user.pk), False, actor=self.root)

        with patch.object(models.Model, "save", hold_profile_transaction):
            with ThreadPoolExecutor(max_workers=2) as pool:
                profile = pool.submit(self.run_connected, lambda: self.client.post("/profile/", self.payload))
                try:
                    self.assertTrue(saved.wait(5))
                    deactivation = pool.submit(self.run_connected, deactivate)
                    self.assertTrue(deactivation_started.wait(5))
                    self.assert_blocked(state["pid"])
                finally:
                    release.set()
                self.assertEqual(profile.result(timeout=10).status_code, 302)
                deactivation.result(timeout=10)
        self.assertFalse(self.row()["is_active"])
        self.assert_profile(self.payload)
        self.assertEqual(AuditEvent.objects.get().action, "user.deactivated")
        self.assert_session_ended(self.client.get("/profile/"))

    def test_profile_does_not_grant_an_activity_trigger_permit(self):
        with transaction.atomic():
            self.form().save()
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertTrue(self.row()["is_active"])
        self.assert_profile(self.payload)
