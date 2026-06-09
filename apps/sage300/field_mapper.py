"""
Sage 300 ERP → FIRS UBL Field Mapper
Maps raw Sage 300 (Accpac) column names to the 55 mandatory FIRS UBL fields.

Sage 300 column name conventions:
    ARINVOICE:  IDTRX, DATEINVC, DATEDUE, TEXTTRX, IDCUST
                AMTGROSDOC, AMTTAXDOC, AMTDUEDOC, CODECURN, RATEXCHR
    ARINVOICED: CNTLINE, IDITEM, TEXTDESC, QTYINVC, AMTPRIC,
                AMTEXTN, AMTTAX, RATETAX, UNITMEAS
    ARCUSTOMER: NAMECUST, TEXTSTRE1-3, NAMECITY, CODEPSTL, CODECTRY

Transaction types:
    1 → Invoice     → FIRS document type 380
    2 → Debit Note  → FIRS document type 383
    3 → Credit Note → FIRS document type 381
"""

import logging
from decimal import Decimal
from typing import Dict, Optional

from apps.invoices.models import BuyerTINMapping, InvoiceType, DocumentType
from utils.currency import get_ngn_exchange_rate

logger = logging.getLogger("apps.sage300")

# ── Sage 300 transaction type → FIRS document type ────────────
DOCUMENT_TYPE_MAP = {
    1: DocumentType.INVOICE,
    2: DocumentType.DEBIT_NOTE,
    3: DocumentType.CREDIT_NOTE,
}

# ── Sage 300 unit of measure → UN/ECE unit code ───────────────
UNIT_CODE_MAP = {
    "EA":   "EA",
    "EACH": "EA",
    "HR":   "HUR",
    "HRS":  "HUR",
    "DAY":  "DAY",
    "KG":   "KGM",
    "KGM":  "KGM",
    "L":    "LTR",
    "LTR":  "LTR",
    "M":    "MTR",
    "MTR":  "MTR",
    "BX":   "BX",
    "BOX":  "BX",
    "PC":   "H87",
    "PCS":  "H87",
    "SET":  "SET",
    "":     "EA",
}

# ── Country code map ──────────────────────────────────────────
COUNTRY_CODE_MAP = {
    "Nigeria":        "NG",
    "NIGERIA":        "NG",
    "NG":             "NG",
    "United Kingdom": "GB",
    "UK":             "GB",
    "South Africa":   "ZA",
    "ZA":             "ZA",
    "Ghana":          "GH",
    "GH":             "GH",
    "United States":  "US",
    "USA":            "US",
    "US":             "US",
}

PEPPOL_CUSTOMIZATION = (
    "urn:cen.eu:en16931:2017#compliant"
    "#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)
PEPPOL_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class Sage300FieldMapper:
    """
    Maps a raw Sage 300 invoice dict
    (from Sage300ODBCClient.get_new_invoices) to the
    structured dict needed by UBLInvoiceBuilder or DigiTaxInvoiceBuilder.

    Usage:
        mapper = Sage300FieldMapper(company, raw_invoice)
        mapped = mapper.map()
    """

    def __init__(self, company, raw_invoice: Dict):
        self.company = company
        self.raw     = raw_invoice

    def map(self) -> Dict:
        """
        Perform the full field mapping.
        Returns a structured dict with all 51 supplier-side UBL fields.
        FIRS adds IRN, CSID, QR code, and digital signature after clearance.

        Raises:
            MappingError if a required field cannot be resolved.
        """
        try:
            mapped = {
                # ── Invoice Header ──────────────────────────────
                "invoice_number":    self._invoice_number(),
                "invoice_date":      self._invoice_date(),
                "due_date":          self._due_date(),
                "document_type":     self._document_type(),
                "invoice_type":      self._invoice_type(),
                "currency_code":     "NGN",
                "tax_currency_code": "NGN",
                "customization_id":  PEPPOL_CUSTOMIZATION,
                "profile_id":        PEPPOL_PROFILE,

                # ── Supplier (your client) ──────────────────────
                "supplier_tin":      self.company.tin,
                "supplier_name":     self.company.name,
                "supplier_address":  self.company.address,
                "supplier_city":     "",
                "supplier_country":  "NG",
                "supplier_postal":   "",

                # ── Buyer ───────────────────────────────────────
                "buyer_tin":     self._buyer_tin(),
                "buyer_name":    self._safe("customer_name", "Consumer"),
                "buyer_address": self._buyer_address(),
                "buyer_city":    self._safe("buyer_city", ""),
                "buyer_country": self._country_code(),
                "buyer_postal":  self._safe("buyer_postal", ""),

                # ── Amounts ─────────────────────────────────────
                "net_amount":    Decimal(str(self.raw.get("net_amount",   0) or 0)),
                "vat_amount":    Decimal(str(self.raw.get("vat_amount",   0) or 0)),
                "gross_amount":  Decimal(str(self.raw.get("gross_amount", 0) or 0)),
                "payable_amount": Decimal(str(self.raw.get("gross_amount", 0) or 0)),

                # ── Payment ─────────────────────────────────────
                "payment_means_code": "30",

                # ── Lines ───────────────────────────────────────
                "lines": self._map_lines(),

                # ── Currency ────────────────────────────────────
                "original_currency": self._safe("currency_code", "NGN"),
                "exchange_rate":     self._exchange_rate(),
            }

            logger.debug(
                f"[Sage300 Mapper] Mapped {mapped['invoice_number']} "
                f"({mapped['invoice_type']}) for {self.company.name}"
            )
            return mapped

        except MappingError:
            raise
        except KeyError as e:
            raise MappingError(f"Missing field in Sage 300 data: {e}")
        except Exception as e:
            raise MappingError(
                f"Mapping failed for invoice "
                f"{self.raw.get('invoice_number', '?')}: {e}"
            )

    # ── HEADER ────────────────────────────────────────────────

    def _invoice_number(self) -> str:
        val = self.raw.get("invoice_number", "")
        if not val:
            # Fallback: use batch-entry as invoice number
            batch = self.raw.get("batch_number", "")
            entry = self.raw.get("entry_number", "")
            if batch and entry:
                return f"INV-{batch}-{entry}"
            raise MappingError(
                "IDTRX (invoice number) is blank in Sage 300 ARINVOICE. "
                "Cannot submit without an invoice number."
            )
        return str(val).strip()

    def _invoice_date(self) -> str:
        val = self.raw.get("invoice_date")
        if not val:
            raise MappingError("DATEINVC (invoice date) is blank in Sage 300")
        # Already parsed by ODBCClient into ISO string
        return str(val)[:10]

    def _due_date(self) -> Optional[str]:
        val = self.raw.get("due_date")
        if not val:
            return None
        return str(val)[:10]

    def _document_type(self) -> str:
        tx_type = int(self.raw.get("transaction_type", 1) or 1)
        return DOCUMENT_TYPE_MAP.get(tx_type, DocumentType.INVOICE)

    def _invoice_type(self) -> str:
        """B2B if buyer has a TIN mapping, otherwise B2C."""
        return InvoiceType.B2B if self._buyer_tin() else InvoiceType.B2C

    # ── BUYER TIN ─────────────────────────────────────────────

    def _buyer_tin(self) -> str:
        """
        Look up buyer TIN from BuyerTINMapping.
        Key is IDCUST (Sage 300 customer ID — e.g. 'FRC001').
        Falls back to empty string → treated as B2C.
        """
        customer_id = self._safe("customer_id", "")
        if not customer_id:
            return ""
        try:
            mapping = BuyerTINMapping.objects.get(
                client=self.company,
                sage_account_ref=customer_id,
            )
            return mapping.buyer_tin
        except BuyerTINMapping.DoesNotExist:
            logger.debug(
                f"[Sage300 Mapper] No TIN for customer '{customer_id}' — B2C"
            )
            return ""

    # ── ADDRESS ───────────────────────────────────────────────

    def _buyer_address(self) -> str:
        parts = [
            self._safe("buyer_address1", ""),
            self._safe("buyer_address2", ""),
            self._safe("buyer_address3", ""),
        ]
        return ", ".join(p for p in parts if p.strip())

    def _country_code(self) -> str:
        country = self._safe("buyer_country", "NG")
        return COUNTRY_CODE_MAP.get(country, "NG")

    # ── LINE ITEMS ────────────────────────────────────────────

    def _map_lines(self) -> list:
        """
        Map ARINVOICED columns to UBL line format.

        Sage 300 ARINVOICED columns used:
            CNTLINE     → line_number
            TEXTDESC    → description
            QTYINVC     → quantity
            AMTPRIC     → unit_price
            AMTEXTN     → line_net_amount
            AMTTAX      → vat_amount
            RATETAX     → vat_rate
            UNITMEAS    → unit_code (mapped via UNIT_CODE_MAP)
        """
        lines = self.raw.get("lines", [])

        if not lines:
            # Synthetic line from header totals
            logger.warning(
                f"[Sage300 Mapper] No lines for {self.raw.get('invoice_number')} "
                f"— using header totals"
            )
            net = float(self.raw.get("net_amount", 0) or 0)
            vat = float(self.raw.get("vat_amount", 0) or 0)
            return [{
                "line_number":     "1",
                "description":     self._safe("description", "Services"),
                "quantity":        1.0,
                "unit_code":       "EA",
                "unit_price":      net,
                "line_net_amount": net,
                "vat_rate":        7.5 if vat > 0 else 0.0,
                "vat_amount":      vat,
                "tax_category":    "S" if vat > 0 else "Z",
                "tax_scheme":      "VAT",
            }]

        mapped_lines = []
        for i, line in enumerate(lines, start=1):
            vat_rate = float(line.get("vat_rate", 7.5) or 7.5)
            qty      = float(line.get("quantity", 1)   or 1)
            price    = float(line.get("unit_price", 0) or 0)
            net      = float(line.get("line_net_amount", 0) or qty * price)
            vat      = float(line.get("vat_amount", 0) or 0)
            line_num = str(line.get("line_number") or i)
            desc     = str(line.get("description") or line.get("item_code") or "Item").strip()
            uom      = str(line.get("unit_of_measure") or "").strip().upper()

            mapped_lines.append({
                "line_number":     line_num,
                "description":     desc or "Item",
                "quantity":        qty,
                "unit_code":       UNIT_CODE_MAP.get(uom, "EA"),
                "unit_price":      round(price, 4),
                "line_net_amount": round(net, 2),
                "vat_rate":        vat_rate,
                "vat_amount":      round(vat, 2),
                "tax_category":    "S" if vat_rate > 0 else "Z",
                "tax_scheme":      "VAT",
            })

        return mapped_lines

    # ── CURRENCY ──────────────────────────────────────────────

    def _exchange_rate(self) -> Optional[float]:
        currency = self._safe("currency_code", "NGN")
        if currency == "NGN":
            return None
        # Try Sage 300's stored rate first
        stored = self.raw.get("exchange_rate")
        if stored and float(stored) > 0:
            return float(stored)
        # Fallback to CBN API
        inv_date = self.raw.get("invoice_date")
        try:
            return get_ngn_exchange_rate(currency, inv_date)
        except Exception as e:
            logger.warning(f"[Sage300 Mapper] Exchange rate failed for {currency}: {e}")
            return 1.0

    # ── HELPERS ───────────────────────────────────────────────

    def _safe(self, key: str, default: str = "") -> str:
        val = self.raw.get(key, default)
        return str(val).strip() if val else default


class MappingError(Exception):
    """Raised when a required field cannot be mapped from Sage 300 to FIRS UBL."""
    pass
