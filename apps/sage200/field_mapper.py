"""
Sage 200 Evolution → FIRS UBL Field Mapper
Maps raw Evolution column names to the 55 mandatory FIRS UBL fields.

Column names confirmed from _bvARTransactionsFull and _btblInvoiceLines:
    invoice_id          → AutoIdx
    invoice_number      → InvNumber
    invoice_date        → TxDate
    transaction_type    → iTransactionType (1=Invoice, 2=CreditNote, 3=DebitNote)
    customer_name       → Client.Name
    buyer_address1-4    → Client.Physical1-4
    gross_amount        → calculated from Debit/Credit
    vat_amount          → Tax_Amount
    net_amount          → gross - vat
    lines.description   → cDescription
    lines.quantity      → fQuantity
    lines.unit_price    → fUnitPriceExcl
    lines.line_net      → fQuantityLineTotExcl
    lines.vat_amount    → fQuantityLineTaxAmount
    lines.vat_rate      → fTaxRate
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Dict, Optional

from apps.invoices.models import BuyerTINMapping, InvoiceType, DocumentType
from utils.currency import get_ngn_exchange_rate

logger = logging.getLogger("apps.sage200")

# ── Evolution transaction type → FIRS document type code ──────
DOCUMENT_TYPE_MAP = {
    1: DocumentType.INVOICE,      # Invoice
    2: DocumentType.CREDIT_NOTE,  # Credit Note
    3: DocumentType.DEBIT_NOTE,   # Debit Note
}

# ── Unit of measure ID → UN/ECE unit code ─────────────────────
# Evolution stores a numeric ID — map to FIRS-required codes
# These are sensible defaults; refine as you see real values
UNIT_CODE_MAP = {
    1:   "EA",   # Each
    2:   "HUR",  # Hour
    3:   "DAY",  # Day
    4:   "KGM",  # Kilogram
    5:   "LTR",  # Litre
    6:   "MTR",  # Metre
    7:   "BX",   # Box
    None: "EA",  # Default
}

# ── Country name → ISO 3166-1 alpha-2 ─────────────────────────
COUNTRY_CODE_MAP = {
    "Nigeria":        "NG",
    "NIGERIA":        "NG",
    "United Kingdom": "GB",
    "UK":             "GB",
    "South Africa":   "ZA",
    "Ghana":          "GH",
    "Kenya":          "KE",
    "United States":  "US",
    "USA":            "US",
}

PEPPOL_CUSTOMIZATION = (
    "urn:cen.eu:en16931:2017#compliant"
    "#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)
PEPPOL_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class Sage200FieldMapper:
    """
    Maps a raw Sage 200 Evolution invoice dict
    (from Sage200ODBCClient.get_new_invoices) to the
    structured dict needed by UBLInvoiceBuilder.

    Usage:
        mapper = Sage200FieldMapper(company, raw_invoice)
        mapped = mapper.map()
    """

    def __init__(self, company, raw_invoice: Dict):
        self.company = company
        self.raw     = raw_invoice

    def map(self) -> Dict:
        """
        Perform the full field mapping.
        Returns structured dict with all supplier-side UBL fields.
        FIRS adds IRN, CSID, QR code and digital signature after clearance.

        Raises:
            MappingError if a required field cannot be resolved.
        """
        try:
            mapped = {
                # ── Invoice Header ──────────────────────────────
                "invoice_number":    self._invoice_number(),
                "invoice_date":      self._invoice_date(),
                "due_date":          None,
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
                "buyer_country": "NG",
                "buyer_postal":  self._safe("buyer_postal", ""),

                # ── Amounts ─────────────────────────────────────
                "net_amount":    Decimal(str(self.raw.get("net_amount",   0) or 0)),
                "vat_amount":    Decimal(str(self.raw.get("vat_amount",   0) or 0)),
                "gross_amount":  Decimal(str(self.raw.get("gross_amount", 0) or 0)),
                "payable_amount": Decimal(str(self.raw.get("gross_amount", 0) or 0)),

                # ── Payment ─────────────────────────────────────
                "payment_means_code": "30",  # Credit transfer

                # ── Lines ───────────────────────────────────────
                "lines": self._map_lines(),

                # ── Currency conversion ─────────────────────────
                "original_currency": self._safe("currency_code", "NGN"),
                "exchange_rate":     self._exchange_rate(),
            }

            logger.debug(
                f"[Mapper] Mapped {mapped['invoice_number']} "
                f"({mapped['invoice_type']}) for {self.company.name}"
            )
            return mapped

        except KeyError as e:
            raise MappingError(f"Missing field in Sage 200 data: {e}")
        except MappingError:
            raise
        except Exception as e:
            raise MappingError(
                f"Mapping failed for invoice "
                f"{self.raw.get('invoice_number', '?')}: {e}"
            )

    # ── HEADER ────────────────────────────────────────────────

    def _invoice_number(self) -> str:
        val = self.raw.get("invoice_number", "")
        if not val:
            raise MappingError(
                "InvNumber is blank in Sage 200 Evolution. "
                "Cannot submit an invoice without an invoice number."
            )
        return str(val).strip()

    def _invoice_date(self) -> str:
        val = self.raw.get("invoice_date")
        if not val:
            raise MappingError("TxDate (invoice date) is blank")
        if isinstance(val, date):
            return val.isoformat()
        # pyodbc returns datetime — take date portion
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
        Look up buyer TIN from BuyerTINMapping table.
        Key is account_code (Evolution's Account field in Client table).
        Falls back to empty string → treated as B2C.
        """
        account_code = self._safe("account_code", "")
        if not account_code:
            return ""
        try:
            mapping = BuyerTINMapping.objects.get(
                client=self.company,
                sage_account_ref=account_code,
            )
            return mapping.buyer_tin
        except BuyerTINMapping.DoesNotExist:
            logger.debug(
                f"[Mapper] No TIN mapping for account '{account_code}' "
                f"— will treat as B2C"
            )
            return ""

    # ── ADDRESS ───────────────────────────────────────────────

    def _buyer_address(self) -> str:
        """Combine Evolution's Physical1/Physical2 into one address string."""
        parts = [
            self._safe("buyer_address1", ""),
            self._safe("buyer_address2", ""),
            self._safe("buyer_address3", ""),
        ]
        return ", ".join(p for p in parts if p.strip())

    # ── LINE ITEMS ────────────────────────────────────────────

    def _map_lines(self) -> list:
        """
        Map _btblInvoiceLines columns to UBL line format.

        Evolution columns used:
            cDescription            → description
            fQuantity               → quantity
            fUnitPriceExcl          → unit_price
            fQuantityLineTotExcl    → line_net_amount
            fQuantityLineTaxAmount  → vat_amount
            fTaxRate                → vat_rate
            iUnitsOfMeasureID       → unit_code (mapped via UNIT_CODE_MAP)
        """
        lines = self.raw.get("lines", [])
        if not lines:
            # Build a single synthetic line from the header totals
            # This handles invoices where line detail wasn't stored
            logger.warning(
                f"[Mapper] No lines found for invoice "
                f"{self.raw.get('invoice_number')} — using header totals"
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
            price    = float(line.get("unit_price_excl", 0) or 0)
            net      = float(line.get("line_net_amount", 0) or qty * price)
            vat      = float(line.get("vat_amount", 0) or 0)

            # Use line_number from DB if available, else sequential
            line_num = str(line.get("line_number") or i)

            mapped_lines.append({
                "line_number":     line_num,
                "description":     str(line.get("description") or "Item").strip() or "Item",
                "quantity":        qty,
                "unit_code":       UNIT_CODE_MAP.get(
                                       line.get("unit_of_measure_id"),
                                       "EA"
                                   ),
                "unit_price":      price,
                "line_net_amount": round(net, 2),
                "vat_rate":        vat_rate,
                "vat_amount":      round(vat, 2),
                "tax_category":    "S" if vat_rate > 0 else "Z",
                "tax_scheme":      "VAT",
            })

        return mapped_lines

    # ── CURRENCY ──────────────────────────────────────────────

    def _exchange_rate(self) -> Optional[float]:
        """Get NGN exchange rate for foreign currency invoices."""
        currency = self._safe("currency_code", "NGN")
        if currency == "NGN":
            return None

        # Try Evolution's stored exchange rate first
        stored = self.raw.get("exchange_rate")
        if stored and float(stored) > 0:
            return float(stored)

        # Fall back to CBN API
        inv_date = self.raw.get("invoice_date")
        try:
            return get_ngn_exchange_rate(currency, inv_date)
        except Exception as e:
            logger.warning(f"[Mapper] Exchange rate lookup failed for {currency}: {e}")
            return 1.0

    # ── HELPERS ───────────────────────────────────────────────

    def _safe(self, key: str, default: str = "") -> str:
        val = self.raw.get(key, default)
        return str(val).strip() if val else default


class MappingError(Exception):
    """Raised when a required field cannot be mapped from Sage 200 to FIRS UBL."""
    pass