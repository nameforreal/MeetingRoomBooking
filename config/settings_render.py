"""Render-only profile; .env is disabled before importing the shared settings."""

import os

from .render_config import cloud_environment

os.environ.update(cloud_environment(os.environ))

from .settings import *  # noqa: E402,F403

RENDER_PROFILE = True
DATABASES["default"].update(
    {  # noqa: F405
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": {
            "connect_timeout": 10,
            "sslmode": "verify-full",
            "sslrootcert": os.environ.get("NEON_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"),
            "channel_binding": "require",
            "prepare_threshold": None,
        },
    }
)
MIDDLEWARE = list(MIDDLEWARE)  # noqa: F405
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
    "whitenoise.middleware.WhiteNoiseMiddleware",
)
MIDDLEWARE.append("config.render_media.RejectUploadsMiddleware")
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "config.render_media.UnavailableMediaStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = False
WHITENOISE_AUTOREFRESH = False
SESSION_ENGINE = "django.contrib.sessions.backends.db"
if REDIS_URL:  # noqa: F405
    CACHES["default"]["OPTIONS"].update(
        {  # noqa: F405
            "ssl_cert_reqs": "required",
            "ssl_check_hostname": True,
        }
    )
else:
    # Disposable cache only. Sessions, locks and idempotency remain in PostgreSQL.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
# Do not preload HSTS for unrelated subdomains while validating a new public host.
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
