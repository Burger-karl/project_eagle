"""
Django Admin — Invoice models
Full admin interface for monitoring invoices, submissions and TIN mappings.
"""
from django.contrib import admin
from django.utils.html import format_html
from apps.invoices.models import Invoice, InvoiceLine, BuyerTINMapping, SubmissionLog


class InvoiceLineInline(admin.TabularInline):
    model  = InvoiceLine
    extra  = 0
    readonly_fields = ["line_number", "description", "quantity", "unit_price",
                       "line_net_amount", "vat_rate", "vat_amount", "tax_category"]
    can_delete = False


class SubmissionLogInline(admin.TabularInline):
    model  = SubmissionLog
    extra  = 0
    readonly_fields = ["attempt_number", "endpoint", "response_status",
                       "success", "duration_ms", "error", "created_at"]
    can_delete = False
    ordering   = ["-created_at"]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display  = [
        "sage_invoice_number", "client", "invoice_type", "invoice_date",
        "buyer_name", "gross_amount", "status_badge", "submission_attempts",
        "irn_short", "cleared_at"
    ]
    list_filter   = ["status", "invoice_type", "document_type", "client", "invoice_date"]
    search_fields = ["sage_invoice_number", "buyer_name", "buyer_tin", "irn"]
    readonly_fields = [
        "id", "created_at", "updated_at", "ubl_payload", "firs_response",
        "irn", "csid", "qr_code", "cleared_at", "written_back_at",
        "submission_attempts", "last_attempt_at", "next_retry_at",
    ]
    ordering      = ["-invoice_date", "-created_at"]
    date_hierarchy = "invoice_date"
    inlines       = [InvoiceLineInline, SubmissionLogInline]

    fieldsets = (
        ("Invoice Identity", {
            "fields": ("id", "client", "source_system", "sage_invoice_id",
                       "sage_invoice_number", "sage_account_ref")
        }),
        ("Invoice Header", {
            "fields": ("invoice_type", "document_type", "invoice_date",
                       "due_date", "currency_code", "original_currency", "exchange_rate")
        }),
        ("Supplier", {
            "fields": ("supplier_tin", "supplier_name", "supplier_address")
        }),
        ("Buyer", {
            "fields": ("buyer_tin", "buyer_name", "buyer_address")
        }),
        ("Amounts", {
            "fields": ("net_amount", "vat_amount", "gross_amount")
        }),
        ("FIRS Status", {
            "fields": ("status", "submission_attempts", "last_attempt_at", "next_retry_at",
                       "irn", "csid", "cleared_at", "written_back_at",
                       "error_code", "error_message")
        }),
        ("Payloads (Technical)", {
            "classes": ("collapse",),
            "fields": ("ubl_payload", "firs_response", "qr_code")
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at")
        }),
    )

    def status_badge(self, obj):
        colours = {
            "PENDING":      "#888",
            "SUBMITTING":   "#2196F3",
            "CLEARED":      "#4CAF50",
            "WRITTEN_BACK": "#1B5E20",
            "REJECTED":     "#FF5722",
            "FAILED":       "#F44336",
            "CANCELLED":    "#9E9E9E",
        }
        colour = colours.get(obj.status, "#888")
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            colour, obj.status
        )
    status_badge.short_description = "Status"

    def irn_short(self, obj):
        if obj.irn:
            return obj.irn[:20] + "…" if len(obj.irn) > 20 else obj.irn
        return "—"
    irn_short.short_description = "IRN"


@admin.register(BuyerTINMapping)
class BuyerTINMappingAdmin(admin.ModelAdmin):
    list_display  = ["sage_account_ref", "buyer_name", "buyer_tin",
                     "client", "is_validated", "validated_at"]
    list_filter   = ["is_validated", "client"]
    search_fields = ["sage_account_ref", "buyer_name", "buyer_tin"]
    readonly_fields = ["id", "created_at", "updated_at", "validated_at"]


@admin.register(SubmissionLog)
class SubmissionLogAdmin(admin.ModelAdmin):
    list_display  = ["invoice", "attempt_number", "endpoint",
                     "response_status", "success", "duration_ms", "created_at"]
    list_filter   = ["success", "response_status"]
    search_fields = ["invoice__sage_invoice_number"]
    readonly_fields = ["id", "created_at", "updated_at"]
