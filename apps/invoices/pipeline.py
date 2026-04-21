"""
Invoice Processing Pipeline
The central engine of Project Eagle.

This module orchestrates the complete invoice lifecycle:
    1. Read from Sage 200 (ODBC)
    2. Map to FIRS UBL fields
    3. Build UBL payload
    4. Validate buyer TIN
    5. Submit to FIRS
    6. Save IRN/CSID
    7. Write IRN back to Sage 200

Called by:
    - Celery tasks (background polling, every 5 minutes)
    - REST API views (manual trigger)
    - Management commands (bulk processing)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from django.utils import timezone as dj_timezone

from apps.core.models import ClientCompany
from apps.sage200.odbc_client import Sage200ODBCClient, Sage200ConnectionError
from apps.sage200.field_mapper import Sage200FieldMapper, MappingError
from apps.sage200.irn_writer import Sage200IRNWriter
from apps.firs.client import FIRSClient
from apps.firs.invoice_schema import UBLInvoiceBuilder
from apps.invoices.models import (
    Invoice, InvoiceLine, InvoiceStatus, InvoiceType,
    DocumentType, SubmissionLog
)
from utils.exceptions import PipelineError
from utils.logger import pipeline_logger

logger = logging.getLogger("apps.invoices")


class InvoicePipeline:
    """
    Full invoice processing pipeline for a single client company.

    Usage:
        pipeline = InvoicePipeline(company)
        results  = pipeline.run()
    """

    def __init__(self, company: ClientCompany):
        self.company  = company
        self.odbc     = Sage200ODBCClient(company)
        self.firs     = FIRSClient(
            api_key=company.firs_api_key or None,
            secret_key=company.firs_secret_key or None,
        )
        self.irn_writer = Sage200IRNWriter(company)

    # ──────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ──────────────────────────────────────────────────────────

    def run(self, since: Optional[datetime] = None) -> Dict:
        """
        Full pipeline run for this client:
            1. Pull new invoices from Sage 200
            2. Process each through to FIRS
            3. Return summary results

        Args:
            since: Only process invoices posted after this datetime.
                   If None, processes all pending invoices.

        Returns:
            {
                "pulled":    int,  # invoices read from Sage 200
                "submitted": int,  # successfully submitted to FIRS
                "cleared":   int,  # cleared by FIRS (IRN received)
                "failed":    int,  # failed submissions
                "skipped":   int,  # already processed
            }
        """
        logger.info(
            f"[Pipeline] Starting for client: {self.company.name}",
            extra={"client": self.company.name}
        )

        results = {"pulled": 0, "submitted": 0, "cleared": 0, "failed": 0, "skipped": 0}

        # Step 1: Pull raw invoices from Sage 200 via ODBC
        try:
            raw_invoices = self.odbc.get_new_invoices(since=since)
            results["pulled"] = len(raw_invoices)
            logger.info(f"[Pipeline] Pulled {len(raw_invoices)} invoices from Sage 200")
        except Sage200ConnectionError as e:
            logger.error(f"[Pipeline] Cannot connect to Sage 200: {e}")
            raise PipelineError(f"Sage 200 connection failed: {e}")

        # Step 2: Process each invoice
        for raw in raw_invoices:
            outcome = self._process_single(raw)
            results[outcome] = results.get(outcome, 0) + 1

        logger.info(
            f"[Pipeline] Complete for {self.company.name}: {results}"
        )
        return results

    # ──────────────────────────────────────────────────────────
    # SINGLE INVOICE PROCESSING
    # ──────────────────────────────────────────────────────────

    def _process_single(self, raw: Dict) -> str:
        """
        Process one invoice through the full pipeline.
        Returns: "submitted" | "cleared" | "failed" | "skipped"
        """
        sage_id     = str(raw.get("invoice_id", ""))
        invoice_num = raw.get("invoice_number", "?")

        # ── Check: Already processed? ─────────────────────────
        if Invoice.objects.filter(
            client=self.company,
            sage_invoice_id=sage_id
        ).exists():
            logger.debug(f"[Pipeline] Invoice {invoice_num} already in system — skipping")
            return "skipped"

        invoice = None
        try:
            # ── Step A: Map Sage 200 fields → FIRS UBL ────────
            mapper = Sage200FieldMapper(self.company, raw)
            mapped = mapper.map()

            # ── Step B: Validate buyer TIN (B2B only) ─────────
            if mapped.get("buyer_tin"):
                tin_result = self.firs.validate_tin(mapped["buyer_tin"])
                if not tin_result["success"]:
                    logger.warning(
                        f"[Pipeline] Buyer TIN invalid for {invoice_num}: "
                        f"{mapped['buyer_tin']}"
                    )
                    # Don't block — log warning and continue as B2C
                    mapped["buyer_tin"] = ""

            # ── Step C: Save invoice record to our DB ─────────
            invoice = self._save_invoice(mapped, sage_id, raw)

            # ── Step D: Build UBL payload ──────────────────────
            builder = UBLInvoiceBuilder(mapped)
            if mapped.get("invoice_type") == InvoiceType.B2C:
                payload = builder.build_b2c()
            else:
                payload = builder.build_b2b()

            # Save payload for audit trail
            invoice.ubl_payload = payload
            invoice.status = InvoiceStatus.SUBMITTING
            invoice.submission_attempts += 1
            invoice.last_attempt_at = dj_timezone.now()
            invoice.save(update_fields=[
                "ubl_payload", "status",
                "submission_attempts", "last_attempt_at"
            ])

            # ── Step E: Submit to FIRS ─────────────────────────
            if mapped.get("invoice_type") == InvoiceType.B2C:
                result = self.firs.report_b2c_invoice(payload)
            else:
                result = self.firs.submit_b2b_invoice(payload)

            # Log this submission attempt
            self._log_submission(invoice, payload, result)

            # ── Step F: Handle FIRS response ───────────────────
            if result["success"]:
                return self._handle_success(invoice, result["data"], mapped)
            else:
                return self._handle_failure(invoice, result)

        except MappingError as e:
            logger.error(f"[Pipeline] Mapping error for {invoice_num}: {e}")
            if invoice:
                self._mark_failed(invoice, "MAPPING_ERROR", str(e))
            return "failed"

        except Exception as e:
            logger.exception(f"[Pipeline] Unexpected error for {invoice_num}: {e}")
            if invoice:
                self._mark_failed(invoice, "UNKNOWN_ERROR", str(e))
            return "failed"

    # ──────────────────────────────────────────────────────────
    # SUCCESS HANDLER
    # ──────────────────────────────────────────────────────────

    def _handle_success(self, invoice: Invoice, firs_data: Dict, mapped: Dict) -> str:
        """
        FIRS cleared the invoice — save IRN/CSID and write back to Sage 200.
        """
        irn  = firs_data.get("irn", "") or firs_data.get("IRN", "")
        csid = firs_data.get("csid", "") or firs_data.get("CSID", "")
        qr   = firs_data.get("qrCode", "") or firs_data.get("qr_code", "")

        logger.info(
            f"[Pipeline] FIRS cleared invoice {invoice.sage_invoice_number}. "
            f"IRN: {irn}"
        )

        # Update invoice record
        invoice.status       = InvoiceStatus.CLEARED
        invoice.irn          = irn
        invoice.csid         = csid
        invoice.qr_code      = qr
        invoice.cleared_at   = dj_timezone.now()
        invoice.firs_response = firs_data
        invoice.error_code    = ""
        invoice.error_message = ""
        invoice.save(update_fields=[
            "status", "irn", "csid", "qr_code",
            "cleared_at", "firs_response",
            "error_code", "error_message"
        ])

        # Write IRN back to Sage 200
        try:
            self.irn_writer.write_irn(
                sage_invoice_id=invoice.sage_invoice_id,
                sage_invoice_number=invoice.sage_invoice_number,
                irn=irn,
                csid=csid,
            )
            invoice.status = InvoiceStatus.WRITTEN_BACK
            invoice.written_back_at = dj_timezone.now()
            invoice.save(update_fields=["status", "written_back_at"])
            logger.info(
                f"[Pipeline] IRN written back to Sage 200 for {invoice.sage_invoice_number}"
            )
        except Exception as e:
            # Write-back failed — invoice is still cleared, just not written back yet
            # Celery retry will attempt write-back again
            logger.error(
                f"[Pipeline] IRN write-back failed for {invoice.sage_invoice_number}: {e}"
            )

        return "cleared"

    # ──────────────────────────────────────────────────────────
    # FAILURE HANDLER
    # ──────────────────────────────────────────────────────────

    def _handle_failure(self, invoice: Invoice, result: Dict) -> str:
        """FIRS rejected or errored — mark for retry."""
        error = result.get("error", {})
        if isinstance(error, dict):
            code = error.get("code", error.get("errorCode", "FIRS_ERROR"))
            msg  = error.get("message", str(error))
        else:
            code = "FIRS_ERROR"
            msg  = str(error)

        logger.warning(
            f"[Pipeline] FIRS rejected {invoice.sage_invoice_number}: {code} — {msg}"
        )

        self._mark_failed(invoice, code, msg)

        # Schedule retry with exponential back-off
        from apps.invoices.tasks import retry_single_invoice
        attempts = invoice.submission_attempts
        delay_seconds = min(300 * (2 ** (attempts - 1)), 3600)  # max 1 hour
        retry_single_invoice.apply_async(
            args=[str(invoice.id)],
            countdown=delay_seconds
        )

        return "failed"

    # ──────────────────────────────────────────────────────────
    # DATABASE HELPERS
    # ──────────────────────────────────────────────────────────

    def _save_invoice(self, mapped: Dict, sage_id: str, raw: Dict) -> Invoice:
        """Create the Invoice record in the middleware database."""
        invoice = Invoice.objects.create(
            client=self.company,
            sage_invoice_id=sage_id,
            sage_invoice_number=mapped["invoice_number"],
            sage_account_ref=raw.get("account_ref", ""),
            invoice_type=mapped.get("invoice_type", InvoiceType.B2B),
            document_type=mapped.get("document_type", DocumentType.INVOICE),
            invoice_date=mapped["invoice_date"],
            due_date=mapped.get("due_date"),
            currency_code="NGN",
            supplier_tin=mapped["supplier_tin"],
            supplier_name=mapped["supplier_name"],
            supplier_address=mapped["supplier_address"],
            buyer_tin=mapped.get("buyer_tin", ""),
            buyer_name=mapped["buyer_name"],
            buyer_address=mapped["buyer_address"],
            net_amount=mapped["net_amount"],
            vat_amount=mapped["vat_amount"],
            gross_amount=mapped["gross_amount"],
            original_currency=mapped.get("original_currency", "NGN"),
            exchange_rate=mapped.get("exchange_rate"),
            status=InvoiceStatus.PENDING,
        )

        # Save line items
        for line_data in mapped.get("lines", []):
            InvoiceLine.objects.create(
                invoice=invoice,
                line_number=int(line_data["line_number"]),
                description=line_data["description"],
                quantity=line_data["quantity"],
                unit_code=line_data.get("unit_code", "EA"),
                unit_price=line_data["unit_price"],
                line_net_amount=line_data["line_net_amount"],
                vat_rate=line_data.get("vat_rate", 7.5),
                vat_amount=line_data["vat_amount"],
                tax_category=line_data.get("tax_category", "S"),
            )

        logger.debug(
            f"[Pipeline] Saved invoice {invoice.sage_invoice_number} to middleware DB"
        )
        return invoice

    def _mark_failed(self, invoice: Invoice, error_code: str, error_message: str):
        """Mark invoice as failed and schedule next retry time."""
        from datetime import timedelta
        attempts = invoice.submission_attempts
        next_retry = dj_timezone.now() + timedelta(
            seconds=min(300 * (2 ** attempts), 3600)
        )
        Invoice.objects.filter(id=invoice.id).update(
            status=InvoiceStatus.FAILED,
            error_code=error_code[:50],
            error_message=error_message[:1000],
            next_retry_at=next_retry,
        )

    def _log_submission(self, invoice: Invoice, payload: Dict, result: Dict):
        """Write a full audit log entry for this submission attempt."""
        SubmissionLog.objects.create(
            invoice=invoice,
            attempt_number=invoice.submission_attempts,
            endpoint=f"/invoices/{'b2c' if invoice.is_b2c else 'b2b'}",
            request_payload=payload,
            response_status=result.get("status_code"),
            response_body=result.get("data") or result.get("error"),
            success=result.get("success", False),
            duration_ms=result.get("duration_ms"),
            error="" if result.get("success") else str(result.get("error", "")),
        )
