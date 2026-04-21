"""
Invoice Models
Tracks every invoice from Sage 200 through FIRS clearance and back.
This is the central table of the entire middleware.
"""
from django.db import models
from apps.core.models import TimeStampedModel, ClientCompany


class InvoiceStatus(models.TextChoices):
    PENDING     = "PENDING",     "Pending — waiting to be submitted to FIRS"
    SUBMITTING  = "SUBMITTING",  "Submitting — currently being sent to FIRS"
    CLEARED     = "CLEARED",     "Cleared — FIRS approved, IRN issued"
    REJECTED    = "REJECTED",    "Rejected — FIRS rejected, see error detail"
    FAILED      = "FAILED",      "Failed — submission error, will retry"
    CANCELLED   = "CANCELLED",   "Cancelled — invoice cancelled in Sage 200"
    WRITTEN_BACK = "WRITTEN_BACK", "Complete — IRN written back to Sage 200"


class InvoiceType(models.TextChoices):
    B2B = "B2B", "Business to Business"
    B2C = "B2C", "Business to Consumer"
    B2G = "B2G", "Business to Government"


class DocumentType(models.TextChoices):
    INVOICE     = "380", "Commercial Invoice"
    CREDIT_NOTE = "381", "Credit Note"
    DEBIT_NOTE  = "383", "Debit Note"


class Invoice(TimeStampedModel):
    """
    Master invoice record.
    One row per invoice pulled from Sage 200.
    Tracks the full lifecycle from Sage → FIRS → IRN write-back.
    """
    # ── Client & Source ───────────────────────────────────────
    client          = models.ForeignKey(ClientCompany, on_delete=models.PROTECT, related_name="invoices")
    source_system   = models.CharField(max_length=50, default="SAGE200")

    # ── Sage 200 Identifiers ──────────────────────────────────
    sage_invoice_id      = models.CharField(max_length=100, help_text="Sage 200 internal invoice ID (SLPostedCustomerTrans.ID)")
    sage_invoice_number  = models.CharField(max_length=100, help_text="Invoice number visible in Sage 200")
    sage_account_ref     = models.CharField(max_length=50,  help_text="Customer account reference in Sage 200")

    # ── Invoice Header ────────────────────────────────────────
    invoice_type    = models.CharField(max_length=3,  choices=InvoiceType.choices,  default=InvoiceType.B2B)
    document_type   = models.CharField(max_length=3,  choices=DocumentType.choices, default=DocumentType.INVOICE)
    invoice_date    = models.DateField()
    due_date        = models.DateField(null=True, blank=True)
    currency_code   = models.CharField(max_length=3, default="NGN")

    # ── Supplier (your client) ────────────────────────────────
    supplier_tin    = models.CharField(max_length=50)
    supplier_name   = models.CharField(max_length=255)
    supplier_address = models.TextField()

    # ── Buyer ─────────────────────────────────────────────────
    buyer_tin       = models.CharField(max_length=50, blank=True, help_text="Required for B2B — validate before submission")
    buyer_name      = models.CharField(max_length=255)
    buyer_address   = models.TextField()

    # ── Amounts ───────────────────────────────────────────────
    net_amount      = models.DecimalField(max_digits=18, decimal_places=2, help_text="Total before VAT (NGN)")
    vat_amount      = models.DecimalField(max_digits=18, decimal_places=2, help_text="Total VAT (NGN)")
    gross_amount    = models.DecimalField(max_digits=18, decimal_places=2, help_text="Total payable including VAT")
    original_currency = models.CharField(max_length=3, blank=True, help_text="Original currency if not NGN")
    exchange_rate   = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    # ── FIRS Submission ───────────────────────────────────────
    status          = models.CharField(max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.PENDING, db_index=True)
    ubl_payload     = models.JSONField(null=True, blank=True, help_text="Full UBL JSON payload sent to FIRS")
    firs_response   = models.JSONField(null=True, blank=True, help_text="Raw response from FIRS API")
    submission_attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at   = models.DateTimeField(null=True, blank=True)

    # ── FIRS Results (populated after clearance) ──────────────
    irn             = models.CharField(max_length=255, blank=True, db_index=True, help_text="Invoice Reference Number from FIRS")
    csid            = models.CharField(max_length=500, blank=True, help_text="Cryptographic Stamp Identifier")
    qr_code         = models.TextField(blank=True, help_text="QR code data for B2C invoices")
    cleared_at      = models.DateTimeField(null=True, blank=True, help_text="When FIRS cleared the invoice")
    written_back_at = models.DateTimeField(null=True, blank=True, help_text="When IRN was written back to Sage 200")

    # ── Error Tracking ────────────────────────────────────────
    error_code      = models.CharField(max_length=50, blank=True)
    error_message   = models.TextField(blank=True)

    # ── Reference to original (for credit/debit notes) ────────
    original_invoice = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="corrections",
        help_text="For credit/debit notes — points to original cleared invoice"
    )

    class Meta:
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"
        ordering = ["-invoice_date", "-created_at"]
        unique_together = [["client", "sage_invoice_id"]]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["client", "invoice_date"]),
            models.Index(fields=["irn"]),
            models.Index(fields=["next_retry_at"]),
        ]

    def __str__(self):
        return f"{self.sage_invoice_number} | {self.status} | {self.gross_amount} NGN"

    @property
    def is_retryable(self):
        """An invoice can be retried if it failed and hasn't exceeded 5 attempts."""
        return self.status == InvoiceStatus.FAILED and self.submission_attempts < 5

    @property
    def is_b2b(self):
        return self.invoice_type == InvoiceType.B2B

    @property
    def is_b2c(self):
        return self.invoice_type == InvoiceType.B2C


class InvoiceLine(TimeStampedModel):
    """
    Individual line items on an invoice.
    Mapped from Sage 200's SLPostedCustomerTransLine table.
    """
    invoice         = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    line_number     = models.PositiveSmallIntegerField()
    description     = models.CharField(max_length=500)
    quantity        = models.DecimalField(max_digits=18, decimal_places=4)
    unit_code       = models.CharField(max_length=10, default="EA", help_text="UN/ECE unit code")
    unit_price      = models.DecimalField(max_digits=18, decimal_places=4)
    line_net_amount = models.DecimalField(max_digits=18, decimal_places=2)
    vat_rate        = models.DecimalField(max_digits=6, decimal_places=2, default=7.5)
    vat_amount      = models.DecimalField(max_digits=18, decimal_places=2)
    tax_category    = models.CharField(max_length=5, default="S", help_text="S=Standard, Z=Zero, E=Exempt")

    class Meta:
        ordering = ["line_number"]
        unique_together = [["invoice", "line_number"]]

    def __str__(self):
        return f"Line {self.line_number}: {self.description} × {self.quantity}"


class BuyerTINMapping(TimeStampedModel):
    """
    Maps Sage 200 customer account references to their FIRS TINs.
    This solves the 'buyer TIN gap' — Sage doesn't store TINs natively.
    The accounts team populates this table for each customer.
    """
    client          = models.ForeignKey(ClientCompany, on_delete=models.CASCADE, related_name="tin_mappings")
    sage_account_ref = models.CharField(max_length=50, help_text="Customer account reference in Sage 200")
    buyer_name      = models.CharField(max_length=255)
    buyer_tin       = models.CharField(max_length=50, help_text="FIRS TIN for this buyer")
    is_validated    = models.BooleanField(default=False, help_text="True once validated against FIRS TIN API")
    validated_at    = models.DateTimeField(null=True, blank=True)
    notes           = models.TextField(blank=True)

    class Meta:
        verbose_name = "Buyer TIN Mapping"
        verbose_name_plural = "Buyer TIN Mappings"
        unique_together = [["client", "sage_account_ref"]]

    def __str__(self):
        return f"{self.sage_account_ref} → {self.buyer_tin}"


class SubmissionLog(TimeStampedModel):
    """
    Full audit trail of every API call made to FIRS.
    Every attempt, success, failure, and retry is recorded.
    Required for NITDA compliance and client reporting.
    """
    invoice         = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="submission_logs")
    attempt_number  = models.PositiveSmallIntegerField()
    endpoint        = models.CharField(max_length=255)
    request_payload = models.JSONField()
    response_status = models.IntegerField(null=True, blank=True)
    response_body   = models.JSONField(null=True, blank=True)
    success         = models.BooleanField(default=False)
    duration_ms     = models.IntegerField(null=True, blank=True, help_text="API call duration in milliseconds")
    error           = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        status = "✅" if self.success else "❌"
        return f"{status} Attempt {self.attempt_number} for {self.invoice.sage_invoice_number}"
