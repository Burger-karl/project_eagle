"""
Management command: poll_sage200

Manually trigger an invoice poll from the command line.

Usage:
    python manage.py poll_sage200
    python manage.py poll_sage200 --client "Company Name"
    python manage.py poll_sage200 --since 2025-11-01
"""

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime


class Command(BaseCommand):
    help = "Manually poll Sage 200 and push new invoices to FIRS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            type=str,
            help="Client company name (omit to process all active clients)",
        )
        parser.add_argument(
            "--since",
            type=str,
            help="Only process invoices since this datetime (ISO format e.g. 2025-11-01T00:00:00)",
        )

    def handle(self, *args, **options):
        from apps.core.models import ClientCompany
        from apps.invoices.pipeline import InvoicePipeline
        from utils.exceptions import PipelineError

        since = None
        if options.get("since"):
            since = parse_datetime(options["since"])
            if not since:
                self.stderr.write(f"Invalid datetime format: {options['since']}")
                return

        # Get target clients
        qs = ClientCompany.objects.filter(is_active=True)
        if options.get("client"):
            qs = qs.filter(name__icontains=options["client"])
            if not qs.exists():
                self.stderr.write(f"No active client found matching: {options['client']}")
                return

        self.stdout.write(f"Processing {qs.count()} client(s)...")

        for company in qs:
            self.stdout.write(f"\n── {company.name} ──────────────────────────")
            try:
                pipeline = InvoicePipeline(company)
                results  = pipeline.run(since=since)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Pulled:    {results['pulled']}\n"
                        f"  Submitted: {results.get('submitted', 0)}\n"
                        f"  Cleared:   {results.get('cleared', 0)}\n"
                        f"  Failed:    {results.get('failed', 0)}\n"
                        f"  Skipped:   {results.get('skipped', 0)}"
                    )
                )
            except PipelineError as e:
                self.stderr.write(self.style.ERROR(f"  Pipeline error: {e}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Unexpected error: {e}"))

        self.stdout.write("\nDone.")
