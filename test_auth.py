import requests

url = "https://einvoice.firs.gov.ng/api/v1/taxpayers/04702493-0001/validate"
headers = {
    "api-key":      "963b3aaa-62c3-4a24-a56e-f52c4b9468eb",
    "secret-key":   "rPEi8ltqpyavbDQOItA972lh6f2IWbBow1xHE9R1wbraM5ACltifbzQ3M9AhH1bi8o5Dx4UqcxZyoute57uABnpj2N2mepebfS3F",
    "Content-Type": "application/json",
    "Accept":       "application/json",
}

r = requests.get(url, headers=headers, timeout=10)
print("Status:", r.status_code)
print("Headers:", dict(r.headers))
print("Body:", r.text[:300])