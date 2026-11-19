"""Bounded labels, no user/request data; shared counters across Gunicorn workers."""

import hmac
import os
from time import monotonic

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.views.decorators.http import require_GET
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from redis.exceptions import RedisError

REQUESTS = Counter("mrb_http_requests_total", "HTTP responses", ["method", "route", "status"])
LATENCY = Histogram(
    "mrb_http_duration_seconds",
    "HTTP latency",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)


class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = monotonic()
        response = self.get_response(request)
        match = request.resolver_match
        route = match.route if match else "unmatched"
        method = (
            request.method
            if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            else "OTHER"
        )
        REQUESTS.labels(method, route, str(response.status_code)).inc()
        LATENCY.labels(method, route).observe(monotonic() - started)
        return response


class Dependencies:
    def collect(self):
        started = monotonic()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            ready = 1
        except DatabaseError:
            ready = 0
        yield GaugeMetricFamily("mrb_database_up", "SQL readiness", value=ready)
        yield GaugeMetricFamily(
            "mrb_database_probe_seconds", "SQL probe duration", value=monotonic() - started
        )
        try:
            cache.get("metrics-readiness")
            ready = 1
        except RedisError:
            ready = 0
        yield GaugeMetricFamily("mrb_cache_up", "Cache readiness", value=ready)


@require_GET
def metrics(request):
    token = getattr(settings, "METRICS_TOKEN", "")
    authorization = request.headers.get("Authorization", "")
    if (
        not token
        or not authorization.isascii()
        or not token.isascii()
        or len(authorization) != len(token) + len("Bearer ")
        or not hmac.compare_digest(authorization, "Bearer " + token)
    ):
        return HttpResponse(status=404)
    registry = CollectorRegistry()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.MultiProcessCollector(registry)
    else:
        registry.register(REQUESTS)
        registry.register(LATENCY)
    registry.register(Dependencies())
    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)
