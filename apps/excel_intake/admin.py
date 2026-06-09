"""Excel Intake Admin Panel."""
from django.contrib import admin
from django.utils.html import format_html
from apps.excel_intake.models import (
    ExcelUpload, ExcelInvoice, FolderWatchConfig, EmailIngestConfig
)


class ExcelInvoiceInline(admin.TabularInline):
    model       = ExcelInvoice
    extra       = 0
    can_delete  = False
    readonly_fields = [
        "row_number", "status", "invoice_number", "buyer_name",
        "gross_amount", "irn", "error_message", "submission_attempts"
    ]


@admin.register(ExcelUpload)
class ExcelUploadAdmin(admin.ModelAdmin):
    list_display  = [
        "original_filename", "client", "method_badge", "status_badge",
        "total_rows", "cleared_rows", "failed_rows", "completion_rate",
        "created_at"
    ]
    list_filter   = ["status", "method", "client"]
    search_fields = ["original_filename", "sender_email"]
    readonly_fields = [
        "id", "file_hash", "parsed_at", "completed_at", "created_at"
    ]
    inlines = [ExcelInvoiceInline]

    def status_badge(self, obj):
        colours = {
            "RECEIVED":   "#888", "PARSING": "#2196F3",
            "PARSED":     "#9C27B0", "PROCESSING": "#FF9800",
            "COMPLETE":   "#4CAF50", "PARTIAL": "#FF5722",
            "FAILED":     "#F44336",
        }
        c = colours.get(obj.status, "#888")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            c, obj.status
        )
    status_badge.short_description = "Status"

    def method_badge(self, obj):
        colours = {
            "FOLDER": "#1B3A6B", "EMAIL": "#2E75B6", "UPLOAD": "#1A6B3A"
        }
        c = colours.get(obj.method, "#888")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 6px;'
            'border-radius:4px;font-size:11px">{}</span>',
            c, obj.get_method_display()
        )
    method_badge.short_description = "Method"


@admin.register(FolderWatchConfig)
class FolderWatchConfigAdmin(admin.ModelAdmin):
    list_display = [
        "client", "watch_path", "is_active",
        "poll_interval", "last_polled_at"
    ]
    list_filter  = ["is_active", "client"]


@admin.register(EmailIngestConfig)
class EmailIngestConfigAdmin(admin.ModelAdmin):
    list_display = [
        "email_address", "client", "imap_host",
        "is_active", "last_checked_at"
    ]
    list_filter  = ["is_active"]
    readonly_fields = ["last_checked_at", "last_uid"]
