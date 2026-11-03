"""Link Options print-style .xls invoices flowing through the real intake pipeline."""
import hashlib
import io
from unittest.mock import MagicMock, patch

import pytest
from openpyxl import Workbook

from apps.core.models import ClientCompany
from apps.excel_intake.excel_parser import ExcelParseError, ExcelParser
from apps.excel_intake.models import ExcelInvoice, ExcelRowStatus, ExcelUpload
from apps.excel_intake.parsing import LinkOptionsFileParser, get_parser, normalise_name
from apps.excel_intake.pipeline import ExcelPipeline
from apps.invoices.models import BuyerTINMapping
from tests.test_excel.lo_fixture import workbook
from tests.fake_app import patch_provider

pytestmark = pytest.mark.django_db
PIPE = "apps.excel_intake.pipeline"


@pytest.fixture
def company():
    co = ClientCompany.objects.create(
        name="Link Options", tin="12345678-0001", address="Lagos", email="lo@example.com",
        mssql_server="s", mssql_database="d",
    )
    BuyerTINMapping.objects.create(client=co, sage_account_ref="EXCEL-ZENITH",
                                   buyer_name="Zenith Beverages Plc", buyer_tin="40112233-0001")
    return co


@pytest.fixture
def firs():
    client = MagicMock()
    ok = {"success": True, "data": {"irn": "IRN-OK", "csid": "CSID", "qrCode": ""}}
    client.submit_b2b_invoice.return_value = ok
    client.report_b2c_invoice.return_value = ok
    with patch_provider(PIPE, client):
        yield client


def upload_for(company, name="batch.xls", **extra):
    return ExcelUpload.objects.create(client=company, method="UPLOAD", status="RECEIVED",
                                      original_filename=name, file_size_bytes=1, **extra)


# ── choosing the parser ──────────────────────────────────────

def test_xls_goes_to_the_link_options_parser():
    assert isinstance(get_parser(workbook(), "invoices.xls"), LinkOptionsFileParser)


def test_xlsx_and_csv_go_to_the_standard_parser():
    buf = io.BytesIO()
    Workbook().save(buf)
    assert isinstance(get_parser(buf.getvalue(), "invoices.xlsx"), ExcelParser)
    assert isinstance(get_parser(b"a,b\n1,2\n", "invoices.csv"), ExcelParser)


def test_an_xlsx_wrongly_named_xls_still_gets_the_standard_parser():
    buf = io.BytesIO()
    Workbook().save(buf)
    assert isinstance(get_parser(buf.getvalue(), "oops.XLS"), ExcelParser)


def test_extension_check_ignores_case():
    assert isinstance(get_parser(workbook(), "BATCH.XLS"), LinkOptionsFileParser)


# ── what the adapter returns ─────────────────────────────────

def test_each_invoice_sheet_becomes_a_row(company):
    result = get_parser(workbook(), "batch.xls", client=company).parse()
    assert [i["invoice_number"] for i in result.invoices] == ["4521", "4522"]
    assert [i["row_number"] for i in result.invoices] == [1, 2]       # the sheet's place in the workbook
    assert result.invoices[0]["invoice_date"] == "2026-09-14"          # plain text, safe to store as JSON
    assert result.invoices[0]["net_amount"] == 125000.0 and result.invoices[0]["gross_amount"] == 134375.0
    assert result.file_hash == hashlib.sha256(workbook()).hexdigest()
    assert result.column_map["format"] == "link_options_xls"


def test_buyer_tin_is_found_by_customer_name(company):
    zenith, walk_in = get_parser(workbook(), "batch.xls", client=company).parse().invoices
    assert zenith["buyer_tin"] == "40112233-0001"     # 'Zenith Beverages Plc,' matched despite the comma
    assert walk_in["buyer_tin"] == ""


def test_without_a_client_no_tins_are_looked_up():
    assert get_parser(workbook(), "batch.xls").parse().invoices[0]["buyer_tin"] == ""


@pytest.mark.parametrize("a,b", [("Zenith Bank Plc.", "ZENITH  BANK PLC"), (" Acme, Ltd ", "acme ltd")])
def test_customer_names_are_compared_loosely(a, b):
    assert normalise_name(a) == normalise_name(b)


def test_sheets_that_were_not_read_are_reported_with_a_reason(company):
    result = get_parser(workbook(), "batch.xls", client=company).parse()
    unread = {e["data"]["sheet"]: e for e in result.errors}
    assert set(unread) == {"Draft notes", "Quote 4523"}                # 'Sheet1' is skipped by name on purpose
    assert "was not read as an invoice" in unread["Draft notes"]["errors"][0]
    assert unread["Draft notes"]["row"] == 4
    assert result.total_rows == 4


def test_an_invoice_whose_lines_do_not_add_up_is_held_back_not_sent(company):
    result = get_parser(workbook(with_mismatch=True), "batch.xls", client=company).parse()
    assert "4524" not in [i["invoice_number"] for i in result.invoices]
    held = [e for e in result.errors if e["data"].get("invoice_number") == "4524"][0]
    assert held["data"]["lines_total"] == 10000.0 and held["data"]["invoice_net"] == 9000.0
    assert "Nothing was sent to FIRS" in held["errors"][0]


def test_a_file_with_no_invoices_is_a_clear_error():
    with pytest.raises(ExcelParseError, match="Cannot open|No valid invoice"):
        get_parser(b"this is not a spreadsheet at all", "junk.xls").parse()


# ── the mapper builds real lines ─────────────────────────────

def test_a_two_item_invoice_becomes_two_lines_that_add_up(company):
    from apps.excel_intake.excel_field_mapper import ExcelFieldMapper
    row = get_parser(workbook(), "batch.xls", client=company).parse().invoices[0]
    lines = ExcelFieldMapper(company, row).map()["lines"]
    assert [l["description"] for l in lines] == ["Network cabling", "Switch configuration"]
    for l in lines:                                                    # quantity x price equals the line amount
        assert round(l["quantity"] * l["unit_price"], 2) == l["line_net_amount"]
    assert sum(l["line_net_amount"] for l in lines) == 125000.0
    assert round(sum(l["vat_amount"] for l in lines), 2) == 9375.0     # line VAT adds up to the invoice VAT


def test_line_vat_rounding_is_absorbed_by_the_last_line(company):
    from apps.excel_intake.excel_field_mapper import ExcelFieldMapper
    row = {"invoice_number": "1", "invoice_date": "2026-09-01", "buyer_name": "X", "net_amount": 100.0,
           "vat_amount": 7.5, "gross_amount": 107.5, "vat_rate": 7.5, "row_number": 1,
           "lines": [{"quantity": 1, "description": "a", "line_net_amount": 33.33},
                     {"quantity": 1, "description": "b", "line_net_amount": 33.33},
                     {"quantity": 1, "description": "c", "line_net_amount": 33.34}]}
    lines = ExcelFieldMapper(company, row).map()["lines"]
    assert round(sum(l["vat_amount"] for l in lines), 2) == 7.5


def test_ordinary_rows_without_lines_still_map_to_one_line(company):
    from apps.excel_intake.excel_field_mapper import ExcelFieldMapper
    row = {"invoice_number": "9", "invoice_date": "2026-09-01", "buyer_name": "X", "description": "Widgets",
           "quantity": 3, "unit_price": 100.0, "net_amount": 300.0, "vat_amount": 22.5, "gross_amount": 322.5,
           "row_number": 1}
    lines = ExcelFieldMapper(company, row).map()["lines"]
    assert len(lines) == 1 and lines[0]["description"] == "Widgets"


# ── end to end through the real pipeline ─────────────────────

def test_the_pipeline_reports_business_and_consumer_invoices_correctly(company, firs):
    upload = upload_for(company)
    result = get_parser(workbook(), "batch.xls", client=company).parse()
    ExcelPipeline(upload, result).run()

    rows = {r.row_number: r for r in ExcelInvoice.objects.filter(upload=upload)}
    assert rows[1].status == ExcelRowStatus.CLEARED and rows[2].status == ExcelRowStatus.CLEARED
    assert rows[4].status == ExcelRowStatus.INVALID and rows[5].status == ExcelRowStatus.INVALID
    assert "was not read as an invoice" in rows[4].validation_errors[0]
    firs.submit_b2b_invoice.assert_called_once()                        # Zenith, TIN known
    firs.report_b2c_invoice.assert_called_once()                        # walk-in, no TIN
    sent = firs.submit_b2b_invoice.call_args.args[0]
    assert len(sent["InvoiceLine"]) == 2
    assert sent["LegalMonetaryTotal"]["TaxInclusiveAmount"]["value"] == 134375.0


def test_a_held_back_invoice_never_reaches_firs(company, firs):
    upload = upload_for(company)
    ExcelPipeline(upload, get_parser(workbook(with_mismatch=True, with_extras=False), "b.xls", client=company).parse()).run()
    assert firs.submit_b2b_invoice.call_count + firs.report_b2c_invoice.call_count == 2   # 4521 and 4522 only
    held = ExcelInvoice.objects.get(upload=upload, status=ExcelRowStatus.INVALID)
    assert "lines add up to" in held.validation_errors[0]


def test_the_web_upload_task_reads_xls_files(company, firs):
    from apps.excel_intake.tasks import process_excel_upload
    upload = upload_for(company)
    process_excel_upload.run(str(upload.id), file_bytes_hex=workbook().hex())
    upload.refresh_from_db()
    assert ExcelInvoice.objects.filter(upload=upload, status=ExcelRowStatus.CLEARED).count() == 2
    assert upload.cleared_rows == 2


def test_the_web_upload_task_still_reads_xlsx_files(company, firs):
    from apps.excel_intake.tasks import process_excel_upload
    wb = Workbook()
    wb.active.append(["Invoice Number", "Invoice Date", "Customer", "Description", "Qty", "Unit Price", "VAT"])
    wb.active.append(["S-1", "2026-09-01", "Walk-in", "Widgets", 2, 1000, 150])
    buf = io.BytesIO()
    wb.save(buf)
    upload = upload_for(company, "plain.xlsx")
    process_excel_upload.run(str(upload.id), file_bytes_hex=buf.getvalue().hex())
    assert ExcelInvoice.objects.filter(upload=upload).count() >= 1


def test_all_three_intake_paths_use_the_parser_selector():
    """Upload, watched folder and email must not bypass it, or .xls files would break in one of them."""
    import inspect
    from apps.excel_intake import email_agent, folder_watcher, tasks
    for module in (tasks, folder_watcher, email_agent):
        source = inspect.getsource(module)
        assert "get_parser(" in source and "ExcelParser(" not in source, module.__name__
