"""
DigiTax Invoice Submitter
Submits invoices to NRS/FIRS via DigiTax POST /invoices.
"""

import logging
import time
from datetime import date
from typing import Any, Dict, Optional

import requests
from django.conf import settings

logger = logging.getLogger("apps.firs")

BASE_URL    = "https://api.digitax.tech/ng/v1"
TIMEOUT     = 30
MAX_RETRIES = 3

# Permanent DigiTax item registered on this sandbox account.
# item_name:   Link Options Software Services
# item_number: ITM26-265-211317380-PUVJUA
DIGITAX_ITEM_ID = "item_01M35FEGT4SFJ4GKVN7PTXPN9A"

TAX_CATEGORY_MAP = {
    "S":            "STANDARD_VAT",
    "Z":            "ZERO_RATED_VAT",
    "E":            "VAT_EXEMPT",
    "STANDARD_VAT": "STANDARD_VAT",
    "ZERO_RATED_VAT": "ZERO_RATED_VAT",
    "VAT_EXEMPT":   "VAT_EXEMPT",
}


class DigiTaxSubmitter:

    def __init__(self, api_key=None, party_id=None):
        self.api_key  = api_key or getattr(settings, "FIRS_API_KEY", "")
        self.party_id = party_id or getattr(settings, "FIRS_DIGITAX_PARTY_ID", "")
        self.base_url = BASE_URL
        if not self.api_key:
            raise ValueError(
                "DigiTax API key is not configured. "
                "Set FIRS_API_KEY in your .env file."
            )

    def submit(self, mapped):
        invoice_number = str(mapped.get("invoice_number", "?")).strip()
        logger.info(
            "[DigiTax] Submitting invoice '%s' (%s) via /invoices",
            invoice_number,
            mapped.get("invoice_type", "?"),
        )

        payload = self._build_payload(mapped)
        logger.info("[DigiTax] Invoice payload prepared for '%s'", invoice_number)

        result = self._request("POST", "/invoices", json=payload)

        if not result["success"]:
            logger.error(
                "[DigiTax] Submission failed for '%s': %s",
                invoice_number,
                result.get("error"),
            )
            return {
                "success":    False,
                "irn":        None,
                "status":     "FAILED",
                "invoice_id": None,
                "qr_code":    None,
                "error":      result.get("error"),
                "raw":        result,
            }

        data = result.get("data", {})
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        irn        = data.get("invoice_reference_number") or data.get("irn")
        invoice_id = data.get("id")
        status     = data.get("status", "DRAFT")
        qr_code    = data.get("qr_code_data") or data.get("qr_code")

        logger.info(
            "[DigiTax] Invoice '%s' submitted -- ID: %s | IRN: %s | Status: %s",
            invoice_number, invoice_id, irn, status,
        )

        return {
            "success":    True,
            "irn":        irn,
            "status":     status,
            "invoice_id": invoice_id,
            "qr_code":    qr_code,
            "error":      None,
            "raw":        data,
        }

    def _build_payload(self, mapped):
        invoice_type   = str(mapped.get("invoice_type",   "B2C")).strip().upper()
        invoice_number = str(mapped.get("invoice_number", "")).strip()
        raw_doc_type  = str(mapped.get("document_type", "380")).strip()
        document_type = "381" if raw_doc_type == "380" else raw_doc_type
        due_date       = mapped.get("due_date")

        # Original accounting date kept only for notes.
        # Both invoice_date and issue_date MUST be today.
        # NRS rejects any past date on either field.
        original_date = str(mapped.get("invoice_date", ""))[:10]
        today         = date.today().isoformat()

        payload = {
            "invoice_date":           today,
            "issue_date":             today,
            "invoice_type_code":      document_type,
            "document_currency_code": "NGN",
            "trader_invoice_number":  invoice_number[:30],
            "invoice_kind":           invoice_type,
            "notes": (
                "Invoice " + invoice_number +
                " (original date: " + original_date + ")"
            ),
            "items": self._build_items(mapped),
            "payment_means": [{
                "payment_means_code": str(mapped.get("payment_means_code", "30")),
                "payment_due_date":   str(due_date)[:10] if due_date else today,
            }],
        }

        if due_date:
            payload["due_date"] = str(due_date)[:10]

        # Party information
        self._add_party_information(payload, mapped, invoice_type)

        return payload

    def _add_party_information(self, payload, mapped, invoice_type):
        # Use a configured DigiTax party_id if available
        mapped_party_id = (
            mapped.get("party_id") or
            mapped.get("buyer_party_id") or ""
        )
        party_id = str(mapped_party_id or self.party_id or "").strip()
        if party_id:
            payload["party_id"] = party_id

        buyer_tin   = str(mapped.get("buyer_tin",   "") or "").strip()
        buyer_name  = str(mapped.get("buyer_name",  "") or "").strip()
        buyer_email = str(mapped.get("buyer_email", "") or "").strip()

        if buyer_tin:
            payload["party_tin"] = buyer_tin
        if buyer_name:
            payload["party_name"] = buyer_name
        if buyer_email:
            payload["party_email"] = buyer_email

        buyer_address = str(mapped.get("buyer_address", "") or "").strip()
        buyer_city    = str(mapped.get("buyer_city",    "") or "").strip()
        buyer_postal  = str(mapped.get("buyer_postal",  "") or "").strip()

        if buyer_address or buyer_city:
            payload["party_address"] = {
                "street_name":  buyer_address or buyer_city or "Lagos",
                "city_name":    buyer_city or "Lagos",
                "postal_zone":  buyer_postal or "100001",
                "country_code": "NGA",
            }

    def _build_items(self, mapped):
        lines  = mapped.get("lines", [])
        result = []

        if not lines:
            logger.warning("[DigiTax] Invoice contains no mapped lines.")
            return result

        for i, line in enumerate(lines, start=1):
            vat_rate = float(line.get("vat_rate", 7.5) or 7.5)
            tax_rate = round(vat_rate / 100, 6) if vat_rate > 1 else round(vat_rate, 6)

            tax_category_code = TAX_CATEGORY_MAP.get(
                line.get("tax_category", "S"), "STANDARD_VAT"
            )

            logger.info(
                "[DigiTax] Invoice line %d using existing DigiTax item: %s",
                i, DIGITAX_ITEM_ID,
            )

            result.append({
                "item_id":          DIGITAX_ITEM_ID,
                "quantity":         float(line.get("quantity",   1) or 1),
                "unit_price":       round(float(line.get("unit_price", 0) or 0), 2),
                "tax_rate":         tax_rate,
                "tax_category_code": tax_category_code,
                "price_unit":       str(line.get("unit_code", "H87") or "H87"),
            })

        return result

    def _request(self, method, endpoint, **kwargs):
        url     = self.base_url + endpoint
        headers = {
            "X-API-Key":    self.api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("[DigiTax] %s %s (attempt %d)", method, endpoint, attempt)
                response = requests.request(
                    method, url, headers=headers, timeout=TIMEOUT, **kwargs
                )

                try:
                    data = response.json() if response.content else {}
                except Exception:
                    data = {"raw": response.text[:1000]}

                logger.info("[DigiTax] Response: %d", response.status_code)

                if response.status_code in (200, 201):
                    return {"success": True, "status_code": response.status_code, "data": data}

                if 400 <= response.status_code < 500:
                    logger.error("[DigiTax] %d: %s", response.status_code, data)
                    return {
                        "success":     False,
                        "status_code": response.status_code,
                        "error":       data.get("message", str(data)) if isinstance(data, dict) else str(data),
                        "response":    data,
                    }

                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)
                    continue

                return {
                    "success":     False,
                    "status_code": response.status_code,
                    "error":       "Server error %d: %s" % (response.status_code, data),
                }

            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)
                    continue
                return {"success": False, "error": "Request timed out"}

            except Exception as exc:
                return {"success": False, "error": str(exc)}

        return {"success": False, "error": "Max retries exceeded"}

    def health_check(self):
        result = self._request("GET", "/invoices?page=1&per_page=1")
        return {
            "success":  result["success"],
            "base_url": self.base_url,
            "auth_ok":  bool(self.api_key),
            "provider": "digitax",
            "detail":   result.get("data") or result.get("error"),
        }

    def get_invoice(self, invoice_id):
        return self._request("GET", "/invoices/" + invoice_id)

    def list_invoices(self, page=1):
        return self._request("GET", "/invoices?page=%d&per_page=20" % page)