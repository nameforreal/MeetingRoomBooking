"""Validated cloud inputs. Never interpolate secret values into diagnostics."""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def required(env, name):
    value = env.get(name, "")
    if not value or any(ord(char) < 32 for char in value):
        raise ImproperlyConfigured(f"Configure {name} in the service environment.")
    return value


def hostname(value):
    return bool(
        len(value) <= 253
        and re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", value
        )
        and all(len(part) <= 63 for part in value.split("."))
    )


def cloud_environment(env):
    host = required(env, "NEON_HOST")
    if not hostname(host) or not host.startswith("ep-") or not host.endswith(".neon.tech"):
        raise ImproperlyConfigured("NEON_HOST must be a Neon endpoint hostname, not a URL.")
    if "-pooler." in host:
        raise ImproperlyConfigured("Use the Neon Direct endpoint for this deployment profile.")
    public_host = required(env, "RENDER_EXTERNAL_HOSTNAME")
    hosts = list(dict.fromkeys([public_host, *env.get("DJANGO_ALLOWED_HOSTS", "").split(",")]))
    hosts = [item.strip() for item in hosts if item.strip()]
    if not all(hostname(item) for item in hosts):
        raise ImproperlyConfigured(
            "Render hosts must be explicit DNS names without schemes or wildcards."
        )
    redis_url = env.get("UPSTASH_REDIS_URL", "")
    if redis_url:
        try:
            redis = urlsplit(redis_url)
            valid = (
                redis.scheme == "rediss"
                and redis.hostname
                and redis.hostname.endswith(".upstash.io")
                and redis.password
                and redis.port
                and redis.path in ("", "/0")
                and not redis.query
                and not redis.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ImproperlyConfigured(
                "UPSTASH_REDIS_URL must be a TLS Redis URL without query options."
            )
    return {
        "DJANGO_READ_DOT_ENV": "false",
        "DJANGO_ENV": "production",
        "DJANGO_DEBUG": "false",
        "DJANGO_SECRET_KEY": required(env, "DJANGO_SECRET_KEY"),
        "DJANGO_ALLOWED_HOSTS": ",".join(hosts),
        "DJANGO_CSRF_TRUSTED_ORIGINS": ",".join("https://" + item for item in hosts),
        "DJANGO_SESSION_COOKIE_SECURE": "true",
        "DJANGO_CSRF_COOKIE_SECURE": "true",
        "DJANGO_SECURE_SSL_REDIRECT": "true",
        "DJANGO_TRUST_PROXY": "true",
        "DB_ENGINE": "postgres",
        "POSTGRES_HOST": host,
        "POSTGRES_DB": required(env, "NEON_DATABASE"),
        "POSTGRES_USER": required(env, "NEON_USER"),
        "POSTGRES_PASSWORD": required(env, "NEON_PASSWORD"),
        "POSTGRES_PORT": "5432",
        "REDIS_URL": redis_url,
    }


def gunicorn_bind(env):
    port = env.get("PORT", "10000")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1024 <= int(port) <= 65535:
        raise ImproperlyConfigured("PORT must be an integer between 1024 and 65535.")
    return "0.0.0.0:" + str(int(port))
