"""Project Eagle URL Configuration."""
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

urlpatterns = [
    path("admin/",    admin.site.urls),

    # ── API Endpoints ─────────────────────────────────────────
    path("api/invoices/",  include("apps.invoices.urls")),
    path("api/reporting/", include("apps.reporting.urls")),

    # ── API Documentation ─────────────────────────────────────
    path("api/schema/",   SpectacularAPIView.as_view(),   name="schema"),
    path("api/docs/",     SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/",    SpectacularRedocView.as_view(url_name="schema"),   name="redoc"),
]
