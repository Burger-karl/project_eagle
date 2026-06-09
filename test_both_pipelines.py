"""
Project Eagle — DigiTax Full Pipeline Test (Sage 200 + Sage 300)
Tests both middleware pipelines against DigiTax sandbox.
Run this once DigiTax sandbox enablement is confirmed.

Usage:
    python test_both_pipelines.py                    # test Sage 200
    python test_both_pipelines.py --system sage300   # test Sage 300
    python test_both_pipelines.py --system both      # test both
"""

import os
import sys
import django
import json
import argparse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.models import ClientCompany
from apps.firs.client import FIRSClient
from django.conf import settings


def ok(msg):    print(f"  ✅ {msg}")
def err(msg):   print(f"  ❌ {msg}")
def info(msg):  print(f"  ℹ  {msg}")
def head(msg):  print(f"\n{'='*65}\n{msg}\n{'='*65}")
def warn(msg):  print(f"  ⚠️  {msg}")


def test_sage200_pipeline(company, firs):
    """Test the complete Sage 200 → DigiTax → NRS pipeline."""
    head("SAGE 200 PIPELINE TEST")

    from apps.sage200.odbc_client import Sage200ODBCClient
    from apps.sage200.field_mapper import Sage200FieldMapper
    from apps.firs.invoice_schema import UBLInvoiceBuilder

    # Step 1 — ODBC Connection
    info("Connecting to Sage 200 database...")
    odbc = Sage200ODBCClient(company)
    if not odbc.test_connection():
        err("Cannot connect to Sage 200. Check .env credentials.")
        return False
    ok(f"Sage 200 connected. Total invoices: {odbc.count_invoices()}")

    # Step 2 — Pull invoice
    invoices = odbc.get_new_invoices(limit=1)
    if not invoices:
        err("No invoices found in Sage 200 database")
        return False
    raw = invoices[0]
    ok(f"Pulled: {raw['invoice_number']} | {raw['customer_name']} | ₦{raw['gross_amount']:,.2f}")

    # Step 3 — Map fields
    mapper = Sage200FieldMapper(company, raw)
    mapped = mapper.map()
    ok(f"Fields mapped: {mapped['invoice_type']} | {len(mapped['lines'])} lines")
    mapped["invoice_date"] = str(mapped["invoice_date"])[:10]

    # Step 4 — Submit via DigiTax
    return _submit_via_digitax(firs, mapped, "SAGE200")


def test_sage300_pipeline(company, firs):
    """Test the complete Sage 300 → DigiTax → NRS pipeline."""
    head("SAGE 300 PIPELINE TEST")

    from apps.sage300.odbc_client import Sage300ODBCClient, Sage300ConnectionError
    from apps.sage300.field_mapper import Sage300FieldMapper

    # Check Sage 300 database is configured
    db = getattr(company, "sage300_mssql_database", None) or company.mssql_database
    if not db:
        warn("Sage 300 database not configured on this company.")
        warn("Waiting for CEO to provide Sage 300 .bak file.")
        warn("Set sage300_mssql_database on the ClientCompany record to enable.")
        return None  # Not a failure — just not configured yet

    # Step 1 — ODBC Connection
    info("Connecting to Sage 300 database...")
    try:
        odbc = Sage300ODBCClient(company)
        if not odbc.test_connection():
            err("Cannot connect to Sage 300. Check credentials.")
            return False
        ok(f"Sage 300 connected. Total invoices: {odbc.count_invoices()}")
    except Sage300ConnectionError as e:
        err(f"Sage 300 connection failed: {e}")
        return False

    # Step 2 — Pull invoice
    invoices = odbc.get_new_invoices(limit=1)
    if not invoices:
        err("No invoices found in Sage 300 database")
        return False
    raw = invoices[0]
    ok(f"Pulled: {raw['invoice_number']} | {raw['customer_name']} | ₦{raw['gross_amount']:,.2f}")

    # Step 3 — Map fields
    mapper = Sage300FieldMapper(company, raw)
    mapped = mapper.map()
    ok(f"Fields mapped: {mapped['invoice_type']} | {len(mapped['lines'])} lines")
    mapped["invoice_date"] = str(mapped["invoice_date"])[:10]

    # Step 4 — Submit via DigiTax
    return _submit_via_digitax(firs, mapped, "SAGE300")


def _submit_via_digitax(firs, mapped, source):
    """Shared DigiTax submission logic for both Sage 200 and Sage 300."""

    # Step A — Create or find buyer party
    buyer_tin  = mapped.get("buyer_tin", "")
    buyer_name = mapped.get("buyer_name", "Consumer")
    party_id   = None

    if buyer_tin:
        info(f"Creating buyer party: {buyer_name} (TIN: {buyer_tin})")
        party_result = firs.create_party({
            "name":    buyer_name,
            "tin":     buyer_tin,
            "address": mapped.get("buyer_address", ""),
            "email":   "",
            "type":    "business",
        })
        if party_result["success"]:
            party_id = (
                party_result["data"].get("id") or
                party_result["data"].get("data", {}).get("id")
            )
            ok(f"Party created: {party_id}")
        else:
            warn(f"Party creation failed — proceeding as B2C: {party_result.get('error')}")
    else:
        info("No buyer TIN — submitting as B2C")

    # Step B — Create line items
    lines    = mapped.get("lines", [])
    line_ids = []

    for i, line in enumerate(lines):
        item_result = firs.create_item({
            "name":        line["description"],
            "description": line["description"],
            "unit_price":  float(line["unit_price"]),
            "tax_rate":    float(line.get("vat_rate", 7.5)),
            "unit_code":   line.get("unit_code", "EA"),
            "currency":    "NGN",
        })
        if item_result["success"]:
            item_id = (
                item_result["data"].get("id") or
                item_result["data"].get("data", {}).get("id")
            )
            line_ids.append({
                "item_id":    item_id,
                "quantity":   float(line["quantity"]),
                "unit_price": float(line["unit_price"]),
                "tax_rate":   float(line.get("vat_rate", 7.5)),
            })
            ok(f"Item {i+1} created: {item_id}")
        else:
            warn(f"Item creation failed — using inline: {item_result.get('error')}")
            line_ids.append({
                "description": line["description"],
                "quantity":    float(line["quantity"]),
                "unit_price":  float(line["unit_price"]),
                "tax_rate":    float(line.get("vat_rate", 7.5)),
            })

    # Step C — Build and submit invoice
    invoice_payload = {
        "supplier_tin":   mapped["supplier_tin"],
        "invoice_number": f"{source}-{mapped['invoice_number']}",
        "invoice_date":   str(mapped["invoice_date"])[:10],
        "currency":       "NGN",
        "invoice_type":   mapped.get("invoice_type", "B2B").lower(),
        "net_amount":     float(mapped["net_amount"]),
        "vat_amount":     float(mapped["vat_amount"]),
        "gross_amount":   float(mapped["gross_amount"]),
        "lines":          line_ids,
    }
    if party_id:
        invoice_payload["buyer_party_id"] = party_id
    if buyer_tin:
        invoice_payload["buyer_tin"] = buyer_tin
    if mapped.get("due_date"):
        invoice_payload["due_date"] = str(mapped["due_date"])[:10]

    info(f"Submitting {source} invoice to DigiTax → NRS MBS...")
    result = firs.create_invoice(invoice_payload)

    if result["success"]:
        data    = result["data"]
        irn     = data.get("irn")     or data.get("data", {}).get("irn")
        qr_code = data.get("qr_code") or data.get("data", {}).get("qr_code")
        status  = data.get("status")  or data.get("data", {}).get("status")
        print()
        print(f"  🎉 {source} INVOICE CLEARED BY NRS!")
        ok(f"IRN:    {irn}")
        ok(f"Status: {status}")
        ok(f"QR:     {str(qr_code)[:60]}...")
        return True
    else:
        err(f"Submission failed: {result.get('error')}")
        info("Full error:")
        print(f"  {json.dumps(result.get('error'), indent=2, default=str)[:400]}")
        return False


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Project Eagle pipelines")
    parser.add_argument(
        "--system",
        choices=["sage200", "sage300", "both"],
        default="sage200",
        help="Which pipeline to test (default: sage200)"
    )
    args = parser.parse_args()

    head("PROJECT EAGLE — PIPELINE TEST")

    # Check credentials
    api_key = getattr(settings, "FIRS_API_KEY", "")
    if not api_key:
        err("FIRS_API_KEY not set. Add your DigiTax X-API-Key to .env")
        sys.exit(1)

    company = ClientCompany.objects.first()
    if not company:
        err("No ClientCompany found. Create one via Django admin first.")
        sys.exit(1)

    firs = FIRSClient(provider="digitax")
    info(f"Provider:    DigiTax SANDBOX")
    info(f"Client:      {company.name}")
    info(f"Supplier TIN:{company.tin}")

    # Health check
    health = firs.health_check()
    if not health["success"]:
        err(f"DigiTax API unreachable: {health.get('detail')}")
        sys.exit(1)
    ok("DigiTax API connected")

    results = {}

    if args.system in ("sage200", "both"):
        results["sage200"] = test_sage200_pipeline(company, firs)

    if args.system in ("sage300", "both"):
        results["sage300"] = test_sage300_pipeline(company, firs)

    # Final summary
    head("FINAL TEST SUMMARY")
    for system, result in results.items():
        if result is True:
            print(f"  {system.upper():10} ✅  Pipeline working end-to-end")
        elif result is False:
            print(f"  {system.upper():10} ❌  Pipeline failed — see errors above")
        elif result is None:
            print(f"  {system.upper():10} ⏳  Not configured yet — awaiting .bak file")

    print()
    if all(r is True for r in results.values() if r is not None):
        print("  🚀 All configured pipelines are working!")
        print("     Next: Wire into Celery for automated background processing")
    elif any(r is None for r in results.values()):
        print("  ⏳ Waiting for CEO to provide Sage 300 .bak file")
        print("     All other components are ready")
