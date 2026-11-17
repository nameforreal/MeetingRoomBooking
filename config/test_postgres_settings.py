"""Only for the disposable container documented in CONCURRENCY.md.

Fixed dedicated port/user/database; never import production settings or .env.
"""
from .test_settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "booking_tests",
        "USER": "booking_tests",
        "PASSWORD": "isolated-test-only",
        "HOST": "127.0.0.1",
        "PORT": "55439",
        "TEST": {"NAME": "test_booking_concurrency"},
        "OPTIONS": {"options": "-c lock_timeout=5000 -c statement_timeout=15000"},
    },
}
