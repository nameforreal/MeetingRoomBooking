from django.contrib import admin
from django.urls import path, include
from .health import health
from .metrics import metrics
from .openapi import schema, docs

urlpatterns = [
    path("health/", health, name="health"),
    path("metrics/", metrics, name="metrics"),
    path("api/schema/", schema, name="openapi-schema"),
    path("api/docs/", docs, name="openapi-docs"),
    path('', include('dashboard.urls')),
    path("admin/", admin.site.urls),
    path("management/", include("management.urls")),
    path("api/", include("rooms.api_urls")),
    path("", include("rooms.urls")),
]

from django.conf import settings
from django.conf.urls.static import static
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
