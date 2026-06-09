"""
FIRS MBS API Client — Multi-APP Support
Supports DigiTax and Interswitch as Access Point Providers.
Switch between them via APP_PROVIDER in your .env file.

DigiTax API docs:   https://ng.docs.digitax.tech/reference
Interswitch:        Contact einvoice@interswitchgroup.com for credentials

DigiTax Flow (from official docs):
    1. Create Party (buyer)     → POST /parties
    2. Create Item (line item)  → POST /items
    3. Create Invoice           → POST /invoices
    4. DigiTax generates IRN    → invoice status: DRAFT
    5. DigiTax validates w/ NRS → invoice status: PENDING (QR code generated)
    6. DigiTax signs w/ NRS     → invoice status: COMPLETE
    7. IRN + QR code returned to you

Authentication:
    DigiTax:     X-API-Key header (get from digitax.tech dashboard)
    Interswitch: Authorization: Bearer {token} (OAuth2)
"""

import time
import logging
import requests
from typing import Dict, Optional
from django.conf import settings

logger = logging.getLogger("apps.firs")

# ── APP Provider configs ──────────────────────────────────────
APP_CONFIGS = {
    "digitax": {
        "sandbox_url":    "https://api.digitax.tech/ng",
        "production_url": "https://api.digitax.tech/ng",
        "auth_type":      "x-api-key",    # X-API-Key header
        "api_version":    "/v1",
    },
    "interswitch": {
        "sandbox_url":    "https://sandbox.interswitchng.com/einvoice/api",
        "production_url": "https://interswitchng.com/einvoice/api",
        "auth_type":      "bearer",        # OAuth2 Bearer token
        "api_version":    "/v1",
    },
    "firs_direct": {
        # Direct FIRS MBS — confirmed from portal CSP headers
        "sandbox_url":    "https://api.firsmbs.com",
        "production_url": "https://api.firsmbs.com",
        "auth_type":      "api-key",       # api-key + secret-key headers
        "api_version":    "/api/v1",
    },
}

REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3
RETRY_BACKOFF   = [5, 15, 30]


class FIRSClient:
    """
    Unified FIRS MBS API client supporting multiple Access Point Providers.

    Usage:
        # Uses APP_PROVIDER from settings (set in .env)
        client = FIRSClient()

        # Or specify provider explicitly
        client = FIRSClient(provider="digitax")
        client = FIRSClient(provider="interswitch")
    """

    def __init__(
        self,
        api_key: str    = None,
        secret_key: str = None,
        provider: str   = None,
    ):
        self.provider = (
            provider
            or getattr(settings, "APP_PROVIDER", "digitax")
        ).lower()

        if self.provider not in APP_CONFIGS:
            raise ValueError(
                f"Unknown APP provider: '{self.provider}'. "
                f"Choose from: {list(APP_CONFIGS.keys())}"
            )

        cfg             = APP_CONFIGS[self.provider]
        use_production  = getattr(settings, "USE_FIRS_PRODUCTION", False)
        base            = cfg["production_url"] if use_production else cfg["sandbox_url"]
        self.base_url   = base + cfg["api_version"]
        self.auth_type  = cfg["auth_type"]

        # Credentials — from args or settings
        self.api_key    = api_key    or getattr(settings, "FIRS_API_KEY",    "")
        self.secret_key = secret_key or getattr(settings, "FIRS_SECRET_KEY", "")

        logger.info(
            f"[FIRS] Using provider: {self.provider.upper()} | "
            f"{'PRODUCTION' if use_production else 'SANDBOX'} | "
            f"Base: {self.base_url}"
        )

    # ── AUTH HEADERS ──────────────────────────────────────────

    def _headers(self) -> Dict:
        """Build authentication headers for the selected APP provider."""

        if self.auth_type == "x-api-key":
            # DigiTax: single X-API-Key header
            return {
                "X-API-Key":    self.api_key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            }

        elif self.auth_type == "bearer":
            # Interswitch: OAuth2 Bearer token
            return {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            }

        else:
            # FIRS direct: api-key + secret-key
            return {
                "api-key":      self.api_key,
                "secret-key":   self.secret_key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            }

    # ── CORE REQUEST ──────────────────────────────────────────

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """
        Central HTTP request handler with retry logic and structured logging.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        for attempt in range(1, MAX_RETRIES + 1):
            start = time.time()
            try:
                logger.info(
                    f"[{self.provider.upper()}] {method.upper()} "
                    f"{endpoint} (attempt {attempt})"
                )

                response = requests.request(
                    method, url,
                    headers=self._headers(),
                    timeout=REQUEST_TIMEOUT,
                    **kwargs,
                )

                duration_ms = int((time.time() - start) * 1000)
                logger.info(
                    f"[{self.provider.upper()}] Response: "
                    f"{response.status_code} in {duration_ms}ms"
                )

                # Parse response body
                try:
                    data = response.json() if response.content else {}
                except Exception:
                    data = {"raw": response.text[:500]}

                # Success
                if response.status_code in (200, 201):
                    return {
                        "success":     True,
                        "status_code": response.status_code,
                        "data":        data,
                        "duration_ms": duration_ms,
                        "provider":    self.provider,
                    }

                # Client error (4xx) — don't retry
                if 400 <= response.status_code < 500:
                    logger.error(
                        f"[{self.provider.upper()}] Client error "
                        f"{response.status_code}: {data}"
                    )
                    return {
                        "success":     False,
                        "status_code": response.status_code,
                        "error":       data,
                        "duration_ms": duration_ms,
                        "provider":    self.provider,
                    }

                # Server error (5xx) — retry
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF[attempt - 1]
                    logger.warning(
                        f"[{self.provider.upper()}] Server error "
                        f"{response.status_code}. Retrying in {wait}s"
                    )
                    time.sleep(wait)
                    continue

                return {
                    "success":     False,
                    "status_code": response.status_code,
                    "error":       f"Server error after {MAX_RETRIES} attempts: {data}",
                    "provider":    self.provider,
                }

            except requests.exceptions.ConnectionError as e:
                logger.error(f"[{self.provider.upper()}] Connection failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt - 1])
                    continue
                return {"success": False, "error": f"Connection failed: {e}", "provider": self.provider}

            except requests.exceptions.Timeout:
                logger.error(f"[{self.provider.upper()}] Request timed out")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt - 1])
                    continue
                return {"success": False, "error": "Request timed out", "provider": self.provider}

            except Exception as e:
                logger.exception(f"[{self.provider.upper()}] Unexpected error: {e}")
                return {"success": False, "error": str(e), "provider": self.provider}

    # ════════════════════════════════════════════════════════
    # DIGITAX-SPECIFIC METHODS
    # DigiTax has a different flow: Create Party → Create Item → Create Invoice
    # ════════════════════════════════════════════════════════

    def create_party(self, party_data: Dict) -> Dict:
        """
        DigiTax Step 1: Create or register a buyer party.
        Must be done before creating an invoice.

        party_data:
        {
            "name": "Buyer Company Ltd",
            "tin":  "12345678-0001",
            "address": "123 Buyer Street, Lagos",
            "email": "buyer@company.com",
            "phone": "+2348012345678"
        }
        """
        return self._request("POST", "/parties", json=party_data)

    def get_or_create_party(self, tin: str, name: str, address: str = "", email: str = "") -> Dict:
        """
        Get an existing party by TIN, or create if not found.
        Convenience method to avoid duplicate party creation errors.
        """
        # Try to find existing party
        result = self._request("GET", f"/parties?tin={tin}")
        if result["success"]:
            parties = result["data"].get("data", result["data"])
            if isinstance(parties, list) and parties:
                logger.info(f"[DigiTax] Found existing party for TIN {tin}")
                return {"success": True, "data": parties[0]}

        # Create new party
        return self.create_party({
            "name":    name,
            "tin":     tin,
            "address": address,
            "email":   email,
        })

    def create_item(self, item_data: Dict) -> Dict:
        """
        DigiTax Step 2: Create an item (product/service being invoiced).

        item_data:
        {
            "name":        "IT Consulting Services",
            "description": "Software development services",
            "unit_price":  500000,
            "tax_rate":    7.5,
            "unit_code":   "EA"
        }
        """
        return self._request("POST", "/items", json=item_data)

    def create_invoice(self, invoice_data: Dict) -> Dict:
        """
        DigiTax Step 3: Create and submit an invoice.
        DigiTax handles IRN generation, NRS validation, and signing automatically.

        On success:
            data.irn      — Invoice Reference Number
            data.qr_code  — QR code for buyer verification
            data.status   — COMPLETE (or DRAFT/PENDING/FAILED)

        invoice_data format (DigiTax schema):
        {
            "supplier_tin":   "04702493-0001",
            "buyer_party_id": "party-uuid-from-step-1",
            "invoice_number": "INV0001",
            "invoice_date":   "2025-03-14",
            "due_date":       "2025-04-14",
            "currency":       "NGN",
            "lines": [
                {
                    "item_id":    "item-uuid-from-step-2",
                    "quantity":   1,
                    "unit_price": 500000,
                    "tax_rate":   7.5
                }
            ]
        }
        """
        return self._request("POST", "/invoices", json=invoice_data)

    def get_invoice(self, invoice_id: str) -> Dict:
        """Get invoice by DigiTax invoice ID."""
        return self._request("GET", f"/invoices/{invoice_id}")

    def list_invoices(self, page: int = 1, per_page: int = 20) -> Dict:
        """List all invoices on this DigiTax account."""
        return self._request("GET", f"/invoices?page={page}&per_page={per_page}")

    # ════════════════════════════════════════════════════════
    # GENERIC METHODS (work across all providers)
    # ════════════════════════════════════════════════════════

    def submit_b2b_invoice(self, ubl_payload: Dict) -> Dict:
        """
        Submit B2B invoice — routes to correct provider endpoint.
        For DigiTax: use create_invoice() with the mapped payload instead.
        This method normalises the call for the pipeline.
        """
        if self.provider == "digitax":
            # DigiTax doesn't use raw UBL — it has its own schema
            # The pipeline should call create_invoice() directly
            # This is a fallback passthrough
            return self.create_invoice(ubl_payload)
        elif self.provider == "interswitch":
            return self._request("POST", "/invoices/b2b", json=ubl_payload)
        else:
            # FIRS direct
            return self._request("POST", "/invoices/b2b", json=ubl_payload)

    def report_b2c_invoice(self, ubl_payload: Dict) -> Dict:
        """Report B2C invoice — routes to correct provider endpoint."""
        if self.provider == "digitax":
            return self.create_invoice({**ubl_payload, "type": "b2c"})
        elif self.provider == "interswitch":
            return self._request("POST", "/invoices/b2c", json=ubl_payload)
        else:
            return self._request("POST", "/invoices/b2c", json=ubl_payload)

    def validate_tin(self, tin: str) -> Dict:
        """Validate a TIN against the NRS database."""
        if self.provider == "digitax":
            return self._request("GET", f"/parties/validate?tin={tin}")
        elif self.provider == "interswitch":
            return self._request("GET", f"/taxpayers/{tin}/validate")
        else:
            return self._request("GET", f"/taxpayers/{tin}/validate")

    def health_check(self) -> Dict:
        """Test API credentials and connection."""
        if self.provider == "digitax":
            result = self._request("GET", "/invoices?page=1&per_page=1")
            return {
                "success":  result["success"],
                "provider": self.provider,
                "base_url": self.base_url,
                "auth_configured": bool(self.api_key),
                "detail": result.get("data") or result.get("error"),
            }
        else:
            tin = getattr(settings, "FIRS_TIN", "") or getattr(settings, "CLIENT_COMPANY_TIN", "")
            result = self.validate_tin(tin) if tin else {"success": False, "error": "No TIN configured"}
            return {
                "success":  result["success"],
                "provider": self.provider,
                "base_url": self.base_url,
                "auth_configured": bool(self.api_key),
                "detail": result.get("data") or result.get("error"),
            }


# ── Custom Exceptions ─────────────────────────────────────────

class FIRSConfigError(Exception):
    """Raised when API credentials are missing or misconfigured."""
    pass


class FIRSSubmissionError(Exception):
    """Raised when invoice submission fails definitively."""
    pass
