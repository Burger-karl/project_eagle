"""
Sage 300 Invoice Processing Pipeline
Complete Sage 300 → FIRS → IRN write-back orchestration.
Mirrors the Sage 200 pipeline pattern exactly.
"""

import logging
from datetime import datetime
from typing import Dict, Optional

from django.utils import timezone as dj_timezone

from apps.core.models import ClientCompany
from apps.sage300.odbc_client import Sage300ODBCClient, Sage300ConnectionError
from apps.sage300.field_mapper import Sage300FieldMapper, MappingError
from apps.sage300.irn_writer import Sage300IRNWriter
from apps.firs.client import FIRSClient
from apps.firs.invoice_schema import UBLInvoiceBuilder
from apps.invoices.models import (
    Invoice, InvoiceLine, InvoiceStatus,
    InvoiceType, DocumentType, SubmissionLog
)
from utils.exceptions import PipelineError

logger = logging.getLogger("apps.invoices")


class Sage300Pipeline:
    """
    Full invoice processing pipeline for a Sage 300 client.

    Usage:
        pipeline = Sage300Pipeline(company)
        results  = pipeline.run()
    """

    def __init__(self, company: ClientCompany):
        self.company    = company
        self.odbc       = Sage300ODBCClient(company)
        self.firs       = FIRSClient(
            api_key=company.firs_api_key or None,
            secret_key=company.firs_secret_key or None,
        )
        self.irn_writer = Sage300IRNWriter(company)
        self.source     = "SAGE300"

    def run(self, since: Optional[datetime] = None) -> Dict:
        """
        Full pipeline run — pull Sage 300 invoices → submit to FIRS → write IRN.
        """
        logger.info(f"[Sage300 Pipeline] Starting for: {self.company.name}")
        results = {
            "pulled": 0, "submitted": 0,
            "cleared": 0, "failed": 0, "skipped": 0
        }

        try:
            raw_invoices = self.odbc.get_new_invoices(since=since)
            results["pulled"] = len(raw_invoices)
            logger.info(
                f"[Sage300 Pipeline] Pulled {len(raw_invoices)} invoices"
            )
        except Sage300ConnectionError as e:
            logger.error(f"[Sage300 Pipeline] Cannot connect to Sage 300: {e}")
            raise PipelineError(f"Sage 300 connection failed: {e}")

        for raw in raw_invoices:
            outcome = self._process_single(raw)
            results[outcome] = results.get(outcome, 0) + 1

        logger.info(f"[Sage300 Pipeline] Complete: {results}")
        return results

    def _process_single(self, raw: Dict) -> str:
        """Process one Sage 300 invoice through the full pipeline."""
        sage_id     = str(raw.get("invoice_id", ""))
        invoice_num = raw.get("invoice_number", "?")

        # Check if already processed
        if Invoice.objects.filter(
            client=self.company,
            sage_invoice_id=sage_id,
            source_system=self.source,
        ).exists():
            logger.debug(f"[Sage300 Pipeline] {invoice_num} already processed — skipping")
            return "skipped"

        invoice = None
        try:
            # Map fields
            mapper = Sage300FieldMapper(self.company, raw)
            mapped = mapper.map()

            # Validate buyer TIN
            if mapped.get("buyer_tin"):
                tin_result = self.firs.validate_tin(mapped["buyer_tin"])
                if not tin_result["success"]:
                    logger.warning(
                        f"[Sage300 Pipeline] Invalid buyer TIN for {invoice_num}"
                    )
                    mapped["buyer_tin"] = ""

            # Save to middleware DB
            invoice = self._save_invoice(mapped, sage_id, raw)

            # Build UBL payload
            builder = UBLInvoiceBuilder(mapped)
            payload = (
                builder.build_b2c()
                if mapped.get("invoice_type") == InvoiceType.B2C
                else builder.build_b2b()
            )

            invoice.ubl_payload         = payload
            invoice.status              = InvoiceStatus.SUBMITTING
            invoice.submission_attempts += 1
            invoice.last_attempt_at     = dj_timezone.now()
            invoice.save(update_fields=[
                "ubl_payload", "status",
                "submission_attempts", "last_attempt_at"
            ])

            # Submit to FIRS
            result = (
                self.firs.report_b2c_invoice(payload)
                if mapped.get("invoice_type") == InvoiceType.B2C
                else self.firs.submit_b2b_invoice(payload)
            )

            # Log submission
            self._log_submission(invoice, payload, result)

            if result["success"]:
                return self._handle_success(invoice, result["data"], raw)
            else:
                return self._handle_failure(invoice, result)

        except MappingError as e:
            logger.error(f"[Sage300 Pipeline] Mapping error for {invoice_num}: {e}")
            if invoice:
                self._mark_failed(invoice, "MAPPING_ERROR", str(e))
            return "failed"

        except Exception as e:
            logger.exception(f"[Sage300 Pipeline] Error for {invoice_num}: {e}")
            if invoice:
                self._mark_failed(invoice, "UNKNOWN_ERROR", str(e))
            return "failed"

    def _handle_success(self, invoice: Invoice, firs_data: Dict, raw: Dict) -> str:
        """FIRS cleared invoice — save IRN and write back."""
        irn = firs_data.get("irn") or firs_data.get("IRN", "")
        csid = firs_data.get("csid") or firs_data.get("CSID", "")
        qr   = firs_data.get("qrCode") or firs_data.get("qr_code", "")

        logger.info(
            f"[Sage300 Pipeline] FIRS cleared {invoice.sage_invoice_number}. "
            f"IRN: {irn}"
        )

        Invoice.objects.filter(id=invoice.id).update(
            status=InvoiceStatus.CLEARED,
            irn=irn, csid=csid, qr_code=qr,
            cleared_at=dj_timezone.now(),
            firs_response=firs_data,
            error_code="", error_message="",
        )

        # Write IRN back to Sage 300
        try:
            self.irn_writer.write_irn(
                invoice_number=invoice.sage_invoice_number,
                batch_number=str(raw.get("batch_number", "")),
                entry_number=str(raw.get("entry_number", "")),
                irn=irn, csid=csid,
            )
            Invoice.objects.filter(id=invoice.id).update(
                status=InvoiceStatus.WRITTEN_BACK,
                written_back_at=dj_timezone.now(),
            )
        except Exception as e:
            logger.error(
                f"[Sage300 Pipeline] IRN write-back failed for "
                f"{invoice.sage_invoice_number}: {e}"
            )

        return "cleared"

    def _handle_failure(self, invoice: Invoice, result: Dict) -> str:
        """FIRS rejected — mark for retry."""
        error = result.get("error", {})
        code  = (error.get("code", "FIRS_ERROR")
                 if isinstance(error, dict) else "FIRS_ERROR")
        msg   = (error.get("message", str(error))
                 if isinstance(error, dict) else str(error))

        self._mark_failed(invoice, code, msg)

        from apps.invoices.tasks import retry_single_invoice
        attempts     = invoice.submission_attempts
        delay        = min(300 * (2 ** (attempts - 1)), 3600)
        retry_single_invoice.apply_async(
            args=[str(invoice.id)], countdown=delay
        )
        return "failed"

    def _save_invoice(self, mapped: Dict, sage_id: str, raw: Dict) -> Invoice:
        """Save Sage 300 invoice to middleware database."""
        invoice = Invoice.objects.create(
            client=self.company,
            source_system=self.source,
            sage_invoice_id=sage_id,
            sage_invoice_number=mapped["invoice_number"],
            sage_account_ref=raw.get("customer_id", ""),
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

        return invoice

    def _mark_failed(self, invoice: Invoice, code: str, msg: str):
        from datetime import timedelta
        next_retry = dj_timezone.now() + timedelta(
            seconds=min(300 * (2 ** invoice.submission_attempts), 3600)
        )
        Invoice.objects.filter(id=invoice.id).update(
            status=InvoiceStatus.FAILED,
            error_code=code[:50],
            error_message=msg[:1000],
            next_retry_at=next_retry,
        )

    def _log_submission(self, invoice: Invoice, payload: Dict, result: Dict):
        SubmissionLog.objects.create(
            invoice=invoice,
            attempt_number=invoice.submission_attempts,
            endpoint=(
                f"/invoices/"
                f"{'b2c' if invoice.is_b2c else 'b2b'}"
            ),
            request_payload=payload,
            response_status=result.get("status_code"),
            response_body=result.get("data") or result.get("error"),
            success=result.get("success", False),
            duration_ms=result.get("duration_ms"),
            error="" if result.get("success") else str(result.get("error", "")),
        )
