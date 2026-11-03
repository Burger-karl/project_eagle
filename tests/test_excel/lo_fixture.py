"""Builds a Link Options style .xls workbook (one invoice per sheet) for tests.

Follows the layout documented in apps/excel_intake/link_options_parser.py. It is a
synthetic file: it proves the wiring and the adapter, not the parser against your
real invoices. Use `manage.py preview_excel <file>` on a real file for that.
"""
import io
from datetime import datetime

import xlwt

DATE = xlwt.easyxf(num_format_str="dd/mm/yyyy")


def invoice_sheet(wb, name, number, customer, lines, vat, total, when=datetime(2026, 9, 14), address=("1 Marina", "Lagos")):
    """lines: [(qty, description, unit_price, line_total)]"""
    ws = wb.add_sheet(name)
    ws.write(0, 3, "LINK OPTIONS & SYSTEMS LIMITED")
    ws.write(2, 12, "Invoice No.")
    ws.write(2, 13, number)
    ws.write(10, 2, "Name")
    ws.write(10, 3, customer)
    ws.write(10, 12, "Date")
    ws.write(10, 13, when, DATE)
    for i, part in enumerate(address):
        ws.write(11 + i, 3, part)
    ws.write(16, 2, "Qty")
    ws.write(16, 3, "Description")
    ws.write(16, 12, "Unit Price")
    ws.write(16, 13, "Amount")
    row = 18
    for qty, desc, unit, line_total in lines:
        ws.write(row, 2, qty)
        ws.write(row, 3, desc)
        ws.write(row, 12, unit)
        ws.write(row, 13, line_total)
        row += 1
    ws.write(40, 12, "VAT")
    ws.write(40, 13, vat)
    ws.write(42, 12, "TOTAL")
    ws.write(42, 13, total)
    ws.write(49, 0, "Thank you for your business")  # pad past 25 rows so the header is outside the totals window
    return ws


def unreadable_sheet(wb, name):
    """Long enough to be considered, but no invoice number or layout."""
    ws = wb.add_sheet(name)
    for r in range(14):
        ws.write(r, 1, f"notes line {r}")


def workbook(*, with_extras=True, with_mismatch=False) -> bytes:
    wb = xlwt.Workbook()
    invoice_sheet(wb, "INV 4521", 4521, "Zenith Beverages Plc,",
                  [(2, "Network cabling", 50000, 100000), (1, "Switch configuration", 25000, 25000)], 9375, 134375)
    invoice_sheet(wb, "INV 4522", 4522, "Walk-in customer", [(1, "Support call-out", 10000, 10000)], 750, 10750,
                  when=datetime(2026, 9, 15))
    if with_mismatch:  # lines total 10,000 but the sheet says net 9,000 (a discount row the reader skips)
        invoice_sheet(wb, "INV 4524", 4524, "Kaduna Grain Millers", [(1, "Maintenance", 10000, 10000)], 675, 9675)
    if with_extras:
        wb.add_sheet("Sheet1")                       # skipped by name on purpose
        unreadable_sheet(wb, "Draft notes")          # considered, but not an invoice
        invoice_sheet(wb, "Quote 4523", 4523, "Ogun Steel Works", [(1, "Costing", 5000, 5000)], 0, 0)  # zero total
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
