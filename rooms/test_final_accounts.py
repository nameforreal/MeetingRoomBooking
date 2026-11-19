"""Final account regressions, including real PostgreSQL lock waits."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import SESSION_KEY, get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, connections, models, transaction
from django.test import Client, TestCase, TransactionTestCase

from . import account_operations, audit
from .admin import ControlledAdminPasswordChangeForm, RestrictedUserAdmin
from .models import AuditEvent


class AccountFixtures:
    def setUp(self):
        self.root = get_user_model().objects.create_superuser(
            "final-root", "", "Original-root-827!"
        )
        self.target = get_user_model().objects.create_user(
            "final-target", password="Original-target-827!", is_staff=True
        )
        self.client.force_login(self.root)
        self.password = "Replacement-password-634!"
        self.payload = {
            "password1": self.password,
            "password2": self.password,
            "usable_password": "true",
        }
        self.url = f"/admin/auth/user/{self.target.pk}/password/"

    def edit_data(self, user):
        data = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "date_joined_0": user.date_joined.date().isoformat(),
            "date_joined_1": user.date_joined.time().isoformat(),
        }
        data.update(
            {
                field: "on"
                for field in ("is_staff", "is_superuser", "is_active")
                if getattr(user, field)
            }
        )
        return data

    def password_saved(self, flag=None):
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(self.password))
        if flag:
            self.assertFalse(getattr(self.target, flag))
        log = LogEntry.objects.get()
        self.assertEqual((log.user_id, log.object_id), (self.root.pk, str(self.target.pk)))
        self.assertNotIn(self.password, log.change_message)


class AdminAccountTests(AccountFixtures, TestCase):
    def test_password_changes_only_password_and_logs_once(self):
        before = get_user_model().objects.values().get(pk=self.target.pk)
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 302)
        self.password_saved()
        after = get_user_model().objects.values().get(pk=self.target.pk)
        self.assertEqual(
            {k: v for k, v in before.items() if k != "password"},
            {k: v for k, v in after.items() if k != "password"},
        )
        self.assertEqual(
            AuditEvent.objects.count(), 0
        )  # Passwords are not business audit payloads.

    def test_password_log_failure_rolls_back_password(self):
        before = self.target.password
        with patch.object(
            RestrictedUserAdmin, "log_change", side_effect=RuntimeError("log unavailable")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, self.payload)
        self.target.refresh_from_db()
        self.assertEqual(self.target.password, before)
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_password_invalid_input_does_not_write(self):
        response = self.client.post(self.url, {**self.payload, "password2": "different"})
        self.assertEqual(response.status_code, 200)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Original-target-827!"))
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_password_late_validation_error_is_handled(self):
        with patch(
            "rooms.admin.account_operations.save_account",
            side_effect=ValidationError("Обновите страницу."),
        ):
            response = self.client.post(self.url, self.payload, follow=True)
        self.assertContains(response, "Обновите страницу.")
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_other_user_session_is_invalidated_root_session_survives(self):
        owner = Client()
        owner.force_login(self.target)
        self.client.post(self.url, self.payload)
        self.assertEqual(owner.get("/profile/").status_code, 302)
        self.assertNotIn(SESSION_KEY, owner.session)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_own_password_keeps_current_session_invalidates_other_session(self):
        other = Client()
        other.force_login(self.root)
        response = self.client.post(f"/admin/auth/user/{self.root.pk}/password/", self.payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(other.get("/profile/").status_code, 302)
        self.assertNotIn(SESSION_KEY, other.session)

    def test_disabling_password_uses_same_controlled_save(self):
        self.assertEqual(
            self.client.post(
                self.url, {"usable_password": "false", "unset-password": "1"}
            ).status_code,
            302,
        )
        self.target.refresh_from_db()
        self.assertFalse(self.target.has_usable_password())
        self.assertTrue(self.target.is_active and self.target.is_staff)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_inactive_target_password_can_change_without_reactivation(self):
        account_operations.set_active(self.target, False, actor=self.root)
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 302)
        self.password_saved("is_active")
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_self_deactivation_and_staff_removal_are_form_errors(self):
        url = f"/admin/auth/user/{self.root.pk}/change/"
        for flag in ("is_active", "is_staff"):
            with self.subTest(flag=flag):
                data = self.edit_data(self.root)
                data.pop(flag)
                response = self.client.post(url, data)
                self.assertContains(
                    response, "Нельзя отключить собственный административный доступ."
                )
                self.root.refresh_from_db()
                self.assertTrue(self.root.is_staff and self.root.is_active)
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_superuser_staff_removal_is_form_error(self):
        self.target.is_superuser = True
        self.target.save(update_fields=["is_superuser"])
        data = self.edit_data(self.target)
        data.pop("is_staff")
        response = self.client.post(f"/admin/auth/user/{self.target.pk}/change/", data)
        self.assertContains(response, "Нельзя снять права сотрудника с суперпользователя.")
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)

    def test_allowed_admin_profile_edit(self):
        data = {**self.edit_data(self.target), "first_name": "Updated"}
        response = self.client.post(f"/admin/auth/user/{self.target.pk}/change/", data)
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "Updated")

    def test_late_account_error_is_handled_and_rolls_back(self):
        original = account_operations.save_account

        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise ValidationError("Конфликт изменения пользователя.")

        data = {**self.edit_data(self.target), "first_name": "Not saved"}
        with patch("rooms.admin.account_operations.save_account", side_effect=failed):
            response = self.client.post(
                f"/admin/auth/user/{self.target.pk}/change/", data, follow=True
            )
        self.assertContains(response, "Конфликт изменения пользователя.")
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "")
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_revoked_superuser_cannot_save_password_with_change_user_permission(self):
        from django.contrib.auth.models import Permission

        self.root.user_permissions.add(Permission.objects.get(codename="change_user"))
        original = ControlledAdminPasswordChangeForm.save

        def revoked(form, *args, **kwargs):
            get_user_model().objects.filter(pk=self.root.pk).update(is_superuser=False)
            return original(form, *args, **kwargs)

        with patch.object(ControlledAdminPasswordChangeForm, "save", revoked):
            self.assertEqual(self.client.post(self.url, self.payload).status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Original-target-827!"))


@skipUnless(
    connection.vendor == "postgresql",
    "Requires independent PostgreSQL connections and real row lock waits.",
)
class AdminAccountConcurrencyTests(AccountFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.owner_session = Client()
        self.owner_session.force_login(self.target)

    def connected(self, job):
        connections.close_all()
        try:
            return job()
        finally:
            connections.close_all()

    def assert_blocked(self, pid):
        deadline = monotonic() + 4
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
                if cursor.fetchone()[0]:
                    return
            Event().wait(0.01)
        self.fail("Expected a real PostgreSQL lock wait")

    def prepare_flag(self, flag):
        if flag == "is_superuser":
            self.target.is_superuser = True
            self.target.save(update_fields=[flag])

    def revoke(self, flag):
        user = get_user_model().objects.get(pk=self.target.pk)
        setattr(user, flag, False)
        with audit.context(source="system", actor=self.root):
            account_operations.save_account(user, actor=self.root, fields=[flag])

    def assert_result(self, flag):
        self.password_saved(flag)
        self.assertEqual(self.owner_session.get("/profile/").status_code, 302)
        self.assertNotIn(SESSION_KEY, self.owner_session.session)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        events = AuditEvent.objects.all()
        self.assertEqual(events.count(), int(flag == "is_active"))
        if flag == "is_active":
            event = events.get()
            self.assertEqual(
                (event.action, event.actor_id, event.before, event.after),
                ("user.deactivated", self.root.pk, {"is_active": True}, {"is_active": False}),
            )
            self.assertNotIn("password", str(event.after))

    def revoke_first(self, flag, *, profile=False):
        self.prepare_flag(flag)
        reached, state = Event(), {}
        original = account_operations.save_account

        def observe(user, **kwargs):
            fields = set(kwargs.get("fields") or ())
            if fields == {"password"} or "first_name" in fields:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    state["pid"] = cursor.fetchone()[0]
                reached.set()
            return original(user, **kwargs)

        url, data = self.url, self.payload
        if profile:
            url = f"/admin/auth/user/{self.target.pk}/change/"
            data = {**self.edit_data(self.target), "first_name": "Concurrent"}
        with patch("rooms.admin.account_operations.save_account", side_effect=observe):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    self.revoke(flag)
                    future = pool.submit(self.connected, lambda: self.client.post(url, data))
                    self.assertTrue(reached.wait(5))
                    self.assert_blocked(state["pid"])
                self.assertEqual(future.result(timeout=10).status_code, 302)
        if profile:
            self.target.refresh_from_db()
            self.assertFalse(getattr(self.target, flag))
            self.assertEqual(self.target.first_name, "Concurrent")
        else:
            self.assert_result(flag)

    def password_first(self, flag):
        self.prepare_flag(flag)
        saved, release, started, state = Event(), Event(), Event(), {}
        original = models.Model.save

        def hold(user, *args, **kwargs):
            result = original(user, *args, **kwargs)
            if isinstance(user, get_user_model()) and set(kwargs.get("update_fields") or ()) == {
                "password"
            }:
                saved.set()
                if not release.wait(8):
                    raise RuntimeError("Password transaction not released")
            return result

        def revoke():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                state["pid"] = cursor.fetchone()[0]
            started.set()
            self.revoke(flag)

        with patch.object(models.Model, "save", hold):
            with ThreadPoolExecutor(max_workers=2) as pool:
                password = pool.submit(
                    self.connected, lambda: self.client.post(self.url, self.payload)
                )
                try:
                    self.assertTrue(saved.wait(5))
                    revocation = pool.submit(self.connected, revoke)
                    self.assertTrue(started.wait(5))
                    self.assert_blocked(state["pid"])
                finally:
                    release.set()
                self.assertEqual(password.result(timeout=10).status_code, 302)
                revocation.result(timeout=10)
        self.assert_result(flag)

    def test_staff_revocation_first(self):
        self.revoke_first("is_staff")

    def test_staff_revocation_after_password(self):
        self.password_first("is_staff")

    def test_superuser_revocation_first(self):
        self.revoke_first("is_superuser")

    def test_superuser_revocation_after_password(self):
        self.password_first("is_superuser")

    def test_deactivation_first(self):
        self.revoke_first("is_active")

    def test_deactivation_after_password(self):
        self.password_first("is_active")

    def test_admin_edit_preserves_concurrent_revocation(self):
        self.revoke_first("is_staff", profile=True)

    def test_admin_staff_removal_rechecks_concurrent_superuser_promotion(self):
        reached, state = Event(), {}
        original = account_operations.save_account
        data = {**self.edit_data(self.target), "first_name": "Not saved"}
        data.pop("is_staff")

        def observe(user, **kwargs):
            if "first_name" in kwargs.get("fields", ()):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    state["pid"] = cursor.fetchone()[0]
                reached.set()
            return original(user, **kwargs)

        with patch("rooms.admin.account_operations.save_account", side_effect=observe):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    get_user_model().objects.filter(pk=self.target.pk).update(is_superuser=True)
                    future = pool.submit(
                        self.connected,
                        lambda: self.client.post(
                            f"/admin/auth/user/{self.target.pk}/change/", data
                        ),
                    )
                    self.assertTrue(reached.wait(5))
                    self.assert_blocked(state["pid"])
                response = future.result(timeout=10)
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_superuser and self.target.is_staff)
        self.assertEqual(self.target.first_name, "")
        self.assertEqual(LogEntry.objects.count(), 0)
