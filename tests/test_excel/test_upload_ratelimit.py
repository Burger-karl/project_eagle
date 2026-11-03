"""Regression tests: the web upload endpoint must not crash on its rate-limit check.

The view used to pass key="ip:<address>" to django-ratelimit, which only accepts
get/post/header prefixes. Every upload therefore raised ImproperlyConfigured and
returned HTTP 500 before the file was even looked at.
"""
import io
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from openpyxl import Workbook

from apps.core.models import ClientCompany
from apps.excel_intake.models import ExcelUpload

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_rate_limit_counters():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _no_broker_and_temp_files(settings, tmp_path):
    settings.EXCEL_UPLOAD_DIR = str(tmp_path)
    with patch("apps.excel_intake.views.process_excel_upload") as task:
        yield task


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user("uploader", password="pw")


@pytest.fixture
def company():
    return ClientCompany.objects.create(
        name="Upload Co", tin="55555555-0001", address="Lagos", email="u@example.com",
        mssql_server="s", mssql_database="d",
    )


def _workbook(marker: str) -> io.BytesIO:
    """A tiny but genuine .xlsx; `marker` makes each file's hash different."""
    wb = Workbook()
    wb.active.append(["Invoice Number", "Invoice Date", marker])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = f"{marker}.xlsx"
    return buf


def _post(client, company, marker="one"):
    return client.post(reverse("excel-upload"), {"file": _workbook(marker), "client_id": str(company.id)})


def test_upload_is_accepted_and_queued(client, user, company, _no_broker_and_temp_files):
    client.force_login(user)
    r = _post(client, company)
    assert r.status_code < 300, r.content[:300]
    assert r.json()["upload_id"]
    assert ExcelUpload.objects.filter(client=company).count() == 1
    _no_broker_and_temp_files.delay.assert_called_once()


def test_upload_rate_limit_is_per_ip_and_returns_429(client, user, company):
    client.force_login(user)
    codes = [_post(client, company, marker=f"batch{i}").status_code for i in range(22)]
    assert 500 not in codes
    assert all(c < 300 for c in codes[:20]), codes
    assert codes[20:] == [429, 429]
