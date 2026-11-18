import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from redis.exceptions import ConnectionError as RedisConnectionError


class HealthTests(TestCase):
    def test_healthy_dependencies(self):
        response = self.client.get("/health/")
        self.assertEqual(response.json(), {"status": "ok", "cache": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_redis_failure_is_not_database_failure(self):
        with patch("config.health.cache.set", side_effect=RedisConnectionError("private endpoint")):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "cache": "unavailable"})
        self.assertNotContains(response, "private endpoint")

    def test_database_failure_is_not_ready(self):
        with patch("config.health.connection.cursor", side_effect=OperationalError("private DB")):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_post_does_not_change_state(self):
        self.assertEqual(self.client.post("/health/").status_code, 405)


class EnvironmentTests(SimpleTestCase):
    def settings(self, **extra):
        env = {"DJANGO_READ_DOT_ENV": "false", "DJANGO_SECRET_KEY": "test-only-" * 8, **extra}
        # Fail if runtime settings attempt to read any local file (.env included).
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(Path, "read_text", side_effect=AssertionError("File read forbidden")),
        ):
            return runpy.run_path(str(Path(__file__).with_name("settings.py")))

    def test_dotenv_is_not_read_when_disabled(self):
        self.assertFalse(self.settings()["DEBUG"])

    def test_legacy_database_defaults_are_preserved(self):
        settings = self.settings(DB_ENGINE="postgres", POSTGRES_PASSWORD="test-only")
        db = settings["DATABASES"]["default"]
        self.assertEqual(
            (db["HOST"], db["PORT"], db["NAME"], db["USER"]),
            ("127.0.0.1", "5432", "meetingroom", "meetingroom"),
        )

    def test_container_database_and_cache_environment(self):
        settings = self.settings(
            DB_ENGINE="postgres",
            POSTGRES_PASSWORD="test-only",
            POSTGRES_HOST="db",
            POSTGRES_PORT="5433",
            POSTGRES_DB="isolated",
            POSTGRES_USER="isolated",
            REDIS_URL="redis://redis:6379/0",
        )
        self.assertEqual(settings["DATABASES"]["default"]["HOST"], "db")
        self.assertEqual(settings["DATABASES"]["default"]["PORT"], "5433")
        self.assertEqual(settings["CACHES"]["default"]["LOCATION"], "redis://redis:6379/0")
        self.assertNotIn("SESSION_ENGINE", settings)

    def test_production_security_defaults(self):
        settings = self.settings(
            DJANGO_ENV="production",
            DB_ENGINE="postgres",
            POSTGRES_PASSWORD="test-only",
            DJANGO_ALLOWED_HOSTS="booking.example.org",
        )
        for name in ["SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"]:
            self.assertTrue(settings[name])

    def test_production_rejects_unsafe_configuration(self):
        from django.core.exceptions import ImproperlyConfigured

        base = dict(
            DJANGO_ENV="production",
            DB_ENGINE="postgres",
            POSTGRES_PASSWORD="test-only",
            DJANGO_ALLOWED_HOSTS="booking.example.org",
        )
        for invalid in [
            {"DJANGO_DEBUG": "true"},
            {"DJANGO_SECRET_KEY": "short"},
            {"DJANGO_ALLOWED_HOSTS": "*"},
            {"DB_ENGINE": "sqlite"},
            {"DJANGO_SESSION_COOKIE_SECURE": "false"},
            {"DJANGO_CSRF_COOKIE_SECURE": "false"},
            {"DJANGO_SECURE_SSL_REDIRECT": "false"},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ImproperlyConfigured):
                self.settings(**(base | invalid))

    def test_proxy_is_not_trusted_by_default(self):
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", self.settings())
        self.assertEqual(
            self.settings(DJANGO_TRUST_PROXY="true")["SECURE_PROXY_SSL_HEADER"],
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )

    def test_static_media_and_csrf_environment(self):
        settings = self.settings(
            DJANGO_STATIC_ROOT="/isolated/static",
            DJANGO_MEDIA_ROOT="/isolated/media",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://booking.example.org, https://other.example.org",
        )
        self.assertEqual(settings["STATIC_ROOT"], Path("/isolated/static"))
        self.assertEqual(settings["MEDIA_ROOT"], Path("/isolated/media"))
        self.assertEqual(len(settings["CSRF_TRUSTED_ORIGINS"]), 2)


class EntrypointTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)

    def main(self):
        return runpy.run_path(str(Path(__file__).resolve().parent.parent / "docker/entrypoint.py"))[
            "main"
        ]

    def test_management_command_is_executed_without_automatic_migration(self):
        command = ["python", "manage.py", "check"]
        with (
            patch.object(sys, "argv", ["entrypoint", *command]),
            patch("os.execvp") as execute,
            patch("subprocess.run") as run,
        ):
            self.main()()
        execute.assert_called_once_with("python", command)
        run.assert_not_called()

    def test_pending_migrations_prevent_gunicorn_start(self):
        with (
            patch.object(sys, "argv", ["entrypoint", "gunicorn"]),
            patch("django.setup"),
            patch("django.db.connection.cursor"),
            patch("django.db.connection.close"),
            patch(
                "subprocess.run", side_effect=subprocess.CalledProcessError(1, "migrate --check")
            ),
            patch("os.execvp") as execute,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                self.main()()
        execute.assert_not_called()

    def test_database_timeout_prevents_serving(self):
        with (
            patch.object(sys, "argv", ["entrypoint", "gunicorn"]),
            patch("django.setup"),
            patch("django.db.connection.cursor", side_effect=OperationalError("offline")),
            patch("django.db.connection.close"),
            patch("time.monotonic", side_effect=[0, 61]),
            patch("os.execvp") as execute,
        ):
            with self.assertRaisesRegex(SystemExit, "readiness timed out"):
                self.main()()
        execute.assert_not_called()

    def test_successful_start_execs_gunicorn_after_schema_and_static_checks(self):
        with (
            patch.object(sys, "argv", ["entrypoint", "gunicorn"]),
            patch("django.setup"),
            patch("django.db.connection.cursor"),
            patch("django.db.connection.close"),
            patch("subprocess.run") as run,
            patch("os.execvp") as execute,
        ):
            self.main()()
        self.assertEqual(run.call_args_list[0].args[0][-2:], ["migrate", "--check"])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["collectstatic", "--noinput"])
        execute.assert_called_once_with("gunicorn", ["gunicorn"])
