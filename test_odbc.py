import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.models import ClientCompany
from apps.sage200.odbc_client import Sage200ODBCClient

company = ClientCompany.objects.first()
odbc    = Sage200ODBCClient(company)

print("Connected:", odbc.test_connection())
print("Total invoices:", odbc.count_invoices())

samples = odbc.get_sample_invoices(5)
print(f"\nSample invoices ({len(samples)} found):")
for s in samples:
    print(f"  #{s['invoice_id']} | {s['invoice_number']} | "
          f"{s['customer_name']} | {s['gross_amount']}")

invoices = odbc.get_new_invoices(limit=1)
if invoices:
    inv = invoices[0]
    print(f"\nFull invoice #{inv['invoice_id']}:")
    print(f"  Number:   {inv['invoice_number']}")
    print(f"  Customer: {inv['customer_name']}")
    print(f"  Net:      {inv['net_amount']}")
    print(f"  VAT:      {inv['vat_amount']}")
    print(f"  Gross:    {inv['gross_amount']}")
    print(f"  Lines:    {len(inv['lines'])}")
    for line in inv["lines"]:
        print(f"    -> {line['description']} | "
              f"qty:{line['quantity']} | "
              f"net:{line['line_net_amount']}")
else:
    print("No invoices returned")