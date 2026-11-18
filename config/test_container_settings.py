"""Only the isolated Compose test profile; never imports runtime settings."""

from copy import deepcopy
from .test_postgres_settings import *  # noqa: F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
DATABASES["default"]["HOST"] = "db"  # noqa: F405
DATABASES["default"]["PORT"] = "5432"  # noqa: F405
