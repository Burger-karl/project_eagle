"""Tests for `manage.py preview_sage200`: read-only, no FIRS, clear failure messages."""
from datetime import datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core.models import ClientCompany
from apps.invoices.models import BuyerTINMapping, Invoice
from apps.sage200.odbc_client import Sage200ConnectionError

pytestmark = pytest.mark.django_db

CMD = "apps.dashboard.sage_batch"


@pytest.fixture
def company():
    return ClientCompany.objects.create(
        name="Acme Ltd", tin="12345678-0001", address="Lagos", email="a@example.com",
        mssql_server="SAGE-PC\\SQL2025", mssql_database="ACME_LIVE",
        mssql_username="sa", mssql_password="super-secret-pw", mssql_trusted=False,
    )


def sage_row(n, *, account="CUS001", code="Inv", code_id=38, debit=1075.0, credit=0.0, day=(2026, 9, 1), **extra):
    """A row shaped like Sage200ODBCClient.get_new_invoices returns."""
    gross = debit or credit
    row = dict(
        invoice_id=n, invoice_date=datetime(*day), invoice_number=f"INV-{n}", tr_type_code=code,
        tr_code_id=code_id, account_code=account, customer_name=f"Customer {account}",
        buyer_address1="1 Marina", debit_amount=debit, credit_amount=credit, vat_amount=75.0,
        gross_amount=gross, net_amount=gross - 75.0, transaction_type=1 if debit else 2,
        exchange_rate=1.0, currency_id=0, lines=[],
    )
    row.update(extra)
    return row


def run(*args):
    out, err = StringIO(), StringIO()
    try:
        call_command("preview_sage200", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()
    except CommandError as exc:
        return out.getvalue(), err.getvalue() + str(exc)


def test_reports_counts_types_and_dates(company):
    rows = [sage_row(1), sage_row(2, day=(2026, 9, 5)), sage_row(3, code="Rct", code_id=12, debit=0.0, credit=500.0)]
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = rows
        out, err = run()
    assert "Sage returned 3 row(s)" in out
    assert "2026-09-01  to  2026-09-05" in out
    assert "Inv  (TrCodeID 38)" in out and "Rct  (TrCodeID 12)" in out
    assert "Debit rows (treated as invoices): 2" in out and "Credit rows (treated as credit notes): 1" in out
    assert "New, would be sent to FIRS by this pull: 3" in out
    assert "nothing was saved and nothing was sent to FIRS" in out
    assert not err


def test_never_prints_the_database_password(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = [sage_row(1)]
        out, err = run()
    assert "super-secret-pw" not in out + err
    assert "ACME_LIVE" in out


def test_saves_nothing_and_never_calls_firs(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client, \
         patch("apps.firs.client.FIRSClient") as firs, \
         patch("apps.invoices.pipeline.FIRSClient") as pipeline_firs:
        client.return_value.get_new_invoices.return_value = [sage_row(i) for i in range(1, 6)]
        run()
    assert Invoice.objects.count() == 0
    firs.assert_not_called()
    pipeline_firs.assert_not_called()


def test_unmapped_customers_are_counted_as_consumer_sales(company):
    BuyerTINMapping.objects.create(client=company, sage_account_ref="CUS001", buyer_name="Known", buyer_tin="99999999-0001")
    rows = [sage_row(1, account="CUS001"), sage_row(2, account="CUS002"), sage_row(3, account="CUS003")]
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = rows
        out, _ = run()
    assert "1 business-to-business (buyer TIN known), 2 consumer (no buyer TIN)" in out


def test_rows_already_in_eagle_are_not_counted_as_new(company):
    from tests.test_dashboard.test_dashboard import make_invoice  # reuse the helper
    from apps.invoices.models import InvoiceStatus
    existing = make_invoice(company, 1, InvoiceStatus.CLEARED)
    Invoice.objects.filter(pk=existing.pk).update(sage_invoice_id="1")
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = [sage_row(1), sage_row(2)]
        out, _ = run()
    assert "Already in Eagle: 1" in out
    assert "New, would be sent to FIRS by this pull: 1" in out


def test_rows_the_mapper_rejects_are_listed(company):
    rows = [sage_row(1), sage_row(2, invoice_number=""), sage_row(3, invoice_date=None)]
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = rows
        out, _ = run()
    assert "2 new row(s) the mapper cannot handle" in out


def test_passes_since_and_limit_to_the_query(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        get = client.return_value.get_new_invoices
        get.return_value = []
        out, _ = run("--since", "2026-09-01", "--limit", "50")
    get.assert_called_once_with(since=datetime(2026, 9, 1), limit=50, after_id=0)
    assert "Nothing more to pull" in out


def test_warns_when_the_row_limit_was_hit(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = [sage_row(i) for i in range(1, 4)]
        out, _ = run("--limit", "3")
    assert "That is the 3 you asked for" in out


def test_connection_failure_is_explained_and_exits_nonzero(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client, \
         patch(f"{CMD}.installed_sql_drivers", return_value=["ODBC Driver 18 for SQL Server"]):
        client.return_value.get_new_invoices.side_effect = Sage200ConnectionError("login failed for user 'sa'")
        out, err = run()
    assert "Could not read Sage 200" in err and "login failed" in err
    assert "'ODBC Driver 17 for SQL Server' is not installed" in err
    assert "ODBC Driver 18 for SQL Server" in err
    assert "could not be read" in err  # the CommandError summary


def test_a_failing_query_is_reported_separately_from_a_failed_connection(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.side_effect = RuntimeError("Invalid object name 'PostAR'")
        _, err = run()
    assert "query failed" in err and "PostAR" in err


def test_no_active_client_gives_a_helpful_error(db):
    _, err = run()
    assert "No active client found" in err and "admin" in err


def test_bad_since_value_is_rejected(company):
    _, err = run("--since", "last tuesday")
    assert "Could not read --since" in err


def test_inactive_clients_are_skipped(company):
    ClientCompany.objects.filter(pk=company.pk).update(is_active=False)
    _, err = run()
    assert "No active client found" in err


def test_warns_when_sage_returns_rows_older_than_since(company):
    """Guards against the old OR/AND precedence bug in the Sage query."""
    rows = [sage_row(1, day=(2026, 1, 10)), sage_row(2, day=(2026, 9, 5))]
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = rows
        out, _ = run("--since", "2026-09-01")
    assert "1 of these rows are dated on or before --since" in out
    assert "updated odbc_client.py" in out


def test_no_warning_when_every_row_is_newer_than_since(company):
    with patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = [sage_row(1, day=(2026, 9, 5))]
        out, _ = run("--since", "2026-09-01")
    assert "WARNING" not in out


def test_log_noise_from_the_mapper_does_not_reach_the_report(company, caplog):
    import logging
    with caplog.at_level(logging.DEBUG, logger="apps.sage200"), patch(f"{CMD}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.return_value = [sage_row(1), sage_row(2, account="")]
        run()
    assert not [r for r in caplog.records if r.name == "apps.sage200"]
    assert logging.getLogger("apps.sage200").level != logging.CRITICAL  # restored afterwards
