"""
Excel → FIRS UBL Field Mapper
Converts a parsed Excel invoice row into the
structured dict needed by UBLInvoiceBuilder or DigiTaxInvoiceBuilder.

Input:  Row dict from ExcelParser
Output: Mapped dict same format as Sage200FieldMapper and Sage300FieldMapper
"""

import logging
from decimal import Decimal
from typing import Dict, Optional

from apps.invoices.models import InvoiceType, DocumentType

logger = logging.getLogger("apps.excel_intake")

PEPPOL_CUSTOMIZATION = (
    "urn:cen.eu:en16931:2017#compliant"
    "#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)
PEPPOL_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class ExcelFieldMapper:
    """
    Maps a parsed Excel row to the 55 FIRS UBL mandatory fields.

    Usage:
        mapper = ExcelFieldMapper(company, parsed_row)
        mapped = mapper.map()
    """

    def __init__(self, company, row: Dict):
        self.company = company
        self.row     = row

    def map(self) -> Dict:
        """
        Map Excel row fields to FIRS UBL format.
        Raises MappingError if required field is missing.
        """
        try:
            buyer_tin = str(self.row.get("buyer_tin") or "").strip()
            inv_type  = InvoiceType.B2B if buyer_tin else InvoiceType.B2C
            net       = Decimal(str(self.row.get("net_amount",   0) or 0))
            vat       = Decimal(str(self.row.get("vat_amount",   0) or 0))
            gross     = Decimal(str(self.row.get("gross_amount", 0) or 0))

            mapped = {
                # ── Invoice Header ──────────────────────────────
                "invoice_number":    self._invoice_number(),
                "invoice_date":      self._invoice_date(),
                "due_date":          None,
                "document_type":     DocumentType.INVOICE,
                "invoice_type":      inv_type,
                "currency_code":     "NGN",
                "tax_currency_code": "NGN",
                "customization_id":  PEPPOL_CUSTOMIZATION,
                "profile_id":        PEPPOL_PROFILE,

                # ── Supplier ────────────────────────────────────
                "supplier_tin":      self.company.tin,
                "supplier_name":     self.company.name,
                "supplier_address":  self.company.address,
                "supplier_city":     "",
                "supplier_country":  "NG",
                "supplier_postal":   "",

                # ── Buyer ───────────────────────────────────────
                "buyer_tin":     buyer_tin,
                "buyer_name":    str(self.row.get("buyer_name") or "Consumer").strip(),
                "buyer_address": str(self.row.get("buyer_address") or "").strip(),
                "buyer_city":    "",
                "buyer_country": "NG",
                "buyer_postal":  "",

                # ── Amounts ─────────────────────────────────────
                "net_amount":     net,
                "vat_amount":     vat,
                "gross_amount":   gross,
                "payable_amount": gross,

                # ── Payment ─────────────────────────────────────
                "payment_means_code": "30",

                # ── Lines ───────────────────────────────────────
                "lines": self._build_lines(),

                # ── Currency ────────────────────────────────────
                "original_currency": str(self.row.get("currency") or "NGN"),
                "exchange_rate":     None,
            }

            logger.debug(
                f"[Excel Mapper] Mapped row {self.row.get('row_number')} | "
                f"{mapped['invoice_number']} | {inv_type}"
            )
            return mapped

        except Exception as e:
            raise MappingError(
                f"Failed to map row {self.row.get('row_number', '?')}: {e}"
            )

    def _invoice_number(self) -> str:
        val = str(self.row.get("invoice_number") or "").strip()
        if not val:
            raise MappingError(
                f"Row {self.row.get('row_number')}: Invoice number is blank"
            )
        return val

    def _invoice_date(self) -> str:
        val = self.row.get("invoice_date")
        if not val:
            raise MappingError(
                f"Row {self.row.get('row_number')}: Invoice date is blank"
            )
        return str(val)[:10]

    def _build_lines(self) -> list:
        vat_rate = float(self.row.get("vat_rate", 7.5) or 7.5)
        qty      = float(self.row.get("quantity", 1)   or 1)
        price    = float(self.row.get("unit_price", 0) or 0)
        net      = float(self.row.get("net_amount", 0) or 0)
        vat      = float(self.row.get("vat_amount", 0) or 0)

        return [{
            "line_number":     "1",
            "description":     str(self.row.get("description") or "Services").strip(),
            "quantity":        qty,
            "unit_code":       self.row.get("unit_code", "EA"),
            "unit_price":      round(price, 4),
            "line_net_amount": round(net, 2),
            "vat_rate":        vat_rate,
            "vat_amount":      round(vat, 2),
            "tax_category":    "S" if vat_rate > 0 else "Z",
            "tax_scheme":      "VAT",
        }]


class MappingError(Exception):
    """Raised when a required field cannot be mapped from Excel row to FIRS UBL."""
    pass
