"""
Interswitch APP adapter — STRUCTURE READY, PENDING INTERSWITCH API DOCS.

What is implemented and generic:
    - OAuth2 client-credentials token fetch with caching (Interswitch Passport style)
    - Configurable endpoints for submit / status / TIN validation / health
    - UBL payload built from the existing UBLInvoiceBuilder (55 FIRS fields)
    - Status normalisation onto ProviderStatus

What MUST be confirmed with Interswitch before sandbox testing
(see the email sent requesting sandbox credentials + OpenAPI spec):
    - INTERSWITCH_TOKEN_URL, INTERSWITCH_BASE_URL and the endpoint paths
    - exact request body (pure UBL JSON vs their own schema) -> build_payload()
    - response field names for IRN / CSID / QR / id / status -> _to_result()
    - their status vocabulary -> STATUS_MAP

Nothing here is guessed silently: every unconfirmed value lives in settings
so it can be changed from .env without a code release.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict

import requests
from django.conf import settings
from django.core.cache import cache

from apps.firs.invoice_schema import UBLInvoiceBuilder
from apps.firs.providers.base import (
    AccessPointProvider, ProviderConfigError, ProviderStatus, SubmissionResult,
)
from apps.firs.providers.registry import register_provider

logger = logging.getLogger("apps.firs")

# Provisional — replace with Interswitch's documented vocabulary.
STATUS_MAP = {
    "CREATED":     ProviderStatus.CREATED,
    "PENDING":     ProviderStatus.CREATED,
    "VALIDATED":   ProviderStatus.VALIDATED,
    "SIGNED":      ProviderStatus.SIGNED,
    "APPROVED":    ProviderStatus.SIGNED,
    "CLEARED":     ProviderStatus.SIGNED,
    "TRANSMITTED": ProviderStatus.TRANSMITTED,
    "DELIVERED":   ProviderStatus.TRANSMITTED,
    "FAILED":      ProviderStatus.FAILED,
    "REJECTED":    ProviderStatus.FAILED,
}


@register_provider("interswitch")
class InterswitchProvider(AccessPointProvider):

    TOKEN_CACHE_KEY = "eagle:interswitch:token:{client_id}"

    @classmethod
    def from_settings(cls, company=None) -> "InterswitchProvider":
        # Per-company: firs_api_key = client_id, firs_secret_key = client_secret
        client_id = (
            (getattr(company, "firs_api_key", "") if company is not None else "")
            or getattr(settings, "INTERSWITCH_CLIENT_ID", "")
        )
        client_secret = (
            (getattr(company, "firs_secret_key", "") if company is not None else "")
            or getattr(settings, "INTERSWITCH_CLIENT_SECRET", "")
        )
        if not (client_id and client_secret):
            raise ProviderConfigError(
                "Interswitch client_id / client_secret not configured. Set them on the "
                "ClientCompany (firs_api_key / firs_secret_key) or as "
                "INTERSWITCH_CLIENT_ID / INTERSWITCH_CLIENT_SECRET in .env."
            )
        return cls(
            credentials={"client_id": client_id, "client_secret": client_secret},
            base_url=getattr(settings, "INTERSWITCH_BASE_URL", ""),
            production=getattr(settings, "USE_FIRS_PRODUCTION", False),
        )

    # ── Auth ──────────────────────────────────────────────────

    def _fetch_token(self) -> str:
        token_url = getattr(settings, "INTERSWITCH_TOKEN_URL", "")
        if not token_url:
            raise ProviderConfigError("INTERSWITCH_TOKEN_URL is not configured")

        cache_key = self.TOKEN_CACHE_KEY.format(client_id=self.credentials["client_id"])
        token = cache.get(cache_key)
        if token:
            return token

        basic = base64.b64encode(
            f"{self.credentials['client_id']}:{self.credentials['client_secret']}".encode()
        ).decode()
        resp = requests.post(
            token_url,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": getattr(settings, "INTERSWITCH_SCOPE", "profile")},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ProviderConfigError(f"Interswitch token request failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        token = body["access_token"]
        # Cache slightly shorter than expiry
        cache.set(cache_key, token, max(int(body.get("expires_in", 3600)) - 60, 60))
        return token

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._fetch_token()}"}

    def _path(self, setting_name: str, default: str) -> str:
        return getattr(settings, setting_name, "") or default

    # ── Payload ───────────────────────────────────────────────

    def build_payload(self, mapped: Dict[str, Any]) -> Dict[str, Any]:
        # Interswitch is an NRS APP, so we start from the 55-field UBL payload.
        # Adjust here once their schema is confirmed.
        builder = UBLInvoiceBuilder(mapped)
        if str(mapped.get("invoice_type", "")).upper() == "B2C":
            return builder.build_b2c()
        return builder.build_b2b()

    # ── Operations ────────────────────────────────────────────

    def normalise_status(self, raw_status: str) -> str:
        return STATUS_MAP.get(str(raw_status or "").strip().upper(), ProviderStatus.UNKNOWN)

    def _to_result(self, response: Dict[str, Any], payload: Dict[str, Any]) -> SubmissionResult:
        if not response.get("success"):
            return SubmissionResult(
                success=False, provider=self.name, payload=payload,
                raw=response.get("data"), error=response.get("error"),
                error_code=self._error_code(response),
                status_code=response.get("status_code"),
                duration_ms=response.get("duration_ms"),
                provider_status=ProviderStatus.FAILED,
            )
        data = self._unwrap(response.get("data"))
        return SubmissionResult(
            success=True, provider=self.name, payload=payload,
            irn=data.get("irn") or data.get("IRN") or data.get("invoice_reference_number") or "",
            csid=data.get("csid") or data.get("CSID") or "",
            qr_code=data.get("qr_code") or data.get("qrCode") or "",
            provider_invoice_id=str(data.get("id") or data.get("invoiceId") or data.get("irn") or ""),
            provider_status=self.normalise_status(data.get("status", "")),
            raw=data, status_code=response.get("status_code"),
            duration_ms=response.get("duration_ms"),
        )

    def _safe_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        try:
            return self._request(method, endpoint, **kwargs)
        except ProviderConfigError as exc:
            return {"success": False, "status_code": None, "error": str(exc)}

    def submit(self, mapped: Dict[str, Any]) -> SubmissionResult:
        try:
            payload = self.build_payload(mapped)
        except Exception as exc:  # noqa: BLE001 — bad mapped data must not crash the pipeline
            return SubmissionResult(success=False, provider=self.name,
                                    error=f"Payload build failed: {exc!r}",
                                    error_code="PAYLOAD_ERROR", provider_status=ProviderStatus.FAILED)
        kind = str(mapped.get("invoice_type", "B2B")).lower()
        path = self._path(f"INTERSWITCH_SUBMIT_PATH_{kind.upper()}", f"/invoices/{kind}")
        return self._to_result(self._safe_request("POST", path, json=payload), payload)

    def get_status(self, provider_invoice_id: str) -> SubmissionResult:
        path = self._path("INTERSWITCH_STATUS_PATH", "/invoices/{id}").format(id=provider_invoice_id)
        return self._to_result(self._safe_request("GET", path), {})

    def validate_tin(self, tin: str) -> Dict[str, Any]:
        path = self._path("INTERSWITCH_TIN_PATH", "")
        if not path:
            return {"success": True, "supported": False, "tin": tin}
        r = self._safe_request("GET", path.format(tin=tin))
        return {"success": r["success"], "supported": True, "tin": tin,
                "detail": r.get("data") or r.get("error")}

    def health_check(self) -> Dict[str, Any]:
        try:
            self._fetch_token()
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "provider": self.name, "base_url": self.base_url,
                    "detail": f"auth failed: {exc}"}
        return {"success": True, "provider": self.name, "base_url": self.base_url,
                "detail": "OAuth token obtained"}
