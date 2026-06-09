# import requests

# # Test directly without the middleware
# base = "https://einvoice.firs.gov.ng/api/v1"
# headers = {
#     "api-key":      "963b3aaa-62c3-4a24-a56e-f52c4b9468eb",
#     "secret-key":   "rPEi8ltqpyavbDQOItA972lh6f2IWbBow1xHE9R1wbraM5ACltifbzQ3M9AhH1bi8o5Dx4UqcxZyoute57uABnpj2N2mepebfS3F",
#     "Content-Type": "application/json",
# }

# # Try different endpoint formats
# endpoints = [
#     "/taxpayers/04702493-0001/validate",
#     "/validate-tin/04702493-0001",
#     "/invoices/b2b",
#     "/invoice/b2b",
#     "/einvoice/b2b",
# ]

# for ep in endpoints:
#     url = base + ep
#     try:
#         r = requests.get(url, headers=headers, timeout=5)
#         print(f"{ep}: {r.status_code}")
#     except Exception as e:
#         print(f"{ep}: ERROR - {e}")





import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import requests
from django.conf import settings

base    = settings.FIRS_BASE_URL
headers = {
    "api-key":      settings.FIRS_API_KEY,
    "secret-key":   settings.FIRS_SECRET_KEY,
    "Content-Type": "application/json",
    "Accept":       "application/json",
}

print(f"Base URL: {base}")
print()

# Test GET endpoints
get_paths = [
    "/taxpayers/04702493-0001/validate",
    "/validate-tin/04702493-0001",
    "/invoices",
    "/invoice",
]

# Test POST and PUT endpoints
write_paths = [
    ("POST", "/invoices/b2b"),
    ("PUT",  "/invoices/b2b"),
    ("POST", "/invoice/b2b"),
    ("PUT",  "/invoice/b2b"),
    ("POST", "/invoices"),
    ("POST", "/einvoice"),
]

print("=== GET ENDPOINTS ===")
for path in get_paths:
    url = base + path
    try:
        r = requests.get(url, headers=headers, timeout=8)
        body = r.text[:80].replace('\n','')
        print(f"  GET {path}: {r.status_code} | {body}")
    except Exception as e:
        print(f"  GET {path}: ERROR - {str(e)[:60]}")

print()
print("=== POST/PUT ENDPOINTS (empty body test) ===")
for method, path in write_paths:
    url = base + path
    try:
        r = requests.request(method, url, headers=headers, json={}, timeout=8)
        body = r.text[:80].replace('\n','')
        print(f"  {method} {path}: {r.status_code} | {body}")
    except Exception as e:
        print(f"  {method} {path}: ERROR - {str(e)[:60]}")