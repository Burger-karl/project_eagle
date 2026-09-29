"""
Link Options Custom Invoice Template Parser
=============================================
Parses the Link Options .xls invoice template format.

These are NOT the standard row-per-invoice Excel format.
They are formatted like printed invoices with data in
fixed cell positions — one invoice per sheet.

Template structure (confirmed from real files):
    Row 3:  Invoice number (col 13 or col 15)
    Row 11: Customer name (col 3), Date (col 13, Excel serial)
    Row 12: Customer address line 1 (col 3)
    Row 13: Address line 2 (col 3)
    Row 14: Address line 3 (col 3)
    Row 15: Address line 4 (col 3)
    Row 17: Column headers — Qty / Description / Unit Price / TOTAL
    Row 19+: Line items (qty in col 2, description in col 3,
              unit price in col 12, total in col 13 or 15)
    Near bottom: VAT row (col 12 or 14 = 'vat', next col = amount)
                 TOTAL row (col 12 or 14 = 'TOTAL', next col = amount)

Usage:
    from apps.excel_intake.link_options_parser import LinkOptionsInvoiceParser

    parser = LinkOptionsInvoiceParser(file_path="invoice.xls")
    invoices = parser.parse()
    for inv in invoices:
        print(inv)
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("apps.excel_intake")


class LinkOptionsParseError(Exception):
    pass


class LinkOptionsInvoiceParser:
    """
    Parses Link Options custom .xls invoice templates.
    Returns one invoice dict per sheet that looks like a valid invoice.

    Usage:
        parser = LinkOptionsInvoiceParser(file_path="invoice.xls")
        invoices = parser.parse()
    """

    # Sheets to skip — not invoice sheets
    SKIP_SHEET_NAMES = {"sheet1 (2)", "sheet1", "pro-forma", "quotation"}

    def __init__(self, file_path: str = None, file_bytes: bytes = None,
                 filename: str = ""):
        if not file_path and not file_bytes:
            raise LinkOptionsParseError("Provide file_path or file_bytes")
        self.file_path  = file_path
        self.file_bytes = file_bytes
        self.filename   = filename or (file_path or "upload")

    def parse(self) -> List[Dict]:
        """
        Parse all invoice sheets in the XLS file.
        Returns list of invoice dicts, one per valid sheet.
        """
        try:
            import xlrd
        except ImportError:
            raise LinkOptionsParseError(
                "xlrd is required for .xls files. Run: pip install xlrd"
            )

        try:
            if self.file_bytes:
                wb = xlrd.open_workbook(file_contents=self.file_bytes)
            else:
                wb = xlrd.open_workbook(self.file_path)
        except Exception as e:
            raise LinkOptionsParseError(
                f"Cannot open '{self.filename}': {e}"
            )

        invoices = []
        for sheet in wb.sheets():
            # Skip non-invoice sheets
            if sheet.name.lower().strip() in self.SKIP_SHEET_NAMES:
                logger.debug(f"[LOParser] Skipping sheet '{sheet.name}'")
                continue
            if sheet.nrows < 10:
                continue

            try:
                inv = self._parse_sheet(sheet, wb.datemode)
                if inv:
                    invoices.append(inv)
                    logger.info(
                        f"[LOParser] Parsed invoice {inv['invoice_number']} "
                        f"from sheet '{sheet.name}' in '{self.filename}'"
                    )
            except Exception as e:
                logger.warning(
                    f"[LOParser] Could not parse sheet '{sheet.name}' "
                    f"in '{self.filename}': {e}"
                )

        if not invoices:
            raise LinkOptionsParseError(
                f"No valid invoice sheets found in '{self.filename}'"
            )

        return invoices

    def _parse_sheet(self, sheet, datemode: int) -> Optional[Dict]:
        """Parse a single sheet into an invoice dict."""
        rows = self._read_rows(sheet)

        # ── Invoice number ────────────────────────────────────
        invoice_number = self._find_invoice_number(rows)
        if not invoice_number:
            logger.debug(
                f"[LOParser] Sheet '{sheet.name}' has no invoice number — skipping"
            )
            return None

        # ── Date ──────────────────────────────────────────────
        invoice_date = self._find_date(rows, datemode)

        # ── Customer ──────────────────────────────────────────
        buyer_name    = self._find_customer_name(rows)
        buyer_address = self._find_customer_address(rows)

        # ── Line items ────────────────────────────────────────
        lines = self._find_lines(rows)

        # ── Totals ────────────────────────────────────────────
        vat_amount, gross_amount = self._find_totals(rows)

        # Skip unfilled template sheets — two signals:
        # 1. Invoice number is the literal placeholder word "Invoice"
        # 2. Gross amount is zero (quotation/costing template, not final)
        if str(invoice_number).strip().lower() == "invoice":
            logger.debug(
                f"[LOParser] Sheet '{sheet.name}' has placeholder invoice "
                f"number 'Invoice' — skipping unfilled template"
            )
            return None

        if gross_amount == 0.0:
            logger.debug(
                f"[LOParser] Sheet '{sheet.name}' has zero gross amount "
                f"— skipping (likely a quotation/costing template)"
            )
            return None

        # Calculate net from gross - vat
        net_amount = round(float(gross_amount) - float(vat_amount), 2)

        # Derive VAT rate from amounts
        vat_rate = 0.0
        if net_amount > 0 and vat_amount > 0:
            vat_rate = round((float(vat_amount) / net_amount) * 100, 2)

        # Build clean invoice dict matching ExcelParser output format
        return {
            "invoice_number":  str(invoice_number).strip(),
            "invoice_date":    invoice_date,
            "buyer_name":      buyer_name,
            "buyer_tin":       "",       # not in template — Link Options will add
            "buyer_address":   buyer_address,
            "description":     lines[0]["description"] if lines else "Services",
            "quantity":        float(lines[0]["quantity"]) if lines else 1.0,
            "unit_price":      float(lines[0]["unit_price"]) if lines else net_amount,
            "unit_code":       "EA",
            "vat_rate":        vat_rate,
            "vat_amount":      float(vat_amount),
            "net_amount":      float(net_amount),
            "gross_amount":    float(gross_amount),
            "currency":        "NGN",
            "supplier_tin":    "",       # set by pipeline from client.tin
            "notes":           f"Sheet: {sheet.name}",
            "lines":           lines,
            "source_sheet":    sheet.name,
        }

    # ── EXTRACTION HELPERS ────────────────────────────────────

    def _read_rows(self, sheet) -> List[List[str]]:
        """Read all sheet rows as lists of stripped string values."""
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                row.append(str(cell.value).strip() if cell.value != "" else "")
            rows.append(row)
        return rows

    def _find_invoice_number(self, rows: List[List[str]]) -> Optional[str]:
        """
        Invoice number is on Row 3 (index 2).
        It appears in the cell after 'Invoice No.' label.
        In most sheets it's at col 13; in 'sage - international' it may be
        at col 15 depending on layout.
        """
        for r in range(min(6, len(rows))):
            row = rows[r]
            for c, val in enumerate(row):
                if "invoice no" in val.lower():
                    # Take the next non-empty cell on the same row
                    for nc in range(c + 1, len(row)):
                        if row[nc]:
                            # Strip trailing .0 from numeric invoice numbers
                            num = row[nc]
                            try:
                                num = str(int(float(num)))
                            except (ValueError, TypeError):
                                pass
                            return num
        # Fallback: look for a numeric value in row 3 after col 10
        if len(rows) > 2:
            row = rows[2]
            for c in range(10, len(row)):
                val = row[c]
                if val and val not in ("INVOICE", "Invoice No."):
                    try:
                        return str(int(float(val)))
                    except (ValueError, TypeError):
                        if len(val) < 20:
                            return val
        return None

    def _find_date(self, rows: List[List[str]], datemode: int) -> str:
        """
        Date is on Row 11 (index 10), in the cell after 'Date' label.
        Stored as an Excel serial date number.
        """
        import xlrd
        for r in range(8, min(15, len(rows))):
            row = rows[r]
            for c, val in enumerate(row):
                if val.lower() == "date":
                    for nc in range(c + 1, len(row)):
                        if row[nc]:
                            try:
                                serial = float(row[nc])
                                return xlrd.xldate_as_datetime(
                                    serial, datemode
                                ).strftime("%Y-%m-%d")
                            except (ValueError, TypeError):
                                return str(row[nc])[:10]
        return datetime.today().strftime("%Y-%m-%d")

    def _find_customer_name(self, rows: List[List[str]]) -> str:
        """Customer name is on Row 11 (index 10), col 3, after 'Name' label."""
        for r in range(8, min(15, len(rows))):
            row = rows[r]
            for c, val in enumerate(row):
                if val.lower() == "name":
                    for nc in range(c + 1, len(row)):
                        if row[nc]:
                            return row[nc].strip().rstrip(",")
        return "Unknown Customer"

    def _find_customer_address(self, rows: List[List[str]]) -> str:
        """Customer address spans rows 12-16 (index 11-15) in col 3."""
        address_parts = []
        # After finding 'Name' row, collect next 4 non-empty cells in col 3
        name_row = None
        for r in range(8, min(15, len(rows))):
            row = rows[r]
            if any(v.lower() == "name" for v in row):
                name_row = r
                break

        if name_row is not None:
            for r in range(name_row + 1, min(name_row + 6, len(rows))):
                row = rows[r]
                # Address is always in col 3 (index 3)
                if len(row) > 3 and row[3]:
                    part = row[3].strip().rstrip(",.")
                    if part and part.lower() not in ("", "customer"):
                        address_parts.append(part)

        return ", ".join(address_parts) if address_parts else ""

    def _find_lines(self, rows: List[List[str]]) -> List[Dict]:
        """
        Line items start after the header row (Row 17, index 16)
        which contains 'Qty' and 'Description'.
        Each data row has a numeric quantity in col 2.
        """
        lines       = []
        header_row  = None

        # Find header row containing 'Qty' and 'Description'
        for r, row in enumerate(rows):
            row_lower = [v.lower() for v in row]
            if "qty" in row_lower and "description" in row_lower:
                header_row = r
                break

        if header_row is None:
            return []

        line_num = 1
        for r in range(header_row + 1, len(rows)):
            row = rows[r]
            if not any(row):
                continue

            # Col 2 must be a valid positive numeric quantity
            qty_val = row[2] if len(row) > 2 else ""
            try:
                qty = float(qty_val)
                if qty <= 0:
                    continue
            except (ValueError, TypeError):
                continue

            # Description in col 3
            description = row[3] if len(row) > 3 else ""
            if not description or len(description) < 3:
                continue

            # Skip subtotal/discount rows that have negative qty or keywords
            if any(kw in description.lower() for kw in
                   ("sub total", "subtotal", "less discount", "discount",
                    "annual subscription", "training", "implementation")):
                continue

            # Unit price — look in cols 12 and 13
            unit_price = 0.0
            for pc in range(12, min(16, len(row))):
                try:
                    v = float(row[pc])
                    if v > 0:
                        unit_price = v
                        break
                except (ValueError, TypeError):
                    pass

            # Line total — take last numeric value in the row
            line_total = unit_price
            for pc in range(len(row) - 1, 11, -1):
                try:
                    v = float(row[pc])
                    if v > 0:
                        line_total = v
                        break
                except (ValueError, TypeError):
                    pass

            if unit_price == 0 and line_total == 0:
                continue

            lines.append({
                "line_number":     str(line_num),
                "description":     description.strip(),
                "quantity":        qty,
                "unit_price":      unit_price if unit_price > 0 else line_total,
                "line_net_amount": line_total,
                "unit_code":       "EA",
                "vat_rate":        7.5,   # will be overridden after totals calc
                "vat_amount":      0.0,   # will be overridden after totals calc
                "tax_category":    "S",
                "tax_scheme":      "VAT",
            })
            line_num += 1

        return lines

    def _find_totals(self, rows: List[List[str]]) -> tuple:
        """
        Find VAT amount and Grand Total from the bottom section.

        VAT row:   a cell contains 'vat' (case-insensitive),
                   the next non-empty cell on the same row is the VAT amount.
        TOTAL row: a cell contains 'TOTAL' (case-insensitive),
                   the next non-empty cell is the grand total.

        Fallback VAT detection: in some Link Options templates there is
        no 'vat' label — the VAT amount sits as a bare number on the row
        immediately before the TOTAL row. We detect this by finding the
        TOTAL row first, then checking the row above it for a lone number.
        """
        vat_amount   = 0.0
        gross_amount = 0.0
        total_row_idx = None

        # Search bottom 25 rows for VAT and TOTAL labels
        start = max(0, len(rows) - 25)
        for r in range(start, len(rows)):
            row = rows[r]
            for c, val in enumerate(row):
                val_lower = val.lower().strip()

                if val_lower == "vat" and vat_amount == 0.0:
                    for nc in range(c + 1, len(row)):
                        try:
                            v = float(row[nc])
                            if v > 0:
                                vat_amount = v
                                break
                        except (ValueError, TypeError):
                            pass

                if val_lower == "total" and gross_amount == 0.0:
                    total_row_idx = r
                    for nc in range(c + 1, len(row)):
                        try:
                            v = float(row[nc])
                            if v > 0:
                                gross_amount = v
                                break
                        except (ValueError, TypeError):
                            pass

        # Fallback VAT: bare number on 1-3 rows above the TOTAL row
        # (no label — used in the 'local' sheet format)
        if vat_amount == 0.0 and total_row_idx is not None and gross_amount > 0:
            for r in range(total_row_idx - 1, max(0, total_row_idx - 4), -1):
                row = rows[r]
                nums = []
                for val in row:
                    try:
                        v = float(val)
                        if v > 0:
                            nums.append(v)
                    except (ValueError, TypeError):
                        pass
                # A VAT row has exactly one positive number and it's < gross
                if len(nums) == 1 and nums[0] < gross_amount:
                    vat_amount = nums[0]
                    break

        # Final fallback: derive from gross - net (if subtotal is visible)
        # If still nothing found, return 0
        if gross_amount == 0.0:
            for r in range(len(rows) - 1, max(0, len(rows) - 20), -1):
                row = rows[r]
                for val in reversed(row):
                    try:
                        v = float(val)
                        if v > 1000:
                            gross_amount = v
                            break
                    except (ValueError, TypeError):
                        pass
                if gross_amount:
                    break

        return vat_amount, gross_amount