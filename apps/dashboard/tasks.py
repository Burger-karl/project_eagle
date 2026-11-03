from celery import shared_task

from apps.core.models import ClientCompany
from apps.dashboard import sage_batch


@shared_task(
    name="apps.dashboard.tasks.pull_sage200_batch",
    soft_time_limit=3 * 3600,  # 1000 invoices at a few seconds each fits comfortably
    time_limit=3 * 3600 + 300,
)
def pull_sage200_batch(company_id: str, count: int) -> dict:
    """Pull the next `count` Sage 200 invoices for one client and send them to FIRS."""
    company = ClientCompany.objects.get(id=company_id)
    return sage_batch.pull_in_background(company, count)
