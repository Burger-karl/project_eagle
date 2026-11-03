"""
Excel Invoice Pipeline
Orchestrates the full flow: parsed Excel rows → FIRS → IRN.
Used by all three intake methods (Folder Watch, Email, Web Upload).
"""

import json
import logging

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from apps.excel_intake.models import (
    ExcelUpload, ExcelInvoice, UploadStatus, ExcelRowStatus
)
from apps.excel_intake.excel_field_mapper import ExcelFieldMapper, MappingError
from apps.firs.providers import get_provider

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
        self._provider   = None

    @property
    def provider(self):
        """Access Point Provider adapter (DigiTax, Interswitch, ...) for this client."""
        if self._provider is None:
            self._provider = get_provider(self.company)
        return self._provider

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

    def _process_row(self, row_data: dict, existing: ExcelInvoice = None) -> bool:
        """
        Process one invoice row. Returns True on success.

        `existing`: when retrying a failed row, pass that row so the retry updates it
        (and counts as another attempt) instead of creating a second record. Without
        this a retry added a duplicate row every time, and the original stayed FAILED
        and was sent again even after a retry had cleared it.
        """
        row_num = row_data.get("row_number", 0)

        if existing is not None:
            inv_record = existing
        else:
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

            # The mapper returns Decimal amounts, which a JSON field cannot store. Keep
            # the original `mapped` for the UBL builder and store a JSON-safe copy.
            inv_record.mapped_data = json.loads(json.dumps(mapped, cls=DjangoJSONEncoder))
            inv_record.status      = ExcelRowStatus.VALID
            inv_record.save(update_fields=["mapped_data", "status"])

            if self.dry_run:
                logger.info(
                    f"[ExcelPipeline] DRY RUN — row {row_num}: "
                    f"{mapped['invoice_number']} mapped OK"
                )
                return True

            # Submit via the configured Access Point Provider
            inv_record.status = ExcelRowStatus.SUBMITTED
            inv_record.submission_attempts += 1
            inv_record.app_provider = self.provider.name
            inv_record.save(update_fields=["status", "submission_attempts", "app_provider"])

            submission = self.provider.submit(mapped)

            if submission.success and submission.irn:
                inv_record.status     = ExcelRowStatus.CLEARED
                inv_record.irn        = submission.irn
                inv_record.csid       = submission.csid
                inv_record.qr_code    = submission.qr_code
                inv_record.cleared_at = timezone.now()
                inv_record.provider_invoice_id = submission.provider_invoice_id
                inv_record.provider_status     = submission.provider_status
                inv_record.provider_status_checked_at = timezone.now()
                inv_record.save(update_fields=[
                    "status", "irn", "csid", "qr_code", "cleared_at",
                    "provider_invoice_id", "provider_status", "provider_status_checked_at",
                ])
                irn = submission.irn

                logger.info(
                    f"[ExcelPipeline] Row {row_num} cleared: IRN={irn}"
                )
                return True
            else:
                error = submission.error or "APP response contained no IRN"
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
