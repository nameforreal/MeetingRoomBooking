"""Cloud configuration tests with synthetic inputs; no Neon or Upstash connection."""

from io import StringIO
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, override_settings

from config.render_config import cloud_environment, gunicorn_bind
from config.render_media import RejectUploadsMiddleware, UnavailableMediaStorage
from rooms.management.commands import neon_bootstrap

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = {
    "DJANGO_SECRET_KEY": "isolated-render-test-key-" * 4,
    "NEON_HOST": "ep-isolated-test.eu-central-1.aws.neon.tech",
    "NEON_DATABASE": "neondb",
    "NEON_USER": "test_role",
    "NEON_PASSWORD": "synthetic-only-password",
    "RENDER_EXTERNAL_HOSTNAME": "isolated-render.example.org",
}


class RenderEnvironmentTests(SimpleTestCase):
    def test_direct_connection_and_secure_environment(self):
        env = cloud_environment(FIXTURE)
        self.assertEqual(env["POSTGRES_HOST"], FIXTURE["NEON_HOST"])
        self.assertEqual(env["DB_ENGINE"], "postgres")
        self.assertEqual(env["DJANGO_READ_DOT_ENV"], "false")
        self.assertEqual(env["DJANGO_DEBUG"], "false")
        self.assertEqual(env["DJANGO_CSRF_TRUSTED_ORIGINS"], "https://isolated-render.example.org")
        self.assertEqual(env["REDIS_URL"], "")

    def test_missing_inputs_are_rejected_without_values(self):
        for name in FIXTURE:
            with self.subTest(name=name), self.assertRaises(ImproperlyConfigured) as error:
                cloud_environment({key: value for key, value in FIXTURE.items() if key != name})
            self.assertNotIn(FIXTURE["NEON_PASSWORD"], str(error.exception))

    def test_pooler_and_non_neon_hosts_rejected(self):
        for host in (
            "localhost",
            "127.0.0.1",
            "ep-test.neon.tech.evil.org",
            "ep-test-pooler.aws.neon.tech",
            "postgresql://secret@ep-test.neon.tech/neondb",
        ):
            with self.subTest(host=host), self.assertRaises(ImproperlyConfigured) as error:
                cloud_environment(FIXTURE | {"NEON_HOST": host})
            self.assertNotIn(host, str(error.exception))

    def test_hosts_are_explicit_and_csrf_is_https(self):
        result = cloud_environment(FIXTURE | {"DJANGO_ALLOWED_HOSTS": "bookings.example.org"})
        self.assertEqual(
            result["DJANGO_CSRF_TRUSTED_ORIGINS"],
            "https://isolated-render.example.org,https://bookings.example.org",
        )
        for host in (
            "*",
            ".example.org",
            "https://example.org",
            "example.org:443",
            "example.org/path",
        ):
            with self.subTest(host=host), self.assertRaises(ImproperlyConfigured):
                cloud_environment(FIXTURE | {"DJANGO_ALLOWED_HOSTS": host})

    def test_unsafe_legacy_environment_does_not_weaken_cloud(self):
        env = cloud_environment(
            FIXTURE
            | {
                "DJANGO_DEBUG": "true",
                "DB_ENGINE": "sqlite",
                "DJANGO_READ_DOT_ENV": "true",
                "DJANGO_SESSION_COOKIE_SECURE": "false",
                "REDIS_URL": "redis://localhost",
            }
        )
        self.assertEqual(env["DJANGO_DEBUG"], "false")
        self.assertEqual(env["DJANGO_SESSION_COOKIE_SECURE"], "true")
        self.assertEqual(env["REDIS_URL"], "")

    def test_upstash_requires_tls_and_cannot_override_certificate_checks(self):
        good = "rediss://default:synthetic@isolated.upstash.io:6379"
        self.assertEqual(
            cloud_environment(FIXTURE | {"UPSTASH_REDIS_URL": good})["REDIS_URL"], good
        )
        for url in (
            "redis://default:secret@isolated.upstash.io:6379",
            good + "?ssl_cert_reqs=none",
            "https://isolated.upstash.io",
            "rediss://isolated.upstash.io:6379",
            good + "/3",
        ):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured) as error:
                cloud_environment(FIXTURE | {"UPSTASH_REDIS_URL": url})
            self.assertNotIn(url, str(error.exception))

    def test_port_validation(self):
        self.assertEqual(gunicorn_bind({}), "0.0.0.0:10000")
        self.assertEqual(gunicorn_bind({"PORT": "12345"}), "0.0.0.0:12345")
        for port in ("", "0", "-1", "65536", "²", "10000;command"):
            with self.subTest(port=port), self.assertRaises(ImproperlyConfigured):
                gunicorn_bind({"PORT": port})

    def test_actual_settings_import_and_deploy_checks_without_network_or_dotenv(self):
        code = """
import os
from pathlib import Path
from unittest.mock import patch
from io import StringIO
original_read = Path.read_text
def guarded_read(path, *args, **kwargs):
    assert path.name != ".env", "dotenv read forbidden"
    return original_read(path, *args, **kwargs)
with patch.object(Path, "read_text", guarded_read), patch("socket.socket.connect", side_effect=AssertionError("network")):
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_render"
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    db = settings.DATABASES["default"]
    assert db["OPTIONS"]["sslmode"] == "verify-full"
    assert db["OPTIONS"]["channel_binding"] == "require"
    assert db["OPTIONS"]["prepare_threshold"] is None
    assert db["DISABLE_SERVER_SIDE_CURSORS"] and db["CONN_MAX_AGE"] == 0
    assert settings.CACHES["default"]["BACKEND"].endswith("LocMemCache")
    assert settings.SESSION_ENGINE.endswith("backends.db")
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.STORAGES["default"]["BACKEND"].endswith("UnavailableMediaStorage")
    call_command("check", deploy=True, stdout=StringIO(), stderr=StringIO())
    from django.core.checks import run_checks
    assert {item.id for item in run_checks(include_deployment_checks=True)} == {"security.W005", "security.W021"}
"""
        env = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "PATH", "TEMP", "TMP")
            if key in os.environ
        }
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            env=env | FIXTURE,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class CloudStaticMediaTests(SimpleTestCase):
    def test_media_storage_cannot_write_or_delete(self):
        storage = UnavailableMediaStorage()
        with self.assertRaises(ValidationError):
            storage.save("private.jpg", ContentFile(b"image"))
        with self.assertRaises(ValidationError):
            storage.delete("private.jpg")
        self.assertFalse(storage.exists("private.jpg"))
        self.assertIn("/static/", storage.url("private.jpg"))

    def test_uploads_return_400_before_view(self):
        view = MagicMock(return_value=HttpResponse("ok"))
        for path in ("/management/rooms/add/", "/admin/rooms/room/add/", "/api/rooms/"):
            request = RequestFactory().post(
                path, {"photo": SimpleUploadedFile("image.jpg", b"data")}
            )
            response = RejectUploadsMiddleware(view)(request)
            self.assertEqual(response.status_code, 400)
        view.assert_not_called()

    def test_requests_without_upload_are_unchanged(self):
        response = RejectUploadsMiddleware(lambda request: HttpResponse("ok"))(
            RequestFactory().post("/management/rooms/add/", {"name": "Room"})
        )
        self.assertEqual(response.status_code, 200)

    def test_collectstatic_manifest_and_whitenoise_http_without_database(self):
        with TemporaryDirectory(prefix="mrb-render-static-") as directory:
            with override_settings(
                DEBUG=False,
                STATIC_ROOT=directory,
                STATIC_URL="/static/",
                STORAGES={
                    "default": {"BACKEND": "config.render_media.UnavailableMediaStorage"},
                    "staticfiles": {
                        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    },
                },
                MIDDLEWARE=[
                    "django.middleware.security.SecurityMiddleware",
                    "whitenoise.middleware.WhiteNoiseMiddleware",
                ],
                WHITENOISE_AUTOREFRESH=False,
            ):
                call_command("collectstatic", interactive=False, verbosity=0)
                from django.contrib.staticfiles.storage import staticfiles_storage

                url = staticfiles_storage.url("rooms/style.css")
                self.assertNotEqual(url, "/static/rooms/style.css")
                response = Client().get(url)
                self.assertEqual(response.status_code, 200)
                self.assertGreater(len(b"".join(response.streaming_content)), 0)
                self.assertIn("immutable", response["Cache-Control"])
                self.assertTrue(Path(directory, "staticfiles.json").is_file())


class NeonBootstrapTests(SimpleTestCase):
    def setUp(self):
        identity = patch.object(neon_bootstrap, "verify_endpoint")
        identity.start()
        self.addCleanup(identity.stop)
        config = patch.object(
            neon_bootstrap,
            "settings",
            SimpleNamespace(
                RENDER_PROFILE=True,
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "HOST": FIXTURE["NEON_HOST"],
                        "OPTIONS": {"sslmode": "verify-full"},
                    }
                },
            ),
        )
        config.start()
        self.addCleanup(config.stop)
        env = patch.dict(
            os.environ, {"NEON_BRANCH": "hosting-test", "NEON_EXPECTED_HOST": FIXTURE["NEON_HOST"]}
        )
        env.start()
        self.addCleanup(env.stop)

    def command(self):
        return neon_bootstrap.Command(stdout=StringIO(), stderr=StringIO())

    def cursor(self, *, apply=False):
        cursor = MagicMock()
        cursor.fetchone.side_effect = (
            ([(True,)] if apply else [])
            + [(180001,), (True,), (True,), (True, True), (False,)]
            + ([(True,), (True,)] if apply else [])
        )
        return cursor

    def test_guard_refuses_production_before_connecting(self):
        with (
            patch.dict(os.environ, {"NEON_BRANCH": "production"}),
            patch.object(neon_bootstrap, "connection") as db,
        ):
            with self.assertRaises(CommandError):
                self.command().handle(apply=False, confirm_host="")
            db.cursor.assert_not_called()

    def test_guard_refuses_unconfirmed_apply_and_endpoint_mismatch(self):
        with self.assertRaises(CommandError):
            neon_bootstrap.validate_target(apply=True, confirm_host="wrong")
        with (
            patch.dict(os.environ, {"NEON_EXPECTED_HOST": "wrong"}),
            self.assertRaises(CommandError),
        ):
            neon_bootstrap.validate_target(apply=False, confirm_host="")

    def test_preflight_does_not_migrate(self):
        with (
            patch.object(neon_bootstrap, "connection") as db,
            patch.object(neon_bootstrap, "call_command") as migrate,
        ):
            db.cursor.return_value.__enter__.return_value = self.cursor()
            self.command().handle(apply=False, confirm_host="")
            migrate.assert_not_called()
            db.close.assert_called_once()

    def test_apply_checks_guards_and_postconditions(self):
        with (
            patch.object(neon_bootstrap, "connection") as db,
            patch.object(neon_bootstrap, "call_command") as migrate,
        ):
            db.cursor.return_value.__enter__.return_value = self.cursor(apply=True)
            self.command().handle(apply=True, confirm_host=FIXTURE["NEON_HOST"])
            migrate.assert_called_once_with("migrate", interactive=False, verbosity=0)
            db.close.assert_called_once()

    def test_nonempty_database_is_never_initialized(self):
        cursor = self.cursor()
        cursor.fetchone.side_effect = [(180001,), (True,), (True,), (True, True), (True,)]
        with self.assertRaisesMessage(CommandError, "not empty"):
            neon_bootstrap.preflight(cursor)

    def test_preflight_rejects_wrong_version_tls_extension_or_privileges(self):
        good = [(180001,), (True,), (True,), (True, True), (False,)]
        for index, bad in ((0, (170012,)), (1, (False,)), (2, (False,)), (3, (False, True))):
            cursor = MagicMock()
            answers = list(good)
            answers[index] = bad
            cursor.fetchone.side_effect = answers
            with self.subTest(index=index), self.assertRaises(CommandError):
                neon_bootstrap.preflight(cursor)

    def test_migration_error_is_sanitized_without_retry(self):
        with (
            patch.object(neon_bootstrap, "connection") as db,
            patch.object(
                neon_bootstrap, "call_command", side_effect=RuntimeError("credential-must-not-leak")
            ) as migrate,
        ):
            db.cursor.return_value.__enter__.return_value = self.cursor(apply=True)
            with self.assertRaises(CommandError) as error:
                self.command().handle(apply=True, confirm_host=FIXTURE["NEON_HOST"])
            self.assertNotIn("credential-must-not-leak", str(error.exception))
            self.assertEqual(migrate.call_count, 1)
            db.close.assert_called_once()


class RenderEntrypointTests(SimpleTestCase):
    def test_pending_migrations_prevent_server_start(self):
        with (
            patch.dict(os.environ),
            patch.object(sys, "argv", ["render_entrypoint"]),
            patch("django.setup"),
            patch(
                "django.core.management.call_command", side_effect=[None, SystemExit(1)]
            ) as command,
            patch("os.execvp") as execute,
        ):
            with self.assertRaises(SystemExit):
                self.main()()
            self.assertEqual(command.call_count, 2)
            execute.assert_not_called()

    def main(self):
        return runpy.run_path(str(ROOT / "docker/render_entrypoint.py"))["main"]

    def test_start_checks_only_never_applies_migrations(self):
        with (
            patch.dict(os.environ),
            patch.object(sys, "argv", ["render_entrypoint"]),
            patch("django.setup"),
            patch("django.core.management.call_command") as command,
            patch("django.db.connection.close"),
            patch("os.execvp") as execute,
        ):
            self.main()()
        self.assertEqual(command.call_args_list[1].args, ("migrate",))
        self.assertTrue(command.call_args_list[1].kwargs["check_unapplied"])
        self.assertEqual(execute.call_args.args[0], "gunicorn")

    def test_failed_start_has_no_secret_traceback_and_no_server(self):
        with (
            patch.dict(os.environ),
            patch.object(sys, "argv", ["render_entrypoint"]),
            patch("django.setup", side_effect=RuntimeError("private-password")),
            patch("os.execvp") as execute,
        ):
            with self.assertRaises(SystemExit) as error:
                self.main()()
            self.assertNotIn("private-password", str(error.exception))
            execute.assert_not_called()
