"""
FIRS MBS API Client
Handles all communication with the FIRS NRS e-Invoice platform.

Endpoints covered:
    POST /invoices/b2b      — Submit B2B invoice for clearance
    POST /invoices/b2c      — Report B2C invoice (within 24hrs)
    POST /invoices/credit-note  — Submit credit note
    POST /invoices/debit-note   — Submit debit note
    GET  /invoices/{irn}    — Retrieve invoice by IRN
    GET  /invoices/{irn}/status — Check invoice status
    GET  /taxpayers/{tin}/validate — Validate a TIN
    GET  /invoices          — List all invoices

Authentication:
    Every request requires two headers:
        api-key:    your FIRS API key
        secret-key: your FIRS secret key
    Both are generated from einvoice.firs.gov.ng dashboard.
"""

import time
import logging
import requests
from typing import Dict, Optional
from django.conf import settings

logger = logging.getLogger("apps.firs")

# Max time to wait for FIRS API response
REQUEST_TIMEOUT = 30

# Retry settings for transient failures
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]  # seconds between retries


class FIRSClient:
    """
    Thread-safe FIRS MBS API client.
    One instance per request — do not share across threads.

    Usage:
        client = FIRSClient(api_key, secret_key)
        result = client.submit_b2b_invoice(ubl_payload)
    """

    def __init__(self, api_key: str = None, secret_key: str = None):
        self.base_url   = settings.FIRS_BASE_URL
        self.api_key    = api_key or settings.FIRS_API_KEY
        self.secret_key = secret_key or settings.FIRS_SECRET_KEY

        if not self.api_key or not self.secret_key:
            raise FIRSConfigError(
                "FIRS_API_KEY and FIRS_SECRET_KEY must be set in .env — "
                "get them from einvoice.firs.gov.ng"
            )

    # ──────────────────────────────────────────────────────────
    # AUTH HEADERS
    # ──────────────────────────────────────────────────────────

    def _headers(self) -> Dict:
        """Build authentication headers for every FIRS API request."""
        return {
            "api-key":      self.api_key,
            "secret-key":   self.secret_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }

    # ──────────────────────────────────────────────────────────
    # CORE REQUEST METHOD
    # ──────────────────────────────────────────────────────────

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """
        Central HTTP request handler with:
        - Structured logging of every request/response
        - Automatic retry on transient failures (5xx errors)
        - Consistent error response format

        Returns:
            {
                "success": bool,
                "status_code": int,
                "data": dict,        # on success
                "error": str,        # on failure
                "duration_ms": int   # API call duration
            }
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        for attempt in range(1, MAX_RETRIES + 1):
            start_time = time.time()
            try:
                logger.info(
                    f"FIRS API {method.upper()} {endpoint} (attempt {attempt})"
                )

                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=REQUEST_TIMEOUT,
                    **kwargs,
                )

                duration_ms = int((time.time() - start_time) * 1000)

                logger.info(
                    f"FIRS API response: {response.status_code} in {duration_ms}ms"
                )

                # ── Success ───────────────────────────────────
                if response.status_code in (200, 201):
                    return {
                        "success":     True,
                        "status_code": response.status_code,
                        "data":        response.json(),
                        "duration_ms": duration_ms,
                    }

                # ── Client error (4xx) — don't retry ─────────
                if 400 <= response.status_code < 500:
                    error_body = {}
                    try:
                        error_body = response.json()
                    except Exception:
                        error_body = {"raw": response.text}

                    logger.error(
                        f"FIRS API client error {response.status_code}: {error_body}"
                    )
                    return {
                        "success":     False,
                        "status_code": response.status_code,
                        "error":       error_body,
                        "duration_ms": duration_ms,
                    }

                # ── Server error (5xx) — retry ────────────────
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF[attempt - 1]
                    logger.warning(
                        f"FIRS API server error {response.status_code}. "
                        f"Retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue

                # All retries exhausted
                return {
                    "success":     False,
                    "status_code": response.status_code,
                    "error":       f"FIRS server error after {MAX_RETRIES} attempts: {response.text[:500]}",
                    "duration_ms": duration_ms,
                }

            except requests.exceptions.ConnectionError as e:
                logger.error(f"FIRS API connection failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt - 1])
                    continue
                return {"success": False, "error": f"Connection failed: {e}"}

            except requests.exceptions.Timeout:
                logger.error(f"FIRS API timed out after {REQUEST_TIMEOUT}s")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF[attempt - 1])
                    continue
                return {"success": False, "error": "FIRS API request timed out"}

            except Exception as e:
                logger.exception(f"Unexpected error calling FIRS API: {e}")
                return {"success": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # TIN VALIDATION — Always call before B2B submission
    # ──────────────────────────────────────────────────────────

    def validate_tin(self, tin: str) -> Dict:
        """
        Validate a buyer's TIN against the FIRS database.
        Always call this before submitting a B2B invoice.

        Returns:
            {"success": True, "data": {"business_name": "...", "tin": "..."}}
        """
        return self._request("GET", f"/taxpayers/{tin}/validate")

    # ──────────────────────────────────────────────────────────
    # B2B INVOICE SUBMISSION (pre-clearance required)
    # ──────────────────────────────────────────────────────────

    def submit_b2b_invoice(self, ubl_payload: Dict) -> Dict:
        """
        Submit a B2B invoice for FIRS clearance.
        FIRS MUST clear it BEFORE you send it to the buyer.

        On success:
            data.irn  — Invoice Reference Number
            data.csid — Cryptographic Stamp Identifier
        """
        return self._request("POST", "/invoices/b2b", json=ubl_payload)

    # ──────────────────────────────────────────────────────────
    # B2C INVOICE REPORTING (report within 24 hours)
    # ──────────────────────────────────────────────────────────

    def report_b2c_invoice(self, ubl_payload: Dict) -> Dict:
        """
        Report a B2C invoice to FIRS within 24 hours of issuing it.
        No pre-clearance needed — but report is mandatory.

        On success:
            data.qr_code — QR code for buyer verification
        """
        return self._request("POST", "/invoices/b2c", json=ubl_payload)

    # ──────────────────────────────────────────────────────────
    # CREDIT & DEBIT NOTES
    # ──────────────────────────────────────────────────────────

    def submit_credit_note(self, ubl_payload: Dict) -> Dict:
        """
        Submit a credit note against a previously cleared invoice.
        ubl_payload must include BillingReference.ID = original IRN.
        """
        return self._request("POST", "/invoices/credit-note", json=ubl_payload)

    def submit_debit_note(self, ubl_payload: Dict) -> Dict:
        """
        Submit a debit note against a previously cleared invoice.
        ubl_payload must include BillingReference.ID = original IRN.
        """
        return self._request("POST", "/invoices/debit-note", json=ubl_payload)

    # ──────────────────────────────────────────────────────────
    # INVOICE RETRIEVAL
    # ──────────────────────────────────────────────────────────

    def get_invoice(self, irn: str) -> Dict:
        """Retrieve a cleared invoice by its IRN."""
        return self._request("GET", f"/invoices/{irn}")

    def get_invoice_status(self, irn: str) -> Dict:
        """Check the current status of a submitted invoice."""
        return self._request("GET", f"/invoices/{irn}/status")

    def list_invoices(self, page: int = 1, page_size: int = 50, filters: Dict = None) -> Dict:
        """List all invoices submitted under the registered TIN."""
        params = {"page": page, "page_size": page_size, **(filters or {})}
        return self._request("GET", "/invoices", params=params)

    # ──────────────────────────────────────────────────────────
    # HEALTH CHECK
    # ──────────────────────────────────────────────────────────

    def health_check(self) -> Dict:
        """
        Quick check that FIRS API credentials are valid.
        Validates the supplier TIN registered in settings.
        """
        tin = getattr(settings, "FIRS_TIN", "") or getattr(settings, "CLIENT_COMPANY_TIN", "")
        if not tin:
            return {"success": False, "error": "FIRS_TIN not set in environment"}
        return self.validate_tin(tin)


# ── Custom Exceptions ─────────────────────────────────────────

class FIRSConfigError(Exception):
    """Raised when FIRS credentials are missing or misconfigured."""
    pass


class FIRSSubmissionError(Exception):
    """Raised when an invoice submission to FIRS fails definitively."""
    pass
