"""
Tests for the Access Point Provider layer (apps/firs/providers/).
No network: all HTTP is mocked with `responses`.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import responses

from apps.firs.providers import (
    ProviderConfigError, ProviderStatus, SubmissionResult,
    available_providers, get_provider, resolve_provider_name,
)
from apps.firs.providers.digitax import DigiTaxProvider
from apps.firs.providers.interswitch import InterswitchProvider

DIGITAX = "https://api.digitax.tech/ng/v1"


def mapped_invoice(**overrides):
    m = {
        "invoice_number": "INV-0042",
        "invoice_date":   "2025-11-15",
        "due_date":       None,
        "document_type":  "380",
        "invoice_type":   "B2C",
        "currency_code":  "NGN",
        "tax_currency_code": "NGN",
        "customization_id": "urn:cen.eu:en16931:2017",
        "profile_id":     "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
        "supplier_city": "Lagos", "supplier_country": "NG", "supplier_postal": "",
        "buyer_city": "Lagos", "buyer_country": "NG", "buyer_postal": "",
        "payable_amount": Decimal("1075.00"),
        "supplier_tin":   "04702493-0001",
        "supplier_name":  "Link Options Systems Limited",
        "supplier_address": "Lagos",
        "buyer_tin":      "",
        "buyer_name":     "Algorithm",
        "buyer_address":  "Lagos",
        "net_amount":     Decimal("1000.00"),
        "vat_amount":     Decimal("75.00"),
        "gross_amount":   Decimal("1075.00"),
        "payment_means_code": "30",
        "lines": [{
            "line_number": "1", "description": "Software renewal",
            "quantity": 1.0, "unit_code": "EA", "unit_price": 1000.0,
            "line_net_amount": 1000.0, "vat_rate": 7.5, "vat_amount": 75.0,
            "tax_category": "S",
        }],
    }
    m.update(overrides)
    return m


def company(**kw):
    defaults = dict(app_provider="", firs_api_key="co-key", firs_secret_key="co-secret", id=1)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── Registry ─────────────────────────────────────────────────

class TestRegistry:

    def test_both_providers_registered(self):
        assert {"digitax", "interswitch"} <= set(available_providers())

    def test_company_override_beats_global(self, settings):
        settings.APP_PROVIDER = "digitax"
        assert resolve_provider_name(company(app_provider="interswitch")) == "interswitch"
        assert resolve_provider_name(company()) == "digitax"

    def test_unknown_provider_raises(self):
        with pytest.raises(ProviderConfigError):
            get_provider(company(), name="nope")

    def test_company_key_used_before_settings(self, settings):
        settings.FIRS_API_KEY = "global-key"
        p = get_provider(company(firs_api_key="co-key"), name="digitax")
        assert p.auth_headers() == {"X-API-Key": "co-key"}

    def test_digitax_without_key_raises(self, settings):
        settings.FIRS_API_KEY = ""
        settings.DIGITAX_API_KEY = ""
        with pytest.raises(ProviderConfigError):
            get_provider(company(firs_api_key=""), name="digitax")


# ── DigiTax payload ──────────────────────────────────────────

class TestDigiTaxPayload:

    def setup_method(self):
        self.p = DigiTaxProvider(credentials={"api_key": "k"}, base_url=DIGITAX)

    def test_dates_are_today_and_original_kept_in_notes(self):
        payload = self.p.build_payload(mapped_invoice())
        today = date.today().isoformat()
        assert payload["invoice_date"] == today
        assert payload["issue_date"] == today
        assert "2025-11-15" in payload["notes"]

    def test_tax_rate_is_decimal(self):
        item = self.p.build_payload(mapped_invoice())["items"][0]
        assert item["tax_rate"] == 0.075
        assert item["tax_category_code"] == "STANDARD_VAT"

    def test_zero_rated_line_stays_zero(self):
        line = dict(mapped_invoice()["lines"][0], vat_rate=0.0, tax_category="Z")
        item = self.p.build_payload(mapped_invoice(lines=[line]))["items"][0]
        assert item["tax_rate"] == 0.0
        assert item["tax_category_code"] == "ZERO_RATED_VAT"

    def test_item_id_per_tax_category(self, settings):
        settings.DIGITAX_ITEM_ID = "item_default"
        settings.DIGITAX_ITEM_IDS = {"ZERO_RATED_VAT": "item_zero"}
        line_z = dict(mapped_invoice()["lines"][0], vat_rate=0.0, tax_category="Z")
        items = self.p.build_payload(mapped_invoice(lines=[mapped_invoice()["lines"][0], line_z]))["items"]
        assert [i["item_id"] for i in items] == ["item_default", "item_zero"]

    def test_b2b_party_fields(self):
        payload = self.p.build_payload(mapped_invoice(invoice_type="B2B", buyer_tin="12345678-0001"))
        assert payload["invoice_kind"] == "B2B"
        assert payload["party_tin"] == "12345678-0001"


# ── DigiTax HTTP ─────────────────────────────────────────────

class TestDigiTaxSubmit:

    def setup_method(self):
        self.p = DigiTaxProvider(credentials={"api_key": "k"}, base_url=DIGITAX)
        self.p.retry_backoff = (0, 0, 0)

    @responses.activate
    def test_success_normalised(self):
        responses.post(f"{DIGITAX}/invoices", status=201, json={"data": {
            "id": "inv_123", "status": "COMPLETED",
            "invoice_reference_number": "39G1M7NE3-E3A89069-20261103",
            "qr_code_data": "QRDATA",
        }})
        r = self.p.submit(mapped_invoice())
        assert r.success
        assert r.irn == "39G1M7NE3-E3A89069-20261103"
        assert r.provider_invoice_id == "inv_123"
        assert r.provider_status == ProviderStatus.SIGNED
        assert responses.calls[0].request.headers["X-API-Key"] == "k"

        as_dict = r.as_pipeline_result()
        assert as_dict["data"]["irn"] == r.irn
        assert as_dict["payload"]["trader_invoice_number"] == "INV-0042"

    @responses.activate
    def test_validation_error_returned_not_raised(self):
        responses.post(f"{DIGITAX}/invoices", status=422,
                       json={"message": "tax_rate does not match item tax category"})
        r = self.p.submit(mapped_invoice())
        assert not r.success
        assert "tax_rate" in r.error
        assert r.as_pipeline_result()["data"] is None

    @responses.activate
    def test_rate_limit_retried(self):
        responses.post(f"{DIGITAX}/invoices", status=429, headers={"Retry-After": "0"})
        responses.post(f"{DIGITAX}/invoices", status=201,
                       json={"data": {"id": "x", "status": "DRAFT", "invoice_reference_number": "IRN1"}})
        r = self.p.submit(mapped_invoice())
        assert r.success and r.provider_status == ProviderStatus.CREATED
        assert len(responses.calls) == 2

    @responses.activate
    def test_get_status_transmitted(self):
        responses.get(f"{DIGITAX}/invoices/inv_123", json={"data": {
            "id": "inv_123", "status": "TRANSMITTED", "invoice_reference_number": "IRN1"}})
        assert self.p.get_status("inv_123").provider_status == ProviderStatus.TRANSMITTED

    def test_unknown_status(self):
        assert self.p.normalise_status("WEIRD") == ProviderStatus.UNKNOWN


# ── Interswitch ──────────────────────────────────────────────

ISW_BASE = "https://isw.test/einvoice/api/v1"
ISW_TOKEN = "https://isw.test/passport/oauth/token"


@pytest.fixture
def isw_settings(settings):
    settings.INTERSWITCH_BASE_URL = ISW_BASE
    settings.INTERSWITCH_TOKEN_URL = ISW_TOKEN
    settings.INTERSWITCH_SUBMIT_PATH_B2C = "/invoices/b2c"
    settings.INTERSWITCH_STATUS_PATH = "/invoices/{id}"
    from django.core.cache import cache
    cache.clear()
    return settings


class TestInterswitch:

    def test_missing_credentials(self, isw_settings):
        isw_settings.INTERSWITCH_CLIENT_ID = ""
        isw_settings.INTERSWITCH_CLIENT_SECRET = ""
        with pytest.raises(ProviderConfigError):
            InterswitchProvider.from_settings(company(firs_api_key="", firs_secret_key=""))

    def test_missing_base_url(self, settings):
        settings.INTERSWITCH_BASE_URL = ""
        with pytest.raises(ProviderConfigError):
            InterswitchProvider.from_settings(company())

    @responses.activate
    def test_token_cached_and_submit(self, isw_settings):
        responses.post(ISW_TOKEN, json={"access_token": "tok", "expires_in": 3600})
        responses.post(f"{ISW_BASE}/invoices/b2c", status=201,
                       json={"data": {"irn": "IRN-ISW-1", "status": "SIGNED", "id": "isw-1"}})
        p = InterswitchProvider.from_settings(company())
        p.retry_backoff = (0, 0, 0)
        r1 = p.submit(mapped_invoice())
        r2 = p.submit(mapped_invoice())
        assert r1.success and r1.irn == "IRN-ISW-1" and r1.provider_status == ProviderStatus.SIGNED
        assert r2.success
        token_calls = [c for c in responses.calls if c.request.url == ISW_TOKEN]
        assert len(token_calls) == 1
        assert responses.calls[1].request.headers["Authorization"] == "Bearer tok"

    @responses.activate
    def test_token_failure_is_a_failed_result(self, isw_settings):
        responses.post(ISW_TOKEN, status=401, json={"error": "invalid_client"})
        p = InterswitchProvider.from_settings(company())
        r = p.submit(mapped_invoice())
        assert not r.success and "token" in r.error.lower()

    def test_bad_mapped_data_is_a_failed_result(self, isw_settings):
        p = InterswitchProvider.from_settings(company())
        r = p.submit({"invoice_type": "B2C"})
        assert not r.success and "Payload build failed" in r.error

    def test_tin_validation_unsupported_until_configured(self, isw_settings):
        isw_settings.INTERSWITCH_TIN_PATH = ""
        p = InterswitchProvider.from_settings(company())
        assert p.validate_tin("123") == {"success": True, "supported": False, "tin": "123"}


# ── Pipeline integration (Sage 200) ──────────────────────────

@pytest.mark.django_db
class TestSage200PipelineUsesProvider:

    def _company(self):
        from apps.core.models import ClientCompany
        return ClientCompany.objects.create(
            name="Link Options Systems Limited", tin="04702493-0001", address="Lagos",
            email="a@b.com", mssql_server="x", mssql_database="y", firs_api_key="k",
        )

    def _raw(self):
        return {"invoice_id": "999", "invoice_number": "INV-0042", "account_ref": "CUS1"}

    def test_cleared_invoice_stores_provider_fields(self):
        from apps.invoices.models import Invoice, InvoiceStatus
        from apps.invoices.pipeline import InvoicePipeline

        co = self._company()
        ok = SubmissionResult(success=True, provider="digitax", payload={"p": 1},
                              irn="IRN-1", provider_invoice_id="inv_1",
                              provider_status=ProviderStatus.SIGNED, raw={"id": "inv_1"})
        with patch("apps.invoices.pipeline.Sage200FieldMapper") as Mapper, \
             patch.object(DigiTaxProvider, "submit", return_value=ok):
            Mapper.return_value.map.return_value = mapped_invoice()
            outcome = InvoicePipeline(co)._process_single(self._raw())

        inv = Invoice.objects.get(sage_invoice_id="999")
        assert outcome == "cleared"
        assert inv.irn == "IRN-1"
        assert inv.app_provider == "digitax"
        assert inv.provider_invoice_id == "inv_1"
        assert inv.provider_status == ProviderStatus.SIGNED
        assert inv.ubl_payload == {"p": 1}
        assert inv.status in (InvoiceStatus.CLEARED, InvoiceStatus.WRITTEN_BACK)

    def test_retry_resubmits_existing_instead_of_skipping(self):
        from apps.invoices.models import Invoice
        from apps.invoices.pipeline import InvoicePipeline

        co = self._company()
        bad = SubmissionResult(success=False, provider="digitax", error="boom")
        ok = SubmissionResult(success=True, provider="digitax", irn="IRN-2",
                              provider_invoice_id="inv_2", provider_status=ProviderStatus.SIGNED)
        with patch("apps.invoices.pipeline.Sage200FieldMapper") as Mapper, \
             patch("apps.invoices.tasks.retry_single_invoice.apply_async"):
            Mapper.return_value.map.return_value = mapped_invoice()
            pipe = InvoicePipeline(co)
            with patch.object(DigiTaxProvider, "submit", return_value=bad):
                assert pipe._process_single(self._raw()) == "failed"
            inv = Invoice.objects.get(sage_invoice_id="999")

            # Without `existing` it is skipped (normal polling)...
            assert pipe._process_single(self._raw()) == "skipped"
            # ...with `existing` it is re-submitted (manual submit / retry)
            with patch.object(DigiTaxProvider, "submit", return_value=ok):
                assert pipe._process_single(self._raw(), existing=inv) == "cleared"

        inv.refresh_from_db()
        assert inv.irn == "IRN-2"
        assert inv.submission_attempts == 2

    def test_success_without_irn_is_not_cleared(self):
        from apps.invoices.models import Invoice, InvoiceStatus
        from apps.invoices.pipeline import InvoicePipeline

        co = self._company()
        no_irn = SubmissionResult(success=True, provider="digitax", irn="")
        with patch("apps.invoices.pipeline.Sage200FieldMapper") as Mapper, \
             patch.object(DigiTaxProvider, "submit", return_value=no_irn), \
             patch("apps.invoices.tasks.retry_single_invoice.apply_async"):
            Mapper.return_value.map.return_value = mapped_invoice()
            assert InvoicePipeline(co)._process_single(self._raw()) == "failed"
        assert Invoice.objects.get(sage_invoice_id="999").status == InvoiceStatus.FAILED


# ── Status sync task ─────────────────────────────────────────

@pytest.mark.django_db
def test_sync_provider_statuses_moves_signed_to_transmitted():
    from apps.core.models import ClientCompany
    from apps.invoices.models import Invoice
    from apps.invoices.tasks import sync_provider_statuses

    co = ClientCompany.objects.create(
        name="Co", tin="1-0001", address="Lagos", email="a@b.com",
        mssql_server="x", mssql_database="y", firs_api_key="k",
    )
    inv = Invoice.objects.create(
        client=co, sage_invoice_id="1", sage_invoice_number="INV1", sage_account_ref="C",
        invoice_date=date.today(), supplier_tin="1-0001", supplier_name="Co",
        supplier_address="Lagos", buyer_name="B", buyer_address="Lagos",
        net_amount=100, vat_amount=7.5, gross_amount=107.5,
        irn="IRN1", app_provider="digitax", provider_invoice_id="inv_1",
        provider_status=ProviderStatus.SIGNED,
    )
    transmitted = SubmissionResult(success=True, provider="digitax",
                                   provider_status=ProviderStatus.TRANSMITTED)
    with patch.object(DigiTaxProvider, "get_status", return_value=transmitted) as gs:
        assert sync_provider_statuses() == {"updated": 1, "errors": 0}
        gs.assert_called_once_with("inv_1")
    inv.refresh_from_db()
    assert inv.provider_status == ProviderStatus.TRANSMITTED

    # Terminal status: not polled again
    with patch.object(DigiTaxProvider, "get_status") as gs:
        assert sync_provider_statuses() == {"updated": 0, "errors": 0}
        gs.assert_not_called()
