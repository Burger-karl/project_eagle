"""
Excel Invoice Parser
Reads Excel files and extracts invoice rows.

Supports both the Link Options Standard Template
and flexible column detection for non-standard files.

Standard Template columns (row 1 = headers, row 2+ = data):
    A: Invoice Number
    B: Invoice Date       (DD/MM/YYYY or YYYY-MM-DD)
    C: Buyer Name
    D: Buyer TIN          (optional — B2C if blank)
    E: Buyer Address
    F: Item Description
    G: Quantity
    H: Unit Price (excl. VAT)
    I: VAT Rate %         (default 7.5)
    J: VAT Amount
    K: Gross Amount
    L: Currency           (default NGN)
    M: Notes              (optional)
"""

import hashlib
import logging
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl
from openpyxl.utils import get_column_letter

logger = logging.getLogger("apps.excel_intake")

# ── Standard Template Column Map ──────────────────────────────
# Key = internal field name, Value = list of accepted header strings
STANDARD_COLUMNS = {
    "invoice_number": [
        "invoice number", "invoice no", "invoice #",
        "inv number", "inv no", "document number", "doc no"
    ],
    "invoice_date": [
        "invoice date", "date", "issue date", "transaction date",
        "inv date", "doc date"
    ],
    "buyer_name": [
        "buyer name", "customer name", "client name", "company name",
        "buyer", "customer", "sold to"
    ],
    "buyer_tin": [
        "buyer tin", "customer tin", "tin", "tax id",
        "vat number", "vat no", "tax identification number"
    ],
    "buyer_address": [
        "buyer address", "customer address", "address",
        "delivery address", "bill to address"
    ],
    "description": [
        "item description", "description", "service description",
        "goods description", "particulars", "details", "item"
    ],
    "quantity": [
        "quantity", "qty", "units", "no of units", "amount of units"
    ],
    "unit_price": [
        "unit price", "price", "rate", "unit cost",
        "price excl vat", "price ex vat", "net price"
    ],
    "vat_rate": [
        "vat rate", "vat %", "tax rate", "tax %",
        "vat rate %", "vat percentage"
    ],
    "vat_amount": [
        "vat amount", "tax amount", "vat", "tax",
        "vat value", "tax value"
    ],
    "gross_amount": [
        "gross amount", "total", "total amount", "gross total",
        "amount due", "payable", "invoice total", "gross value"
    ],
    "currency": [
        "currency", "currency code", "ccy"
    ],
    "notes": [
        "notes", "remarks", "comment", "comments", "reference"
    ],
}

# ── Required fields ────────────────────────────────────────────
REQUIRED_FIELDS = [
    "invoice_number", "invoice_date", "buyer_name",
    "description", "quantity", "unit_price", "gross_amount"
]


class ExcelParseError(Exception):
    """Raised when an Excel file cannot be parsed."""
    pass


class ExcelParser:
    """
    Parses an Excel invoice file and extracts structured invoice data.

    Supports:
        - Standard Link Options template (column positions fixed)
        - Flexible header detection (finds columns by name)
        - Multi-row invoices (multiple lines per invoice grouped by invoice number)
        - Single-row invoices (one row = one invoice)

    Usage:
        parser = ExcelParser(file_path="/path/to/invoices.xlsx")
        result = parser.parse()
        # result.invoices  → list of invoice dicts
        # result.errors    → list of row-level errors
        # result.file_hash → SHA256 of the file
    """

    def __init__(
        self,
        file_path: str = None,
        file_bytes: bytes = None,
        filename:   str   = "",
        header_row: int   = 1,
    ):
        """
        Args:
            file_path:  Path to the Excel file on disk
            file_bytes: Raw bytes of the Excel file (for web upload)
            filename:   Original filename (for logging)
            header_row: Row number containing column headers (default 1)
        """
        if not file_path and not file_bytes:
            raise ExcelParseError("Must provide either file_path or file_bytes")

        self.file_path  = file_path
        self.file_bytes = file_bytes
        self.filename   = filename or (Path(file_path).name if file_path else "upload")
        self.header_row = header_row
        self._col_map   = {}  # maps field_name → column index

    # ── MAIN PARSE METHOD ─────────────────────────────────────

    def parse(self) -> "ParseResult":
        """
        Parse the Excel file and return a ParseResult.

        Returns:
            ParseResult with .invoices, .errors, .file_hash, .total_rows
        """
        logger.info(f"[ExcelParser] Parsing: {self.filename}")

        # Load workbook
        try:
            if self.file_bytes:
                from io import BytesIO
                wb = openpyxl.load_workbook(
                    BytesIO(self.file_bytes), read_only=True, data_only=True
                )
            else:
                wb = openpyxl.load_workbook(
                    self.file_path, read_only=True, data_only=True
                )
        except Exception as e:
            raise ExcelParseError(f"Cannot open Excel file '{self.filename}': {e}")

        # Use first sheet
        ws = wb.active
        if ws is None:
            raise ExcelParseError("Excel file has no sheets")

        # Compute file hash for duplicate detection
        file_hash = self._compute_hash()

        # Detect column positions from header row
        headers = self._read_header_row(ws)
        self._col_map = self._detect_columns(headers)

        missing_required = [
            f for f in REQUIRED_FIELDS if f not in self._col_map
        ]
        if missing_required:
            logger.warning(
                f"[ExcelParser] Missing columns: {missing_required}. "
                f"Detected: {list(self._col_map.keys())}"
            )

        # Parse data rows
        invoices   = []
        row_errors = []
        row_num    = 0

        for row in ws.iter_rows(
            min_row=self.header_row + 1,
            values_only=True
        ):
            row_num += 1

            # Skip completely empty rows
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue

            try:
                row_data = self._extract_row(row, row_num)
                errors   = self._validate_row(row_data, row_num)

                if errors:
                    row_errors.append({
                        "row":    row_num + self.header_row,
                        "errors": errors,
                        "data":   row_data,
                    })
                else:
                    invoices.append(row_data)

            except Exception as e:
                row_errors.append({
                    "row":    row_num + self.header_row,
                    "errors": [str(e)],
                    "data":   {},
                })

        wb.close()
        logger.info(
            f"[ExcelParser] {self.filename}: "
            f"{len(invoices)} valid, {len(row_errors)} errors"
        )

        return ParseResult(
            invoices=invoices,
            errors=row_errors,
            file_hash=file_hash,
            total_rows=row_num,
            column_map=self._col_map,
        )

    # ── HEADER DETECTION ──────────────────────────────────────

    def _read_header_row(self, ws) -> List[str]:
        """Read header row and return list of cleaned header strings."""
        headers = []
        for row in ws.iter_rows(
            min_row=self.header_row,
            max_row=self.header_row,
            values_only=True
        ):
            for cell in row:
                val = str(cell).strip().lower() if cell is not None else ""
                headers.append(val)
        return headers

    def _detect_columns(self, headers: List[str]) -> Dict[str, int]:
        """
        Match header strings to known field names.
        Returns dict of {field_name: column_index}.
        """
        col_map = {}
        for field, aliases in STANDARD_COLUMNS.items():
            for i, header in enumerate(headers):
                if any(alias in header for alias in aliases):
                    col_map[field] = i
                    break
        logger.debug(f"[ExcelParser] Detected columns: {col_map}")
        return col_map

    # ── ROW EXTRACTION ────────────────────────────────────────

    def _extract_row(self, row: tuple, row_num: int) -> Dict:
        """Extract one data row into a structured dict."""

        def get(field: str, default=None):
            idx = self._col_map.get(field)
            if idx is None or idx >= len(row):
                return default
            val = row[idx]
            return str(val).strip() if val is not None else default

        # Parse date
        raw_date = get("invoice_date")
        inv_date = self._parse_date(raw_date)

        # Parse numeric values
        quantity   = self._parse_decimal(get("quantity",   "1"), "quantity",   row_num)
        unit_price = self._parse_decimal(get("unit_price", "0"), "unit_price", row_num)
        vat_rate   = self._parse_decimal(get("vat_rate",  "7.5"), "vat_rate",  row_num)
        vat_amount = self._parse_decimal(get("vat_amount", "0"), "vat_amount", row_num)
        gross      = self._parse_decimal(get("gross_amount", "0"), "gross",    row_num)

        # Calculate missing values
        if not vat_amount and unit_price and quantity:
            net_amount = round(unit_price * quantity, 2)
            vat_amount = round(net_amount * (float(vat_rate) / 100), 2)
            gross      = round(net_amount + vat_amount, 2)
        else:
            net_amount = round(float(gross) - float(vat_amount), 2)

        return {
            "row_number":    row_num,
            "invoice_number": get("invoice_number", ""),
            "invoice_date":  inv_date,
            "buyer_name":    get("buyer_name", ""),
            "buyer_tin":     get("buyer_tin", ""),
            "buyer_address": get("buyer_address", ""),
            "description":   get("description", ""),
            "quantity":      float(quantity),
            "unit_price":    float(unit_price),
            "unit_code":     "EA",
            "vat_rate":      float(vat_rate),
            "vat_amount":    float(vat_amount),
            "net_amount":    float(net_amount),
            "gross_amount":  float(gross),
            "currency":      get("currency", "NGN") or "NGN",
            "notes":         get("notes", ""),
        }

    # ── VALIDATION ────────────────────────────────────────────

    def _validate_row(self, data: Dict, row_num: int) -> List[str]:
        """Validate a parsed row. Returns list of error strings."""
        errors = []

        if not data.get("invoice_number"):
            errors.append("Invoice number is blank")

        if not data.get("invoice_date"):
            errors.append("Invoice date is blank or invalid format")

        if not data.get("buyer_name"):
            errors.append("Buyer name is blank")

        if not data.get("description"):
            errors.append("Item description is blank")

        qty = data.get("quantity", 0)
        if not qty or float(qty) <= 0:
            errors.append("Quantity must be greater than 0")

        price = data.get("unit_price", 0)
        if float(price) < 0:
            errors.append("Unit price cannot be negative")

        gross = data.get("gross_amount", 0)
        if float(gross) <= 0:
            errors.append("Gross amount must be greater than 0")

        buyer_tin = data.get("buyer_tin", "")
        if buyer_tin and len(buyer_tin.replace("-", "")) < 8:
            errors.append(f"Buyer TIN '{buyer_tin}' appears invalid (too short)")

        currency = data.get("currency", "NGN")
        if currency and len(currency) != 3:
            errors.append(f"Currency code '{currency}' must be 3 characters (e.g. NGN)")

        return errors

    # ── HELPERS ───────────────────────────────────────────────

    @staticmethod
    def _parse_date(raw: str) -> Optional[str]:
        """Parse date from various formats → ISO string YYYY-MM-DD."""
        if not raw:
            return None
        raw = str(raw).strip()

        # Already ISO format
        if len(raw) == 10 and raw[4] == "-":
            return raw

        # Handle Excel serial date number
        try:
            serial = int(float(raw))
            if 30000 < serial < 60000:
                from datetime import timedelta
                base = datetime(1899, 12, 30)
                return (base + timedelta(days=serial)).date().isoformat()
        except (ValueError, TypeError):
            pass

        # Try common formats
        for fmt in (
            "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
            "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
            "%Y%m%d",
        ):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue

        # Handle datetime objects from openpyxl
        if hasattr(raw, "date"):
            return raw.date().isoformat()

        return None

    @staticmethod
    def _parse_decimal(raw: str, field: str, row: int) -> Decimal:
        """Parse a numeric value, stripping currency symbols and commas."""
        if raw is None:
            return Decimal("0")
        cleaned = (
            str(raw)
            .strip()
            .replace(",", "")
            .replace("₦", "")
            .replace("$", "")
            .replace("£", "")
            .replace(" ", "")
        )
        if not cleaned:
            return Decimal("0")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            logger.warning(
                f"[ExcelParser] Row {row}: Cannot parse {field}='{raw}' as number"
            )
            return Decimal("0")

    def _compute_hash(self) -> str:
        """SHA256 hash of file for duplicate detection."""
        h = hashlib.sha256()
        if self.file_bytes:
            h.update(self.file_bytes)
        elif self.file_path:
            with open(self.file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
        return h.hexdigest()


class ParseResult:
    """Result object returned by ExcelParser.parse()."""

    def __init__(
        self,
        invoices:   List[Dict],
        errors:     List[Dict],
        file_hash:  str,
        total_rows: int,
        column_map: Dict,
    ):
        self.invoices   = invoices
        self.errors     = errors
        self.file_hash  = file_hash
        self.total_rows = total_rows
        self.column_map = column_map

    @property
    def valid_count(self):
        return len(self.invoices)

    @property
    def error_count(self):
        return len(self.errors)

    @property
    def has_errors(self):
        return len(self.errors) > 0

    def summary(self) -> Dict:
        return {
            "total_rows":  self.total_rows,
            "valid_rows":  self.valid_count,
            "error_rows":  self.error_count,
            "file_hash":   self.file_hash,
            "columns_detected": list(self.column_map.keys()),
        }
