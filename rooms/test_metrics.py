from unittest.mock import MagicMock, patch

from django.db import DatabaseError
from django.test import SimpleTestCase

from config.metrics import Dependencies


class MetricsCollectorTests(SimpleTestCase):
    def collect_values(self):
        return {
            metric.name: metric.samples[0].value
            for metric in Dependencies().collect()
            if metric.samples
        }

    def make_cursor(self, connection_mock):
        cursor = MagicMock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection_mock.cursor.return_value = cursor_context
        return cursor

    def test_postgres_connection_metrics_are_collected(self):
        with (
            patch("config.metrics.connection") as connection_mock,
            patch("config.metrics.cache.get"),
        ):
            connection_mock.vendor = "postgresql"
            cursor = self.make_cursor(connection_mock)
            cursor.fetchone.side_effect = [(1,), (12, 3, 100)]
            values = self.collect_values()

        self.assertEqual(values["mrb_database_up"], 1)
        self.assertEqual(values["mrb_database_connections"], 12)
        self.assertEqual(values["mrb_database_active_connections"], 3)
        self.assertEqual(values["mrb_database_max_connections"], 100)
        self.assertIn("pg_stat_activity", cursor.execute.call_args_list[1].args[0])

    def test_sqlite_does_not_emit_postgres_only_metrics(self):
        with (
            patch("config.metrics.connection") as connection_mock,
            patch("config.metrics.cache.get"),
        ):
            connection_mock.vendor = "sqlite"
            cursor = self.make_cursor(connection_mock)
            cursor.fetchone.return_value = (1,)
            values = self.collect_values()

        self.assertEqual(values["mrb_database_up"], 1)
        self.assertNotIn("mrb_database_connections", values)

    def test_database_failure_reports_unavailable(self):
        with (
            patch("config.metrics.connection") as connection_mock,
            patch("config.metrics.cache.get"),
        ):
            connection_mock.cursor.side_effect = DatabaseError("isolated test failure")
            values = self.collect_values()

        self.assertEqual(values["mrb_database_up"], 0)
        self.assertNotIn("mrb_database_connections", values)
