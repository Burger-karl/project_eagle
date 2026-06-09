"""
Sage 300 Unit Tests
Full test suite for the Sage 300 middleware pipeline.
All tests use mock data so they run WITHOUT a real database.
When the CEO sends the Sage 300 .bak file, the integration
tests at the bottom will use the real database.

Run:
    pytest tests/test_sage300/ -v
    pytest tests/test_sage300/ -v -k "not integration"  # mock only
    pytest tests/test_sage300/ -v -k "integration"      # real DB only
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from decimal import Decimal
from datetime import date, datetime


# ── Mock Sage 300 raw invoice (mirrors ARINVOICE + ARCUSTOMER) ─
SAMPLE_RAW_INVOICE = {
    "batch_number":    12,
    "entry_number":    3,
    "invoice_id":      "12-3",
    "invoice_number":  "INV-S300-001",
    "transaction_type": 1,              # 1 = Invoice
    "invoice_date":    "2025-03-14",    # already parsed by ODBCClient
    "due_date":        "2025-04-14",
    "description":     "Professional Services",
    "customer_id":     "FRC001",
    "customer_name":   "Financial Reporting Council",
    "buyer_address1":  "Plot 17 Dar-es-Salaam Street",
    "buyer_address2":  "Off Aminu Kano Crescent",
    "buyer_address3":  "",
    "buyer_city":      "Abuja",
    "buyer_postal":    "900001",
    "buyer_country":   "Nigeria",
    "buyer_email":     "info@frc.gov.ng",
    "gross_amount":    1548000.0,
    "vat_amount":      108000.0,
    "net_amount":      1440000.0,
    "payable_amount":  1548000.0,
    "currency_code":   "NGN",
    "exchange_rate":   1.0,
    "lines": [
        {
            "line_number":     1,
            "item_code":       "CONSULT001",
            "description":     "Revenue Software License",
            "quantity":        1.0,
            "unit_price":      1440000.0,
            "line_net_amount": 1440000.0,
            "vat_amount":      108000.0,
            "vat_rate":        7.5,
            "unit_of_measure": "EA",
        }
    ]
}

# ── Mock company ──────────────────────────────────────────────
MOCK_COMPANY = MagicMock()
MOCK_COMPANY.name    = "Link Options & Systems Limited"
MOCK_COMPANY.tin     = "04702493-0001"
MOCK_COMPANY.address = "Onikan, Lagos"
MOCK_COMPANY.firs_api_key    = "test-api-key"
MOCK_COMPANY.firs_secret_key = "test-secret-key"
MOCK_COMPANY.sage300_mssql_database = "SAMLTD"
MOCK_COMPANY.sage300_mssql_server   = "localhost"
MOCK_COMPANY.mssql_database = "SAMLTD"
MOCK_COMPANY.mssql_server   = "localhost"


# ════════════════════════════════════════════════════════════════
# TESTS: Sage300FieldMapper
# ════════════════════════════════════════════════════════════════

class TestSage300FieldMapper:
    """Tests for the Sage 300 → FIRS UBL field mapper."""

    def _get_mapper(self, raw=None):
        from apps.sage300.field_mapper import Sage300FieldMapper
        return Sage300FieldMapper(MOCK_COMPANY, raw or SAMPLE_RAW_INVOICE)

    def test_maps_invoice_number(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["invoice_number"] == "INV-S300-001"

    def test_maps_invoice_date_string(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["invoice_date"] == "2025-03-14"

    def test_maps_invoice_date_strips_time(self):
        raw = {**SAMPLE_RAW_INVOICE, "invoice_date": "2025-03-14T00:00:00"}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["invoice_date"] == "2025-03-14"

    def test_maps_supplier_from_company(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["supplier_tin"]  == "04702493-0001"
        assert mapped["supplier_name"] == "Link Options & Systems Limited"

    def test_maps_buyer_name(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["buyer_name"] == "Financial Reporting Council"

    def test_maps_amounts(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert float(mapped["net_amount"])   == 1440000.0
        assert float(mapped["vat_amount"])   == 108000.0
        assert float(mapped["gross_amount"]) == 1548000.0

    def test_maps_country_code(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["buyer_country"] == "NG"

    def test_maps_due_date(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["due_date"] == "2025-04-14"

    def test_maps_line_items(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert len(mapped["lines"]) == 1
        line = mapped["lines"][0]
        assert line["description"]    == "Revenue Software License"
        assert line["quantity"]       == 1.0
        assert line["unit_code"]      == "EA"
        assert line["vat_rate"]       == 7.5
        assert line["tax_category"]   == "S"
        assert line["line_net_amount"] == 1440000.0

    def test_document_type_invoice(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["document_type"] == "380"

    def test_document_type_credit_note(self):
        raw = {**SAMPLE_RAW_INVOICE, "transaction_type": 3}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["document_type"] == "381"

    def test_document_type_debit_note(self):
        raw = {**SAMPLE_RAW_INVOICE, "transaction_type": 2}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["document_type"] == "383"

    def test_b2c_when_no_buyer_tin(self):
        """No TIN mapping → invoice classified as B2C."""
        with patch("apps.sage300.field_mapper.BuyerTINMapping") as mock_model:
            mock_model.objects.get.side_effect = Exception("DoesNotExist")
            mapper = self._get_mapper()
            mapped = mapper.map()
            assert mapped["invoice_type"] == "B2C"
            assert mapped["buyer_tin"]    == ""

    def test_b2b_when_buyer_tin_found(self):
        """TIN mapping found → invoice classified as B2B."""
        with patch("apps.sage300.field_mapper.BuyerTINMapping") as mock_model:
            mock_tin = MagicMock()
            mock_tin.buyer_tin = "31569955-0001"
            mock_model.objects.get.return_value = mock_tin
            mapper = self._get_mapper()
            mapped = mapper.map()
            assert mapped["invoice_type"] == "B2B"
            assert mapped["buyer_tin"]    == "31569955-0001"

    def test_synthetic_line_when_no_lines(self):
        """No line items → creates synthetic line from header totals."""
        raw = {**SAMPLE_RAW_INVOICE, "lines": []}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert len(mapped["lines"]) == 1
        line = mapped["lines"][0]
        assert line["line_number"] == "1"
        assert line["quantity"]    == 1.0

    def test_missing_invoice_number_raises(self):
        from apps.sage300.field_mapper import MappingError
        raw = {**SAMPLE_RAW_INVOICE, "invoice_number": "", "batch_number": None}
        mapper = self._get_mapper(raw)
        with pytest.raises(MappingError):
            mapper.map()

    def test_fallback_invoice_number_from_batch(self):
        """If IDTRX is blank, fall back to batch-entry format."""
        raw = {**SAMPLE_RAW_INVOICE, "invoice_number": "",
               "batch_number": 12, "entry_number": 3}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["invoice_number"] == "INV-12-3"

    def test_buyer_address_combines_lines(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert "Plot 17 Dar-es-Salaam Street" in mapped["buyer_address"]
        assert "Off Aminu Kano Crescent" in mapped["buyer_address"]

    def test_peppol_customization_id_present(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert "peppol" in mapped["customization_id"].lower()

    def test_currency_code_always_ngn(self):
        mapper = self._get_mapper()
        mapped = mapper.map()
        assert mapped["currency_code"]     == "NGN"
        assert mapped["tax_currency_code"] == "NGN"

    def test_unit_code_mapping(self):
        raw = {**SAMPLE_RAW_INVOICE, "lines": [{
            **SAMPLE_RAW_INVOICE["lines"][0],
            "unit_of_measure": "HR"
        }]}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["lines"][0]["unit_code"] == "HUR"

    def test_zero_vat_maps_to_z_category(self):
        raw = {**SAMPLE_RAW_INVOICE, "lines": [{
            **SAMPLE_RAW_INVOICE["lines"][0],
            "vat_rate": 0.0, "vat_amount": 0.0
        }]}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["lines"][0]["tax_category"] == "Z"

    def test_foreign_currency_exchange_rate(self):
        raw = {**SAMPLE_RAW_INVOICE, "currency_code": "USD", "exchange_rate": 1580.0}
        mapper = self._get_mapper(raw)
        mapped = mapper.map()
        assert mapped["original_currency"] == "USD"
        assert mapped["exchange_rate"]     == 1580.0


# ════════════════════════════════════════════════════════════════
# TESTS: SAGE 300 Date Parsing
# ════════════════════════════════════════════════════════════════

class TestSage300DateParsing:
    """Tests for the _parse_date static method in ODBCClient."""

    def _parse(self, val):
        from apps.sage300.odbc_client import Sage300ODBCClient
        return Sage300ODBCClient._parse_date(val)

    def test_integer_yyyymmdd(self):
        assert self._parse(20250314) == "2025-03-14"

    def test_string_yyyymmdd(self):
        assert self._parse("20250314") == "2025-03-14"

    def test_datetime_object(self):
        dt = datetime(2025, 3, 14, 0, 0, 0)
        assert self._parse(dt) == "2025-03-14"

    def test_date_object(self):
        d = date(2025, 3, 14)
        assert self._parse(d) == "2025-03-14"

    def test_none_returns_none(self):
        assert self._parse(None) is None

    def test_iso_string_passthrough(self):
        assert self._parse("2025-03-14") == "2025-03-14"


# ════════════════════════════════════════════════════════════════
# TESTS: UBL Payload Building (Sage 300 mapped data)
# ════════════════════════════════════════════════════════════════

class TestSage300UBLBuilder:
    """Tests that UBLInvoiceBuilder works correctly with Sage 300 mapped data."""

    def _get_mapped(self, buyer_tin=""):
        with patch("apps.sage300.field_mapper.BuyerTINMapping") as mock_model:
            if buyer_tin:
                mock_tin = MagicMock()
                mock_tin.buyer_tin = buyer_tin
                mock_model.objects.get.return_value = mock_tin
            else:
                mock_model.objects.get.side_effect = Exception("DoesNotExist")
            from apps.sage300.field_mapper import Sage300FieldMapper
            return Sage300FieldMapper(MOCK_COMPANY, SAMPLE_RAW_INVOICE).map()

    def test_b2b_payload_structure(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        assert payload["ID"]                   == "INV-S300-001"
        assert payload["IssueDate"]            == "2025-03-14"
        assert payload["InvoiceTypeCode"]       == "380"
        assert payload["DocumentCurrencyCode"]  == "NGN"
        assert "AccountingSupplierParty"        in payload
        assert "AccountingCustomerParty"        in payload
        assert "TaxTotal"                       in payload
        assert "LegalMonetaryTotal"             in payload
        assert "InvoiceLine"                    in payload

    def test_b2b_monetary_totals(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        totals = payload["LegalMonetaryTotal"]
        assert totals["TaxExclusiveAmount"]["value"]  == 1440000.0
        assert totals["TaxInclusiveAmount"]["value"]  == 1548000.0
        assert totals["PayableAmount"]["value"]       == 1548000.0

    def test_b2b_supplier_tin(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        supplier = payload["AccountingSupplierParty"]["Party"]
        assert supplier["PartyTaxScheme"]["CompanyID"] == "04702493-0001"

    def test_b2b_buyer_tin(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        buyer = payload["AccountingCustomerParty"]["Party"]
        assert buyer["PartyTaxScheme"]["CompanyID"] == "31569955-0001"

    def test_b2c_payload_no_buyer_tin(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="")
        payload = UBLInvoiceBuilder(mapped).build_b2c()

        buyer = payload["AccountingCustomerParty"]["Party"]
        assert "PartyTaxScheme" not in buyer
        assert payload.get("InvoiceSubtype") == "B2C"

    def test_invoice_line_mapped_correctly(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        assert len(payload["InvoiceLine"]) == 1
        line = payload["InvoiceLine"][0]
        assert line["ID"]                           == "1"
        assert line["InvoicedQuantity"]["unitCode"] == "EA"
        assert line["Item"]["Description"]          == "Revenue Software License"

    def test_tax_total_correct(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped  = self._get_mapped(buyer_tin="31569955-0001")
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        tax = payload["TaxTotal"][0]
        assert tax["TaxAmount"]["value"] == 108000.0


# ════════════════════════════════════════════════════════════════
# TESTS: SAGE 300 ODBC Client (mock — no real DB needed)
# ════════════════════════════════════════════════════════════════

class TestSage300ODBCClientMock:
    """Tests for ODBCClient using mocked pyodbc connections."""

    def _get_client(self):
        from apps.sage300.odbc_client import Sage300ODBCClient
        company = MagicMock()
        company.name = "Test Client"
        company.mssql_database = "SAMLTD"
        company.mssql_server   = "localhost"
        company.sage300_mssql_database = "SAMLTD"
        company.sage300_mssql_server   = "localhost"
        company.sage300_mssql_trusted  = True
        return Sage300ODBCClient(company)

    def test_connection_string_includes_database(self):
        client = self._get_client()
        assert "SAMLTD" in client._conn_str

    def test_connection_string_uses_trusted_auth(self):
        client = self._get_client()
        assert "Trusted_Connection=yes" in client._conn_str

    def test_parse_date_in_odbc_client(self):
        from apps.sage300.odbc_client import Sage300ODBCClient
        assert Sage300ODBCClient._parse_date(20250314) == "2025-03-14"
        assert Sage300ODBCClient._parse_date(None)     is None

    def test_test_connection_returns_false_on_error(self):
        client = self._get_client()
        with patch("pyodbc.connect", side_effect=Exception("connection refused")):
            assert client.test_connection() is False

    def test_test_connection_returns_true_on_success(self):
        client = self._get_client()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        with patch("pyodbc.connect", return_value=mock_conn):
            assert client.test_connection() is True


# ════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — require real Sage 300 database
# Mark: pytest -k "integration" to run these only
# ════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestSage300Integration:
    """
    Integration tests using the real Sage 300 database.
    These tests are SKIPPED until the CEO provides the Sage 300 .bak file.
    Run with: pytest tests/test_sage300/ -v -k "integration"
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Skip all integration tests if no Sage 300 DB configured."""
        import os
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        if "SAGE300_MSSQL_DATABASE" not in os.environ:
            pytest.skip("Sage 300 database not configured — skipping integration tests")

    def test_connection_to_real_sage300(self):
        import os
        django.setup()
        from apps.core.models import ClientCompany
        from apps.sage300.odbc_client import Sage300ODBCClient

        company = ClientCompany.objects.filter(
            sage300_mssql_database__gt=""
        ).first()

        if not company:
            pytest.skip("No Sage 300 client company in database")

        client = Sage300ODBCClient(company)
        assert client.test_connection() is True

    def test_count_real_invoices(self):
        import os
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from apps.core.models import ClientCompany
        from apps.sage300.odbc_client import Sage300ODBCClient

        company = ClientCompany.objects.filter(
            sage300_mssql_database__gt=""
        ).first()
        if not company:
            pytest.skip("No Sage 300 client configured")

        client = Sage300ODBCClient(company)
        count  = client.count_invoices()
        assert isinstance(count, int)
        print(f"\nReal Sage 300 invoice count: {count}")

    def test_pull_sample_invoices(self):
        import os
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from apps.core.models import ClientCompany
        from apps.sage300.odbc_client import Sage300ODBCClient

        company = ClientCompany.objects.filter(
            sage300_mssql_database__gt=""
        ).first()
        if not company:
            pytest.skip("No Sage 300 client configured")

        client  = Sage300ODBCClient(company)
        samples = client.get_sample_invoices(3)
        print(f"\nSage 300 sample invoices:")
        for s in samples:
            print(
                f"  {s['invoice_number']} | "
                f"{s['customer_name']} | "
                f"₦{s['gross_amount']:,.2f}"
            )
        assert isinstance(samples, list)

    def test_full_invoice_with_lines(self):
        import os
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from apps.core.models import ClientCompany
        from apps.sage300.odbc_client import Sage300ODBCClient

        company = ClientCompany.objects.filter(
            sage300_mssql_database__gt=""
        ).first()
        if not company:
            pytest.skip("No Sage 300 client configured")

        client   = Sage300ODBCClient(company)
        invoices = client.get_new_invoices(limit=1)

        if not invoices:
            pytest.skip("No invoices in Sage 300 database")

        inv = invoices[0]
        print(f"\nFull Sage 300 invoice:")
        print(f"  Number:   {inv['invoice_number']}")
        print(f"  Customer: {inv['customer_name']}")
        print(f"  Net:      ₦{inv['net_amount']:,.2f}")
        print(f"  VAT:      ₦{inv['vat_amount']:,.2f}")
        print(f"  Gross:    ₦{inv['gross_amount']:,.2f}")
        print(f"  Lines:    {len(inv['lines'])}")
        for line in inv["lines"]:
            print(
                f"    → {line['description']} | "
                f"qty:{line['quantity']} | "
                f"net:{line['line_net_amount']}"
            )

        assert inv["invoice_number"] != ""
        assert inv["gross_amount"]   > 0
        assert "invoice_date"        in inv

    def test_field_mapping_with_real_invoice(self):
        import os
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from apps.core.models import ClientCompany
        from apps.sage300.odbc_client import Sage300ODBCClient
        from apps.sage300.field_mapper import Sage300FieldMapper

        company  = ClientCompany.objects.filter(
            sage300_mssql_database__gt=""
        ).first()
        if not company:
            pytest.skip("No Sage 300 client configured")

        client   = Sage300ODBCClient(company)
        invoices = client.get_new_invoices(limit=1)
        if not invoices:
            pytest.skip("No invoices found")

        mapper = Sage300FieldMapper(company, invoices[0])
        mapped = mapper.map()

        print(f"\nMapped invoice:")
        print(f"  Number:       {mapped['invoice_number']}")
        print(f"  Type:         {mapped['invoice_type']}")
        print(f"  Supplier TIN: {mapped['supplier_tin']}")
        print(f"  Buyer TIN:    {mapped['buyer_tin'] or '(B2C)'}")
        print(f"  Net:          ₦{float(mapped['net_amount']):,.2f}")

        assert mapped["invoice_number"]  != ""
        assert mapped["supplier_tin"]    == company.tin
        assert mapped["invoice_date"]    != ""
        assert len(mapped["invoice_date"]) == 10
        assert "T" not in mapped["invoice_date"]
        assert len(mapped["lines"])      > 0
