"""
Eagle dashboard.

One HTML page (a small single-page app written in plain JavaScript) plus two
read-only JSON endpoints for things the existing API does not expose:

    GET /dashboard/               the page (staff users only)
    GET /dashboard/api/daily/     invoices per day, grouped for the chart
    GET /dashboard/api/clients/   active clients, for the upload and TIN forms

Everything else the page needs comes from the existing endpoints under
/api/invoices/ and /api/excel/. The page calls them with the signed-in
user's session, so no API token is needed.
"""
import json
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import ClientCompany
from apps.dashboard import sage_batch
from apps.dashboard.sage_batch import AlreadyRunning, PullError
from apps.invoices.models import Invoice, InvoiceStatus

_NIL_UUID = "00000000-0000-0000-0000-000000000000"

DEFAULT_DAYS = 30
MIN_DAYS = 7
MAX_DAYS = 90

# Which chart segment each invoice status counts towards.
# CANCELLED is left out on purpose: those invoices never go to FIRS.
_CHART_BUCKET = {
    InvoiceStatus.PENDING: "in_progress",
    InvoiceStatus.SUBMITTING: "in_progress",
    InvoiceStatus.CLEARED: "cleared",
    InvoiceStatus.WRITTEN_BACK: "cleared",
    InvoiceStatus.FAILED: "failed",
    InvoiceStatus.REJECTED: "failed",
}


def _endpoints() -> dict:
    """URLs the page calls. Built with reverse() so paths are never hard-coded."""

    def with_id(name: str) -> str:
        return reverse(name, args=[_NIL_UUID]).replace(_NIL_UUID, "{id}")

    return {
        "stats": reverse("stats"),
        "poll": reverse("poll-sage200"),
        "health": reverse("health"),
        "invoices": reverse("invoice-list"),
        "invoice": with_id("invoice-detail"),
        "submit": with_id("invoice-submit"),
        "retry": with_id("invoice-retry"),
        "validateTin": reverse("validate-tin"),
        "buyerTins": reverse("buyer-tins"),
        "excelHealth": reverse("excel-health"),
        "excelUploads": reverse("excel-upload-list"),
        "excelUploadDetail": with_id("excel-upload-detail"),
        "excelUpload": reverse("excel-upload"),
        "excelTemplate": reverse("excel-template"),
        "daily": reverse("dashboard:daily"),
        "clients": reverse("dashboard:clients"),
        "sagePreview": reverse("dashboard:sage-preview"),
        "sagePull": reverse("dashboard:sage-pull"),
    }


@method_decorator(staff_member_required, name="dispatch")
@method_decorator(ensure_csrf_cookie, name="dispatch")
class DashboardView(View):
    """The dashboard page. Anonymous and non-staff users go to the admin login."""

    def get(self, request):
        return render(
            request,
            "dashboard/index.html",
            {"endpoints_json": json.dumps(_endpoints())},
        )


class DailyStatsView(APIView):
    """
    Invoices per day, split into cleared / in progress / failed.

    Query params:
        days  how many days to return, ending today (7 to 90, default 30)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", DEFAULT_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        days = max(MIN_DAYS, min(MAX_DAYS, days))

        today = timezone.localdate()
        start = today - timedelta(days=days - 1)

        series, by_date = [], {}
        for offset in range(days):
            day = start + timedelta(days=offset)
            row = {"date": day.isoformat(), "cleared": 0, "in_progress": 0, "failed": 0}
            series.append(row)
            by_date[day] = row

        counts = (
            Invoice.objects.filter(invoice_date__range=(start, today))
            .order_by()
            .values("invoice_date", "status")
            .annotate(n=Count("id"))
        )
        for item in counts:
            bucket = _CHART_BUCKET.get(item["status"])
            row = by_date.get(item["invoice_date"])
            if bucket and row:
                row[bucket] += item["n"]

        return Response({"days": days, "series": series})


class ClientListView(APIView):
    """Active client companies. Id, name, TIN and Sage pull progress; never credentials."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        clients = ClientCompany.objects.filter(is_active=True).order_by("name")
        return Response(
            [
                {"id": str(c.id), "name": c.name, "tin": c.tin, "sage": sage_batch.describe(c)}
                for c in clients
            ]
        )


def _chosen_client(request):
    """The active client named in the request body, or a PullError the caller turns into a 400."""
    try:
        return ClientCompany.objects.get(id=request.data.get("client"), is_active=True)
    except (ClientCompany.DoesNotExist, ValidationError, ValueError, TypeError):
        raise PullError("Choose one of the active clients.")


class SagePreviewView(APIView):
    """
    Show what the next pull would take. Reads Sage 200, saves nothing, never contacts FIRS.

    POST {"client": "<id>", "count": 50}
    """

    permission_classes = [IsAdminUser]

    def post(self, request):
        try:
            company = _chosen_client(request)
            return Response(sage_batch.preview(company, request.data.get("count")))
        except PullError as exc:
            return Response({"error": str(exc)}, status=400)


class SagePullView(APIView):
    """
    Pull the next `count` invoices from Sage 200 and send them to FIRS. Runs in the
    background (Celery), so it needs Redis and a worker; the dashboard shows progress
    as invoices appear and the result when the pull finishes.

    POST {"client": "<id>", "count": 50}
    """

    permission_classes = [IsAdminUser]

    def post(self, request):
        try:
            company = _chosen_client(request)
            count = sage_batch.clamp_count(request.data.get("count"))
            if sage_batch.describe(company)["running"]:
                raise AlreadyRunning(f"A pull for {company.name} is already running. Wait for it to finish.")
        except AlreadyRunning as exc:
            return Response({"error": str(exc)}, status=409)
        except PullError as exc:
            return Response({"error": str(exc)}, status=400)

        from apps.dashboard.tasks import pull_sage200_batch
        try:
            pull_sage200_batch.delay(str(company.id), count)
        except Exception:
            return Response(
                {
                    "error": "The pull could not be queued because Redis or the Celery worker is not available. "
                             f"Start them, or run this in a terminal instead: "
                             f"python manage.py pull_sage200 --client \"{company.name}\" --limit {count}"
                },
                status=503,
            )
        return Response(
            {"message": f"Queued: pulling the next {count} invoice(s) for {company.name}.", "count": count},
            status=202,
        )
