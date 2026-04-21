"""
Tests for the Invoice Pipeline.
Run with: pytest tests/ -v
"""

import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


# ── Sample Sage 200 raw invoice (as returned by ODBC client) ──
SAMPLE_RAW_INVOICE = {
    "invoice_id":       "12345",
    "invoice_number":   "SI-2025-0001",
    "invoice_date":     "2025-11-15",
    "due_date":         "2025-12-15",
    "document_type":    1,  # Invoice
    "account_ref":      "CUS001",
    "buyer_name":       "Buyer Company Ltd",
    "buyer_address1":   "123 Buyer Street",
    "buyer_address2":   "Victoria Island",
    "buyer_city":       "Lagos",
    "buyer_postal":     "100001",
    "buyer_country":    "Nigeria",
    "net_amount":       Decimal("500000.00"),
    "vat_amount":       Decimal("37500.00"),
    "gross_amount":     Decimal("537500.00"),
    "currency_code":    "NGN",
    "exchange_rate":    None,
    "lines": [
        {
            "line_number":     1,
            "description":     "IT Consulting Services",
            "quantity":        1.0,
            "unit_price":      500000.00,
            "line_net_amount": 500000.00,
            "vat_rate":        7.5,
            "vat_amount":      37500.00,
            "tax_code":        "T1",
            "unit_of_measure": "Each",
        }
    ]
}

SAMPLE_COMPANY = MagicMock()
SAMPLE_COMPANY.name    = "Test Company Ltd"
SAMPLE_COMPANY.tin     = "12345678-0001"
SAMPLE_COMPANY.address = "1 Business Avenue, Lagos"
SAMPLE_COMPANY.firs_api_key    = "test-api-key"
SAMPLE_COMPANY.firs_secret_key = "test-secret-key"


class TestSage200FieldMapper:

    def test_maps_invoice_number(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert mapped["invoice_number"] == "SI-2025-0001"

    def test_maps_invoice_date(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert mapped["invoice_date"] == "2025-11-15"

    def test_maps_supplier_from_company(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert mapped["supplier_tin"]  == "12345678-0001"
        assert mapped["supplier_name"] == "Test Company Ltd"

    def test_maps_amounts(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert float(mapped["net_amount"])   == 500000.00
        assert float(mapped["vat_amount"])   == 37500.00
        assert float(mapped["gross_amount"]) == 537500.00

    def test_maps_line_items(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert len(mapped["lines"]) == 1
        line = mapped["lines"][0]
        assert line["description"]    == "IT Consulting Services"
        assert line["quantity"]       == 1.0
        assert line["unit_code"]      == "EA"  # "Each" → "EA"
        assert line["tax_category"]   == "S"   # 7.5% → Standard
        assert line["vat_rate"]       == 7.5

    def test_country_code_mapping(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
        mapped = mapper.map()
        assert mapped["buyer_country"] == "NG"  # "Nigeria" → "NG"

    def test_missing_invoice_number_raises(self):
        from apps.sage200.field_mapper import Sage200FieldMapper, MappingError
        bad_invoice = {**SAMPLE_RAW_INVOICE, "invoice_number": ""}
        mapper = Sage200FieldMapper(SAMPLE_COMPANY, bad_invoice)
        with pytest.raises(MappingError):
            mapper.map()

    def test_b2c_when_no_buyer_tin(self):
        """Invoice with no TIN mapping should be classified as B2C."""
        from apps.sage200.field_mapper import Sage200FieldMapper
        with patch("apps.sage200.field_mapper.BuyerTINMapping.objects.get") as mock_get:
            mock_get.side_effect = Exception("DoesNotExist")
            mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
            mapped = mapper.map()
            assert mapped["invoice_type"] == "B2C"


class TestUBLInvoiceBuilder:

    def _get_mapped(self):
        from apps.sage200.field_mapper import Sage200FieldMapper
        with patch("apps.sage200.field_mapper.BuyerTINMapping.objects.get") as m:
            m.return_value = MagicMock(buyer_tin="98765432-0001")
            mapper = Sage200FieldMapper(SAMPLE_COMPANY, SAMPLE_RAW_INVOICE)
            return mapper.map()

    def test_b2b_payload_has_required_fields(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped = self._get_mapped()
        mapped["buyer_tin"] = "98765432-0001"
        mapped["invoice_type"] = "B2B"

        builder = UBLInvoiceBuilder(mapped)
        payload = builder.build_b2b()

        assert payload["ID"]             == "SI-2025-0001"
        assert payload["IssueDate"]      == "2025-11-15"
        assert payload["InvoiceTypeCode"] == "380"
        assert payload["DocumentCurrencyCode"] == "NGN"
        assert "AccountingSupplierParty" in payload
        assert "AccountingCustomerParty" in payload
        assert "TaxTotal"               in payload
        assert "LegalMonetaryTotal"     in payload
        assert "InvoiceLine"            in payload

    def test_b2b_supplier_tin_present(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped = self._get_mapped()
        mapped["buyer_tin"] = "98765432-0001"
        mapped["invoice_type"] = "B2B"
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        supplier = payload["AccountingSupplierParty"]["Party"]
        assert supplier["PartyTaxScheme"]["CompanyID"] == "12345678-0001"

    def test_monetary_totals_correct(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped = self._get_mapped()
        mapped["buyer_tin"] = "98765432-0001"
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        totals = payload["LegalMonetaryTotal"]
        assert totals["TaxExclusiveAmount"]["value"]  == 500000.00
        assert totals["TaxInclusiveAmount"]["value"]  == 537500.00
        assert totals["PayableAmount"]["value"]       == 537500.00

    def test_invoice_line_built_correctly(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped = self._get_mapped()
        mapped["buyer_tin"] = "98765432-0001"
        payload = UBLInvoiceBuilder(mapped).build_b2b()

        assert len(payload["InvoiceLine"]) == 1
        line = payload["InvoiceLine"][0]
        assert line["ID"]                           == "1"
        assert line["InvoicedQuantity"]["unitCode"] == "EA"
        assert line["InvoicedQuantity"]["value"]    == 1.0
        assert line["Item"]["Description"]          == "IT Consulting Services"

    def test_b2c_payload_has_no_buyer_tin(self):
        from apps.firs.invoice_schema import UBLInvoiceBuilder
        mapped = self._get_mapped()
        mapped["buyer_tin"] = ""
        payload = UBLInvoiceBuilder(mapped).build_b2c()

        customer = payload["AccountingCustomerParty"]["Party"]
        assert "PartyTaxScheme" not in customer
        assert payload.get("InvoiceSubtype") == "B2C"
