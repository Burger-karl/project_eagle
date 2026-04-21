"""Serializers — convert model instances to/from JSON for the REST API."""

from rest_framework import serializers
from apps.invoices.models import Invoice, InvoiceLine, SubmissionLog, BuyerTINMapping
from apps.core.models import ClientCompany


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model  = InvoiceLine
        fields = [
            "line_number", "description", "quantity", "unit_code",
            "unit_price", "line_net_amount", "vat_rate", "vat_amount",
            "tax_category",
        ]


class SubmissionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SubmissionLog
        fields = [
            "attempt_number", "endpoint", "response_status",
            "success", "duration_ms", "error", "created_at",
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    line_count  = serializers.IntegerField(source="lines.count", read_only=True)

    class Meta:
        model  = Invoice
        fields = [
            "id", "client_name", "sage_invoice_number",
            "invoice_type", "document_type", "invoice_date",
            "buyer_name", "buyer_tin",
            "net_amount", "vat_amount", "gross_amount", "currency_code",
            "status", "irn", "submission_attempts",
            "cleared_at", "written_back_at",
            "error_code", "error_message",
            "line_count", "created_at",
        ]


class InvoiceDetailSerializer(serializers.ModelSerializer):
    client_name    = serializers.CharField(source="client.name", read_only=True)
    lines          = InvoiceLineSerializer(many=True, read_only=True)
    submission_logs = SubmissionLogSerializer(many=True, read_only=True)

    class Meta:
        model  = Invoice
        fields = "__all__"


class BuyerTINMappingSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)

    class Meta:
        model  = BuyerTINMapping
        fields = [
            "id", "client", "client_name", "sage_account_ref",
            "buyer_name", "buyer_tin", "is_validated", "validated_at", "notes",
        ]


class ClientCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model  = ClientCompany
        fields = [
            "id", "name", "tin", "address", "email", "is_active",
            "mssql_server", "mssql_database",
            "sage_site_id", "sage_company_id",
        ]
        # Never expose credentials in API responses
        read_only_fields = ["id"]
