"""Readiness without sensitive diagnostics; Redis is not business storage."""

from uuid import uuid4

from django.core.cache import cache
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis.exceptions import RedisError


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    key = "health:" + uuid4().hex
    try:
        cache.set(key, "ready", timeout=10)
        cache_ready = cache.get(key) == "ready"
        cache.delete(key)
    except RedisError:
        cache_ready = False
    response = JsonResponse({"status": "ok", "cache": "ok" if cache_ready else "unavailable"})
    response["Cache-Control"] = "no-store"
    return response
