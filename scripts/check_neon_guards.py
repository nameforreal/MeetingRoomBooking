"""Only a dedicated empty local PostgreSQL 18 on 55488; never runs migrations."""

import os
from pathlib import Path
import sys
import types
import unittest
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import test_postgres_settings as base  # noqa: E402

isolated = types.ModuleType("isolated_neon_guard_settings")
for key in dir(base):
    if key.isupper():
        setattr(isolated, key, deepcopy(getattr(base, key)))
isolated.DATABASES["default"]["PORT"] = "55488"
sys.modules[isolated.__name__] = isolated
os.environ["DJANGO_SETTINGS_MODULE"] = isolated.__name__
os.environ["MRB_EMPTY_CATALOG_TEST"] = "1"

if __name__ == "__main__":
    import django

    django.setup()
    from django.db import connection
    from rooms.bootstrap_catalog import require_empty

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, current_setting('server_version_num')::int"
        )
        database, user, version = cursor.fetchone()
        if (database, user) != ("booking_tests", "booking_tests") or not 180000 <= version < 190000:
            raise SystemExit("Refusing unexpected test database or PostgreSQL version.")
        require_empty(cursor)
    suite = unittest.defaultTestLoader.loadTestsFromNames(
        ["config.test_neon_guards", "config.test_render"]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    connection.close()
    raise SystemExit(0 if result.wasSuccessful() else 1)
