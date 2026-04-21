"""
Invoice API Views
REST endpoints for the Project Eagle middleware.

Endpoints:
    GET  /api/invoices/              — List all invoices with filters
    GET  /api/invoices/{id}/         — Get one invoice detail
    POST /api/invoices/{id}/submit/  — Manually trigger FIRS submission
    POST /api/invoices/{id}/retry/   — Retry a failed invoice
    GET  /api/invoices/health/       — System health check
    GET  /api/invoices/stats/        — Dashboard statistics
    POST /api/invoices/poll/         — Manually trigger Sage 200 poll
    GET  /api/invoices/tin-validate/ — Validate a buyer TIN
    GET  /api/invoices/buyer-tins/   — List TIN mappings
    POST /api/invoices/buyer-tins/   — Add a buyer TIN mapping
"""

import logging
from django.conf import settings
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from apps.invoices.models import Invoice, InvoiceStatus, BuyerTINMapping
from apps.invoices.serializers import (
    InvoiceListSerializer,
    InvoiceDetailSerializer,
    BuyerTINMappingSerializer,
)
from apps.invoices.tasks import (
    submit_single_invoice,
    retry_single_invoice,
    poll_sage200_invoices,
)
from apps.firs.client import FIRSClient
from apps.sage200.odbc_client import Sage200ODBCClient
from apps.core.models import ClientCompany

logger = logging.getLogger("apps.invoices")


# ──────────────────────────────────────────────────────────────
# HEALTH CHECK
# ──────────────────────────────────────────────────────────────

class HealthCheckView(APIView):
    """
    System health check.
    Tests: Sage 200 ODBC connection, FIRS API credentials, Redis/Celery.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        health = {
            "middleware":  "ok",
            "sage200_odbc": "unknown",
            "firs_api":    "unknown",
            "redis":       "unknown",
            "environment": "PRODUCTION" if settings.USE_FIRS_PRODUCTION else "SANDBOX",
            "firs_url":    settings.FIRS_BASE_URL,
        }

        # Test Sage 200 ODBC
        try:
            company = ClientCompany.objects.filter(is_active=True).first()
            if company:
                odbc = Sage200ODBCClient(company)
                health["sage200_odbc"] = "ok" if odbc.test_connection() else "failed"
                health["sage200_db"]   = company.mssql_database
            else:
                health["sage200_odbc"] = "no_clients_configured"
        except Exception as e:
            health["sage200_odbc"] = f"error: {str(e)[:100]}"

        # Test FIRS API
        try:
            firs = FIRSClient()
            result = firs.health_check()
            health["firs_api"] = "ok" if result["success"] else f"failed: {result.get('error', '')}"
        except Exception as e:
            health["firs_api"] = f"error: {str(e)[:100]}"

        # Test Redis
        try:
            from django_redis import get_redis_connection
            conn = get_redis_connection("default")
            conn.ping()
            health["redis"] = "ok"
        except Exception:
            health["redis"] = "unavailable"

        overall = all(v == "ok" for k, v in health.items() if k not in ("environment", "firs_url", "sage200_db"))
        status_code = 200 if overall else 503

        return Response(health, status=status_code)


# ──────────────────────────────────────────────────────────────
# INVOICE LIST & DETAIL
# ──────────────────────────────────────────────────────────────

class InvoiceListView(generics.ListAPIView):
    """
    List all invoices with filtering, search, and ordering.

    Query params:
        status      — filter by status (PENDING, CLEARED, FAILED, etc.)
        invoice_type — B2B, B2C, B2G
        date_from   — filter by invoice_date >= date
        date_to     — filter by invoice_date <= date
        search      — search invoice number, buyer name, IRN
        ordering    — order by field (e.g. -invoice_date)
    """
    serializer_class   = InvoiceListSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ["status", "invoice_type", "document_type", "client"]
    search_fields      = ["sage_invoice_number", "buyer_name", "irn", "buyer_tin"]
    ordering_fields    = ["invoice_date", "created_at", "gross_amount"]
    ordering           = ["-invoice_date"]

    def get_queryset(self):
        qs = Invoice.objects.select_related("client").prefetch_related("lines")
        date_from = self.request.query_params.get("date_from")
        date_to   = self.request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(invoice_date__gte=date_from)
        if date_to:
            qs = qs.filter(invoice_date__lte=date_to)
        return qs


class InvoiceDetailView(generics.RetrieveAPIView):
    """Full detail view of one invoice including lines and submission logs."""
    serializer_class   = InvoiceDetailSerializer
    permission_classes = [IsAuthenticated]
    queryset           = Invoice.objects.select_related("client").prefetch_related(
        "lines", "submission_logs"
    )


# ──────────────────────────────────────────────────────────────
# MANUAL SUBMISSION TRIGGERS
# ──────────────────────────────────────────────────────────────

class SubmitInvoiceView(APIView):
    """
    Manually submit a specific invoice to FIRS.
    Queues a Celery task — returns immediately.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            invoice = Invoice.objects.get(id=pk)
        except Invoice.DoesNotExist:
            return Response({"error": "Invoice not found"}, status=404)

        if invoice.status == InvoiceStatus.CLEARED:
            return Response({
                "message": "Invoice already cleared",
                "irn": invoice.irn
            })

        task = submit_single_invoice.delay(str(invoice.id))
        logger.info(
            f"[API] Manual submission triggered: {invoice.sage_invoice_number} → task {task.id}"
        )
        return Response({
            "message": f"Submission queued for invoice {invoice.sage_invoice_number}",
            "task_id": task.id,
            "invoice_id": str(invoice.id),
        }, status=202)


class RetryInvoiceView(APIView):
    """Retry a failed invoice submission."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            invoice = Invoice.objects.get(id=pk)
        except Invoice.DoesNotExist:
            return Response({"error": "Invoice not found"}, status=404)

        if not invoice.is_retryable:
            return Response({
                "error": f"Invoice cannot be retried. "
                         f"Status: {invoice.status}, Attempts: {invoice.submission_attempts}"
            }, status=400)

        task = retry_single_invoice.delay(str(invoice.id))
        return Response({
            "message": f"Retry queued for {invoice.sage_invoice_number}",
            "task_id": task.id,
            "attempt": invoice.submission_attempts + 1,
        }, status=202)


class PollSage200View(APIView):
    """
    Manually trigger a Sage 200 invoice poll for all active clients.
    Normally this runs automatically every 5 minutes via Celery Beat.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        task = poll_sage200_invoices.delay()
        logger.info(f"[API] Manual Sage 200 poll triggered → task {task.id}")
        return Response({
            "message": "Sage 200 poll queued",
            "task_id": task.id,
        }, status=202)


# ──────────────────────────────────────────────────────────────
# TIN VALIDATION
# ──────────────────────────────────────────────────────────────

class ValidateTINView(APIView):
    """Validate a buyer TIN against the FIRS database."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tin = request.query_params.get("tin")
        if not tin:
            return Response({"error": "tin query parameter is required"}, status=400)

        firs = FIRSClient()
        result = firs.validate_tin(tin)
        return Response(result)


# ──────────────────────────────────────────────────────────────
# BUYER TIN MAPPINGS
# ──────────────────────────────────────────────────────────────

class BuyerTINMappingListCreateView(generics.ListCreateAPIView):
    """
    List all buyer TIN mappings or add a new one.

    POST body:
    {
        "client": "company-uuid",
        "sage_account_ref": "CUS001",
        "buyer_name": "Buyer Company Ltd",
        "buyer_tin": "12345678-0001"
    }
    """
    serializer_class   = BuyerTINMappingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return BuyerTINMapping.objects.select_related("client").order_by("buyer_name")

    def perform_create(self, serializer):
        mapping = serializer.save()
        # Validate the TIN against FIRS immediately
        firs = FIRSClient()
        result = firs.validate_tin(mapping.buyer_tin)
        if result["success"]:
            from django.utils import timezone
            mapping.is_validated = True
            mapping.validated_at = timezone.now()
            mapping.save(update_fields=["is_validated", "validated_at"])


# ──────────────────────────────────────────────────────────────
# DASHBOARD STATISTICS
# ──────────────────────────────────────────────────────────────

class StatsView(APIView):
    """
    Dashboard statistics — invoice counts by status.
    Used by the reporting dashboard.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Sum
        from datetime import date, timedelta

        today = date.today()
        month_start = today.replace(day=1)

        stats = {
            "total":        Invoice.objects.count(),
            "by_status":    dict(
                Invoice.objects.values_list("status")
                               .annotate(count=Count("id"))
                               .values_list("status", "count")
            ),
            "this_month": {
                "count":       Invoice.objects.filter(invoice_date__gte=month_start).count(),
                "total_value": Invoice.objects.filter(
                    invoice_date__gte=month_start
                ).aggregate(total=Sum("gross_amount"))["total"] or 0,
            },
            "pending_retry": Invoice.objects.filter(
                status=InvoiceStatus.FAILED,
                submission_attempts__lt=5,
            ).count(),
            "cleared_today": Invoice.objects.filter(
                status=InvoiceStatus.CLEARED,
                cleared_at__date=today,
            ).count(),
        }

        return Response(stats)
