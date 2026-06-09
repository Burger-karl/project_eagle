"""
Sage 300 Celery Tasks
Background processing tasks for Sage 300 invoice pipeline.
Mirrors the Sage 200 tasks pattern exactly.
"""

import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("apps.invoices")


@shared_task(
    bind=True,
    name="apps.sage300.tasks.poll_sage300_invoices",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=240,
    time_limit=300,
)
def poll_sage300_invoices(self):
    """
    Pull all new invoices from every active Sage 300 client
    and push through the FIRS submission pipeline.
    Runs every 5 minutes via CELERY_BEAT_SCHEDULE.
    """
    from apps.core.models import ClientCompany

    # Only process clients that have a Sage 300 database configured
    active_clients = ClientCompany.objects.filter(
        is_active=True,
        sage300_mssql_database__gt="",
    )

    if not active_clients.exists():
        logger.info("[Sage300 Task] No active Sage 300 clients configured")
        return {"status": "no_sage300_clients"}

    logger.info(f"[Sage300 Task] Polling {active_clients.count()} Sage 300 clients")

    results = {}
    for company in active_clients:
        try:
            process_sage300_client.delay(str(company.id))
            results[company.name] = "queued"
        except Exception as e:
            logger.error(f"[Sage300 Task] Failed to queue {company.name}: {e}")
            results[company.name] = f"error: {e}"

    return results


@shared_task(
    bind=True,
    name="apps.sage300.tasks.process_sage300_client",
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=180,
    time_limit=240,
)
def process_sage300_client(self, company_id: str):
    """Run the full Sage 300 invoice pipeline for one client."""
    from apps.core.models import ClientCompany
    from apps.sage300.pipeline import Sage300Pipeline
    from utils.exceptions import PipelineError

    try:
        company = ClientCompany.objects.get(id=company_id, is_active=True)
    except ClientCompany.DoesNotExist:
        logger.error(f"[Sage300 Task] Client {company_id} not found")
        return {"error": "client_not_found"}

    logger.info(f"[Sage300 Task] Processing: {company.name}")

    try:
        pipeline = Sage300Pipeline(company)
        results  = pipeline.run()
        logger.info(f"[Sage300 Task] {company.name}: {results}")
        return {"client": company.name, "results": results}

    except PipelineError as e:
        logger.error(f"[Sage300 Task] Pipeline error for {company.name}: {e}")
        raise self.retry(exc=e, countdown=120)

    except Exception as e:
        logger.exception(f"[Sage300 Task] Unexpected error for {company.name}: {e}")
        raise self.retry(exc=e, countdown=300)
