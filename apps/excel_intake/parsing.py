"""
Choose the right parser for an invoice file.

Upload, watched folder and email all call get_parser(), so a file is read the same
way whichever door it comes in through:

  * .xlsx / .csv   the standard one-row-per-invoice-line layout (ExcelParser)
  * .xls           Link Options print-style invoices: one invoice per sheet,
                   fixed cell positions (LinkOptionsInvoiceParser)

The Link Options parser returns bare invoice dicts and quietly skips any sheet it
cannot read. This adapter turns its output into the same ParseResult the standard
parser gives the pipeline, and adds the two things the sheets cannot provide:

  * a row for every sheet that was NOT read, with the reason, so invoices never
    vanish without a trace
  * the buyer's TIN, matched by customer name against the Buyer TINs table. The
    template has no TIN, and a row is only reported as business-to-business if it
    carries one.
"""
import hashlib
import logging
import re
from datetime import date, datetime

from .excel_parser import ExcelParseError, ExcelParser, ParseResult
from .link_options_parser import LinkOptionsInvoiceParser, LinkOptionsParseError

logger = logging.getLogger("apps.excel_intake")

ZIP_MAGIC = b"PK\x03\x04"  # an .xlsx is a zip, even if someone gave it an .xls name
TOLERANCE = 1.00  # naira: lines may differ from the invoice net by rounding, no more


def get_parser(file_bytes: bytes, filename: str, client=None):
    """Return an object with .parse() -> ParseResult. `client` lets Link Options files find buyer TINs."""
    is_old_xls = (filename or "").lower().endswith(".xls") and not file_bytes[:4] == ZIP_MAGIC
    if is_old_xls:
        return LinkOptionsFileParser(file_bytes, filename, client=client)
    return ExcelParser(file_bytes=file_bytes, filename=filename)


def normalise_name(name) -> str:
    """'Zenith Bank Plc.' and 'ZENITH  BANK PLC' compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[.,]", " ", str(name or "")).casefold()).strip()


def _tin_by_name(client) -> dict:
    if client is None:
        return {}
    from apps.invoices.models import BuyerTINMapping
    return {
        normalise_name(m.buyer_name): m.buyer_tin
        for m in BuyerTINMapping.objects.filter(client=client)
        if m.buyer_tin
    }


def _iso(value) -> str:
    """Dates must be plain text: the row is stored as JSON."""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return "" if value is None else str(value)


def _sheet_names(file_bytes: bytes):
    """(name, rows) for every sheet, or [] if xlrd cannot read the file."""
    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=file_bytes)
        return [(s.name, s.nrows) for s in wb.sheets()]
    except Exception:
        return []


class LinkOptionsFileParser:
    def __init__(self, file_bytes: bytes, filename: str = "", client=None):
        self.file_bytes = file_bytes
        self.filename = filename
        self.client = client

    def parse(self) -> ParseResult:
        try:
            found = LinkOptionsInvoiceParser(file_bytes=self.file_bytes, filename=self.filename).parse()
        except LinkOptionsParseError as exc:
            raise ExcelParseError(str(exc))

        sheets = _sheet_names(self.file_bytes)
        position = {name: i for i, (name, _) in enumerate(sheets, start=1)}
        tins = _tin_by_name(self.client)

        invoices, errors = [], []
        for n, inv in enumerate(found, start=1):
            row = dict(inv)
            row["row_number"] = position.get(inv.get("source_sheet"), n)  # the sheet's place in the workbook
            row["invoice_date"] = _iso(row.get("invoice_date"))
            if not row.get("buyer_tin"):
                row["buyer_tin"] = tins.get(normalise_name(row.get("buyer_name")), "")

            # The reader skips some lines (discounts, anything mentioning training or
            # implementation). If what is left no longer adds up to the invoice, sending
            # it would report the wrong amounts to FIRS, so show it as a problem instead.
            lines_net = round(sum(float(l.get("line_net_amount") or 0) for l in row.get("lines") or []), 2)
            if row.get("lines") and abs(lines_net - float(row["net_amount"])) > TOLERANCE:
                errors.append({
                    "row": row["row_number"],
                    "data": {"sheet": row.get("source_sheet"), "invoice_number": row["invoice_number"],
                             "lines_total": lines_net, "invoice_net": row["net_amount"]},
                    "errors": [
                        f"Invoice {row['invoice_number']}: the lines add up to {lines_net:,.2f} but the invoice "
                        f"total before VAT is {row['net_amount']:,.2f}. A line may be missing (the reader skips "
                        "lines such as discounts, or ones mentioning training, implementation or annual "
                        "subscription). Nothing was sent to FIRS."
                    ],
                })
                continue
            invoices.append(row)

        read = {inv["source_sheet"] for inv in found}
        skip_names = LinkOptionsInvoiceParser.SKIP_SHEET_NAMES
        for name, nrows in sheets:
            if name in read or name.lower().strip() in skip_names or nrows < 10:
                continue  # read fine, or deliberately not an invoice sheet
            errors.append({
                "row": position[name],
                "data": {"sheet": name},
                "errors": [
                    f"Sheet '{name}' was not read as an invoice. Its layout did not match, it has no "
                    "invoice number, it is an unfilled template, or its total is zero."
                ],
            })

        return ParseResult(
            invoices=invoices,
            errors=errors,
            file_hash=hashlib.sha256(self.file_bytes).hexdigest(),
            total_rows=len(invoices) + len(errors),
            column_map={"format": "link_options_xls", "sheets": len(sheets)},
        )
