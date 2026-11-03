"""
DigiTax APP adapter.

Payload rules here are the ones proven against the DigiTax sandbox
(5 invoices COMPLETED with live IRNs):

    - POST /invoices only; B2C skips party creation
    - every line reuses a permanent DigiTax item_id (item_code must be
      unique per account, so we never create items per submission)
    - invoice_date and issue_date are both TODAY — NRS rejects back-dating;
      the original accounting date is preserved in `notes`
    - tax_rate is a decimal 0–1 (0.075 for 7.5%)

Auth: X-API-Key header.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict

from django.conf import settings

from apps.firs.providers.base import (
    AccessPointProvider, ProviderConfigError, ProviderStatus, SubmissionResult,
)
from apps.firs.providers.registry import register_provider

logger = logging.getLogger("apps.firs")

TAX_CATEGORY_MAP = {
    "S": "STANDARD_VAT",
    "Z": "ZERO_RATED_VAT",
    "E": "VAT_EXEMPT",
    "STANDARD_VAT": "STANDARD_VAT",
    "ZERO_RATED_VAT": "ZERO_RATED_VAT",
    "VAT_EXEMPT": "VAT_EXEMPT",
}

# Middleware document_type -> DigiTax invoice_type_code.
# NOTE: carried over unchanged from apps/firs/digitax_submitter.py, which
# produced the successful sandbox IRNs with 380 -> "381". Confirm with the
# DigiTax technical officer which code NRS expects for a commercial invoice
# vs a credit note before go-live — today a Sage credit note (381) and a
# commercial invoice (380) are both sent as "381".
DOCUMENT_TYPE_MAP = {
    "380": "381",
}

STATUS_MAP = {
    "DRAFT":       ProviderStatus.CREATED,
    "CREATED":     ProviderStatus.CREATED,
    "PENDING":     ProviderStatus.VALIDATED,
    "VALIDATED":   ProviderStatus.VALIDATED,
    "SIGNED":      ProviderStatus.SIGNED,
    "COMPLETE":    ProviderStatus.SIGNED,
    "COMPLETED":   ProviderStatus.SIGNED,
    "TRANSMITTED": ProviderStatus.TRANSMITTED,
    "FAILED":      ProviderStatus.FAILED,
    "REJECTED":    ProviderStatus.FAILED,
    "ERROR":       ProviderStatus.FAILED,
}


@register_provider("digitax")
class DigiTaxProvider(AccessPointProvider):

    @classmethod
    def from_settings(cls, company=None) -> "DigiTaxProvider":
        api_key = (
            (getattr(company, "firs_api_key", "") if company is not None else "")
            or getattr(settings, "DIGITAX_API_KEY", "")
            or getattr(settings, "FIRS_API_KEY", "")
        )
        if not api_key:
            raise ProviderConfigError(
                "DigiTax API key is not configured. Set ClientCompany.firs_api_key "
                "or DIGITAX_API_KEY / FIRS_API_KEY in .env."
            )
        return cls(
            credentials={"api_key": api_key},
            base_url=getattr(settings, "DIGITAX_BASE_URL", "https://api.digitax.tech/ng/v1"),
            production=getattr(settings, "USE_FIRS_PRODUCTION", False),
        )

    def auth_headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.credentials["api_key"]}

    # ── Payload ───────────────────────────────────────────────

    def build_payload(self, mapped: Dict[str, Any]) -> Dict[str, Any]:
        invoice_type   = str(mapped.get("invoice_type", "B2C")).strip().upper()
        invoice_number = str(mapped.get("invoice_number", "")).strip()
        raw_doc_type   = str(mapped.get("document_type", "380")).strip()
        due_date       = mapped.get("due_date")
        original_date  = str(mapped.get("invoice_date", ""))[:10]
        today          = date.today().isoformat()

        payload: Dict[str, Any] = {
            "invoice_date":           today,
            "issue_date":             today,
            "invoice_type_code":      DOCUMENT_TYPE_MAP.get(raw_doc_type, raw_doc_type),
            "document_currency_code": "NGN",
            "trader_invoice_number":  invoice_number[:30],
            "invoice_kind":           invoice_type,
            "notes":                  f"Invoice {invoice_number} (original date: {original_date})",
            "items":                  self._build_items(mapped),
            "payment_means": [{
                "payment_means_code": str(mapped.get("payment_means_code", "30")),
                "payment_due_date":   str(due_date)[:10] if due_date else today,
            }],
        }
        if due_date:
            payload["due_date"] = str(due_date)[:10]

        self._add_party(payload, mapped)
        return payload

    def _item_id_for(self, tax_category_code: str) -> str:
        per_category = getattr(settings, "DIGITAX_ITEM_IDS", {}) or {}
        item_id = per_category.get(tax_category_code) or getattr(settings, "DIGITAX_ITEM_ID", "")
        if not item_id:
            raise ProviderConfigError(
                "No DigiTax item_id configured. Set DIGITAX_ITEM_ID (and optionally "
                "DIGITAX_ITEM_ID_ZERO_RATED / DIGITAX_ITEM_ID_EXEMPT) in .env."
            )
        return item_id

    def _build_items(self, mapped: Dict[str, Any]):
        items = []
        for line in mapped.get("lines", []) or []:
            vat_rate = line.get("vat_rate", 7.5)
            vat_rate = 7.5 if vat_rate is None or vat_rate == "" else float(vat_rate)
            tax_rate = round(vat_rate / 100, 6) if vat_rate > 1 else round(vat_rate, 6)
            tax_category_code = TAX_CATEGORY_MAP.get(str(line.get("tax_category", "S")), "STANDARD_VAT")

            items.append({
                "item_id":           self._item_id_for(tax_category_code),
                "quantity":          float(line.get("quantity", 1) or 1),
                "unit_price":        round(float(line.get("unit_price", 0) or 0), 2),
                "tax_rate":          tax_rate,
                "tax_category_code": tax_category_code,
                "price_unit":        str(line.get("unit_code", "H87") or "H87"),
            })
        if not items:
            logger.warning("[digitax] Invoice %s has no lines", mapped.get("invoice_number"))
        return items

    def _add_party(self, payload: Dict[str, Any], mapped: Dict[str, Any]) -> None:
        party_id = str(
            mapped.get("party_id") or mapped.get("buyer_party_id")
            or getattr(settings, "FIRS_DIGITAX_PARTY_ID", "") or ""
        ).strip()
        if party_id:
            payload["party_id"] = party_id

        for src, dst in (("buyer_tin", "party_tin"), ("buyer_name", "party_name"),
                         ("buyer_email", "party_email")):
            value = str(mapped.get(src, "") or "").strip()
            if value:
                payload[dst] = value

        address = str(mapped.get("buyer_address", "") or "").strip()
        city    = str(mapped.get("buyer_city", "") or "").strip()
        postal  = str(mapped.get("buyer_postal", "") or "").strip()
        if address or city:
            payload["party_address"] = {
                "street_name":  address or city or "Lagos",
                "city_name":    city or "Lagos",
                "postal_zone":  postal or "100001",
                "country_code": "NGA",
            }

    # ── Operations ────────────────────────────────────────────

    def normalise_status(self, raw_status: str) -> str:
        return STATUS_MAP.get(str(raw_status or "").strip().upper(), ProviderStatus.UNKNOWN)

    def _to_result(self, response: Dict[str, Any], payload: Dict[str, Any]) -> SubmissionResult:
        if not response.get("success"):
            return SubmissionResult(
                success=False, provider=self.name, payload=payload,
                raw=response.get("data"), error=response.get("error"),
                status_code=response.get("status_code"),
                duration_ms=response.get("duration_ms"),
                provider_status=ProviderStatus.FAILED,
            )
        data = self._unwrap(response.get("data"))
        raw_status = data.get("status", "")
        return SubmissionResult(
            success=True, provider=self.name, payload=payload,
            irn=data.get("invoice_reference_number") or data.get("irn") or "",
            csid=data.get("csid") or data.get("cryptographic_stamp") or "",
            qr_code=data.get("qr_code_data") or data.get("qr_code") or "",
            provider_invoice_id=str(data.get("id") or ""),
            provider_status=self.normalise_status(raw_status),
            raw=data, status_code=response.get("status_code"),
            duration_ms=response.get("duration_ms"),
        )

    def submit(self, mapped: Dict[str, Any]) -> SubmissionResult:
        try:
            payload = self.build_payload(mapped)
        except ProviderConfigError as exc:
            return SubmissionResult(success=False, provider=self.name, error=str(exc),
                                    provider_status=ProviderStatus.FAILED)
        except Exception as exc:  # noqa: BLE001 — bad mapped data must not crash the pipeline
            return SubmissionResult(success=False, provider=self.name,
                                    error=f"Payload build failed: {exc!r}",
                                    provider_status=ProviderStatus.FAILED)
        return self._to_result(self._request("POST", "/invoices", json=payload), payload)

    def get_status(self, provider_invoice_id: str) -> SubmissionResult:
        return self._to_result(self._request("GET", f"/invoices/{provider_invoice_id}"), {})

    def health_check(self) -> Dict[str, Any]:
        r = self._request("GET", "/invoices?page=1&per_page=1")
        return {"success": r["success"], "provider": self.name, "base_url": self.base_url,
                "detail": r.get("error") or "authenticated"}
