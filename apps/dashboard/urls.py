"""Dashboard URLs. Mounted at /dashboard/ in config/urls.py."""
from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="index"),
    path("api/daily/", views.DailyStatsView.as_view(), name="daily"),
    path("api/clients/", views.ClientListView.as_view(), name="clients"),
    path("api/sage/preview/", views.SagePreviewView.as_view(), name="sage-preview"),
    path("api/sage/pull/", views.SagePullView.as_view(), name="sage-pull"),
]
