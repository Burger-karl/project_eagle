"""
Excel Intake Models
Tracks every Excel file received through all three intake methods:
    Method A — Folder Watch (local file drop)
    Method B — Email Ingestion (email attachment)
    Method C — Web Upload Portal (browser upload)

Every Excel file creates one ExcelUpload record.
Each invoice row inside the file creates one ExcelInvoice record.
"""

import uuid
from django.db import models
from apps.core.models import TimeStampedModel, ClientCompany


class IntakeMethod(models.TextChoices):
    FOLDER_WATCH   = "FOLDER",  "Folder Watch (auto-pickup)"
    EMAIL_INGESTION = "EMAIL",  "Email Ingestion (attachment)"
    WEB_UPLOAD     = "UPLOAD",  "Web Upload Portal (browser)"


class UploadStatus(models.TextChoices):
    RECEIVED   = "RECEIVED",   "Received — file received, not yet parsed"
    PARSING    = "PARSING",    "Parsing — reading Excel rows"
    PARSED     = "PARSED",     "Parsed — rows extracted, ready to submit"
    PROCESSING = "PROCESSING", "Processing — submitting to FIRS"
    COMPLETE   = "COMPLETE",   "Complete — all invoices cleared"
    PARTIAL    = "PARTIAL",    "Partial — some invoices failed"
    FAILED     = "FAILED",     "Failed — parsing or submission error"


class ExcelRowStatus(models.TextChoices):
    PENDING   = "PENDING",  "Pending"
    VALID     = "VALID",    "Valid — ready to submit"
    INVALID   = "INVALID",  "Invalid — validation error"
    SUBMITTED = "SUBMITTED", "Submitted to FIRS"
    CLEARED   = "CLEARED",  "Cleared — IRN received"
    FAILED    = "FAILED",   "Failed — submission error"


class ExcelUpload(TimeStampedModel):
    """
    One record per Excel file received.
    Tracks the file's journey from receipt through to full clearance.
    """
    client          = models.ForeignKey(
        ClientCompany, on_delete=models.PROTECT, related_name="excel_uploads"
    )
    method          = models.CharField(
        max_length=10, choices=IntakeMethod.choices,
        help_text="How this file was received"
    )
    status          = models.CharField(
        max_length=15, choices=UploadStatus.choices,
        default=UploadStatus.RECEIVED, db_index=True
    )

    # File info
    original_filename = models.CharField(max_length=255)
    file_path         = models.CharField(
        max_length=500, blank=True,
        help_text="Path on server where the file is stored"
    )
    file_size_bytes   = models.IntegerField(null=True, blank=True)
    file_hash         = models.CharField(
        max_length=64, blank=True,
        help_text="SHA256 hash — prevents duplicate processing"
    )

    # Email-specific (Method B)
    sender_email    = models.EmailField(blank=True)
    email_subject   = models.CharField(max_length=500, blank=True)
    email_message_id = models.CharField(max_length=255, blank=True, db_index=True)

    # Parsing results
    total_rows      = models.IntegerField(default=0, help_text="Total invoice rows found")
    valid_rows      = models.IntegerField(default=0)
    invalid_rows    = models.IntegerField(default=0)
    cleared_rows    = models.IntegerField(default=0)
    failed_rows     = models.IntegerField(default=0)

    # Error tracking
    parse_error     = models.TextField(blank=True)
    parsed_at       = models.DateTimeField(null=True, blank=True)
    completed_at    = models.DateTimeField(null=True, blank=True)

    # Upload metadata (Method C — web upload)
    uploaded_by_ip  = models.GenericIPAddressField(null=True, blank=True)
    uploaded_by_user = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name        = "Excel Upload"
        verbose_name_plural = "Excel Uploads"
        ordering            = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["file_hash"]),
            models.Index(fields=["method", "status"]),
        ]

    def __str__(self):
        return (
            f"{self.original_filename} | {self.method} | "
            f"{self.status} | {self.total_rows} rows"
        )

    @property
    def is_duplicate(self):
        """Check if this file was already processed (same hash)."""
        return ExcelUpload.objects.filter(
            client=self.client,
            file_hash=self.file_hash,
            status=UploadStatus.COMPLETE,
        ).exclude(id=self.id).exists()

    @property
    def completion_rate(self):
        if not self.total_rows:
            return 0
        return round((self.cleared_rows / self.total_rows) * 100, 1)


class ExcelInvoice(TimeStampedModel):
    """
    One record per invoice row extracted from an Excel file.
    Each row goes through the full FIRS submission pipeline.
    """
    upload          = models.ForeignKey(
        ExcelUpload, on_delete=models.CASCADE, related_name="invoices"
    )
    row_number      = models.IntegerField(help_text="Row number in the Excel file")
    status          = models.CharField(
        max_length=15, choices=ExcelRowStatus.choices,
        default=ExcelRowStatus.PENDING, db_index=True
    )

    # Raw extracted data from Excel
    raw_data        = models.JSONField(
        help_text="Raw column values extracted from the Excel row"
    )

    # Mapped UBL data (after field mapping)
    mapped_data     = models.JSONField(
        null=True, blank=True,
        help_text="Mapped FIRS UBL fields ready for submission"
    )

    # Invoice fields (populated after parsing)
    invoice_number  = models.CharField(max_length=100, blank=True)
    invoice_date    = models.DateField(null=True, blank=True)
    buyer_name      = models.CharField(max_length=255, blank=True)
    buyer_tin       = models.CharField(max_length=50, blank=True)
    net_amount      = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    vat_amount      = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    gross_amount    = models.DecimalField(max_digits=18, decimal_places=2, null=True)

    # FIRS results
    irn             = models.CharField(max_length=255, blank=True, db_index=True)
    csid            = models.CharField(max_length=500, blank=True)
    qr_code         = models.TextField(blank=True)
    cleared_at      = models.DateTimeField(null=True, blank=True)

    # Error tracking
    validation_errors = models.JSONField(default=list)
    error_message     = models.TextField(blank=True)
    submission_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name        = "Excel Invoice Row"
        verbose_name_plural = "Excel Invoice Rows"
        ordering            = ["upload", "row_number"]
        unique_together     = [["upload", "row_number"]]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["irn"]),
        ]

    def __str__(self):
        return (
            f"Row {self.row_number} | {self.invoice_number or 'no number'} | "
            f"{self.status}"
        )


class FolderWatchConfig(TimeStampedModel):
    """
    Configuration for Method A (Folder Watch).
    One record per watched folder per client.
    """
    client          = models.ForeignKey(
        ClientCompany, on_delete=models.CASCADE, related_name="folder_watches"
    )
    is_active       = models.BooleanField(default=True)
    watch_path      = models.CharField(
        max_length=500,
        help_text="Full path to watch folder e.g. C:\\InvoiceDropbox\\ or /home/invoices/"
    )
    processed_path  = models.CharField(
        max_length=500, blank=True,
        help_text="Where to move processed files e.g. C:\\InvoiceDropbox\\Processed\\"
    )
    failed_path     = models.CharField(
        max_length=500, blank=True,
        help_text="Where to move failed files"
    )
    poll_interval   = models.IntegerField(
        default=30, help_text="How often to check folder in seconds"
    )
    last_polled_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Folder Watch Config"

    def __str__(self):
        return f"{self.client.name} → {self.watch_path}"


class EmailIngestConfig(TimeStampedModel):
    """
    Configuration for Method B (Email Ingestion).
    IMAP credentials for the dedicated invoice inbox.
    """
    client          = models.ForeignKey(
        ClientCompany, on_delete=models.CASCADE,
        related_name="email_configs",
        null=True, blank=True,
        help_text="Leave blank for shared Link Options inbox"
    )
    is_active       = models.BooleanField(default=True)
    imap_host       = models.CharField(
        max_length=255, default="imap.gmail.com"
    )
    imap_port       = models.IntegerField(default=993)
    imap_use_ssl    = models.BooleanField(default=True)
    email_address   = models.EmailField(
        help_text="The inbox to monitor e.g. invoices@linkoptions.com"
    )
    email_password  = models.CharField(
        max_length=255,
        help_text="App password (not account password)"
    )
    inbox_folder    = models.CharField(
        max_length=100, default="INBOX"
    )
    processed_folder = models.CharField(
        max_length=100, default="Processed",
        help_text="IMAP folder to move processed emails to"
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_uid        = models.CharField(
        max_length=50, blank=True,
        help_text="Last processed email UID — prevents reprocessing"
    )

    class Meta:
        verbose_name = "Email Ingest Config"

    def __str__(self):
        return f"{self.email_address} (IMAP)"
