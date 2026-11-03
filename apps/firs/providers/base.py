"""
Access Point Provider (APP) — base contract.

Every APP (DigiTax, Interswitch, any future provider) is an adapter that
implements AccessPointProvider. The pipelines (Sage 200, Sage 300, Excel)
only ever talk to this interface, so adding a provider never touches
pipeline code.

Input to every adapter is the provider-neutral `mapped` dict produced by
Sage200FieldMapper / Sage300FieldMapper / ExcelFieldMapper.

Output is a SubmissionResult, which `as_pipeline_result()` converts into
the dict shape the pipelines already understand:

    {
        "success": bool,
        "status_code": int | None,
        "duration_ms": int | None,
        "payload": dict,            # exactly what was POSTed (audit trail)
        "data": {                   # present on success
            "irn", "csid", "qr_code",
            "provider", "provider_invoice_id", "provider_status",
            "raw",
        },
        "error": str | None,
    }
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("apps.firs")


class ProviderStatus:
    """
    Provider-neutral lifecycle of an invoice on the NRS network.
    Each adapter maps its own status vocabulary onto these values.

        CREATED      -> accepted by the APP, IRN generated
        VALIDATED    -> NRS validated the invoice
        SIGNED       -> NRS signed it (CSID / QR issued) — legally cleared
        TRANSMITTED  -> delivered to the buyer's APP (B2B only; B2C stays SIGNED)
        FAILED       -> rejected by APP or NRS
        UNKNOWN      -> status string we have not mapped yet (raw kept)
    """
    CREATED     = "CREATED"
    VALIDATED   = "VALIDATED"
    SIGNED      = "SIGNED"
    TRANSMITTED = "TRANSMITTED"
    FAILED      = "FAILED"
    UNKNOWN     = "UNKNOWN"

    # Statuses after which we stop polling the provider
    TERMINAL = {TRANSMITTED, FAILED}

    CHOICES = [
        (CREATED, "Created"),
        (VALIDATED, "Validated"),
        (SIGNED, "Signed"),
        (TRANSMITTED, "Transmitted"),
        (FAILED, "Failed"),
        (UNKNOWN, "Unknown"),
    ]


@dataclass
class SubmissionResult:
    success: bool
    provider: str
    payload: Dict[str, Any] = field(default_factory=dict)
    irn: str = ""
    csid: str = ""
    qr_code: str = ""
    provider_invoice_id: str = ""
    provider_status: str = ProviderStatus.UNKNOWN
    raw: Any = None
    error: Optional[str] = None
    error_code: str = ""
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None

    def as_pipeline_result(self) -> Dict[str, Any]:
        result = {
            "success":     self.success,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "payload":     self.payload,
            "provider":    self.provider,
            # Pipelines read error["code"] / error["message"] into Invoice.error_code / error_message
            "error":       None if self.success else {
                "code":    self.error_code or (f"HTTP_{self.status_code}" if self.status_code else "APP_ERROR"),
                "message": self.error or "",
            },
        }
        if self.success:
            result["data"] = {
                "irn":                 self.irn,
                "csid":                self.csid,
                "qr_code":             self.qr_code,
                "provider":            self.provider,
                "provider_invoice_id": self.provider_invoice_id,
                "provider_status":     self.provider_status,
                "raw":                 self.raw,
            }
        else:
            result["data"] = None
        return result


class ProviderConfigError(Exception):
    """Credentials / endpoints for a provider are missing or invalid."""


class AccessPointProvider(abc.ABC):
    """
    Base class for every APP adapter.

    Subclasses must set `name` and implement build_payload(), submit(),
    get_status() and normalise_status(). HTTP plumbing with retry and
    timing is provided by _request().
    """

    name: str = ""
    timeout: int = 30
    max_retries: int = 3
    retry_backoff = (5, 15, 30)

    def __init__(self, credentials: Dict[str, str], base_url: str, production: bool = False):
        self.credentials = credentials or {}
        self.base_url = (base_url or "").rstrip("/")
        self.production = production
        if not self.base_url:
            raise ProviderConfigError(f"[{self.name}] base URL is not configured")

    # ── Contract ──────────────────────────────────────────────

    @classmethod
    @abc.abstractmethod
    def from_settings(cls, company=None) -> "AccessPointProvider":
        """Build an adapter using the company's credentials, falling back to settings."""

    @abc.abstractmethod
    def build_payload(self, mapped: Dict[str, Any]) -> Dict[str, Any]:
        """Convert the provider-neutral mapped invoice into this APP's request body."""

    @abc.abstractmethod
    def submit(self, mapped: Dict[str, Any]) -> SubmissionResult:
        """Submit one invoice. Must never raise for HTTP/API errors — return success=False."""

    @abc.abstractmethod
    def get_status(self, provider_invoice_id: str) -> SubmissionResult:
        """Fetch the current lifecycle status of a previously submitted invoice."""

    @abc.abstractmethod
    def normalise_status(self, raw_status: str) -> str:
        """Map this APP's status string onto ProviderStatus."""

    def validate_tin(self, tin: str) -> Dict[str, Any]:
        """
        Validate a buyer TIN. Providers that do not expose TIN validation
        return supported=False and success=True so the pipeline does not
        wrongly strip a valid TIN.
        """
        return {"success": True, "supported": False, "tin": tin}

    def health_check(self) -> Dict[str, Any]:
        return {"success": False, "provider": self.name, "detail": "not implemented"}

    # ── HTTP plumbing ─────────────────────────────────────────

    def auth_headers(self) -> Dict[str, str]:
        return {}

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            **self.auth_headers(),
            **kwargs.pop("headers", {}),
        }

        for attempt in range(1, self.max_retries + 1):
            start = time.monotonic()
            try:
                logger.info("[%s] %s %s (attempt %d)", self.name, method, endpoint, attempt)
                resp = requests.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
                duration_ms = int((time.monotonic() - start) * 1000)

                try:
                    data = resp.json() if resp.content else {}
                except ValueError:
                    data = {"raw": resp.text[:1000]}

                if resp.status_code in (200, 201, 202):
                    return {"success": True, "status_code": resp.status_code,
                            "data": data, "duration_ms": duration_ms}

                # 429 = rate limited: back off and retry
                if resp.status_code == 429 and attempt < self.max_retries:
                    wait = int(resp.headers.get("Retry-After", self.retry_backoff[attempt - 1]))
                    logger.warning("[%s] Rate limited, retrying in %ss", self.name, wait)
                    time.sleep(wait)
                    continue

                if 400 <= resp.status_code < 500:
                    logger.error("[%s] %d: %s", self.name, resp.status_code, data)
                    return {"success": False, "status_code": resp.status_code,
                            "error": self._extract_error(data), "data": data,
                            "duration_ms": duration_ms}

                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff[attempt - 1])
                    continue
                return {"success": False, "status_code": resp.status_code,
                        "error": f"Server error {resp.status_code}: {self._extract_error(data)}",
                        "data": data, "duration_ms": duration_ms}

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff[attempt - 1])
                    continue
                return {"success": False, "status_code": None, "error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:  # noqa: BLE001 — adapters must not raise on transport errors
                logger.exception("[%s] Unexpected error", self.name)
                return {"success": False, "status_code": None, "error": str(exc)}

        return {"success": False, "status_code": None, "error": "Max retries exceeded"}

    @staticmethod
    def _extract_error(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("message", "error", "detail", "errors"):
                if data.get(key):
                    return str(data[key])
        return str(data)

    @staticmethod
    def _error_code(response: Dict[str, Any]) -> str:
        """Best-effort machine-readable error code from an APP error response."""
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("code", "error_code", "errorCode"):
                if data.get(key):
                    return str(data[key])[:50]
        if response.get("status_code"):
            return f"HTTP_{response['status_code']}"
        return "APP_UNREACHABLE"

    @staticmethod
    def _unwrap(data: Any) -> Dict[str, Any]:
        """Many APPs wrap the object as {"data": {...}}; return the inner object."""
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data if isinstance(data, dict) else {}
