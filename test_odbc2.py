import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.models import ClientCompany
import pyodbc

company = ClientCompany.objects.first()
conn_str = company.odbc_connection_string
print("Connecting with:", conn_str)
print()

conn = pyodbc.connect(conn_str, timeout=30)
cursor = conn.cursor()

# Test 1 — How many rows total in the AR view?
print("=== Test 1: Total rows in _bvARTransactionsFull ===")
cursor.execute("SELECT COUNT(*) FROM _bvARTransactionsFull")
print("Total rows:", cursor.fetchone()[0])
print()

# Test 2 — What transaction types exist?
print("=== Test 2: Transaction types in _bvARTransactionsFull ===")
cursor.execute("""
    SELECT iTransactionType, COUNT(*) as cnt
    FROM _bvARTransactionsFull
    GROUP BY iTransactionType
    ORDER BY iTransactionType
""")
rows = cursor.fetchall()
if rows:
    for row in rows:
        print(f"  iTransactionType = {row[0]} → {row[1]} records")
else:
    print("  No rows found in _bvARTransactionsFull")
print()

# Test 3 — Check PostAR table directly
print("=== Test 3: Total rows in PostAR ===")
try:
    cursor.execute("SELECT COUNT(*) FROM PostAR")
    print("PostAR rows:", cursor.fetchone()[0])
except Exception as e:
    print("PostAR error:", e)
print()

# Test 4 — Sample from PostAR
print("=== Test 4: Sample rows from PostAR ===")
try:
    cursor.execute("SELECT TOP 5 * FROM PostAR")
    rows = cursor.fetchall()
    cols = [c[0] for c in cursor.description]
    print("Columns:", cols[:10])
    for row in rows:
        print(" ", dict(zip(cols[:10], row[:10])))
except Exception as e:
    print("PostAR sample error:", e)
print()

# Test 5 — Check Client table
print("=== Test 5: Total customers in Client table ===")
try:
    cursor.execute("SELECT COUNT(*) FROM Client")
    print("Client rows:", cursor.fetchone()[0])
    cursor.execute("SELECT TOP 3 DCLink, Account, Name FROM Client")
    for row in cursor.fetchall():
        print(f"  DCLink={row[0]} | Account={row[1]} | Name={row[2]}")
except Exception as e:
    print("Client error:", e)
print()

# Test 6 — Check _btblInvoiceLines
print("=== Test 6: Total rows in _btblInvoiceLines ===")
try:
    cursor.execute("SELECT COUNT(*) FROM _btblInvoiceLines")
    print("Invoice lines:", cursor.fetchone()[0])
except Exception as e:
    print("_btblInvoiceLines error:", e)

conn.close()
print()
print("Done.")