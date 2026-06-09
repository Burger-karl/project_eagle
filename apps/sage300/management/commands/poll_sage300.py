"""
Management command: poll_sage300

Manually trigger a Sage 300 invoice poll from the command line.

Usage:
    python manage.py poll_sage300
    python manage.py poll_sage300 --client "Company Name"
    python manage.py poll_sage300 --since 2025-11-01T00:00:00
    python manage.py poll_sage300 --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime


class Command(BaseCommand):
    help = "Manually poll Sage 300 and push new invoices to FIRS via DigiTax"

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            type=str,
            help="Client company name (omit to process all active Sage 300 clients)",
        )
        parser.add_argument(
            "--since",
            type=str,
            help="Only process invoices since this datetime (ISO format)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pull invoices and map fields but do NOT submit to FIRS",
        )

    def handle(self, *args, **options):
        from apps.core.models import ClientCompany
        from apps.sage300.pipeline import Sage300Pipeline
        from apps.sage300.odbc_client import Sage300ODBCClient
        from apps.sage300.field_mapper import Sage300FieldMapper
        from utils.exceptions import PipelineError

        since = None
        if options.get("since"):
            since = parse_datetime(options["since"])
            if not since:
                self.stderr.write(f"Invalid datetime: {options['since']}")
                return

        # Get target clients with Sage 300 configured
        qs = ClientCompany.objects.filter(
            is_active=True,
            sage300_mssql_database__gt="",
        )
        if options.get("client"):
            qs = qs.filter(name__icontains=options["client"])
            if not qs.exists():
                self.stderr.write(
                    f"No active Sage 300 client found matching: {options['client']}"
                )
                return

        if not qs.exists():
            self.stderr.write(
                "No Sage 300 clients configured. "
                "Set sage300_mssql_database on a ClientCompany record first."
            )
            return

        self.stdout.write(f"Processing {qs.count()} Sage 300 client(s)...")

        for company in qs:
            self.stdout.write(f"\n── {company.name} (Sage 300) ──────────────")

            if options.get("dry_run"):
                self.stdout.write("  [DRY RUN] Pulling and mapping only...")
                try:
                    odbc     = Sage300ODBCClient(company)
                    invoices = odbc.get_new_invoices(since=since, limit=5)
                    self.stdout.write(
                        self.style.SUCCESS(f"  Pulled {len(invoices)} invoices")
                    )
                    for raw in invoices:
                        mapper = Sage300FieldMapper(company, raw)
                        mapped = mapper.map()
                        self.stdout.write(
                            f"    {mapped['invoice_number']} | "
                            f"{mapped['invoice_type']} | "
                            f"₦{float(mapped['gross_amount']):,.2f}"
                        )
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  Error: {e}"))
            else:
                try:
                    pipeline = Sage300Pipeline(company)
                    results  = pipeline.run(since=since)
                    self.stdout.write(self.style.SUCCESS(
                        f"  Pulled:    {results['pulled']}\n"
                        f"  Cleared:   {results.get('cleared', 0)}\n"
                        f"  Failed:    {results.get('failed', 0)}\n"
                        f"  Skipped:   {results.get('skipped', 0)}"
                    ))
                except PipelineError as e:
                    self.stderr.write(self.style.ERROR(f"  Pipeline error: {e}"))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  Unexpected error: {e}"))

        self.stdout.write("\nDone.")
