"""`manage.py preview_excel`: read-only, readable, honest about problems."""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core.models import ClientCompany
from apps.excel_intake.models import ExcelInvoice, ExcelUpload
from apps.invoices.models import BuyerTINMapping
from tests.test_excel.lo_fixture import workbook

pytestmark = pytest.mark.django_db


@pytest.fixture
def company():
    co = ClientCompany.objects.create(name="Link Options", tin="12345678-0001", address="Lagos", email="a@b.c",
                                      mssql_server="s", mssql_database="d")
    BuyerTINMapping.objects.create(client=co, sage_account_ref="EXCEL-1", buyer_name="Zenith Beverages Plc",
                                   buyer_tin="40112233-0001")
    return co


def preview(tmp_path, data, name="batch.xls", *args):
    path = tmp_path / name
    path.write_bytes(data)
    out, err = StringIO(), StringIO()
    try:
        call_command("preview_excel", str(path), *args, stdout=out, stderr=err, no_color=True)
    except CommandError as exc:
        return out.getvalue(), str(exc)
    return out.getvalue(), ""


def test_shows_each_invoice_with_its_lines_and_totals(company, tmp_path):
    out, err = preview(tmp_path, workbook())
    assert not err
    assert "2 invoice(s) would be sent, 2 problem(s)" in out
    assert "Sheet 1   Invoice 4521   2026-09-14" in out
    assert "Network cabling" in out and "Switch configuration" in out
    assert "Net 125,000.00   VAT 9,375.00   Total 134,375.00" in out


def test_says_which_invoices_are_business_and_which_consumer(company, tmp_path):
    out, _ = preview(tmp_path, workbook())
    assert "as BUSINESS (buyer TIN 40112233-0001)" in out
    assert "as CONSUMER: no buyer TIN found" in out
    assert "Business-to-business: 1    Consumer: 1" in out


def test_lists_sheets_that_were_not_read_with_the_reason(company, tmp_path):
    out, _ = preview(tmp_path, workbook())
    assert "NOT SENT  (Draft notes)" in out and "NOT SENT  (Quote 4523)" in out
    assert "was not read as an invoice" in out


def test_flags_an_invoice_whose_lines_do_not_add_up(company, tmp_path):
    out, _ = preview(tmp_path, workbook(with_mismatch=True))
    assert "NOT SENT  (INV 4524)" in out and "the lines add up to 10,000.00" in out


def test_saves_nothing_and_never_calls_firs(company, tmp_path):
    with patch("apps.excel_intake.pipeline.get_provider") as firs:
        preview(tmp_path, workbook())
    firs.assert_not_called()
    assert ExcelUpload.objects.count() == 0 and ExcelInvoice.objects.count() == 0


def test_explains_when_no_client_is_available_to_match_tins(tmp_path):
    out, _ = preview(tmp_path, workbook())
    assert "No active client yet" in out and "as CONSUMER" in out


def test_asks_for_a_client_when_several_are_active(company, tmp_path):
    ClientCompany.objects.create(name="Other Ltd", tin="22222222-0001", address="x", email="o@b.c",
                                 mssql_server="s", mssql_database="d")
    out, _ = preview(tmp_path, workbook())
    assert "Several clients are active; add --client" in out
    out, _ = preview(tmp_path, workbook(), "batch.xls", "--client", "Link")
    assert "as BUSINESS" in out


def test_an_unknown_client_name_is_an_error(company, tmp_path):
    _, err = preview(tmp_path, workbook(), "batch.xls", "--client", "Nobody")
    assert "No active client matches 'Nobody'" in err


def test_a_missing_file_is_a_clear_error(company):
    with pytest.raises(CommandError, match="File not found"):
        call_command("preview_excel", "/no/such/file.xls")


def test_an_unreadable_file_is_a_clear_error(company, tmp_path):
    _, err = preview(tmp_path, b"not a spreadsheet", "junk.xls")
    assert "Could not read junk.xls" in err


def test_plain_xlsx_files_are_previewed_too(company, tmp_path):
    import io
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["Invoice Number", "Invoice Date", "Customer", "Description", "Qty", "Unit Price",
                      "Net Amount", "VAT Amount", "Gross Amount"])
    wb.active.append(["S-1", "2026-09-01", "Walk-in", "Widgets", 2, 1000, 2000, 150, 2150])
    buf = io.BytesIO()
    wb.save(buf)
    out, err = preview(tmp_path, buf.getvalue(), "plain.xlsx")
    assert not err and "Invoice S-1" in out and "as CONSUMER" in out
