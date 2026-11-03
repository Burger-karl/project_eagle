"""
Sample data so the dashboard has something to show before real invoices arrive.

    python manage.py seed_demo_data            # create about 140 sample invoices
    python manage.py seed_demo_data --clear    # remove everything this command made

Safety:
  * Everything belongs to one client, "Demo client (sample data)". That client
    is inactive, so the Sage pollers and the health check skip it and nothing
    here is ever sent to FIRS.
  * Creating data is refused unless DEBUG is on (or you pass --force).
  * --clear deletes only that client's rows.
"""
import random
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import ClientCompany
from apps.excel_intake.models import ExcelUpload
from apps.invoices.models import (
    BuyerTINMapping,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    SubmissionLog,
)

DEMO_NAME = "Demo client (sample data)"
DEMO_TIN = "00000000-0000"

BUYERS = [
    ("Zenith Beverages Plc", "40112233-0001"),
    ("Ogun Steel Works Ltd", "40223344-0001"),
    ("Abuja Retail Hub", "40334455-0001"),
    ("Niger Delta Marine Services", "40445566-0001"),
    ("Port Harcourt Cold Chain Ltd", "40556677-0001"),
    ("Ikeja Medical Supplies", "40667788-0001"),
    ("Kaduna Grain Millers", "40990011-0001"),
    ("Enugu Pharma Distributors", "41001122-0001"),
]
GOVERNMENT = ("Federal Ministry of Works (Procurement)", "40778899-0001")
WALK_IN = ("Walk-in customer", "")
ITEMS = [
    "Bagged rice, 50 kg",
    "Vegetable oil, 25 L",
    "Freight, Lagos to Kano",
    "Cold storage per pallet",
    "Sesame seed, export grade",
    "Packaging materials",
    "Monthly logistics retainer",
    "Warehouse handling fee",
]
FAILURES = [
    ("FIRS_TIMEOUT", "FIRS gateway timed out after 30 seconds", 504),
    ("CONN_RESET", "Connection to FIRS was reset before a response arrived", 0),
    ("HTTP_503", "FIRS returned 503 Service Unavailable", 503),
]
REJECTIONS = [
    ("TIN_NOT_FOUND", "Buyer TIN is not registered with FIRS"),
    ("BAD_DATE", "IssueDate must be an ISO 8601 datetime"),
    ("VAT_MISMATCH", "Line VAT does not add up to the invoice VAT total"),
    ("DUP_INVOICE", "An invoice with this number was already submitted"),
]
VAT_RATE = Decimal("7.5")


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = "Create (or with --clear, remove) sample data for the Eagle dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Remove sample data.")
        parser.add_argument("--count", type=int, default=140, help="Invoices to create.")
        parser.add_argument(
            "--force", action="store_true", help="Allow creating data with DEBUG off."
        )

    def handle(self, *args, **opts):
        if opts["clear"]:
            return self.clear()
        if not settings.DEBUG and not opts["force"]:
            raise CommandError(
                "DEBUG is off, so sample data was not created. "
                "Run with --force if you really want it."
            )
        if ClientCompany.objects.filter(name=DEMO_NAME).exists():
            raise CommandError(
                "Sample data already exists. Run with --clear first to start over."
            )
        self.create(max(1, opts["count"]))

    # ------------------------------------------------------------------
    def clear(self):
        client = ClientCompany.objects.filter(name=DEMO_NAME).first()
        if not client:
            self.stdout.write("No sample data to remove.")
            return
        with transaction.atomic():
            Invoice.objects.filter(client=client).delete()
            ExcelUpload.objects.filter(client=client).delete()
            client.delete()  # tin mappings cascade
        self.stdout.write(self.style.SUCCESS("Sample data removed."))

    # ------------------------------------------------------------------
    @transaction.atomic
    def create(self, count: int):
        rng = random.Random(2026)
        now = timezone.now()
        client = ClientCompany.objects.create(
            name=DEMO_NAME,
            tin=DEMO_TIN,
            address="Lagos, Nigeria",
            email="demo@example.invalid",
            is_active=False,  # keeps pollers and health checks away from it
            mssql_server="demo",
            mssql_database="demo",
        )

        made = []
        for n in range(count):
            days_ago = int(rng.random() ** 1.5 * 45)
            created = now - timedelta(days=days_ago, minutes=rng.randint(0, 600))
            invoice = self._invoice(rng, client, n, days_ago, created, now)
            made.append((invoice, created))

        # created_at is auto_now_add, so backdate it after creation.
        for invoice, created in made:
            Invoice.objects.filter(pk=invoice.pk).update(created_at=created)

        for i, (name, tin) in enumerate(BUYERS):
            ok = i not in (3, 6)
            BuyerTINMapping.objects.create(
                client=client,
                sage_account_ref=f"CUS{101 + i:03d}",
                buyer_name=name,
                buyer_tin=tin,
                is_validated=ok,
                validated_at=now - timedelta(days=i + 2) if ok else None,
            )

        self._uploads(client, now)
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {count} sample invoices under '{DEMO_NAME}'. "
                "Remove them any time with: python manage.py seed_demo_data --clear"
            )
        )

    # ------------------------------------------------------------------
    def _invoice(self, rng, client, n, days_ago, created, now):
        kind = rng.random()
        inv_type = "B2B" if kind < 0.78 else "B2C" if kind < 0.92 else "B2G"
        buyer = (
            WALK_IN if inv_type == "B2C"
            else GOVERNMENT if inv_type == "B2G"
            else rng.choice(BUYERS)
        )

        roll = rng.random()
        if days_ago <= 1:
            status = (
                "PENDING" if roll < 0.20 else "SUBMITTING" if roll < 0.32
                else "CLEARED" if roll < 0.60 else "WRITTEN_BACK" if roll < 0.83
                else "FAILED" if roll < 0.92 else "REJECTED"
            )
        else:
            status = (
                "FAILED" if roll < 0.06 else "REJECTED" if roll < 0.12
                else "CANCELLED" if roll < 0.15 else "CLEARED" if roll < 0.25
                else "WRITTEN_BACK"
            )

        # Lines first, so the header totals always add up.
        lines, net, vat = [], Decimal("0"), Decimal("0")
        for line_no in range(1, rng.randint(1, 3) + 1):
            qty = Decimal(rng.randint(1, 40))
            base = 9_000 if inv_type == "B2C" else 60_000
            unit = money(base * (0.4 + rng.random() * 6))
            line_net = money(qty * unit)
            line_vat = money(line_net * VAT_RATE / 100)
            net += line_net
            vat += line_vat
            lines.append((line_no, rng.choice(ITEMS), qty, unit, line_net, line_vat))

        invoice_date = timezone.localtime(created).date()
        number = f"INV-{10400 + n}"
        attempts = {
            "PENDING": 0, "CANCELLED": 0, "SUBMITTING": 1,
            "REJECTED": rng.choice([1, 1, 2]),
            "CLEARED": rng.choice([1, 1, 1, 2]),
            "WRITTEN_BACK": rng.choice([1, 1, 1, 2]),
            "FAILED": 5 if days_ago > 1 else rng.randint(1, 4),
        }[status]

        fields = dict(
            client=client,
            sage_invoice_id=f"DEMO-{n:05d}",
            sage_invoice_number=number,
            sage_account_ref=f"CUS{101 + n % 8:03d}",
            invoice_type=inv_type,
            document_type="381" if rng.random() < 0.05 else "380",
            invoice_date=invoice_date,
            due_date=invoice_date + timedelta(days=30),
            supplier_tin=client.tin,
            supplier_name=client.name,
            supplier_address=client.address,
            buyer_tin=buyer[1],
            buyer_name=buyer[0],
            buyer_address="Nigeria",
            net_amount=net,
            vat_amount=vat,
            gross_amount=net + vat,
            status=status,
            submission_attempts=attempts,
        )
        if attempts:
            fields["last_attempt_at"] = created + timedelta(minutes=rng.randint(3, 20))

        cleared_at = None
        if status in ("CLEARED", "WRITTEN_BACK"):
            cleared_at = fields["last_attempt_at"] + timedelta(seconds=rng.randint(2, 40))
            fields["cleared_at"] = cleared_at
            fields["irn"] = f"{number}-{uuid.UUID(int=rng.getrandbits(128)).hex[:8].upper()}-{invoice_date:%Y%m%d}"
            fields["csid"] = uuid.UUID(int=rng.getrandbits(128)).hex.upper()
        if status == "WRITTEN_BACK":
            fields["written_back_at"] = cleared_at + timedelta(minutes=rng.randint(1, 5))

        failure = rng.choice(FAILURES)
        rejection = rng.choice(REJECTIONS)
        if status == "FAILED":
            fields["error_code"], fields["error_message"] = failure[0], failure[1]
            if attempts < 5:
                fields["next_retry_at"] = now + timedelta(minutes=rng.randint(3, 40))
        elif status == "REJECTED":
            fields["error_code"], fields["error_message"] = rejection

        invoice = Invoice.objects.create(**fields)
        InvoiceLine.objects.bulk_create(
            InvoiceLine(
                invoice=invoice, line_number=no, description=desc, quantity=qty,
                unit_price=unit, line_net_amount=ln, vat_rate=VAT_RATE, vat_amount=lv,
            )
            for no, desc, qty, unit, ln, lv in lines
        )

        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            if last and status == "SUBMITTING":
                continue
            ok = last and status in ("CLEARED", "WRITTEN_BACK")
            if ok:
                code, error = 200, ""
            elif last and status == "REJECTED":
                code, error = 422, rejection[1]
            elif last and status == "FAILED":
                code, error = failure[2] or None, failure[1]
            else:
                code, error = 503, "FIRS returned 503 Service Unavailable"
            log = SubmissionLog.objects.create(
                invoice=invoice, attempt_number=attempt, endpoint="/invoice/submit",
                request_payload={}, response_status=code, success=ok,
                duration_ms=1000 + attempt * 120 if ok else 30000, error=error,
            )
            SubmissionLog.objects.filter(pk=log.pk).update(
                created_at=created + timedelta(minutes=attempt * 10)
            )
        return invoice

    # ------------------------------------------------------------------
    def _uploads(self, client, now):
        rows = [
            ("october_sales_batch_04.xlsx", "UPLOAD", "PROCESSING", 120, 118, 2, 64, 0, "", ""),
            ("LagosDepot_Oct_week4.xlsx", "FOLDER", "COMPLETE", 86, 86, 0, 86, 0, "", ""),
            ("invoices_kano_sept.xlsx", "EMAIL", "PARTIAL", 210, 204, 6, 171, 9, "", "accounts@example.invalid"),
            ("freight_manifest_oct.csv", "FOLDER", "PARSED", 44, 44, 0, 0, 0, "", ""),
            ("sales_export_27sep.xlsx", "UPLOAD", "FAILED", 0, 0, 0, 0, 0,
             "Column 'Invoice Date' was not found in sheet 1", ""),
            ("weekly_invoices_wk39.xlsx", "EMAIL", "COMPLETE", 63, 63, 0, 63, 0, "", "billing@example.invalid"),
        ]
        for i, (name, method, status, total, valid, invalid, cleared, failed, err, sender) in enumerate(rows):
            upload = ExcelUpload.objects.create(
                client=client, method=method, status=status, original_filename=name,
                total_rows=total, valid_rows=valid, invalid_rows=invalid,
                cleared_rows=cleared, failed_rows=failed, parse_error=err,
                sender_email=sender,
            )
            ExcelUpload.objects.filter(pk=upload.pk).update(
                created_at=now - timedelta(days=i * 1.4, hours=1)
            )
