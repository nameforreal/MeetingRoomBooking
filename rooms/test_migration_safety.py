from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, connections, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone

from . import booking_operations

LATEST = "0009_user_activity_guard"


class MigrationHelpers:
    def migrate(self, target):
        executor = MigrationExecutor(connection)
        targets = [("rooms", target)]
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps if target else None

    def setUp(self):
        self.apps = self.migrate(LATEST)

    def tearDown(self):
        # All destructive scenarios involve this runner-created test DB only.
        # Reapply before TransactionTestCase flushes the disposable fixture rows.
        self.migrate(LATEST)
        super().tearDown()

    def applied(self):
        return set(MigrationRecorder(connection).applied_migrations())

    def snapshot(self, apps):
        snapshot = {}
        for name in ("Room", "Booking", "AuditEvent", "BookingCreationRequest"):
            try:
                model = apps.get_model("rooms", name)
            except LookupError:
                continue
            snapshot[name] = list(model.objects.order_by("pk").values())
        return snapshot

    def seed_booking(self, apps=None):
        apps = apps or self.apps
        user = apps.get_model("auth", "User").objects.create(username="migration-owner")
        room = apps.get_model("rooms", "Room").objects.create(name="migration-room")
        start = (timezone.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        booking = apps.get_model("rooms", "Booking").objects.create(
            user_id=user.pk, room_id=room.pk, start_time=start, end_time=start + timedelta(hours=1), status="confirmed",
        )
        return booking, user, room

    def seed_receipt(self, apps=None):
        apps = apps or self.apps
        booking, user, _ = self.seed_booking(apps)
        return apps.get_model("rooms", "BookingCreationRequest").objects.create(
            actor_id=user.pk, booking_id=booking.pk, key="migration-key", canonical_payload={"room": booking.room_id},
            payload_hash="0" * 64, response_payload={"id": booking.pk}, completed_at=timezone.now(),
        )

    def assert_pg_schema_matches_recorder(self):
        if connection.vendor != "postgresql":
            return
        applied = self.applied()
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname='meetingroom_user_activity_guard' AND NOT tgisinternal")
            self.assertEqual(cursor.fetchone()[0], int(("rooms", LATEST) in applied))
            cursor.execute("SELECT count(*) FROM pg_proc WHERE proname='meetingroom_guard_user_activity' AND pronamespace=current_schema()::regnamespace")
            self.assertEqual(cursor.fetchone()[0], int(("rooms", LATEST) in applied))
            cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname='booking_no_active_overlap'")
            self.assertEqual(cursor.fetchone()[0], int(("rooms", "0005_booking_concurrency") in applied))

    def assert_refused_unchanged(self, target, name, apps=None):
        apps = apps or self.apps
        before, applied = self.snapshot(apps), self.applied()
        with self.assertRaisesRegex(IrreversibleError, name):
            self.migrate(target)
        self.assertEqual(self.applied(), applied)
        self.assertEqual(self.snapshot(apps), before)
        self.assert_pg_schema_matches_recorder()


class MigrationSafetyTests(MigrationHelpers, TransactionTestCase):
    def test_empty_chain_can_reverse_and_reapply_at_every_stage(self):
        stages = [name for app, name in sorted(self.applied()) if app == "rooms"]
        for target in list(reversed(stages[:-1])) + [None]:
            with self.subTest(target=target):
                self.migrate(target)
                self.assert_pg_schema_matches_recorder()
                self.migrate(LATEST)
                self.assert_pg_schema_matches_recorder()
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_extension WHERE extname='btree_gist'")
                self.assertEqual(cursor.fetchone()[0], 1)  # Never drop a possibly shared extension.

    def test_latest_booking_without_audit_already_prevents_guard_removal(self):
        self.seed_booking()
        for target in ["0008_booking_baselines", "0006_booking_creation_request", None]:
            self.assert_refused_unchanged(target, "Booking")

    def test_latest_audit_without_booking_prevents_guard_removal(self):
        room = self.apps.get_model("rooms", "Room").objects.create(name="audit-target")
        self.apps.get_model("rooms", "AuditEvent").objects.create(
            source="system", action="room.archived", object_type="room", object_id=room.pk,
            room_id=room.pk, before={"is_active": True}, after={"is_active": False},
        )
        self.assert_refused_unchanged("0006_booking_creation_request", "AuditEvent")

    def test_latest_baseline_is_preserved_with_trigger(self):
        apps = self.migrate("0007_audit_history")
        self.seed_booking(apps)
        self.apps = self.migrate(LATEST)
        self.assertEqual(self.apps.get_model("rooms", "AuditEvent").objects.get().action, "baseline")
        self.assert_refused_unchanged(None, "AuditEvent")

    def test_latest_receipt_and_original_result_are_preserved(self):
        self.seed_receipt()
        self.assert_refused_unchanged("0005_booking_concurrency", "BookingCreationRequest")

    def test_legacy_receipts_cannot_be_dropped_before_audit_was_installed(self):
        apps = self.migrate("0006_booking_creation_request")
        self.seed_receipt(apps)
        self.assert_refused_unchanged("0005_booking_concurrency", "BookingCreationRequest", apps)

    def test_legacy_booking_cannot_lose_revision_or_exclusion_constraint(self):
        apps = self.migrate("0005_booking_concurrency")
        self.seed_booking(apps)
        self.assert_refused_unchanged("0004_booking_export_permission", "Booking", apps)

    def test_legacy_booking_table_cannot_be_dropped(self):
        apps = self.migrate("0002_booking")
        self.seed_booking(apps)
        self.assert_refused_unchanged("0001_initial", "Booking", apps)

    def test_legacy_catalog_cannot_be_dropped(self):
        apps = self.migrate("0001_initial")
        apps.get_model("rooms", "Room").objects.create(name="preserve-catalog")
        self.assert_refused_unchanged(None, "Room", apps)

    def test_photo_links_cannot_be_dropped(self):
        apps = self.migrate("0003_room_photo")
        apps.get_model("rooms", "Room").objects.create(name="photo-room", photo="room_photos/existing.png")
        self.assert_refused_unchanged("0002_booking", "Room.photo", apps)

    def test_audit_schema_guard_preserves_legacy_booking_without_events(self):
        apps = self.migrate("0007_audit_history")
        self.seed_booking(apps)
        self.assert_refused_unchanged("0006_booking_creation_request", "Booking", apps)

    def test_baseline_migration_cannot_be_marked_reversed_with_captured_history(self):
        apps = self.migrate("0007_audit_history")
        self.seed_booking(apps)
        apps = self.migrate("0008_booking_baselines")
        self.assert_refused_unchanged("0007_audit_history", "AuditEvent", apps)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger, transactional DDL and lock tests.")
class PostgreSQLMigrationSafetyTests(MigrationHelpers, TransactionTestCase):
    def assert_activity_guard_works(self, user):
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                get_user_model().objects.filter(pk=user.pk).update(is_active=False)

    def test_failed_downgrade_keeps_real_activity_guard(self):
        _, user, _ = self.seed_booking()
        self.assert_refused_unchanged("0006_booking_creation_request", "Booking")
        self.assert_activity_guard_works(user)

    def test_empty_reverse_really_removes_guard_and_reapply_installs_once(self):
        user = get_user_model().objects.create_user(username="trigger-roundtrip")
        self.migrate("0008_booking_baselines")
        self.assert_pg_schema_matches_recorder()
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
        get_user_model().objects.filter(pk=user.pk).update(is_active=True)
        self.migrate(LATEST)
        self.assert_pg_schema_matches_recorder()
        self.assert_activity_guard_works(user)

    def test_reverse_ddl_failure_rolls_back_trigger_and_recorder(self):
        user = get_user_model().objects.create_user(username="reverse-failure")
        applied = self.applied()
        editor = connection.SchemaEditorClass
        original = editor.execute

        def fail_after_drop(instance, sql, *args, **kwargs):
            result = original(instance, sql, *args, **kwargs)
            if "DROP TRIGGER meetingroom_user_activity_guard" in str(sql):
                raise RuntimeError("injected reverse DDL failure")
            return result

        with patch.object(editor, "execute", fail_after_drop):
            with self.assertRaisesRegex(RuntimeError, "injected reverse"):
                self.migrate("0008_booking_baselines")
        self.assertEqual(self.applied(), applied)
        self.assert_pg_schema_matches_recorder()
        self.assert_activity_guard_works(user)

    def test_forward_ddl_failure_leaves_no_unrecorded_trigger(self):
        self.migrate("0008_booking_baselines")
        applied = self.applied()
        editor = connection.SchemaEditorClass
        original = editor.execute

        def fail_after_create(instance, sql, *args, **kwargs):
            result = original(instance, sql, *args, **kwargs)
            if "CREATE TRIGGER meetingroom_user_activity_guard" in str(sql):
                raise RuntimeError("injected forward DDL failure")
            return result

        with patch.object(editor, "execute", fail_after_create):
            with self.assertRaisesRegex(RuntimeError, "injected forward"):
                self.migrate(LATEST)
        self.assertEqual(self.applied(), applied)
        self.assert_pg_schema_matches_recorder()
        self.migrate(LATEST)
        self.assert_pg_schema_matches_recorder()

    def test_reverse_waits_for_creation_and_then_preserves_committed_history(self):
        from .models import Room
        user = get_user_model().objects.create_user(username="concurrent-migration")
        room = Room.objects.create(name="concurrent-migration")
        start = (timezone.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        created, release, reverse_started = Event(), Event(), Event()
        state = {}

        def run(job):
            connections.close_all()
            try:
                return job()
            finally:
                connections.close_all()

        def create():
            with transaction.atomic():
                booking_operations.create_once(actor=user, key="concurrent-key", room_id=room.pk,
                                               start_time=start, end_time=start + timedelta(hours=1))
                created.set()
                if not release.wait(5):
                    raise RuntimeError("Writer was not released")

        def reverse():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                state["pid"] = cursor.fetchone()[0]
            reverse_started.set()
            try:
                self.migrate("0006_booking_creation_request")
            except IrreversibleError:
                return "protected"
            return "unexpected rollback"

        applied = self.applied()
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(run, create)
            try:
                self.assertTrue(created.wait(5))
                rollback = pool.submit(run, reverse)
                self.assertTrue(reverse_started.wait(5))
                deadline, blocked = monotonic() + 3, False
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [state["pid"]])
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    Event().wait(0.01)
                self.assertTrue(blocked)
            finally:
                release.set()
            writer.result(timeout=10)
            self.assertEqual(rollback.result(timeout=10), "protected")
        self.assertEqual(self.applied(), applied)
        for name in ["Booking", "BookingCreationRequest", "AuditEvent"]:
            self.assertEqual(self.apps.get_model("rooms", name).objects.count(), 1)
        self.assert_pg_schema_matches_recorder()
        self.assert_activity_guard_works(user)
