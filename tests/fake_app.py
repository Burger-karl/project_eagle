"""
Test double for the Access Point Provider layer.

Pipelines now submit through `get_provider(company).submit(mapped)`. Many
flow tests were written against the old FIRSClient mock, with
`submit_b2b_invoice` / `report_b2c_invoice` returning plain dicts, and they
assert on B2B vs B2C routing and on the UBL content built from the mapped
invoice. `LegacyFIRSProvider` keeps those tests meaningful: it sits behind the
provider interface, builds the same UBL payload, routes it to the mock by
invoice type, and turns the dict reply into a SubmissionResult.

Use:
    with patch_provider("apps.invoices.pipeline", firs_mock):
        ...
"""

from contextlib import contextmanager
from unittest.mock import patch

from apps.firs.invoice_schema import UBLInvoiceBuilder
from apps.firs.providers import ProviderStatus, SubmissionResult


class LegacyFIRSProvider:
    name = "digitax"

    def __init__(self, client):
        self.client = client

    def validate_tin(self, tin):
        result = self.client.validate_tin(tin)
        return result if isinstance(result, dict) else {"success": True, "supported": False}

    def submit(self, mapped):
        builder = UBLInvoiceBuilder(mapped)
        if str(mapped.get("invoice_type", "")).upper() == "B2C":
            payload = builder.build_b2c()
            reply = self.client.report_b2c_invoice(payload)
        else:
            payload = builder.build_b2b()
            reply = self.client.submit_b2b_invoice(payload)

        if reply.get("success"):
            data = reply.get("data") or {}
            return SubmissionResult(
                success=True, provider=self.name, payload=payload,
                irn=data.get("irn") or data.get("IRN") or "",
                csid=data.get("csid") or data.get("CSID") or "",
                qr_code=data.get("qrCode") or data.get("qr_code") or "",
                provider_invoice_id=data.get("id", ""),
                provider_status=ProviderStatus.SIGNED, raw=data,
                status_code=reply.get("status_code"),
            )

        error = reply.get("error")
        if isinstance(error, dict):
            code, message = error.get("code", ""), error.get("message", str(error))
        else:
            code, message = reply.get("error_code", ""), str(error or "")
        return SubmissionResult(
            success=False, provider=self.name, payload=payload,
            error=message, error_code=code, status_code=reply.get("status_code"),
            provider_status=ProviderStatus.FAILED,
        )

    def get_status(self, provider_invoice_id):
        return SubmissionResult(success=True, provider=self.name, provider_status=ProviderStatus.SIGNED)


@contextmanager
def patch_provider(module_path, client):
    """Patch `get_provider` in a pipeline module so it returns a LegacyFIRSProvider around `client`."""
    with patch(f"{module_path}.get_provider", return_value=LegacyFIRSProvider(client)) as gp:
        yield gp
