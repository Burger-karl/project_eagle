"""
Read-only preview of the next Sage 200 pull.

    python manage.py preview_sage200                    # the next 50 after the bookmark
    python manage.py preview_sage200 --limit 200
    python manage.py preview_sage200 --after 0          # look from the very beginning
    python manage.py preview_sage200 --client "Acme" --since 2026-09-01

It connects to each active client's Sage 200 database and reports what the next
pull would take. It saves nothing and never contacts FIRS, so it is safe to run
against a live company at any time. Use it before the first pull to check the
connection, see which kinds of transaction Sage returns, and spot rows the mapper
cannot handle.
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date, parse_datetime

from apps.core.models import ClientCompany
from apps.dashboard import sage_batch
from apps.dashboard.sage_batch import PullError


def parse_since(text):
    """Accept 2026-09-01 or 2026-09-01T08:00:00. Returns a naive datetime."""
    if not text:
        return None
    moment = parse_datetime(text)
    if moment is None:
        day = parse_date(text)
        if day is None:
            raise CommandError(f"Could not read --since '{text}'. Use 2026-09-01 or 2026-09-01T08:00:00.")
        moment = datetime(day.year, day.month, day.day)
    return moment.replace(tzinfo=None)  # SQL Server datetimes carry no timezone


def active_clients(name_part=None):
    clients = ClientCompany.objects.filter(is_active=True).order_by("name")
    if name_part:
        clients = clients.filter(name__icontains=name_part)
    if not clients.exists():
        raise CommandError(
            "No active client found. Add one in the admin (Core > Client companies) with "
            "'Is active' ticked and its Sage 200 server and database filled in."
        )
    return clients


class Command(BaseCommand):
    help = "Show what the next Sage 200 pull would take. Saves nothing and never contacts FIRS."

    def add_arguments(self, parser):
        parser.add_argument("--client", help="Only clients whose name contains this text.")
        parser.add_argument("--limit", type=int, default=sage_batch.DEFAULT_BATCH,
                            help=f"How many invoices (default {sage_batch.DEFAULT_BATCH}, most {sage_batch.MAX_BATCH}).")
        parser.add_argument("--after", type=int, help="Start after this Sage record number instead of the bookmark.")
        parser.add_argument("--since", help="Only rows with a transaction date after this (2026-09-01).")

    def handle(self, *args, **opts):
        since = parse_since(opts["since"])
        failures = 0
        for company in active_clients(opts["client"]):
            if not self._preview(company, opts["limit"], opts["after"], since):
                failures += 1
        if failures:
            raise CommandError(f"{failures} client(s) could not be read. See the messages above.")

    # ------------------------------------------------------------------
    def _preview(self, company, limit, after, since) -> bool:
        self._header(company)
        try:
            result = sage_batch.preview(company, limit, since=since, after_id=after)
        except PullError as exc:
            self.stderr.write(self.style.ERROR(f"\n  {exc}"))
            return False
        self._position(result, after, since)
        self._report(result, limit)
        return True

    def _header(self, company):
        out = self.stdout.write
        auth = "Windows login" if company.mssql_trusted else f"SQL login '{company.mssql_username}'"
        out(self.style.MIGRATE_HEADING(f"\n{company.name}"))
        out(f"  Server    {company.mssql_server}")
        out(f"  Database  {company.mssql_database}")
        out(f"  Driver    {company.mssql_driver}   ({auth})")
        out(f"  FIRS      {sage_batch.firs_environment()}")

    def _position(self, result, after, since):
        out = self.stdout.write
        out(f"  Position  after Sage record #{result['bookmark']:,}"
            + ("   (the bookmark: where the last pull stopped)" if after is None else "   (you chose this)"))
        if since:
            out(f"  Since     {since:%Y-%m-%d %H:%M} (rows with a transaction date after this)")

    # ------------------------------------------------------------------
    def _report(self, r, limit):
        out = self.stdout.write
        out(self.style.SUCCESS(f"\n  Connected. Sage returned {r['returned']} row(s)."))
        if not r["returned"]:
            out("  Nothing more to pull from this position.")
            return self._footer()
        if r["returned"] >= limit:
            out(f"  That is the {limit} you asked for; there may be more after it.")

        if r["older_than_since"]:
            out(self.style.ERROR(
                f"  WARNING: {r['older_than_since']} of these rows are dated on or before --since. Your copy of "
                "apps/sage200/odbc_client.py still has the date-filter bug (OR/AND without brackets), "
                "so old invoices are included. Replace it with the updated odbc_client.py from the update. "
                "Until then the counts below are not trustworthy."
            ))
        if r["date_from"]:
            out(f"  Dates     {r['date_from']}  to  {r['date_to']}")

        out("\n  Transaction types in these rows (Sage's own codes):")
        for kind in r["types"]:
            out(f"    {kind['count']:>5}  {kind['code']}  (TrCodeID {kind['code_id']})")
        out(f"  Debit rows (treated as invoices): {r['debit_rows']}   "
            f"Credit rows (treated as credit notes): {r['credit_rows']}")

        out(f"\n  Already in Eagle: {r['already_in_eagle']}")
        out(self.style.WARNING(f"  New, would be sent to FIRS by this pull: {r['new']}"))
        if r["new"]:
            out(f"  Would be reported as: {r['b2b']} business-to-business (buyer TIN known), "
                f"{r['b2c']} consumer (no buyer TIN)")
        if r["problem_total"]:
            out(self.style.ERROR(f"  {r['problem_total']} new row(s) the mapper cannot handle:"))
            for p in r["problems"]:
                out(f"    {p['count']:>5}  {p['reason']}")

        if r["samples"]:
            out(f"\n  First {len(r['samples'])} new row(s):")
            for s in r["samples"]:
                out("    {number:<14} {date}  {customer:<28.28} {gross:>16,.2f}  {code}".format(**s))
        self._footer()

    def _footer(self):
        self.stdout.write(self.style.SUCCESS("\n  Preview only: nothing was saved and nothing was sent to FIRS."))
