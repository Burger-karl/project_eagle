"""
IRN Writer
Coordinates writing FIRS IRN and CSID back to Sage 200
after an invoice is cleared. Uses the Sage 200 REST API
(NOT ODBC — we never write to the database directly).
"""

import logging
from apps.sage200.api_client import Sage200APIClient, Sage200AuthError

logger = logging.getLogger("apps.sage200")


class Sage200IRNWriter:
    """
    Writes FIRS clearance data back into Sage 200.

    Usage:
        writer = Sage200IRNWriter(company)
        writer.write_irn(sage_invoice_id, sage_invoice_number, irn, csid)
    """

    def __init__(self, company):
        self.company = company
        # Only initialise API client if Sage credentials are configured
        self._api = None
        if company.sage_client_id and company.sage_access_token:
            self._api = Sage200APIClient(company)

    def write_irn(
        self,
        sage_invoice_id: str,
        sage_invoice_number: str,
        irn: str,
        csid: str = "",
    ) -> bool:
        """
        Write IRN and CSID to the Sage 200 invoice record.

        Returns True on success, raises on failure.
        """
        if not self._api:
            logger.warning(
                f"[IRNWriter] No Sage 200 API credentials for {self.company.name}. "
                "IRN will be stored in middleware DB only."
            )
            # Not a hard failure — IRN is safely stored in our DB
            return False

        try:
            result = self._api.write_irn_to_invoice(
                sage_invoice_number=sage_invoice_number,
                irn=irn,
                csid=csid,
            )
            if result["success"]:
                logger.info(
                    f"[IRNWriter] IRN written to Sage 200: {sage_invoice_number} → {irn}"
                )
                return True
            else:
                logger.error(
                    f"[IRNWriter] Write-back failed for {sage_invoice_number}: "
                    f"{result.get('error')}"
                )
                raise IRNWriteError(
                    f"Could not write IRN to Sage 200 invoice {sage_invoice_number}: "
                    f"{result.get('error')}"
                )
        except Sage200AuthError as e:
            logger.error(f"[IRNWriter] Auth error for {self.company.name}: {e}")
            raise IRNWriteError(f"Sage 200 authentication failed: {e}")


class IRNWriteError(Exception):
    """Raised when writing IRN back to Sage 200 fails."""
    pass
