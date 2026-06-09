"""
Excel Intake — Serializers
"""
from rest_framework import serializers
from apps.excel_intake.models import ExcelUpload, ExcelInvoice


class ExcelInvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ExcelInvoice
        fields = [
            "id", "row_number", "status", "invoice_number",
            "invoice_date", "buyer_name", "buyer_tin",
            "net_amount", "vat_amount", "gross_amount",
            "irn", "csid", "cleared_at",
            "validation_errors", "error_message",
            "submission_attempts",
        ]


class ExcelUploadSerializer(serializers.ModelSerializer):
    client_name     = serializers.CharField(source="client.name", read_only=True)
    completion_rate = serializers.FloatField(read_only=True)

    class Meta:
        model  = ExcelUpload
        fields = [
            "id", "client_name", "method", "status",
            "original_filename", "file_size_bytes",
            "total_rows", "valid_rows", "invalid_rows",
            "cleared_rows", "failed_rows", "completion_rate",
            "sender_email", "uploaded_by_ip",
            "parse_error", "parsed_at", "completed_at",
            "created_at",
        ]


class ExcelUploadDetailSerializer(serializers.ModelSerializer):
    client_name     = serializers.CharField(source="client.name", read_only=True)
    completion_rate = serializers.FloatField(read_only=True)
    invoices        = ExcelInvoiceSerializer(many=True, read_only=True)

    class Meta:
        model  = ExcelUpload
        fields = "__all__"


class ExcelUploadStatusSerializer(serializers.ModelSerializer):
    completion_rate = serializers.FloatField(read_only=True)

    class Meta:
        model  = ExcelUpload
        fields = [
            "id", "status", "total_rows", "valid_rows",
            "cleared_rows", "failed_rows", "completion_rate",
            "completed_at",
        ]
