import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.models import ClientCompany
from apps.sage200.odbc_client import Sage200ODBCClient
from apps.sage200.field_mapper import Sage200FieldMapper
from apps.firs.client import FIRSClient
from apps.firs.invoice_schema import UBLInvoiceBuilder

company = ClientCompany.objects.first()
odbc    = Sage200ODBCClient(company)
firs    = FIRSClient(
    api_key=company.firs_api_key or None,
    secret_key=company.firs_secret_key or None,
)

print("FIRS Base URL:", firs.base_url)

# Test what the actual endpoints look like
print("TIN validate URL:", f"{firs.base_url}/taxpayers/test/validate")
print("B2B submit URL:", f"{firs.base_url}/invoices/b2b")

print("=" * 60)
print("STEP 1 — Pull invoice from Sage 200")
print("=" * 60)
invoices = odbc.get_new_invoices(limit=1)
if not invoices:
    print("ERROR: No invoices found")
    exit()

raw = invoices[0]
print(f"Got: {raw['invoice_number']} | {raw['customer_name']} | {raw['gross_amount']}")

print()
print("=" * 60)
print("STEP 2 — Map to FIRS UBL fields")
print("=" * 60)
mapper = Sage200FieldMapper(company, raw)
mapped = mapper.map()
print(f"Invoice type:  {mapped['invoice_type']}")
print(f"Buyer TIN:     {mapped['buyer_tin'] or '(none - will submit as B2C)'}")
print(f"Supplier TIN:  {mapped['supplier_tin']}")
print(f"Net:           {mapped['net_amount']}")
print(f"VAT:           {mapped['vat_amount']}")
print(f"Lines:         {len(mapped['lines'])}")

print()
print("=" * 60)
print("STEP 3 — Build UBL payload")
print("=" * 60)
builder = UBLInvoiceBuilder(mapped)
if mapped["invoice_type"] == "B2C":
    payload = builder.build_b2c()
    print("Built B2C payload")
else:
    payload = builder.build_b2b()
    print("Built B2B payload")

import json
print("Payload preview:")
print(json.dumps(payload, indent=2, default=str)[:800], "...")

print()
print("=" * 60)
print("STEP 4 — Validate buyer TIN with FIRS")
print("=" * 60)
if mapped["buyer_tin"]:
    tin_result = firs.validate_tin(mapped["buyer_tin"])
    print("TIN validation result:", tin_result)
else:
    print("Skipping TIN validation (B2C invoice)")

print()
print("=" * 60)
print("STEP 5 — Submit to FIRS Sandbox")
print("=" * 60)
if mapped["invoice_type"] == "B2C":
    result = firs.report_b2c_invoice(payload)
else:
    result = firs.submit_b2b_invoice(payload)

print("Success:", result["success"])
print("Status code:", result.get("status_code"))
if result["success"]:
    data = result.get("data", {})
    print("IRN:", data.get("irn") or data.get("IRN", "not in response"))
    print("CSID:", data.get("csid") or data.get("CSID", "not in response"))
    print("Full FIRS response:", json.dumps(data, indent=2, default=str))
else:
    print("Error:", result.get("error"))