"""
Regression tests for retrying failed Excel rows.

The retry task tried to create a second row for the same (upload, row number), which
the database forbids, so every retry raised IntegrityError (logged and swallowed) and
no failed row was ever retried. A retry must update the failed row itself, count as
another attempt, and stop after five.
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.core.models import ClientCompany
from apps.excel_intake.models import ExcelInvoice, ExcelRowStatus, ExcelUpload
from apps.excel_intake.tasks import retry_failed_excel_rows
from tests.fake_app import patch_provider

pytestmark = pytest.mark.django_db
RAW = {"row_number": 2, "invoice_number": "S-1", "invoice_date": "2026-09-01", "buyer_name": "Walk-in",
       "description": "Widgets", "quantity": 2, "unit_price": 1000.0, "net_amount": 2000.0,
       "vat_amount": 150.0, "gross_amount": 2150.0}


@pytest.fixture
def failed_row():
    co = ClientCompany.objects.create(name="X", tin="12345678-0001", address="Lagos", email="a@b.c",
                                      mssql_server="s", mssql_database="d")
    upload = ExcelUpload.objects.create(client=co, method="UPLOAD", status="COMPLETE",
                                        original_filename="a.xlsx", file_size_bytes=1)
    return ExcelInvoice.objects.create(upload=upload, row_number=2, status=ExcelRowStatus.FAILED, raw_data=RAW,
                                       mapped_data={"anything": 1}, submission_attempts=1,
                                       invoice_number="S-1", error_message="FIRS timeout")


def firs_returning(result):
    client = MagicMock()
    client.report_b2c_invoice.return_value = result
    return patch_provider("apps.excel_intake.pipeline", client), client


def test_a_successful_retry_updates_the_same_row(failed_row):
    ctx, client = firs_returning({"success": True, "data": {"irn": "IRN-9", "csid": "C"}})
    with ctx:
        retry_failed_excel_rows.run()
    assert ExcelInvoice.objects.count() == 1                       # no duplicate row
    failed_row.refresh_from_db()
    assert failed_row.status == ExcelRowStatus.CLEARED and failed_row.irn == "IRN-9"
    assert failed_row.submission_attempts == 2


def test_a_cleared_row_is_never_sent_again(failed_row):
    ctx, client = firs_returning({"success": True, "data": {"irn": "IRN-9"}})
    with ctx:
        retry_failed_excel_rows.run()
        retry_failed_excel_rows.run()
        retry_failed_excel_rows.run()
    assert client.report_b2c_invoice.call_count == 1               # once, not every 30 minutes


def test_a_failing_retry_counts_attempts_and_stops_at_five(failed_row):
    ctx, client = firs_returning({"success": False, "error": "still down"})
    with ctx:
        for _ in range(8):
            retry_failed_excel_rows.run()
    failed_row.refresh_from_db()
    assert ExcelInvoice.objects.count() == 1
    assert failed_row.status == ExcelRowStatus.FAILED and failed_row.submission_attempts == 5
    assert client.report_b2c_invoice.call_count == 4               # attempts 2, 3, 4, 5, then it stops
