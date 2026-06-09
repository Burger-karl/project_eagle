"""
Sage 300 IRN Writer
Coordinates writing FIRS IRN and CSID back to Sage 300
after an invoice is cleared. Uses the Sage 300 Web API.
"""

import logging
from apps.sage300.api_client import Sage300APIClient, Sage300AuthError

logger = logging.getLogger("apps.sage300")


class Sage300IRNWriter:
    """
    Writes FIRS clearance data back into Sage 300.

    Usage:
        writer = Sage300IRNWriter(company)
        writer.write_irn(invoice_number, batch_number, entry_number, irn, csid)
    """

    def __init__(self, company):
        self.company = company
        self._api    = None
        if getattr(company, "sage300_api_server", None):
            self._api = Sage300APIClient(company)

    def write_irn(
        self,
        invoice_number: str,
        batch_number:   str,
        entry_number:   str,
        irn:            str,
        csid:           str = "",
    ) -> bool:
        """
        Write IRN and CSID to Sage 300 invoice.
        Returns True on success, raises on failure.
        """
        if not self._api:
            logger.warning(
                f"[Sage300 IRNWriter] No Sage 300 API credentials for "
                f"{self.company.name}. IRN stored in middleware DB only."
            )
            return False

        try:
            result = self._api.write_irn_to_invoice(
                invoice_number=invoice_number,
                batch_number=batch_number,
                entry_number=entry_number,
                irn=irn,
                csid=csid,
            )
            if result["success"]:
                logger.info(
                    f"[Sage300 IRNWriter] ✅ IRN written: "
                    f"{invoice_number} → {irn}"
                )
                return True
            else:
                raise IRNWriteError(
                    f"Write-back failed for {invoice_number}: "
                    f"{result.get('error')}"
                )
        except Sage300AuthError as e:
            raise IRNWriteError(f"Sage 300 auth error: {e}")


class IRNWriteError(Exception):
    """Raised when writing IRN back to Sage 300 fails."""
    pass
