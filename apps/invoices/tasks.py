"""
Celery Tasks — Async Invoice Processing
All background jobs run through here.

Tasks:
    poll_sage200_invoices       — Pull new invoices every 5 minutes
    submit_single_invoice       — Submit one invoice to FIRS
    retry_failed_submissions    — Retry all failed invoices
    retry_single_invoice        — Retry one specific invoice
    write_irn_to_sage           — Write IRN back to Sage 200
    refresh_access_token        — Refresh Sage 200 OAuth token
"""

import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("apps.invoices")


# ──────────────────────────────────────────────────────────────
# MAIN POLLING TASK — runs every 5 minutes via Celery Beat
# ──────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="apps.invoices.tasks.poll_sage200_invoices",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=240,
    time_limit=300,
)
def poll_sage200_invoices(self):
    """
    Pull all new invoices from every active client's Sage 200 database
    and push them through the FIRS submission pipeline.

    Runs every 5 minutes via CELERY_BEAT_SCHEDULE in settings.
    Each client is processed in its own sub-task for isolation.
    """
    from apps.core.models import ClientCompany

    active_clients = ClientCompany.objects.filter(is_active=True)

    if not active_clients.exists():
        logger.info("[Task] poll_sage200_invoices: No active clients configured")
        return {"status": "no_clients"}

    logger.info(f"[Task] Polling {active_clients.count()} active clients")

    results = {}
    for company in active_clients:
        try:
            # Each client gets its own isolated task
            process_client_invoices.delay(str(company.id))
            results[company.name] = "queued"
        except Exception as e:
            logger.error(f"[Task] Failed to queue {company.name}: {e}")
            results[company.name] = f"error: {e}"

    return results


# ──────────────────────────────────────────────────────────────
# PER-CLIENT PROCESSING TASK
# ──────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="apps.invoices.tasks.process_client_invoices",
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=180,
    time_limit=240,
)
def process_client_invoices(self, company_id: str):
    """
    Run the full invoice pipeline for one specific client.
    Pulls invoices since the last successful poll.
    """
    from apps.core.models import ClientCompany
    from apps.invoices.pipeline import InvoicePipeline
    from utils.exceptions import PipelineError

    try:
        company = ClientCompany.objects.get(id=company_id, is_active=True)
    except ClientCompany.DoesNotExist:
        logger.error(f"[Task] Client {company_id} not found or inactive")
        return {"error": "client_not_found"}

    logger.info(f"[Task] Processing invoices for: {company.name}")

    try:
        pipeline = InvoicePipeline(company)
        results  = pipeline.run()
        logger.info(f"[Task] {company.name} pipeline results: {results}")
        return {"client": company.name, "results": results}

    except PipelineError as e:
        logger.error(f"[Task] Pipeline error for {company.name}: {e}")
        # Retry after 2 minutes
        raise self.retry(exc=e, countdown=120)

    except Exception as e:
        logger.exception(f"[Task] Unexpected error for {company.name}: {e}")
        raise self.retry(exc=e, countdown=300)


# ──────────────────────────────────────────────────────────────
# SUBMIT ONE INVOICE (manual trigger or direct call)
# ──────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="apps.invoices.tasks.submit_single_invoice",
    max_retries=3,
    default_retry_delay=60,
)
def submit_single_invoice(self, invoice_id: str):
    """
    Submit a single invoice to FIRS by its middleware UUID.
    Called when a user manually triggers submission via the API.
    """
    from apps.invoices.models import Invoice, InvoiceStatus
    from apps.invoices.pipeline import InvoicePipeline

    try:
        invoice = Invoice.objects.select_related("client").get(id=invoice_id)
    except Invoice.DoesNotExist:
        logger.error(f"[Task] Invoice {invoice_id} not found")
        return {"error": "invoice_not_found"}

    if invoice.status == InvoiceStatus.CLEARED:
        logger.info(f"[Task] Invoice {invoice.sage_invoice_number} already cleared — skipping")
        return {"status": "already_cleared", "irn": invoice.irn}

    logger.info(f"[Task] Manually submitting invoice {invoice.sage_invoice_number}")

    try:
        # Re-pull the raw data from Sage 200
        pipeline = InvoicePipeline(invoice.client)
        raw = pipeline.odbc.get_invoice_by_id(invoice.sage_invoice_id)

        if not raw:
            logger.error(f"[Task] Invoice {invoice.sage_invoice_number} not found in Sage 200")
            return {"error": "not_found_in_sage"}

        outcome = pipeline._process_single(raw)
        return {"outcome": outcome, "invoice": invoice.sage_invoice_number}

    except Exception as e:
        logger.exception(f"[Task] Error submitting {invoice.sage_invoice_number}: {e}")
        raise self.retry(exc=e)


# ──────────────────────────────────────────────────────────────
# RETRY FAILED INVOICES — runs every 15 minutes via Celery Beat
# ──────────────────────────────────────────────────────────────

@shared_task(
    name="apps.invoices.tasks.retry_failed_submissions",
    soft_time_limit=300,
    time_limit=360,
)
def retry_failed_submissions():
    """
    Find all failed invoices that are due for retry and re-queue them.
    Respects exponential back-off via the next_retry_at field.
    """
    from apps.invoices.models import Invoice, InvoiceStatus

    now = timezone.now()
    due_for_retry = Invoice.objects.filter(
        status=InvoiceStatus.FAILED,
        submission_attempts__lt=5,
        next_retry_at__lte=now,
    ).select_related("client")

    count = due_for_retry.count()
    if count == 0:
        logger.debug("[Task] retry_failed_submissions: No invoices due for retry")
        return {"retried": 0}

    logger.info(f"[Task] Retrying {count} failed invoices")

    queued = 0
    for invoice in due_for_retry:
        try:
            retry_single_invoice.delay(str(invoice.id))
            queued += 1
        except Exception as e:
            logger.error(
                f"[Task] Could not queue retry for {invoice.sage_invoice_number}: {e}"
            )

    return {"retried": queued}


# ──────────────────────────────────────────────────────────────
# RETRY ONE INVOICE
# ──────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="apps.invoices.tasks.retry_single_invoice",
    max_retries=5,
)
def retry_single_invoice(self, invoice_id: str):
    """
    Retry submission for a single failed invoice.
    Called by retry_failed_submissions or after a rejection.
    """
    from apps.invoices.models import Invoice, InvoiceStatus

    try:
        invoice = Invoice.objects.select_related("client").get(id=invoice_id)
    except Invoice.DoesNotExist:
        return {"error": "not_found"}

    if not invoice.is_retryable:
        logger.warning(
            f"[Task] Invoice {invoice.sage_invoice_number} is not retryable "
            f"(status={invoice.status}, attempts={invoice.submission_attempts})"
        )
        return {"status": "not_retryable"}

    logger.info(
        f"[Task] Retrying invoice {invoice.sage_invoice_number} "
        f"(attempt {invoice.submission_attempts + 1})"
    )
    submit_single_invoice.delay(invoice_id)
    return {"status": "queued_for_retry"}


# ──────────────────────────────────────────────────────────────
# WRITE IRN BACK TO SAGE 200 (called after clearance)
# ──────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name="apps.invoices.tasks.write_irn_to_sage",
    max_retries=5,
    default_retry_delay=30,
)
def write_irn_to_sage(self, invoice_id: str):
    """
    Write the FIRS-issued IRN and CSID back to the Sage 200
    invoice record using the official Sage 200 API.
    """
    from apps.invoices.models import Invoice, InvoiceStatus
    from apps.sage200.irn_writer import Sage200IRNWriter

    try:
        invoice = Invoice.objects.select_related("client").get(id=invoice_id)
    except Invoice.DoesNotExist:
        return {"error": "not_found"}

    if not invoice.irn:
        logger.error(f"[Task] No IRN to write back for {invoice.sage_invoice_number}")
        return {"error": "no_irn"}

    try:
        writer = Sage200IRNWriter(invoice.client)
        writer.write_irn(
            sage_invoice_id=invoice.sage_invoice_id,
            sage_invoice_number=invoice.sage_invoice_number,
            irn=invoice.irn,
            csid=invoice.csid,
        )
        Invoice.objects.filter(id=invoice.id).update(
            status=InvoiceStatus.WRITTEN_BACK,
            written_back_at=timezone.now(),
        )
        logger.info(
            f"[Task] IRN written back to Sage 200: {invoice.sage_invoice_number}"
        )
        return {"status": "success", "irn": invoice.irn}

    except Exception as e:
        logger.error(
            f"[Task] IRN write-back failed for {invoice.sage_invoice_number}: {e}"
        )
        raise self.retry(exc=e)


# ──────────────────────────────────────────────────────────────
# TOKEN REFRESH — runs every 7 hours via Celery Beat
# ──────────────────────────────────────────────────────────────

@shared_task(name="apps.sage200.tasks.refresh_access_token")
def refresh_access_token():
    """
    Refresh the Sage 200 OAuth access token for all active clients
    before the 8-hour expiry window closes.
    """
    from apps.core.models import ClientCompany
    from apps.sage200.api_client import Sage200APIClient

    active = ClientCompany.objects.filter(is_active=True, sage_client_id__gt="")
    refreshed = 0

    for company in active:
        try:
            api = Sage200APIClient(company)
            api.refresh_token()
            refreshed += 1
            logger.info(f"[Task] Sage 200 token refreshed for {company.name}")
        except Exception as e:
            logger.error(f"[Task] Token refresh failed for {company.name}: {e}")

    return {"refreshed": refreshed}
