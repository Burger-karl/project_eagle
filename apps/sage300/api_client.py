"""
Sage 300 Web API Client
Handles OAuth2/session authentication and IRN write-back for Sage 300.

Sage 300 Web API uses session-based authentication (different from Sage 200 OAuth2).
The Web API must be installed on the Sage 300 server.

Default Web API URL: https://{server}/Sage300WebApi/v1.0/-/SAMLTD/
Where SAMLTD is your company database name.

Sage 300 Web API docs:
    https://developer.sage.com/sage-300/

IRN write-back approach:
    We write the IRN to the OEORDHD.PONUMBER field (or a UDF custom field)
    on the original document via PATCH /AR/ARInvoiceBatches/{batch}
"""

import logging
import requests
from typing import Dict, Optional
from django.utils import timezone

logger = logging.getLogger("apps.sage300")

# Sage 300 Web API session timeout
SESSION_TIMEOUT = 1800  # 30 minutes


class Sage300APIClient:
    """
    Sage 300 Web API client for IRN write-back.
    Uses session-based auth — different from Sage 200's OAuth2.

    Usage:
        client = Sage300APIClient(company)
        client.write_irn_to_invoice(invoice_number, irn, csid)
    """

    def __init__(self, company):
        self.company    = company
        self.base_url   = self._build_base_url()
        self._session   = None
        self._session_id = None

    def _build_base_url(self) -> str:
        """
        Build Sage 300 Web API base URL.
        Format: https://{server}/Sage300WebApi/v1.0/-/{company_db}/
        """
        server  = getattr(self.company, "sage300_api_server",   "localhost")
        company = getattr(self.company, "sage300_api_company",  "SAMLTD")
        version = getattr(self.company, "sage300_api_version",  "v1.0")
        return f"https://{server}/Sage300WebApi/{version}/-/{company}"

    def _authenticate(self) -> bool:
        """
        Authenticate with Sage 300 Web API using Basic Auth.
        Sage 300 uses username/password not OAuth2.
        """
        username = getattr(self.company, "sage300_api_username", "ADMIN")
        password = getattr(self.company, "sage300_api_password", "")

        try:
            response = requests.get(
                f"{self.base_url}/GL/GLAccounts?$top=1",
                auth=(username, password),
                headers={"Accept": "application/json"},
                timeout=10,
                verify=False,  # Sage 300 often uses self-signed certs on LAN
            )
            if response.status_code in (200, 401):
                self._session = requests.Session()
                self._session.auth = (username, password)
                self._session.headers.update({
                    "Accept":       "application/json",
                    "Content-Type": "application/json",
                })
                logger.info(f"[Sage300 API] Authenticated as {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"[Sage300 API] Auth failed: {e}")
            return False

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Central request handler for Sage 300 Web API."""
        if not self._session:
            if not self._authenticate():
                return {"success": False, "error": "Authentication failed"}

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = self._session.request(
                method, url, timeout=15, verify=False, **kwargs
            )
            if response.status_code in (200, 201, 204):
                try:
                    data = response.json() if response.content else {}
                except Exception:
                    data = {}
                return {"success": True, "status": response.status_code, "data": data}
            else:
                return {
                    "success": False,
                    "status":  response.status_code,
                    "error":   response.text[:500],
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── IRN WRITE-BACK ────────────────────────────────────────

    def write_irn_to_invoice(
        self,
        invoice_number: str,
        batch_number:   str,
        entry_number:   str,
        irn:            str,
        csid:           str = "",
    ) -> Dict:
        """
        Write FIRS IRN and CSID back to Sage 300 invoice.

        Sage 300 doesn't have a dedicated IRN field — we write to:
            REFERENCE field (ARINVOICE.TEXTREF2) if available
            Or a User Defined Field (UDF) configured for IRN storage

        Sage 300 Web API endpoint:
            PATCH /AR/ARInvoiceBatches(BatchNumber='{batch}',EntryNumber='{entry}')
        """
        logger.info(
            f"[Sage300 API] Writing IRN to invoice {invoice_number}: {irn}"
        )

        # Try to update via the PATCH endpoint
        # The exact field depends on the Sage 300 configuration
        endpoint = (
            f"AR/ARInvoiceBatches"
            f"(BatchNumber='{batch_number}',EntryNumber='{entry_number}')"
        )

        # Write IRN to TEXTREF2 (second reference field)
        # or COMMENT1 if available in this Sage 300 version
        payload = {
            "TEXTREF":  f"IRN:{irn[:30]}",    # Reference field (30 char limit)
            "TEXTREF2": csid[:30] if csid else "",
        }

        result = self._request("PATCH", endpoint, json=payload)

        if result["success"]:
            logger.info(
                f"[Sage300 API] IRN written to invoice {invoice_number}"
            )
        else:
            logger.error(
                f"[Sage300 API] IRN write-back failed for {invoice_number}: "
                f"{result.get('error')}"
            )

        return result

    def get_sites(self) -> Dict:
        """List available Sage 300 companies — used during setup."""
        return self._request("GET", "GL/GLAccounts?$top=1")


class Sage300AuthError(Exception):
    """Raised when Sage 300 API authentication fails."""
    pass
