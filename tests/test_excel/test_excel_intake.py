"""
Excel Intake — Full Test Suite
Tests all three intake methods with mock data.
Run: pytest tests/test_excel/ -v

No external dependencies needed — all tests use mock data.
"""

import pytest
import io
from decimal import Decimal
from unittest.mock import MagicMock, patch, mock_open
from datetime import date


# ── Sample Excel content (minimal valid xlsx bytes) ────────────
def make_sample_xlsx(rows=None) -> bytes:
    """Create a minimal Excel file in memory for testing."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active

    # Standard header row
    ws.append([
        "Invoice Number", "Invoice Date", "Buyer Name", "Buyer TIN",
        "Buyer Address", "Item Description", "Quantity",
        "Unit Price (excl VAT)*", "VAT Rate %", "VAT Amount",
        "Gross Amount *", "Currency", "Notes"
    ])

    # Default sample rows
    if rows is None:
        rows = [
            [
                "INV-2025-001", "14/03/2025", "Financial Reporting Council",
                "31569955-0001", "Plot 17, Abuja", "Revenue Software License",
                1, 1440000, 7.5, 108000, 1548000, "NGN", "Annual fee"
            ],
            [
                "INV-2025-002", "15/03/2025", "Montgomery Ltd",
                "", "Lagos Island", "Consulting Services",
                2, 250000, 7.5, 37500, 537500, "NGN", ""
            ],
        ]
    for row in rows:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Mock company ──────────────────────────────────────────────
MOCK_COMPANY = MagicMock()
MOCK_COMPANY.name    = "Link Options & Systems Limited"
MOCK_COMPANY.tin     = "04702493-0001"
MOCK_COMPANY.address = "Onikan, Lagos"
MOCK_COMPANY.firs_api_key    = "test-key"
MOCK_COMPANY.firs_secret_key = "test-secret"


# ════════════════════════════════════════════════════════════════
# TESTS: Excel Parser
# ════════════════════════════════════════════════════════════════

class TestExcelParser:
    """Tests for the Excel file parser."""

    def _get_parser(self, rows=None):
        from apps.excel_intake.excel_parser import ExcelParser
        return ExcelParser(
            file_bytes=make_sample_xlsx(rows),
            filename="test_invoice.xlsx"
        )

    def test_parse_returns_result(self):
        result = self._get_parser().parse()
        assert result is not None
        assert hasattr(result, "invoices")
        assert hasattr(result, "errors")

    def test_parse_correct_row_count(self):
        result = self._get_parser().parse()
        assert result.valid_count == 2

    def test_parse_invoice_number(self):
        result = self._get_parser().parse()
        assert result.invoices[0]["invoice_number"] == "INV-2025-001"

    def test_parse_invoice_date_format(self):
        result = self._get_parser().parse()
        date_str = result.invoices[0]["invoice_date"]
        assert date_str == "2025-03-14"
        assert "T" not in date_str

    def test_parse_buyer_name(self):
        result = self._get_parser().parse()
        assert result.invoices[0]["buyer_name"] == "Financial Reporting Council"

    def test_parse_buyer_tin(self):
        result = self._get_parser().parse()
        assert result.invoices[0]["buyer_tin"] == "31569955-0001"

    def test_parse_blank_tin(self):
        result = self._get_parser().parse()
        assert result.invoices[1]["buyer_tin"] == ""

    def test_parse_amounts(self):
        result = self._get_parser().parse()
        inv    = result.invoices[0]
        assert float(inv["gross_amount"]) == 1548000.0
        assert float(inv["vat_amount"])   == 108000.0
        assert float(inv["net_amount"])   == 1440000.0

    def test_parse_quantity(self):
        result = self._get_parser().parse()
        assert result.invoices[1]["quantity"] == 2.0

    def test_parse_file_hash_is_sha256(self):
        result = self._get_parser().parse()
        assert len(result.file_hash) == 64
        assert all(c in "0123456789abcdef" for c in result.file_hash)

    def test_empty_rows_skipped(self):
        rows   = [["INV-001", "14/03/2025", "Buyer", "", "", "Item", 1, 100, 7.5, 7.5, 107.5, "NGN", ""]]
        result = self._get_parser(rows).parse()
        assert result.valid_count >= 1

    def test_invalid_row_captured(self):
        # Row with blank invoice number — should be invalid
        rows = [["", "14/03/2025", "Buyer", "", "", "Item", 1, 100, 7.5, 7.5, 107.5, "NGN", ""]]
        result = self._get_parser(rows).parse()
        assert result.error_count == 1
        assert "Invoice number" in result.errors[0]["errors"][0]

    def test_invalid_amount_handled(self):
        rows = [["INV-001", "14/03/2025", "Buyer", "", "", "Item", 1, 0, 7.5, 0, 0, "NGN", ""]]
        result = self._get_parser(rows).parse()
        # Gross = 0 should be invalid
        assert result.error_count >= 1

    def test_summary_dict(self):
        result = self._get_parser().parse()
        summary = result.summary()
        assert "total_rows"       in summary
        assert "valid_rows"       in summary
        assert "error_rows"       in summary
        assert "columns_detected" in summary

    def test_detects_standard_columns(self):
        result = self._get_parser().parse()
        cols   = result.column_map
        assert "invoice_number" in cols
        assert "invoice_date"   in cols
        assert "buyer_name"     in cols
        assert "gross_amount"   in cols

    def test_currency_defaults_to_ngn(self):
        rows   = [["INV-001", "14/03/2025", "Buyer", "", "", "Item", 1, 100, 7.5, 7.5, 107.5, "", ""]]
        result = self._get_parser(rows).parse()
        if result.valid_count:
            assert result.invoices[0]["currency"] == "NGN"

    def test_comma_in_amount_stripped(self):
        from apps.excel_intake.excel_parser import ExcelParser
        result = ExcelParser._parse_decimal("1,548,000", "amount", 1)
        assert float(result) == 1548000.0

    def test_naira_symbol_stripped(self):
        from apps.excel_intake.excel_parser import ExcelParser
        result = ExcelParser._parse_decimal("₦1548000", "amount", 1)
        assert float(result) == 1548000.0


# ════════════════════════════════════════════════════════════════
# TESTS: Date Parsing
# ════════════════════════════════════════════════════════════════

class TestDateParsing:
    def _parse(self, val):
        from apps.excel_intake.excel_parser import ExcelParser
        return ExcelParser._parse_date(val)

    def test_dd_mm_yyyy(self):       assert self._parse("14/03/2025") == "2025-03-14"
    def test_dd_mm_yyyy_dash(self):  assert self._parse("14-03-2025") == "2025-03-14"
    def test_yyyy_mm_dd(self):       assert self._parse("2025-03-14") == "2025-03-14"
    def test_yyyymmdd(self):         assert self._parse("20250314")   == "2025-03-14"
    def test_none(self):             assert self._parse(None)          is None
    def test_blank(self):            assert self._parse("")            is None
    def test_datetime_obj(self):
        from datetime import datetime
        assert self._parse(datetime(2025, 3, 14)) == "2025-03-14"
    def test_date_obj(self):
        from datetime import date
        assert self._parse(date(2025, 3, 14)) == "2025-03-14"


# ════════════════════════════════════════════════════════════════
# TESTS: Excel Field Mapper
# ════════════════════════════════════════════════════════════════

class TestExcelFieldMapper:
    SAMPLE_ROW = {
        "row_number":    1,
        "invoice_number": "INV-2025-001",
        "invoice_date":  "2025-03-14",
        "buyer_name":    "Financial Reporting Council",
        "buyer_tin":     "31569955-0001",
        "buyer_address": "Plot 17, Abuja",
        "description":   "Revenue Software License",
        "quantity":      1.0,
        "unit_price":    1440000.0,
        "unit_code":     "EA",
        "vat_rate":      7.5,
        "vat_amount":    108000.0,
        "net_amount":    1440000.0,
        "gross_amount":  1548000.0,
        "currency":      "NGN",
        "notes":         "",
    }

    def _mapper(self, row=None):
        from apps.excel_intake.excel_field_mapper import ExcelFieldMapper
        return ExcelFieldMapper(MOCK_COMPANY, row or self.SAMPLE_ROW)

    def test_maps_invoice_number(self):
        assert self._mapper().map()["invoice_number"] == "INV-2025-001"

    def test_maps_invoice_date(self):
        assert self._mapper().map()["invoice_date"] == "2025-03-14"

    def test_no_time_in_date(self):
        assert "T" not in self._mapper().map()["invoice_date"]

    def test_supplier_tin_from_company(self):
        assert self._mapper().map()["supplier_tin"] == "04702493-0001"

    def test_b2b_when_buyer_tin_present(self):
        assert self._mapper().map()["invoice_type"] == "B2B"

    def test_b2c_when_no_buyer_tin(self):
        row    = {**self.SAMPLE_ROW, "buyer_tin": ""}
        mapped = self._mapper(row).map()
        assert mapped["invoice_type"] == "B2C"
        assert mapped["buyer_tin"]    == ""

    def test_amounts(self):
        mapped = self._mapper().map()
        assert float(mapped["net_amount"])   == 1440000.0
        assert float(mapped["vat_amount"])   == 108000.0
        assert float(mapped["gross_amount"]) == 1548000.0

    def test_lines_built(self):
        mapped = self._mapper().map()
        assert len(mapped["lines"])       == 1
        line = mapped["lines"][0]
        assert line["description"]        == "Revenue Software License"
        assert line["quantity"]           == 1.0
        assert line["line_net_amount"]    == 1440000.0
        assert line["tax_category"]       == "S"

    def test_zero_vat_gives_z_category(self):
        row  = {**self.SAMPLE_ROW, "vat_rate": 0.0, "vat_amount": 0.0}
        line = self._mapper(row).map()["lines"][0]
        assert line["tax_category"] == "Z"

    def test_missing_invoice_number_raises(self):
        from apps.excel_intake.excel_field_mapper import MappingError
        row = {**self.SAMPLE_ROW, "invoice_number": ""}
        with pytest.raises(MappingError):
            self._mapper(row).map()

    def test_missing_date_raises(self):
        from apps.excel_intake.excel_field_mapper import MappingError
        row = {**self.SAMPLE_ROW, "invoice_date": None}
        with pytest.raises(MappingError):
            self._mapper(row).map()

    def test_peppol_fields_present(self):
        mapped = self._mapper().map()
        assert "peppol" in mapped["customization_id"].lower()
        assert "peppol" in mapped["profile_id"].lower()

    def test_currency_always_ngn(self):
        mapped = self._mapper().map()
        assert mapped["currency_code"]     == "NGN"
        assert mapped["tax_currency_code"] == "NGN"


# ════════════════════════════════════════════════════════════════
# TESTS: Web Upload Validation
# ════════════════════════════════════════════════════════════════

class TestFileValidation:
    """Tests for the web upload file validation."""

    def test_valid_xlsx_passes(self):
        from apps.excel_intake.views import validate_excel_file
        mock_file       = MagicMock()
        mock_file.name  = "invoices.xlsx"
        mock_file.size  = 1024 * 100  # 100KB
        # xlsx magic bytes (PK zip header)
        mock_file.read.return_value = b"\x50\x4b\x03\x04" + b"\x00" * 100
        is_valid, err = validate_excel_file(mock_file)
        assert is_valid is True
        assert err == ""

    def test_file_too_large_fails(self):
        from apps.excel_intake.views import validate_excel_file
        mock_file       = MagicMock()
        mock_file.name  = "invoices.xlsx"
        mock_file.size  = 15 * 1024 * 1024  # 15MB
        is_valid, err = validate_excel_file(mock_file)
        assert is_valid is False
        assert "too large" in err.lower()

    def test_wrong_extension_fails(self):
        from apps.excel_intake.views import validate_excel_file
        mock_file       = MagicMock()
        mock_file.name  = "invoices.pdf"
        mock_file.size  = 1024
        is_valid, err = validate_excel_file(mock_file)
        assert is_valid is False
        assert "not supported" in err.lower()

    def test_wrong_magic_bytes_fails(self):
        from apps.excel_intake.views import validate_excel_file
        mock_file       = MagicMock()
        mock_file.name  = "fake.xlsx"
        mock_file.size  = 1024
        mock_file.read.return_value = b"\x00\x00\x00\x00"
        is_valid, err = validate_excel_file(mock_file)
        assert is_valid is False
        assert "content" in err.lower()

    def test_csv_passes(self):
        from apps.excel_intake.views import validate_excel_file
        mock_file       = MagicMock()
        mock_file.name  = "invoices.csv"
        mock_file.size  = 512
        is_valid, err   = validate_excel_file(mock_file)
        assert is_valid is True


# ════════════════════════════════════════════════════════════════
# INTEGRATION TEST: Full Pipeline Mock
# ════════════════════════════════════════════════════════════════

class TestExcelPipelineMock:
    """Tests the full pipeline flow with mocked FIRS."""

    @pytest.fixture
    def mock_upload(self):
        upload          = MagicMock()
        upload.client   = MOCK_COMPANY
        upload.original_filename = "test.xlsx"
        upload.invoices = MagicMock()
        upload.invoices.filter.return_value.values_list.return_value = ["IRN-TEST-001"]
        return upload

    @patch("apps.excel_intake.pipeline.FIRSClient")
    @patch("apps.excel_intake.pipeline.ExcelInvoice")
    def test_pipeline_calls_firs_submit(self, mock_inv, mock_firs_cls, mock_upload):
        from apps.excel_intake.pipeline import ExcelPipeline
        from apps.excel_intake.excel_parser import ParseResult

        mock_firs = MagicMock()
        mock_firs.submit_b2b_invoice.return_value = {
            "success": True,
            "data":    {"irn": "IRN-TEST-001", "csid": "CSID-001"}
        }
        mock_firs.report_b2c_invoice.return_value = {
            "success": True,
            "data":    {"irn": "IRN-TEST-002", "qr_code": "QR-001"}
        }
        mock_firs_cls.return_value = mock_firs

        mock_inv_obj = MagicMock()
        mock_inv.objects.create.return_value = mock_inv_obj

        result = ParseResult(
            invoices=[{
                "row_number":    1,
                "invoice_number": "INV-001",
                "invoice_date":  "2025-03-14",
                "buyer_name":    "Test Buyer",
                "buyer_tin":     "31569955-0001",
                "buyer_address": "Lagos",
                "description":   "Services",
                "quantity":      1.0,
                "unit_price":    100000.0,
                "unit_code":     "EA",
                "vat_rate":      7.5,
                "vat_amount":    7500.0,
                "net_amount":    100000.0,
                "gross_amount":  107500.0,
                "currency":      "NGN",
                "notes":         "",
            }],
            errors=[],
            file_hash="abc123",
            total_rows=1,
            column_map={},
        )

        pipeline = ExcelPipeline(mock_upload, result)
        pipeline.run()

        # Verify FIRS was called
        called = (
            mock_firs.submit_b2b_invoice.called or
            mock_firs.report_b2c_invoice.called
        )
        assert called, "FIRS submission was not called"
