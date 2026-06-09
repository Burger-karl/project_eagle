"""
Excel Invoice Pipeline
Orchestrates the full flow: parsed Excel rows → FIRS → IRN.
Used by all three intake methods (Folder Watch, Email, Web Upload).
"""

import logging
from django.utils import timezone

from apps.excel_intake.models import (
    ExcelUpload, ExcelInvoice, UploadStatus, ExcelRowStatus
)
from apps.excel_intake.excel_field_mapper import ExcelFieldMapper, MappingError
from apps.firs.client import FIRSClient
from apps.firs.invoice_schema import UBLInvoiceBuilder
from apps.invoices.models import InvoiceType

logger = logging.getLogger("apps.excel_intake")


class ExcelPipeline:
    """
    Processes a parsed Excel file through the full FIRS pipeline.

    Usage:
        pipeline = ExcelPipeline(upload, parse_result)
        pipeline.run()
    """

    def __init__(self, upload: ExcelUpload, parse_result, dry_run: bool = False):
        self.upload      = upload
        self.result      = parse_result
        self.company     = upload.client
        self.dry_run     = dry_run
        self.firs        = FIRSClient(
            api_key=self.company.firs_api_key or None,
            secret_key=self.company.firs_secret_key or None,
        )

    def run(self):
        """Run full pipeline for all valid rows in the parsed Excel file."""
        upload  = self.upload
        result  = self.result

        # Update upload with parse results
        upload.status      = UploadStatus.PARSING
        upload.total_rows  = result.total_rows
        upload.valid_rows  = result.valid_count
        upload.invalid_rows = result.error_count
        upload.parsed_at   = timezone.now()
        upload.save(update_fields=[
            "status", "total_rows", "valid_rows",
            "invalid_rows", "parsed_at"
        ])

        # Save invalid rows for reporting
        for err in result.errors:
            ExcelInvoice.objects.create(
                upload=upload,
                row_number=err["row"],
                status=ExcelRowStatus.INVALID,
                raw_data=err.get("data", {}),
                validation_errors=err.get("errors", []),
            )

        if not result.invoices:
            upload.status = UploadStatus.FAILED
            upload.parse_error = "No valid invoice rows found in file"
            upload.save(update_fields=["status", "parse_error"])
            return

        upload.status = UploadStatus.PROCESSING
        upload.save(update_fields=["status"])

        cleared = 0
        failed  = 0

        for row_data in result.invoices:
            success = self._process_row(row_data)
            if success:
                cleared += 1
            else:
                failed += 1

        # Final status
        if failed == 0:
            upload.status = UploadStatus.COMPLETE
        elif cleared == 0:
            upload.status = UploadStatus.FAILED
        else:
            upload.status = UploadStatus.PARTIAL

        upload.cleared_rows  = cleared
        upload.failed_rows   = failed
        upload.completed_at  = timezone.now()
        upload.save(update_fields=[
            "status", "cleared_rows", "failed_rows", "completed_at"
        ])

        logger.info(
            f"[ExcelPipeline] {upload.original_filename} complete: "
            f"{cleared} cleared, {failed} failed"
        )

    def _process_row(self, row_data: dict) -> bool:
        """Process one invoice row. Returns True on success."""
        row_num = row_data.get("row_number", 0)

        # Create invoice record
        inv_record = ExcelInvoice.objects.create(
            upload=self.upload,
            row_number=row_num,
            status=ExcelRowStatus.VALID,
            raw_data=row_data,
            invoice_number=row_data.get("invoice_number", ""),
            buyer_name=row_data.get("buyer_name", ""),
            buyer_tin=row_data.get("buyer_tin", ""),
            net_amount=row_data.get("net_amount", 0),
            vat_amount=row_data.get("vat_amount", 0),
            gross_amount=row_data.get("gross_amount", 0),
        )

        try:
            # Parse invoice date
            raw_date = row_data.get("invoice_date")
            if raw_date:
                from datetime import date
                if isinstance(raw_date, str):
                    from datetime import datetime
                    inv_record.invoice_date = datetime.strptime(
                        raw_date[:10], "%Y-%m-%d"
                    ).date()
                    inv_record.save(update_fields=["invoice_date"])

            # Map to FIRS UBL
            mapper = ExcelFieldMapper(self.company, row_data)
            mapped = mapper.map()

            inv_record.mapped_data = mapped
            inv_record.status      = ExcelRowStatus.VALID
            inv_record.save(update_fields=["mapped_data", "status"])

            if self.dry_run:
                logger.info(
                    f"[ExcelPipeline] DRY RUN — row {row_num}: "
                    f"{mapped['invoice_number']} mapped OK"
                )
                return True

            # Build UBL payload
            builder = UBLInvoiceBuilder(mapped)
            payload = (
                builder.build_b2c()
                if mapped.get("invoice_type") == InvoiceType.B2C
                else builder.build_b2b()
            )

            # Submit to FIRS via DigiTax
            inv_record.status = ExcelRowStatus.SUBMITTED
            inv_record.submission_attempts += 1
            inv_record.save(update_fields=["status", "submission_attempts"])

            if mapped.get("invoice_type") == InvoiceType.B2C:
                result = self.firs.report_b2c_invoice(payload)
            else:
                result = self.firs.submit_b2b_invoice(payload)

            if result["success"]:
                data = result.get("data", {})
                irn  = data.get("irn") or data.get("IRN", "")
                csid = data.get("csid") or data.get("CSID", "")
                qr   = data.get("qrCode") or data.get("qr_code", "")

                inv_record.status     = ExcelRowStatus.CLEARED
                inv_record.irn        = irn
                inv_record.csid       = csid
                inv_record.qr_code    = qr
                inv_record.cleared_at = timezone.now()
                inv_record.save(update_fields=[
                    "status", "irn", "csid", "qr_code", "cleared_at"
                ])

                logger.info(
                    f"[ExcelPipeline] Row {row_num} cleared: IRN={irn}"
                )
                return True
            else:
                error = result.get("error", "Unknown error")
                inv_record.status        = ExcelRowStatus.FAILED
                inv_record.error_message = str(error)[:1000]
                inv_record.save(update_fields=["status", "error_message"])
                logger.warning(
                    f"[ExcelPipeline] Row {row_num} failed: {error}"
                )
                return False

        except MappingError as e:
            inv_record.status        = ExcelRowStatus.FAILED
            inv_record.error_message = str(e)
            inv_record.validation_errors = [str(e)]
            inv_record.save(update_fields=[
                "status", "error_message", "validation_errors"
            ])
            logger.error(f"[ExcelPipeline] Mapping error row {row_num}: {e}")
            return False

        except Exception as e:
            inv_record.status        = ExcelRowStatus.FAILED
            inv_record.error_message = str(e)
            inv_record.save(update_fields=["status", "error_message"])
            logger.exception(
                f"[ExcelPipeline] Unexpected error row {row_num}: {e}"
            )
            return False
