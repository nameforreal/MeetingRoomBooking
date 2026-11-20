"""Run via unittest on a NEW empty PostgreSQL 18; no migration runner, no Neon access."""

from copy import deepcopy
import os
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from django.core.management.base import CommandError
from django.db import connection, transaction
from config import neon_identity as identity
from rooms.bootstrap_catalog import require_empty
from rooms.management.commands import neon_bootstrap as bootstrap
from rooms.management.commands.seed_hosting_rooms import NAMES, create_five, validate_manifest
from rooms.models import Room

ENV = {
    "NEON_API_KEY": "synthetic-key",
    "NEON_PROJECT_ID": "project-test",
    "NEON_BRANCH_ID": "br-test",
    "NEON_ENDPOINT_ID": "ep-test",
}
HOST = "ep-test.eu-central-1.aws.neon.tech"
RESPONSES = [
    {"project": {"id": "project-test"}},
    {
        "branch": {
            "id": "br-test",
            "project_id": "project-test",
            "name": "hosting-test",
            "default": False,
        }
    },
    {
        "endpoint": {
            "id": "ep-test",
            "project_id": "project-test",
            "branch_id": "br-test",
            "host": HOST,
            "type": "read_write",
        }
    },
]


class IdentityTests(unittest.TestCase):
    def test_matching_api_identity(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(identity, "api_get", side_effect=deepcopy(RESPONSES)) as api,
        ):
            identity.verify_endpoint(HOST)
            self.assertEqual(api.call_count, 3)

    def test_substituted_production_and_missing_evidence_never_touch_database(self):
        cases = [
            (1, "name", "production"),
            (1, "default", True),
            (1, "protected", True),
            (1, "id", "br-production"),
            (1, "project_id", "other"),
            (2, "branch_id", "br-production"),
            (2, "host", "ep-production.aws.neon.tech"),
            (2, "project_id", "other"),
            (2, "type", "read_only"),
            (0, "id", "other"),
        ]
        for index, field, value in cases:
            answers = deepcopy(RESPONSES)
            next(iter(answers[index].values()))[field] = value
            with (
                self.subTest(field=field),
                patch.dict(
                    os.environ, ENV | {"NEON_BRANCH": "hosting-test", "NEON_EXPECTED_HOST": HOST}
                ),
                patch.object(identity, "api_get", side_effect=answers),
                patch.object(
                    bootstrap,
                    "settings",
                    SimpleNamespace(
                        RENDER_PROFILE=True,
                        DATABASES={
                            "default": {
                                "ENGINE": "django.db.backends.postgresql",
                                "HOST": HOST,
                                "OPTIONS": {"sslmode": "verify-full"},
                            }
                        },
                    ),
                ),
                patch.object(bootstrap, "connection") as db,
                patch.object(bootstrap, "call_command") as migrate,
            ):
                with self.assertRaises(CommandError):
                    bootstrap.Command().handle(apply=True, confirm_host=HOST)
                db.cursor.assert_not_called()
                migrate.assert_not_called()

    def test_api_failures_and_bad_json_fail_closed(self):
        for failure in (TimeoutError("synthetic-key"), OSError("private endpoint")):
            with patch.object(identity, "build_opener", side_effect=failure):
                with self.assertRaises(CommandError) as error:
                    identity.api_get("/projects/test", "synthetic-key")
                self.assertNotIn("synthetic-key", str(error.exception))
        for body in (b"invalid", b"[]", b"x" * 1048577):
            response = MagicMock(status=200)
            response.read.return_value = body
            with patch.object(identity, "build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value = response
                with self.assertRaises(CommandError):
                    identity.api_get("/projects/test", "synthetic-key")

    def test_missing_credentials_and_redirect_refused(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(identity, "api_get") as api:
            with self.assertRaises(CommandError):
                identity.verify_endpoint(HOST)
            api.assert_not_called()
        with self.assertRaises(CommandError):
            identity.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_missing_response_fields_refused(self):
        with patch.dict(os.environ, ENV), patch.object(identity, "api_get", return_value={}):
            with self.assertRaises(CommandError):
                identity.verify_endpoint(HOST)


@unittest.skipUnless(
    connection.vendor == "postgresql" and os.environ.get("MRB_EMPTY_CATALOG_TEST") == "1",
    "PostgreSQL catalog inspection required",
)
class CatalogTests(unittest.TestCase):
    def test_empty_database(self):
        with connection.cursor() as cursor:
            require_empty(cursor)

    def test_exact_neon_default_privileges_are_exempt(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("CREATE ROLE cloud_admin")
                cursor.execute("CREATE ROLE neon_superuser")

                cursor.execute("""
                    ALTER DEFAULT PRIVILEGES FOR ROLE cloud_admin
                    IN SCHEMA public
                    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, MAINTAIN
                    ON TABLES TO neon_superuser WITH GRANT OPTION
                """)

                cursor.execute("""
                    ALTER DEFAULT PRIVILEGES FOR ROLE cloud_admin
                    IN SCHEMA public
                    GRANT SELECT, UPDATE, USAGE
                    ON SEQUENCES TO neon_superuser WITH GRANT OPTION
                """)

                cursor.execute("""
                    SELECT d.defaclobjtype, d.defaclacl::text
                    FROM pg_default_acl d
                    JOIN pg_roles r ON r.oid = d.defaclrole
                    JOIN pg_namespace n ON n.oid = d.defaclnamespace
                    WHERE r.rolname = 'cloud_admin'
                      AND n.nspname = 'public'
                """)

                actual = dict(cursor.fetchall())

                self.assertEqual(
                    actual,
                    {
                        "S": "{neon_superuser=r*w*U*/cloud_admin}",
                        "r": "{neon_superuser=a*r*w*d*D*m*/cloud_admin}",
                    },
                )

                # Exact Neon defaults must not block initialization.
                require_empty(cursor)

                # Any additional privilege changes the ACL and must be rejected.
                cursor.execute("""
                    ALTER DEFAULT PRIVILEGES FOR ROLE cloud_admin
                    IN SCHEMA public
                    GRANT REFERENCES ON TABLES TO neon_superuser
                """)

                with self.assertRaises(CommandError):
                    require_empty(cursor)

            transaction.set_rollback(True)

    def test_each_object_category_is_rejected(self):
        statements = [
            "CREATE TABLE probe(id int)",
            "CREATE VIEW probe AS SELECT 1 AS id",
            "CREATE MATERIALIZED VIEW probe AS SELECT 1 AS id",
            "CREATE SEQUENCE probe",
            "CREATE FUNCTION probe() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "CREATE PROCEDURE probe() LANGUAGE sql AS 'SELECT 1'",
            "CREATE TYPE probe AS ENUM ('one')",
            "CREATE DOMAIN probe AS int",
            "CREATE TYPE probe AS (id int)",
            "CREATE SCHEMA pgXuser",
            "CREATE SCHEMA custom",
            "CREATE EXTENSION btree_gist",
            "SELECT lo_create(0)",
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC",
            "CREATE PUBLICATION probe",
            'CREATE COLLATION probe FROM "C"',
            "CREATE FOREIGN DATA WRAPPER probe",
            "CREATE TEXT SEARCH CONFIGURATION probe (COPY=pg_catalog.simple)",
        ]
        for statement in statements:
            with self.subTest(statement=statement), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                    with self.assertRaises(CommandError):
                        require_empty(cursor)
                transaction.set_rollback(True)


@unittest.skipUnless(
    connection.vendor == "postgresql" and os.environ.get("MRB_EMPTY_CATALOG_TEST") == "1",
    "PostgreSQL room initialization required",
)
class FiveRoomTests(unittest.TestCase):
    def data(self):
        # Synthetic fixture values, NOT approved production/demo parameters.
        return [
            dict(
                name=name,
                capacity=7,
                is_vip=False,
                is_active=True,
                description="fixture",
                photo=None,
            )
            for name in NAMES
        ]

    def test_manifest_requires_all_explicit_fields(self):
        for field in ("capacity", "is_vip", "is_active", "description", "photo"):
            data = self.data()
            del data[0][field]
            with self.subTest(field=field), self.assertRaises(CommandError):
                validate_manifest(data)

    def test_create_repeat_conflict_and_unrelated_room_preserved(self):
        with transaction.atomic():
            # Only a test table fixture; no migration files or migration commands executed.
            with connection.schema_editor() as schema:
                schema.create_model(Room)
            with patch("rooms.audit.record"):
                Room.objects.create(name="Unrelated")
                self.assertEqual(create_five(self.data()), 5)
                self.assertEqual(Room.objects.count(), 1)
                self.assertEqual(create_five(self.data(), apply=True), 5)
                self.assertEqual(create_five(self.data(), apply=True), 0)
                self.assertEqual(Room.objects.count(), 6)
                data = self.data()
                data[-1]["capacity"] = 8
                with self.assertRaises(CommandError):
                    create_five(data, apply=True)
                self.assertEqual(Room.objects.filter(capacity=7).count(), 5)
            transaction.set_rollback(True)
