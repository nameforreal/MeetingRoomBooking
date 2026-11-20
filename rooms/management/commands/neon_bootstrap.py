"""Explicit first initialization of an empty, operator-verified hosting-test database."""

import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from config.neon_identity import verify_endpoint
from rooms.bootstrap_catalog import require_empty

LOCK_ID = 531846209173


def validate_target(*, apply, confirm_host):
    db = settings.DATABASES["default"]
    if (
        not getattr(settings, "RENDER_PROFILE", False)
        or os.environ.get("NEON_BRANCH") != "hosting-test"
        or os.environ.get("NEON_EXPECTED_HOST") != db.get("HOST")
        or db.get("ENGINE") != "django.db.backends.postgresql"
        or db.get("OPTIONS", {}).get("sslmode") != "verify-full"
    ):
        raise CommandError(
            "Select the Render profile and explicitly verify the hosting-test endpoint."
        )
    if apply and confirm_host != db["HOST"]:
        raise CommandError(
            "Applying migrations requires --confirm-host matching the verified endpoint."
        )
    verify_endpoint(db["HOST"])


def preflight(cursor, django_connection):
    cursor.execute("SELECT current_setting('server_version_num')::int")
    if not 180000 <= cursor.fetchone()[0] < 190000:
        raise CommandError("This bootstrap procedure requires PostgreSQL 18.")
    # Check the client-side Psycopg TLS session; Neon may terminate TLS at its proxy.
    client_connection = django_connection.connection
    client_libpq = getattr(client_connection, "pgconn", None)
    if not getattr(client_libpq, "ssl_in_use", False):
        raise CommandError("An active TLS connection is required.")
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist')"
    )
    if not cursor.fetchone()[0]:
        raise CommandError("btree_gist is not available; do not weaken the overlap constraint.")
    cursor.execute(
        "SELECT has_database_privilege(current_database(), 'CREATE'), has_schema_privilege('public', 'CREATE')"
    )
    if cursor.fetchone() != (True, True):
        raise CommandError("The bootstrap role requires CREATE on the database and public schema.")
    require_empty(cursor)


class Command(BaseCommand):
    help = (
        "Read-only Neon preflight by default; --apply initializes an empty hosting-test database."
    )
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm-host", default="")

    def handle(self, *args, **options):
        validate_target(apply=options["apply"], confirm_host=options["confirm_host"])
        locked = False
        try:
            with connection.cursor() as cursor:
                if options["apply"]:
                    # Session lock is valid only on the Direct connection.
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
                    locked = cursor.fetchone()[0]
                    if not locked:
                        raise CommandError("Another initialization is running.")
                preflight(cursor, connection)
            if not options["apply"]:
                self.stdout.write(
                    "Preflight passed: PostgreSQL 18, TLS, empty database, btree_gist available. No changes applied."
                )
                return
            # Recheck the control plane immediately before the first DDL.
            verify_endpoint(settings.DATABASES["default"]["HOST"])
            call_command("migrate", interactive=False, verbosity=0)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'booking_no_active_overlap' AND contype = 'x')"
                )
                if not cursor.fetchone()[0]:
                    raise CommandError("Required exclusion constraint missing after migration.")
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'meetingroom_user_activity_guard' AND tgenabled = 'O')"
                )
                if not cursor.fetchone()[0]:
                    raise CommandError("Required user activity trigger missing after migration.")
            self.stdout.write(
                "Initial migrations completed; exclusion constraint and activity trigger present. No users or demo data seeded."
            )
        except CommandError:
            raise
        except Exception:
            raise CommandError(
                "Neon bootstrap failed. No automatic retry or cleanup; review the database state privately before proceeding."
            ) from None
        finally:
            # Closing releases the advisory lock even after a broken connection.
            connection.close()
