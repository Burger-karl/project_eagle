from apps.core.models import ClientCompany
from apps.sage200.odbc_client import Sage200ODBCClient

company = ClientCompany.objects.first()
odbc = Sage200ODBCClient(company)

print("Connected:", odbc.test_connection())
print("Total invoices:", odbc.count_invoices())

samples = odbc.get_sample_invoices(3)
for s in samples:
    print(s["invoice_number"], "|", s["customer_name"], "|", s["gross_amount"])

invoices = odbc.get_new_invoices(limit=1)
if invoices:
    inv = invoices[0]
    print("\nFull invoice:")
    print("  Number:", inv["invoice_number"])
    print("  Customer:", inv["customer_name"])
    print("  Net:", inv["net_amount"])
    print("  VAT:", inv["vat_amount"])
    print("  Gross:", inv["gross_amount"])
    print("  Lines:", len(inv["lines"]))
    for line in inv["lines"]:
        print(f"    -> {line['description']} | qty:{line['quantity']} | net:{line['line_net_amount']}")
else:
    print("No invoices found")