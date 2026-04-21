from django.urls import path
from apps.reporting import views
urlpatterns = [
    path("stats/", views.ReportingStatsView.as_view(), name="reporting-stats"),
]
