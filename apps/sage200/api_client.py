"""
Sage 200 REST API Client
Handles OAuth2 authentication and IRN write-back.

This is the WRITE layer — used ONLY to write IRN/CSID back
into Sage 200 after FIRS clears an invoice.

We NEVER use this to read invoice data (ODBC handles that).
"""

import logging
import requests
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger("apps.sage200")

SAGE_AUTH_URL  = "https://id.sage.com/oauth/token"
TOKEN_EXPIRY_BUFFER = 30  # refresh 30 min before expiry


class Sage200APIClient:
    """
    Sage 200 REST API client with automatic token refresh.

    Usage:
        client = Sage200APIClient(company)
        client.write_irn_to_invoice(sage_invoice_number, irn, csid)
    """

    def __init__(self, company):
        self.company   = company
        self.base_url  = company.sage_api_base_url or settings.SAGE200_API_BASE_URL
        self._token    = company.sage_access_token
        self._expires  = company.sage_token_expires

    # ──────────────────────────────────────────────────────────
    # AUTHENTICATION
    # ──────────────────────────────────────────────────────────

    def _get_valid_token(self) -> str:
        """
        Return a valid access token, refreshing if needed.
        Sage 200 tokens expire after 480 minutes (8 hours).
        """
        if self._is_token_valid():
            return self._token

        logger.info(f"[Sage API] Refreshing access token for {self.company.name}")
        self.refresh_token()
        return self._token

    def _is_token_valid(self) -> bool:
        """Check if the stored token is still valid (with buffer)."""
        if not self._token or not self._expires:
            return False
        buffer = timedelta(minutes=TOKEN_EXPIRY_BUFFER)
        return timezone.now() < (self._expires - buffer)

    def refresh_token(self):
        """
        Use the stored refresh token to get a new access token.
        Sage 200 uses OAuth2 authorization code flow.
        """
        if not self.company.sage_refresh_token:
            raise Sage200AuthError(
                f"No refresh token for {self.company.name}. "
                "User must re-authenticate via OAuth2 flow."
            )

        payload = {
            "grant_type":    "refresh_token",
            "client_id":     self.company.sage_client_id,
            "client_secret": self.company.sage_client_secret,
            "refresh_token": self.company.sage_refresh_token,
        }

        try:
            response = requests.post(SAGE_AUTH_URL, data=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            raise Sage200AuthError(f"Token refresh failed for {self.company.name}: {e}")

        # Calculate expiry (token valid for 480 minutes)
        expires_in = data.get("expires_in", 28800)
        new_expiry = timezone.now() + timedelta(seconds=expires_in)

        # Persist new tokens to the database
        from apps.core.models import ClientCompany
        ClientCompany.objects.filter(id=self.company.id).update(
            sage_access_token=data["access_token"],
            sage_refresh_token=data.get("refresh_token", self.company.sage_refresh_token),
            sage_token_expires=new_expiry,
        )

        self._token   = data["access_token"]
        self._expires = new_expiry
        logger.info(f"[Sage API] Token refreshed for {self.company.name}, expires {new_expiry}")

    # ──────────────────────────────────────────────────────────
    # REQUEST HELPER
    # ──────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        """Build authenticated headers for every Sage 200 API request."""
        return {
            "Authorization": f"Bearer {self._get_valid_token()}",
            "X-Site":        self.company.sage_site_id,
            "X-Company":     self.company.sage_company_id,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Central request handler for Sage 200 API calls."""
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            response = requests.request(
                method, url,
                headers=self._headers(),
                timeout=20,
                **kwargs
            )
            if response.status_code in (200, 201, 204):
                return {"success": True, "status": response.status_code,
                        "data": response.json() if response.content else {}}
            else:
                return {"success": False, "status": response.status_code,
                        "error": response.text[:500]}
        except requests.RequestException as e:
            return {"success": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # IRN WRITE-BACK
    # ──────────────────────────────────────────────────────────

    def write_irn_to_invoice(
        self,
        sage_invoice_number: str,
        irn: str,
        csid: str = "",
    ) -> dict:
        """
        Write the FIRS IRN and CSID into a Sage 200 invoice's
        reference fields using the Sage 200 REST API.

        We write to:
            reference        → IRN (Invoice Reference Number)
            second_reference → CSID (truncated if needed)

        Sage 200 API endpoint:
            PATCH /sales_invoices?$filter=invoice_number eq '{number}'
        """
        logger.info(
            f"[Sage API] Writing IRN to invoice {sage_invoice_number}: {irn}"
        )

        # Find the invoice by invoice number
        search_result = self._request(
            "GET",
            f"sales_invoices?$filter=invoice_number eq '{sage_invoice_number}'"
        )

        if not search_result["success"]:
            return {"success": False, "error": f"Invoice not found in Sage 200: {sage_invoice_number}"}

        invoices = search_result["data"].get("$items", [])
        if not invoices:
            return {"success": False, "error": f"No invoice found: {sage_invoice_number}"}

        invoice_id = invoices[0].get("id")
        if not invoice_id:
            return {"success": False, "error": "Sage 200 invoice has no ID"}

        # Write IRN to reference fields
        patch_payload = {
            "reference":        irn[:60],            # Sage 200 reference field (60 char max)
            "second_reference": csid[:60] if csid else "",  # Second reference for CSID
        }

        result = self._request("PATCH", f"sales_invoices/{invoice_id}", json=patch_payload)

        if result["success"]:
            logger.info(
                f"[Sage API] IRN successfully written to {sage_invoice_number}"
            )
        else:
            logger.error(
                f"[Sage API] IRN write-back failed for {sage_invoice_number}: {result.get('error')}"
            )

        return result

    def get_sites(self) -> dict:
        """
        Get all Sage 200 sites/companies for this user.
        Used to find Site ID and Company ID during setup.
        """
        try:
            resp = requests.get(
                f"{self.base_url.rstrip('/')}/sites",
                headers={"Authorization": f"Bearer {self._get_valid_token()}"},
                timeout=10
            )
            return {"success": True, "data": resp.json()}
        except Exception as e:
            return {"success": False, "error": str(e)}


class Sage200AuthError(Exception):
    """Raised when Sage 200 OAuth authentication fails."""
    pass
