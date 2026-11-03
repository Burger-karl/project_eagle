"""
Read-only preview of an invoice file: what Eagle would send to FIRS.

    python manage.py preview_excel "C:/invoices/September.xls"
    python manage.py preview_excel September.xls --client "Link Options"

It reads the file the same way an upload does (.xls Link Options sheets, .xlsx and
.csv standard files) and prints every invoice it found: number, date, customer,
whether it would be reported as business-to-business or consumer, the lines, and
whether the amounts add up. Sheets it could not read are listed with the reason.
It saves nothing and never contacts FIRS, so try your real files here first.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import ClientCompany
from apps.excel_intake.excel_field_mapper import ExcelFieldMapper, MappingError
from apps.excel_intake.excel_parser import ExcelParseError
from apps.excel_intake.parsing import get_parser


class Command(BaseCommand):
    help = "Show what Eagle would send for an invoice file. Saves nothing and never contacts FIRS."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The .xls, .xlsx or .csv file.")
        parser.add_argument("--client", help="Client name (or part). Needed to match buyer TINs when several are active.")

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        client, note = self._client(opts["client"])
        data = path.read_bytes()

        try:
            result = get_parser(data, path.name, client=client).parse()
        except ExcelParseError as exc:
            raise CommandError(f"Could not read {path.name}: {exc}")

        out = self.stdout.write
        out(self.style.MIGRATE_HEADING(f"\n{path.name}"))
        out(f"  Format   {result.column_map.get('format', 'standard spreadsheet')}")
        out(f"  Client   {client.name if client else '(none: buyer TINs not matched)'}")
        if note:
            out(self.style.WARNING(f"  {note}"))
        out(self.style.SUCCESS(f"\n  {result.valid_count} invoice(s) would be sent, {result.error_count} problem(s)."))

        sent_b2b = sent_b2c = 0
        for inv in result.invoices:
            b2b = bool(str(inv.get("buyer_tin") or "").strip())
            sent_b2b, sent_b2c = sent_b2b + b2b, sent_b2c + (not b2b)
            self._invoice(inv, client, b2b)

        for err in result.errors:
            where = err.get("data", {}).get("sheet") or f"row {err['row']}"
            out(self.style.ERROR(f"\n  NOT SENT  ({where})"))
            for message in err.get("errors", []):
                out(f"    {message}")

        if result.invoices:
            out(f"\n  Business-to-business: {sent_b2b}    Consumer: {sent_b2c}")
        out(self.style.SUCCESS("\n  Preview only: nothing was saved and nothing was sent to FIRS."))

    # ------------------------------------------------------------------
    def _client(self, name_part):
        clients = ClientCompany.objects.filter(is_active=True).order_by("name")
        if name_part:
            clients = clients.filter(name__icontains=name_part)
            if not clients.exists():
                raise CommandError(f"No active client matches '{name_part}'.")
        if clients.count() == 1:
            return clients.first(), ""
        if clients.count() > 1:
            return None, "Several clients are active; add --client to match buyer TINs and check the mapping."
        return None, "No active client yet; add one in the admin to match buyer TINs."

    def _invoice(self, inv, client, b2b):
        out = self.stdout.write
        label = inv.get("source_sheet") and f"Sheet {inv['row_number']}" or f"Row {inv.get('row_number')}"
        out(self.style.MIGRATE_LABEL(f"\n  {label}   Invoice {inv.get('invoice_number')}   {inv.get('invoice_date')}"))
        out(f"    Customer  {inv.get('buyer_name')}")
        if b2b:
            out(f"    Reported  as BUSINESS (buyer TIN {inv['buyer_tin']})")
        else:
            out(self.style.WARNING(
                "    Reported  as CONSUMER: no buyer TIN found. If this is a business, add the customer "
                "(exact name) on the Buyer TINs page."))
        for line in inv.get("lines") or []:
            out("      {qty:>6g} x {desc:<34.34} {unit:>14,.2f} = {net:>14,.2f}".format(
                qty=float(line.get("quantity") or 0), desc=str(line.get("description")),
                unit=float(line.get("unit_price") or 0), net=float(line.get("line_net_amount") or 0)))
        out("    Net {n:,.2f}   VAT {v:,.2f}   Total {g:,.2f}".format(
            n=float(inv.get("net_amount") or 0), v=float(inv.get("vat_amount") or 0), g=float(inv.get("gross_amount") or 0)))
        if client:
            try:
                ExcelFieldMapper(client, inv).map()
            except MappingError as exc:
                out(self.style.ERROR(f"    PROBLEM   {exc}"))
