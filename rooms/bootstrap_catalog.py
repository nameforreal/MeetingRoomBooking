"""Conservative inventory of PostgreSQL objects, without reading application data."""

from django.core.management.base import CommandError

# plpgsql is installed in a fresh PostgreSQL template. No other extension is exempt.
EMPTY_SQL = """
WITH user_ns AS (
 SELECT oid, nspname FROM pg_namespace
 WHERE left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'
), objects AS (
 SELECT 1 FROM user_ns WHERE nspname <> 'public'
 UNION ALL SELECT 1 FROM pg_class WHERE relnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_proc WHERE pronamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_type WHERE typnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_operator WHERE oprnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_collation WHERE collnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_conversion WHERE connamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_opclass WHERE opcnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_opfamily WHERE opfnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_ts_config WHERE cfgnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_ts_dict WHERE dictnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_ts_parser WHERE prsnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_ts_template WHERE tmplnamespace IN (SELECT oid FROM user_ns)
 UNION ALL SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'
 UNION ALL SELECT 1 FROM pg_event_trigger
 UNION ALL SELECT 1 FROM pg_foreign_data_wrapper
 UNION ALL SELECT 1 FROM pg_foreign_server
 UNION ALL SELECT 1 FROM pg_publication
 UNION ALL SELECT 1 FROM pg_subscription
 UNION ALL SELECT 1 FROM pg_largeobject_metadata
 UNION ALL SELECT 1 FROM pg_default_acl
)
SELECT EXISTS (SELECT 1 FROM objects)
"""


def require_empty(cursor):
    cursor.execute(EMPTY_SQL)
    if cursor.fetchone()[0]:
        raise CommandError(
            "Database is not empty. Refusing initial migration; do not delete existing objects."
        )
