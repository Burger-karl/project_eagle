"""Tests for the Eagle dashboard: page access, JSON endpoints and sample data."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import ClientCompany
from apps.dashboard.management.commands.seed_demo_data import DEMO_NAME
from apps.dashboard.views import _endpoints
from apps.excel_intake.models import ExcelUpload
from apps.invoices.models import BuyerTINMapping, Invoice, InvoiceStatus

pytestmark = pytest.mark.django_db


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user("staff", password="pw", is_staff=True)


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user("member", password="pw")  # not staff


@pytest.fixture
def company():
    return ClientCompany.objects.create(
        name="Acme Ltd", tin="12345678-0001", address="Lagos", email="a@example.com",
        mssql_server="srv", mssql_database="db", mssql_password="do-not-leak",
        firs_secret_key="do-not-leak-either",
    )


def make_invoice(company, n, status, days_ago=0):
    return Invoice.objects.create(
        client=company,
        sage_invoice_id=f"T{n}",
        sage_invoice_number=f"INV-{n}",
        invoice_date=timezone.localdate() - timedelta(days=days_ago),
        supplier_tin=company.tin,
        supplier_name=company.name,
        buyer_name="Buyer",
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("7.50"),
        gross_amount=Decimal("107.50"),
        status=status,
    )


# ── the page ─────────────────────────────────────────────────

def test_anonymous_user_is_sent_to_the_admin_login(client):
    r = client.get(reverse("dashboard:index"))
    assert r.status_code == 302
    assert r["Location"].startswith(reverse("admin:login"))
    assert "next=" in r["Location"]


def test_signed_in_non_staff_user_cannot_open_the_page(client, member):
    client.force_login(member)
    r = client.get(reverse("dashboard:index"))
    assert r.status_code == 302
    assert r["Location"].startswith(reverse("admin:login"))


def test_staff_user_gets_the_page(client, staff):
    client.force_login(staff)
    r = client.get(reverse("dashboard:index"))
    html = r.content.decode()
    assert r.status_code == 200
    assert "dashboard/css/dashboard.css" in html
    assert "dashboard/js/dashboard.js" in html
    assert "staff" in html                      # the signed-in user's name
    assert re.search(r'data-csrf="[^"]{20,}"', html)
    assert "csrftoken" in r.cookies              # so the page's POST requests pass CSRF


def test_page_has_no_inline_scripts(client, staff):
    """The site's Content-Security-Policy is script-src 'self'; inline scripts never run."""
    client.force_login(staff)
    html = client.get(reverse("dashboard:index")).content.decode()
    tags = re.findall(r"<script\b[^>]*>", html)
    assert tags, "expected the page to load its scripts"
    assert all("src=" in t for t in tags), tags
    assert not re.search(r"\son(click|load|error)=", html)


def test_page_loads_nothing_from_other_domains(client, staff):
    client.force_login(staff)
    html = client.get(reverse("dashboard:index")).content.decode()
    assert not re.search(r'(src|href)="https?://', html)


def test_endpoint_map_resolves_every_url(client, staff):
    eps = _endpoints()
    assert eps["invoice"].count("{id}") == 1
    assert eps["retry"].endswith("/retry/") and eps["submit"].endswith("/submit/")
    assert all(v.startswith("/") for v in eps.values())
    client.force_login(staff)
    served = json.loads(
        re.search(r'data-endpoints="([^"]+)"', client.get(reverse("dashboard:index")).content.decode())
        .group(1).replace("&quot;", '"')
    )
    assert served == eps


# ── /dashboard/api/daily/ ────────────────────────────────────

def test_daily_endpoint_requires_a_signed_in_user(client):
    assert client.get(reverse("dashboard:daily")).status_code in (401, 403)


def test_daily_groups_statuses_into_chart_buckets(client, staff, company):
    n = iter(range(1, 100))
    for status, ago in [
        (InvoiceStatus.CLEARED, 0), (InvoiceStatus.WRITTEN_BACK, 0), (InvoiceStatus.PENDING, 0),
        (InvoiceStatus.CANCELLED, 0),                       # never counted
        (InvoiceStatus.SUBMITTING, 1), (InvoiceStatus.FAILED, 1),
        (InvoiceStatus.REJECTED, 2),
    ]:
        make_invoice(company, next(n), status, ago)
    client.force_login(staff)
    data = client.get(reverse("dashboard:daily")).json()
    by_date = {row["date"]: row for row in data["series"]}
    today = timezone.localdate()

    def day(ago):
        return by_date[(today - timedelta(days=ago)).isoformat()]

    assert (day(0)["cleared"], day(0)["in_progress"], day(0)["failed"]) == (2, 1, 0)
    assert (day(1)["cleared"], day(1)["in_progress"], day(1)["failed"]) == (0, 1, 1)
    assert (day(2)["cleared"], day(2)["in_progress"], day(2)["failed"]) == (0, 0, 1)


def test_daily_returns_one_row_per_day_ending_today(client, staff):
    client.force_login(staff)
    data = client.get(reverse("dashboard:daily")).json()
    assert data["days"] == 30 and len(data["series"]) == 30
    assert data["series"][-1]["date"] == timezone.localdate().isoformat()
    assert all(r["cleared"] == r["in_progress"] == r["failed"] == 0 for r in data["series"])


def test_daily_ignores_invoices_outside_the_window(client, staff, company):
    make_invoice(company, 1, InvoiceStatus.CLEARED, days_ago=45)
    client.force_login(staff)
    series = client.get(reverse("dashboard:daily")).json()["series"]
    assert sum(r["cleared"] for r in series) == 0


@pytest.mark.parametrize("raw,expected", [("1", 7), ("500", 90), ("abc", 30), ("14", 14)])
def test_daily_days_parameter_is_clamped(client, staff, raw, expected):
    client.force_login(staff)
    data = client.get(reverse("dashboard:daily"), {"days": raw}).json()
    assert data["days"] == expected and len(data["series"]) == expected


# ── /dashboard/api/clients/ ──────────────────────────────────

def test_clients_endpoint_requires_a_signed_in_user(client):
    assert client.get(reverse("dashboard:clients")).status_code in (401, 403)


def test_clients_lists_only_active_clients_and_no_credentials(client, staff, company):
    ClientCompany.objects.create(
        name="Dormant Ltd", tin="99999999-0001", address="x", email="d@example.com",
        mssql_server="s", mssql_database="d", is_active=False,
    )
    client.force_login(staff)
    r = client.get(reverse("dashboard:clients"))
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert {k: body[0][k] for k in ("id", "name", "tin")} == {"id": str(company.id), "name": "Acme Ltd", "tin": "12345678-0001"}
    assert set(body[0]["sage"]) == {"bookmark", "imported", "last_pull_at", "last_pull", "running"}
    assert b"do-not-leak" not in r.content


# ── seed_demo_data ───────────────────────────────────────────

@override_settings(DEBUG=True)
def test_seed_creates_labelled_inactive_sample_data():
    call_command("seed_demo_data", count=12, verbosity=0)
    demo = ClientCompany.objects.get(name=DEMO_NAME)
    assert demo.is_active is False                     # pollers and health checks skip it
    assert Invoice.objects.filter(client=demo).count() == 12
    assert BuyerTINMapping.objects.filter(client=demo).exists()
    assert ExcelUpload.objects.filter(client=demo).exists()
    for inv in Invoice.objects.filter(client=demo).prefetch_related("lines"):
        assert inv.gross_amount == inv.net_amount + inv.vat_amount
        assert inv.net_amount == sum(l.line_net_amount for l in inv.lines.all())
        if inv.status in (InvoiceStatus.CLEARED, InvoiceStatus.WRITTEN_BACK):
            assert inv.irn and inv.cleared_at


@override_settings(DEBUG=False)
def test_seed_refuses_to_run_with_debug_off():
    with pytest.raises(CommandError, match="DEBUG is off"):
        call_command("seed_demo_data", count=3, verbosity=0)
    assert not ClientCompany.objects.filter(name=DEMO_NAME).exists()


@override_settings(DEBUG=True)
def test_seed_will_not_run_twice():
    call_command("seed_demo_data", count=3, verbosity=0)
    with pytest.raises(CommandError, match="already exists"):
        call_command("seed_demo_data", count=3, verbosity=0)


@override_settings(DEBUG=True)
def test_clear_removes_only_sample_data(company):
    make_invoice(company, 1, InvoiceStatus.CLEARED)
    call_command("seed_demo_data", count=5, verbosity=0)
    call_command("seed_demo_data", clear=True, verbosity=0)
    assert not ClientCompany.objects.filter(name=DEMO_NAME).exists()
    assert not ExcelUpload.objects.exists()
    assert Invoice.objects.count() == 1                 # the real client's invoice survives
    assert ClientCompany.objects.filter(pk=company.pk).exists()


@override_settings(DEBUG=False)
def test_clear_works_even_with_debug_off():
    with override_settings(DEBUG=True):
        call_command("seed_demo_data", count=3, verbosity=0)
    call_command("seed_demo_data", clear=True, verbosity=0)
    assert not ClientCompany.objects.filter(name=DEMO_NAME).exists()
