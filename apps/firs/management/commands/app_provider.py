"""
Inspect and smoke-test Access Point Providers.

    python manage.py app_provider list
    python manage.py app_provider check  --tin 04702493-0001
    python manage.py app_provider check  --tin 04702493-0001 --provider interswitch
    python manage.py app_provider status --tin 04702493-0001 --id <provider_invoice_id>
    python manage.py app_provider use    --tin 04702493-0001 --provider interswitch
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import ClientCompany
from apps.firs.providers import available_providers, get_provider, resolve_provider_name


class Command(BaseCommand):
    help = "List, health-check, query or switch the Access Point Provider (APP) for a client."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "check", "status", "use"])
        parser.add_argument("--tin", help="ClientCompany TIN")
        parser.add_argument("--provider", help="Override provider name (digitax, interswitch, ...)")
        parser.add_argument("--id", dest="invoice_id", help="Provider invoice ID (for status)")

    def _company(self, tin):
        if not tin:
            return None
        try:
            return ClientCompany.objects.get(tin=tin)
        except ClientCompany.DoesNotExist:
            raise CommandError(f"No ClientCompany with TIN {tin}")

    def handle(self, *args, **opts):
        action  = opts["action"]
        company = self._company(opts.get("tin"))

        if action == "list":
            self.stdout.write("Registered providers: " + ", ".join(available_providers()))
            for c in ClientCompany.objects.all():
                self.stdout.write(f"  {c.name} ({c.tin}) -> {resolve_provider_name(c)}")
            return

        if action == "use":
            if not company or not opts.get("provider"):
                raise CommandError("use requires --tin and --provider")
            name = opts["provider"].lower()
            if name not in available_providers():
                raise CommandError(f"Unknown provider {name}. Registered: {available_providers()}")
            company.app_provider = name
            company.save(update_fields=["app_provider"])
            self.stdout.write(self.style.SUCCESS(f"{company.name} now uses {name}"))
            return

        provider = get_provider(company, name=opts.get("provider"))

        if action == "check":
            result = provider.health_check()
            style = self.style.SUCCESS if result.get("success") else self.style.ERROR
            self.stdout.write(style(json.dumps(result, indent=2, default=str)))
            return

        if action == "status":
            if not opts.get("invoice_id"):
                raise CommandError("status requires --id")
            r = provider.get_status(opts["invoice_id"])
            self.stdout.write(json.dumps({
                "success": r.success, "provider": r.provider, "irn": r.irn,
                "provider_status": r.provider_status, "error": r.error, "raw": r.raw,
            }, indent=2, default=str))
