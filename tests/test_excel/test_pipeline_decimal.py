"""
Regression test: the Excel pipeline must be able to store what the mapper returns.

The mapper returns Decimal amounts and the pipeline saved them into a JSONField, which
raised "Object of type Decimal is not JSON serializable". Every row, in every file
format, ended up FAILED before anything was sent to FIRS.
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.core.models import ClientCompany
from apps.excel_intake.excel_parser import ParseResult
from apps.excel_intake.models import ExcelInvoice, ExcelRowStatus, ExcelUpload
from apps.excel_intake.pipeline import ExcelPipeline

pytestmark = pytest.mark.django_db


def test_a_plain_standard_format_row_is_stored_and_sent():
    co = ClientCompany.objects.create(name="X", tin="12345678-0001", address="Lagos", email="a@b.c",
                                      mssql_server="s", mssql_database="d")
    upload = ExcelUpload.objects.create(client=co, method="UPLOAD", status="RECEIVED",
                                        original_filename="a.xlsx", file_size_bytes=1)
    row = {"row_number": 2, "invoice_number": "S-1", "invoice_date": "2026-09-01", "buyer_name": "Walk-in",
           "description": "Widgets", "quantity": 2, "unit_price": 1000.0, "net_amount": 2000.0,
           "vat_amount": 150.0, "gross_amount": 2150.0}
    firs = MagicMock()
    firs.report_b2c_invoice.return_value = {"success": True, "data": {"irn": "IRN-1", "csid": "C"}}
    with patch("apps.excel_intake.pipeline.FIRSClient", return_value=firs):
        ExcelPipeline(upload, ParseResult([row], [], "hash", 1, {})).run()

    saved = ExcelInvoice.objects.get(upload=upload)
    assert saved.status == ExcelRowStatus.CLEARED and saved.irn == "IRN-1"
    assert saved.mapped_data["gross_amount"] == "2150.0"            # stored as text, not lost
    sent = firs.report_b2c_invoice.call_args.args[0]
    assert sent["LegalMonetaryTotal"]["PayableAmount"]["value"] == 2150.0
