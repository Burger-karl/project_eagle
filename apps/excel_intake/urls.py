"""Excel Intake URL Configuration."""
from django.urls import path
from apps.excel_intake import views

urlpatterns = [
    path("health/",                    views.ExcelHealthView.as_view(),        name="excel-health"),
    path("upload/",                    views.ExcelUploadView.as_view(),         name="excel-upload"),
    path("uploads/",                   views.ExcelUploadListView.as_view(),     name="excel-upload-list"),
    path("uploads/<uuid:pk>/",         views.ExcelUploadDetailView.as_view(),   name="excel-upload-detail"),
    path("uploads/<uuid:pk>/status/",  views.ExcelUploadStatusView.as_view(),   name="excel-upload-status"),
    path("template/",                  views.DownloadTemplateView.as_view(),    name="excel-template"),
]
