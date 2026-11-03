"""The batch service: pick how many invoices, continue where the last pull stopped."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.dashboard import sage_batch
from apps.dashboard.models import SagePullState
from apps.dashboard.sage_batch import AlreadyRunning, PullError
from apps.core.models import ClientCompany
from apps.invoices.models import Invoice, InvoiceStatus
from apps.sage200.odbc_client import Sage200ConnectionError
from tests.test_dashboard.test_dashboard import make_invoice
from tests.test_dashboard.test_preview_sage200 import sage_row

pytestmark = pytest.mark.django_db

SB = "apps.dashboard.sage_batch"
PIPE = "apps.invoices.pipeline"


@pytest.fixture
def company():
    return ClientCompany.objects.create(
        name="Acme Ltd", tin="12345678-0001", address="Lagos", email="a@example.com",
        mssql_server="srv", mssql_database="db",
    )


@pytest.fixture
def sage():
    """A fake Sage 200 that serves a numbered list of rows, honouring limit and after_id."""
    class FakeSage:
        rows = [sage_row(n) for n in range(1, 31)]
        calls = []

        def get_new_invoices(self, since=None, limit=500, after_id=None):
            FakeSage.calls.append({"limit": limit, "after_id": after_id})
            return [r for r in FakeSage.rows if after_id is None or r["invoice_id"] > after_id][:limit]

    FakeSage.calls = []
    FakeSage.rows = [sage_row(n) for n in range(1, 31)]
    with patch(f"{SB}.Sage200ODBCClient", return_value=FakeSage()), \
         patch(f"{PIPE}.Sage200ODBCClient", return_value=FakeSage()):
        yield FakeSage


@pytest.fixture
def firs():
    """FIRS accepts everything and Sage's IRN write-back succeeds."""
    client = MagicMock()
    client.validate_tin.return_value = {"success": True}
    ok = {"success": True, "data": {"irn": "IRN-1", "csid": "CSID", "qrCode": ""}}
    client.submit_b2b_invoice.return_value = ok
    client.report_b2c_invoice.return_value = ok
    with patch(f"{PIPE}.FIRSClient", return_value=client), patch(f"{PIPE}.Sage200IRNWriter") as writer, \
         patch(f"{SB}.broker_reachable", return_value=True), patch("apps.invoices.tasks.retry_single_invoice") as retry:
        writer.return_value.write_irn.return_value = True
        client.retry_task = retry   # lets tests see whether a retry was queued
        yield client


# ── the bookmark ─────────────────────────────────────────────

def test_first_bookmark_starts_after_what_eagle_already_holds(company):
    for n, source in [("5", "SAGE200"), ("12", "SAGE200"), ("abc", "SAGE200"), ("999", "SAGE300")]:
        inv = make_invoice(company, int(n) if n.isdigit() else 1000, InvoiceStatus.CLEARED)
        Invoice.objects.filter(pk=inv.pk).update(sage_invoice_id=n, source_system=source)
    assert sage_batch.get_state(company).last_record_id == 12   # not 999 (Sage 300), not "abc"


def test_a_brand_new_client_starts_at_the_beginning(company):
    assert sage_batch.get_state(company).last_record_id == 0


# ── choosing the count ───────────────────────────────────────

@pytest.mark.parametrize("bad", [0, -5, 1001, "abc", None, "2.5"])
def test_count_must_be_a_whole_number_in_range(bad):
    with pytest.raises(PullError):
        sage_batch.clamp_count(bad)


@pytest.mark.parametrize("good", [1, "25", 1000])
def test_valid_counts_are_accepted(good):
    assert sage_batch.clamp_count(good) == int(good)


# ── preview ──────────────────────────────────────────────────

def test_preview_asks_for_exactly_the_chosen_count_after_the_bookmark(company, sage):
    SagePullState.objects.create(client=company, last_record_id=10)
    result = sage_batch.preview(company, 5)
    assert sage.calls == [{"limit": 5, "after_id": 10}]
    assert result["returned"] == 5 and result["new"] == 5
    assert result["bookmark"] == 10 and result["last_record_id"] == 15
    assert result["environment"] in ("SANDBOX", "PRODUCTION")


def test_preview_saves_nothing(company, sage, firs):
    sage_batch.preview(company, 5)
    assert Invoice.objects.count() == 0
    firs.submit_b2b_invoice.assert_not_called()
    firs.report_b2c_invoice.assert_not_called()
    assert sage_batch.get_state(company).last_record_id == 0   # preview never moves the bookmark


def test_preview_explains_a_connection_failure(company):
    with patch(f"{SB}.Sage200ODBCClient") as client, patch(f"{SB}.installed_sql_drivers", return_value=["ODBC Driver 18 for SQL Server"]):
        client.return_value.get_new_invoices.side_effect = Sage200ConnectionError("login failed")
        with pytest.raises(PullError) as err:
            sage_batch.preview(company, 5)
    assert "login failed" in str(err.value) and "ODBC Driver 18 for SQL Server" in str(err.value)


# ── pull ─────────────────────────────────────────────────────

def test_pull_takes_only_the_chosen_number_and_moves_the_bookmark(company, sage, firs):
    summary = sage_batch.pull(company, 5)
    assert Invoice.objects.filter(client=company).count() == 5
    assert summary["requested"] == 5 and summary["pulled"] == 5 and summary["cleared"] == 5
    assert summary["bookmark_before"] == 0 and summary["bookmark_after"] == 5
    assert sage_batch.get_state(company).last_record_id == 5


def test_pulling_twice_gives_different_invoices(company, sage, firs):
    sage_batch.pull(company, 5)
    sage_batch.pull(company, 5)
    numbers = sorted(Invoice.objects.filter(client=company).values_list("sage_invoice_id", flat=True), key=int)
    assert numbers == [str(n) for n in range(1, 11)]
    assert [c["after_id"] for c in sage.calls] == [0, 5]


def test_pulling_everything_in_batches_ends_cleanly(company, sage, firs):
    for _ in range(3):
        sage_batch.pull(company, 10)
    assert Invoice.objects.filter(client=company).count() == 30
    last = sage_batch.pull(company, 10)                      # nothing left
    assert last["pulled"] == 0 and last["bookmark_after"] == 30


def test_invoices_the_mapper_rejects_are_reported_not_lost(company, sage, firs):
    sage.rows = [sage_row(1), sage_row(2, invoice_number=""), sage_row(3)]
    summary = sage_batch.pull(company, 10)
    assert summary["cleared"] == 2 and summary["failed"] == 1
    assert len(summary["errors"]) == 1 and summary["errors"][0]["sage_id"] == "2"
    assert summary["errors"][0]["reason"]
    assert summary["bookmark_after"] == 3                    # moved past the bad row, so it cannot trap the next pull
    assert sage_batch.get_state(company).last_pull_summary["errors"][0]["sage_id"] == "2"


def test_a_batch_of_only_bad_rows_still_moves_the_bookmark(company, sage, firs):
    sage.rows = [sage_row(n, invoice_number="") for n in range(1, 4)]
    summary = sage_batch.pull(company, 3)
    assert summary["failed"] == 3 and summary["bookmark_after"] == 3


def test_firs_rejections_are_saved_as_failed_invoices_you_can_retry(company, sage, firs):
    firs.report_b2c_invoice.return_value = {"success": False, "error_code": "HTTP_503", "error": "down", "status_code": 503}
    sage.rows = [sage_row(1)]
    summary = sage_batch.pull(company, 1)
    assert summary["failed"] == 1 and not summary["errors"]
    assert Invoice.objects.get(client=company).status == InvoiceStatus.FAILED


def test_an_override_bookmark_can_restart_from_a_chosen_point(company, sage, firs):
    SagePullState.objects.create(client=company, last_record_id=20)
    sage_batch.pull(company, 3, after_id=0)
    assert sage.calls[-1]["after_id"] == 0


def test_a_second_pull_is_refused_while_one_is_running(company, sage, firs):
    SagePullState.objects.create(client=company, running_since=timezone.now())
    with pytest.raises(AlreadyRunning):
        sage_batch.pull(company, 5)
    assert Invoice.objects.count() == 0


def test_a_crashed_pull_does_not_block_forever(company, sage, firs):
    SagePullState.objects.create(client=company, running_since=timezone.now() - timedelta(hours=4))
    assert sage_batch.pull(company, 2)["cleared"] == 2


def test_the_running_flag_is_cleared_even_when_sage_is_unreachable(company, firs):
    with patch(f"{PIPE}.Sage200ODBCClient") as client:
        client.return_value.get_new_invoices.side_effect = Sage200ConnectionError("no route to host")
        with pytest.raises(PullError, match="no route to host"):
            sage_batch.pull(company, 5)
    assert sage_batch.get_state(company).running_since is None
    assert sage_batch.get_state(company).last_record_id == 0


def test_describe_summarises_what_the_dashboard_shows(company, sage, firs):
    sage_batch.pull(company, 4)
    info = sage_batch.describe(company)
    assert info["bookmark"] == 4 and info["imported"] == 4 and info["running"] is False
    assert info["last_pull"]["cleared"] == 4 and info["last_pull_at"]


def test_a_down_broker_never_hides_the_real_firs_error(company, sage, firs):
    """Redis down used to overwrite FIRS's error with a connection error (UNKNOWN_ERROR)."""
    firs.report_b2c_invoice.return_value = {"success": False, "error": {"code": "TIN_INVALID", "message": "bad tin"}}
    firs.retry_task.apply_async.side_effect = ConnectionError("Error 10061 connecting to localhost:6379")
    sage.rows = [sage_row(1)]
    sage_batch.pull(company, 1)
    inv = Invoice.objects.get(client=company)
    assert inv.status == InvoiceStatus.FAILED
    assert inv.error_code == "TIN_INVALID" and "bad tin" in inv.error_message


def test_no_retries_are_queued_when_the_broker_is_unreachable(company, sage, firs):
    firs.report_b2c_invoice.return_value = {"success": False, "error": {"code": "HTTP_503", "message": "down"}}
    sage.rows = [sage_row(1)]
    with patch(f"{SB}.broker_reachable", return_value=False):
        summary = sage_batch.pull(company, 1)
    firs.retry_task.apply_async.assert_not_called()
    assert summary["retries_scheduled"] is False and summary["failed"] == 1


def test_retries_are_queued_when_the_broker_is_up(company, sage, firs):
    firs.report_b2c_invoice.return_value = {"success": False, "error": {"code": "HTTP_503", "message": "down"}}
    sage.rows = [sage_row(1)]
    assert sage_batch.pull(company, 1)["retries_scheduled"] is True
    firs.retry_task.apply_async.assert_called_once()


def test_broker_check_is_quick_and_honest():
    import time
    started = time.monotonic()
    assert sage_batch.broker_reachable("redis://127.0.0.1:1/0", timeout=1.0) is False
    assert time.monotonic() - started < 6
    assert sage_batch.broker_reachable("memory://") is True
