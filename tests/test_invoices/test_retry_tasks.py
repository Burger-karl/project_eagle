"""Retries must skip inactive (demo) clients and must not hammer an unreachable Sage."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.core.models import ClientCompany
from apps.invoices.models import Invoice, InvoiceStatus
from apps.invoices.tasks import retry_failed_submissions, submit_single_invoice
from apps.sage200.odbc_client import Sage200ConnectionError

pytestmark = pytest.mark.django_db


def failed_invoice(active=True, number="INV-1"):
    co = ClientCompany.objects.create(
        name=f"Co {number}", tin=f"{number}-0001", address="Lagos", email="a@b.c",
        mssql_server="demo", mssql_database="demo", is_active=active,
    )
    return Invoice.objects.create(
        client=co, sage_invoice_id=number, sage_invoice_number=number, sage_account_ref="C",
        invoice_date=date.today(), supplier_tin="1", supplier_name="Co", supplier_address="Lagos",
        buyer_name="B", buyer_address="Lagos", net_amount=100, vat_amount=7.5, gross_amount=107.5,
        status=InvoiceStatus.FAILED, submission_attempts=1,
        next_retry_at=timezone.now() - timedelta(minutes=1),
    )


def test_inactive_clients_are_not_retried():
    failed_invoice(active=False, number="DEMO-1")
    live = failed_invoice(active=True, number="LIVE-1")
    with patch("apps.invoices.tasks.retry_single_invoice.delay") as delay:
        assert retry_failed_submissions() == {"retried": 1}
    delay.assert_called_once_with(str(live.id))


def test_submitting_an_inactive_clients_invoice_does_nothing():
    inv = failed_invoice(active=False, number="DEMO-2")
    with patch("apps.invoices.pipeline.Sage200ODBCClient") as odbc:
        assert submit_single_invoice.run(str(inv.id)) == {"status": "client_inactive"}
    odbc.return_value.get_invoice_by_id.assert_not_called()


def test_unreachable_sage_postpones_instead_of_retrying_in_the_worker():
    inv = failed_invoice(number="LIVE-2")
    with patch("apps.invoices.pipeline.Sage200ODBCClient") as odbc:
        odbc.return_value.get_invoice_by_id.side_effect = Sage200ConnectionError("server not found")
        assert submit_single_invoice.run(str(inv.id)) == {"error": "sage_unreachable"}
    inv.refresh_from_db()
    assert inv.error_code == "SAGE_UNREACHABLE"
    assert inv.next_retry_at > timezone.now() + timedelta(minutes=25)
    assert inv.status == InvoiceStatus.FAILED
