"""
Excel Intake Celery Tasks
Async processing for all three Excel intake methods.
"""

import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("apps.excel_intake")


@shared_task(
    bind=True,
    name="apps.excel_intake.tasks.process_excel_upload",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=300,
    time_limit=360,
)
def process_excel_upload(self, upload_id: str, file_bytes_hex: str = None):
    """
    Process an Excel upload through the full FIRS pipeline.
    Called by Method C (Web Upload) after file is received.
    """
    from apps.excel_intake.models import ExcelUpload, UploadStatus
    from apps.excel_intake.excel_parser import ExcelParser, ExcelParseError
    from apps.excel_intake.pipeline import ExcelPipeline

    try:
        upload = ExcelUpload.objects.get(id=upload_id)
    except ExcelUpload.DoesNotExist:
        logger.error(f"[Task] ExcelUpload {upload_id} not found")
        return {"error": "upload_not_found"}

    logger.info(
        f"[Task] Processing upload {upload_id}: {upload.original_filename}"
    )

    try:
        # Load file bytes
        if file_bytes_hex:
            file_bytes = bytes.fromhex(file_bytes_hex)
        elif upload.file_path:
            with open(upload.file_path, "rb") as f:
                file_bytes = f.read()
        else:
            raise ValueError("No file data available")

        # Parse
        parser = ExcelParser(
            file_bytes=file_bytes,
            filename=upload.original_filename
        )
        result = parser.parse()

        # Run pipeline
        pipeline = ExcelPipeline(upload, result)
        pipeline.run()

        return {
            "upload_id": upload_id,
            "status":    upload.status,
            "cleared":   upload.cleared_rows,
            "failed":    upload.failed_rows,
        }

    except ExcelParseError as e:
        logger.error(f"[Task] Parse error for {upload_id}: {e}")
        ExcelUpload.objects.filter(id=upload_id).update(
            status=UploadStatus.FAILED,
            parse_error=str(e)
        )
        return {"error": str(e)}

    except Exception as e:
        logger.exception(f"[Task] Error for upload {upload_id}: {e}")
        ExcelUpload.objects.filter(id=upload_id).update(
            status=UploadStatus.FAILED,
            parse_error=str(e)[:500]
        )
        raise self.retry(exc=e)


@shared_task(
    name="apps.excel_intake.tasks.check_email_inbox",
    soft_time_limit=240,
    time_limit=300,
)
def check_email_inbox():
    """
    Method B — Check all active email inboxes for new Excel attachments.
    Runs every 5 minutes via Celery Beat.
    """
    from apps.excel_intake.models import EmailIngestConfig
    from apps.excel_intake.email_agent import EmailIngestAgent

    configs = EmailIngestConfig.objects.filter(is_active=True)
    if not configs.exists():
        logger.debug("[Task] No active email ingest configs")
        return {"status": "no_configs"}

    results = {}
    for config in configs:
        try:
            agent  = EmailIngestAgent(config)
            result = agent.process_new_emails()
            results[config.email_address] = result
            logger.info(
                f"[Task] Email check {config.email_address}: {result}"
            )
        except Exception as e:
            logger.error(
                f"[Task] Email check failed for {config.email_address}: {e}"
            )
            results[config.email_address] = {"error": str(e)}

    return results


@shared_task(
    name="apps.excel_intake.tasks.poll_watch_folders",
    soft_time_limit=180,
    time_limit=240,
)
def poll_watch_folders():
    """
    Method A — Poll all active watch folders for new Excel files.
    Runs every 30 seconds via Celery Beat.
    Only used as fallback when watchdog is not running.
    """
    from apps.excel_intake.models import FolderWatchConfig
    from apps.excel_intake.folder_watcher import FolderWatchAgent
    from pathlib import Path
    from django.utils import timezone

    configs = FolderWatchConfig.objects.filter(is_active=True)
    if not configs.exists():
        return {"status": "no_configs"}

    results = {}
    for config in configs:
        try:
            watch_dir = Path(config.watch_path)
            if not watch_dir.exists():
                results[config.watch_path] = "folder_not_found"
                continue

            agent = FolderWatchAgent(config)
            agent._scan_existing()

            config.last_polled_at = timezone.now()
            config.save(update_fields=["last_polled_at"])
            results[config.watch_path] = "scanned"

        except Exception as e:
            logger.error(
                f"[Task] Folder poll failed for {config.watch_path}: {e}"
            )
            results[config.watch_path] = f"error: {e}"

    return results


@shared_task(
    name="apps.excel_intake.tasks.retry_failed_excel_rows",
    soft_time_limit=300,
    time_limit=360,
)
def retry_failed_excel_rows():
    """
    Retry failed Excel invoice rows.
    Runs every 30 minutes via Celery Beat.
    Only retries rows with fewer than 5 attempts.
    """
    from apps.excel_intake.models import ExcelInvoice, ExcelRowStatus
    from apps.excel_intake.pipeline import ExcelPipeline

    failed_rows = ExcelInvoice.objects.filter(
        status=ExcelRowStatus.FAILED,
        submission_attempts__lt=5,
    ).select_related("upload__client")

    count = failed_rows.count()
    if count == 0:
        return {"retried": 0}

    logger.info(f"[Task] Retrying {count} failed Excel rows")
    retried = 0

    for row in failed_rows:
        try:
            if not row.mapped_data:
                continue
            pipeline = ExcelPipeline(row.upload, None)
            success  = pipeline._process_row(row.raw_data)
            if success:
                retried += 1
        except Exception as e:
            logger.warning(
                f"[Task] Retry failed for row {row.id}: {e}"
            )

    return {"retried": retried, "total_failed": count}
