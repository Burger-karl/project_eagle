"""Dashboard endpoints and commands for pulling from Sage 200 in batches."""
import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from apps.dashboard import sage_batch
from apps.dashboard.models import SagePullState
from apps.dashboard.sage_batch import AlreadyRunning, PullError
from apps.invoices.models import Invoice
from tests.test_dashboard.test_preview_sage200 import sage_row
from tests.test_dashboard.test_sage_batch import company, firs, sage  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db
SB = "apps.dashboard.sage_batch"


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user("staff", password="pw", is_staff=True)


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user("member", password="pw")


def post(http, name, **body):
    return http.post(reverse(name), json.dumps(body), content_type="application/json")


# ── permissions ──────────────────────────────────────────────

@pytest.mark.parametrize("name", ["dashboard:sage-preview", "dashboard:sage-pull"])
def test_only_staff_may_preview_or_pull(client, member, company, name):
    assert post(client, name, client=str(company.id), count=5).status_code in (401, 403)   # anonymous
    client.force_login(member)
    assert post(client, name, client=str(company.id), count=5).status_code == 403          # signed in, not staff


# ── preview endpoint ─────────────────────────────────────────

def test_preview_returns_the_analysis(client, staff, company, sage):
    client.force_login(staff)
    r = post(client, "dashboard:sage-preview", client=str(company.id), count=5)
    body = r.json()
    assert r.status_code == 200
    assert body["returned"] == 5 and body["new"] == 5 and body["requested"] == 5
    assert body["bookmark"] == 0 and body["environment"] in ("SANDBOX", "PRODUCTION")
    assert body["samples"][0]["number"] == "INV-1"
    assert Invoice.objects.count() == 0


@pytest.mark.parametrize("count", [0, 5000, "lots", None])
def test_preview_rejects_a_bad_count(client, staff, company, sage, count):
    client.force_login(staff)
    r = post(client, "dashboard:sage-preview", client=str(company.id), count=count)
    assert r.status_code == 400 and r.json()["error"]


@pytest.mark.parametrize("bad", ["not-a-uuid", "00000000-0000-0000-0000-000000000000", None, 7])
def test_preview_rejects_an_unknown_client(client, staff, sage, bad):
    client.force_login(staff)
    r = post(client, "dashboard:sage-preview", client=bad, count=5)
    assert r.status_code == 400 and "active clients" in r.json()["error"]


def test_preview_reports_a_sage_failure_as_a_readable_error(client, staff, company):
    client.force_login(staff)
    with patch(f"{SB}.preview", side_effect=PullError("Could not read Sage 200: login failed")):
        r = post(client, "dashboard:sage-preview", client=str(company.id), count=5)
    assert r.status_code == 400 and "login failed" in r.json()["error"]


# ── pull endpoint ────────────────────────────────────────────

def test_pull_queues_a_background_task(client, staff, company):
    client.force_login(staff)
    with patch("apps.dashboard.tasks.pull_sage200_batch.delay") as delay:
        r = post(client, "dashboard:sage-pull", client=str(company.id), count="25")
    assert r.status_code == 202 and "25" in r.json()["message"]
    delay.assert_called_once_with(str(company.id), 25)


def test_pull_refuses_while_another_is_running(client, staff, company):
    from django.utils import timezone
    SagePullState.objects.create(client=company, running_since=timezone.now())
    client.force_login(staff)
    with patch("apps.dashboard.tasks.pull_sage200_batch.delay") as delay:
        r = post(client, "dashboard:sage-pull", client=str(company.id), count=5)
    assert r.status_code == 409 and "already running" in r.json()["error"]
    delay.assert_not_called()


def test_pull_validates_the_count_before_queueing(client, staff, company):
    client.force_login(staff)
    with patch("apps.dashboard.tasks.pull_sage200_batch.delay") as delay:
        assert post(client, "dashboard:sage-pull", client=str(company.id), count=0).status_code == 400
    delay.assert_not_called()


def test_pull_explains_what_to_do_when_redis_is_down(client, staff, company):
    client.force_login(staff)
    with patch("apps.dashboard.tasks.pull_sage200_batch.delay", side_effect=ConnectionError("redis down")):
        r = post(client, "dashboard:sage-pull", client=str(company.id), count=12)
    assert r.status_code == 503
    assert "Redis" in r.json()["error"] and "pull_sage200" in r.json()["error"] and "--limit 12" in r.json()["error"]


# ── clients endpoint carries pull progress ───────────────────

def test_clients_endpoint_shows_pull_progress(client, staff, company, sage, firs):
    sage_batch.pull(company, 4)
    client.force_login(staff)
    info = client.get(reverse("dashboard:clients")).json()[0]["sage"]
    assert info["bookmark"] == 4 and info["imported"] == 4 and info["running"] is False
    assert info["last_pull"]["cleared"] == 4


# ── background task ──────────────────────────────────────────

def test_the_task_runs_a_pull(company, sage, firs):
    from apps.dashboard.tasks import pull_sage200_batch
    summary = pull_sage200_batch.run(str(company.id), 3)
    assert summary["cleared"] == 3 and Invoice.objects.filter(client=company).count() == 3


def test_a_failed_background_pull_is_saved_for_the_dashboard(company):
    with patch(f"{SB}.pull", side_effect=PullError("Could not read Sage 200: no route to host")):
        out = sage_batch.pull_in_background(company, 10)
    saved = SagePullState.objects.get(client=company)
    assert "no route to host" in out["error"] and saved.last_pull_summary["error"] == out["error"]
    assert saved.last_pull_at


def test_a_refused_second_pull_does_not_erase_the_running_ones_state(company):
    SagePullState.objects.create(client=company, last_pull_summary={"cleared": 9})
    with patch(f"{SB}.pull", side_effect=AlreadyRunning("already running")):
        out = sage_batch.pull_in_background(company, 10)
    assert out == {"error": "already running"}
    assert SagePullState.objects.get(client=company).last_pull_summary == {"cleared": 9}


# ── pull_sage200 command ─────────────────────────────────────

def run(*args, answer=None):
    out, err = StringIO(), StringIO()
    with patch("builtins.input", return_value=answer or "") as ask:
        try:
            call_command("pull_sage200", *args, stdout=out, stderr=err, no_color=True)
        except CommandError as exc:
            return out.getvalue(), str(exc), ask
    return out.getvalue(), "", ask


def test_command_pulls_the_chosen_number(company, sage, firs):
    out, err, ask = run("--limit", "4", "--yes")
    assert not err and ask.call_count == 0
    assert Invoice.objects.filter(client=company).count() == 4
    assert "Cleared by FIRS : 4" in out and "#0  ->  #4" in out


def test_command_shows_a_preview_and_asks_first(company, sage, firs):
    out, err, ask = run("--limit", "3", answer="yes")
    assert "New, would be sent to FIRS by this pull: 3" in out
    assert "Type yes to continue" in ask.call_args.args[0]
    assert Invoice.objects.filter(client=company).count() == 3


def test_command_sends_nothing_when_you_decline(company, sage, firs):
    _, err, _ = run("--limit", "3", answer="no")
    assert "Cancelled" in err and Invoice.objects.count() == 0


@override_settings(USE_FIRS_PRODUCTION=True)
def test_production_needs_the_word_production(company, sage, firs):
    _, err, ask = run("--limit", "2", answer="yes")
    assert "Type PRODUCTION" in ask.call_args.args[0] and "Cancelled" in err and Invoice.objects.count() == 0
    run("--limit", "2", answer="PRODUCTION")
    assert Invoice.objects.filter(client=company).count() == 2


def test_command_continues_after_the_previous_pull(company, sage, firs):
    run("--limit", "3", "--yes")
    run("--limit", "3", "--yes")
    assert sorted(int(i) for i in Invoice.objects.values_list("sage_invoice_id", flat=True)) == [1, 2, 3, 4, 5, 6]


def test_command_can_restart_from_a_chosen_record(company, sage, firs):
    run("--limit", "3", "--yes")
    run("--limit", "2", "--after", "10", "--yes")
    assert Invoice.objects.filter(sage_invoice_id__in=["11", "12"]).count() == 2


def test_command_lists_invoices_it_could_not_read(company, sage, firs):
    sage.rows = [sage_row(1), sage_row(2, invoice_number=""), sage_row(3)]
    out, _, _ = run("--limit", "10", "--yes")
    assert "could not be read and were NOT saved" in out and "Cleared by FIRS : 2" in out


def test_command_says_so_when_redis_is_down(company, sage, firs):
    with patch(f"{SB}.broker_reachable", return_value=False):
        out, _, _ = run("--limit", "2", "--yes")
    assert "automatic retries were not queued" in out


def test_command_needs_a_client_when_several_are_active(company, sage, firs):
    from apps.core.models import ClientCompany
    ClientCompany.objects.create(name="Beta Ltd", tin="22222222-0001", address="x", email="b@example.com",
                                 mssql_server="s", mssql_database="d")
    _, err, _ = run("--limit", "2", "--yes")
    assert "Choose one with --client" in err and Invoice.objects.count() == 0
    run("--client", "Acme", "--limit", "2", "--yes")
    assert Invoice.objects.filter(client=company).count() == 2


def test_command_has_nothing_to_do_when_sage_is_empty(company, sage, firs):
    sage.rows = []
    out, _, ask = run("--limit", "5")
    assert "Nothing new to send" in out and ask.call_count == 0


def test_command_stops_cleanly_when_sage_cannot_be_read(company, firs):
    with patch(f"{SB}.preview", side_effect=PullError("Could not read Sage 200: login failed")):
        _, err, _ = run("--limit", "5", "--yes")
    assert "login failed" in err and Invoice.objects.count() == 0
