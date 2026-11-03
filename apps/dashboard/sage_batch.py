"""
Batch pulls from Sage 200.

You choose how many invoices to pull. Each pull continues after the last one, so
pulling 50 and then 50 more gives 100 different invoices. Everything that reads
Sage in batches goes through here: the dashboard, the background task and the
`preview_sage200` / `pull_sage200` commands. That way a preview always shows
exactly what the pull that follows it will take.

    preview(company, 50)   read-only: connects to Sage, saves nothing, never calls FIRS
    pull(company, 50)      reads the next 50, sends them to FIRS, moves the bookmark
"""
import logging
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.dashboard.models import SagePullState
from apps.invoices.models import Invoice
from apps.sage200.field_mapper import MappingError, Sage200FieldMapper
from apps.sage200.odbc_client import Sage200ConnectionError, Sage200ODBCClient

MAX_BATCH = 1000
DEFAULT_BATCH = 50
STALE_AFTER = timedelta(hours=3)  # a pull that "has been running" longer than this is assumed dead
SAMPLE_ROWS = 8


class PullError(Exception):
    """A problem worth showing to the person who clicked the button."""


class AlreadyRunning(PullError):
    pass


# ── small helpers ────────────────────────────────────────────

def clamp_count(value) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise PullError("Choose how many invoices to pull, as a whole number.")
    if not 1 <= count <= MAX_BATCH:
        raise PullError(f"Choose a number between 1 and {MAX_BATCH}.")
    return count


def firs_environment() -> str:
    return "PRODUCTION" if getattr(settings, "USE_FIRS_PRODUCTION", False) else "SANDBOX"


@contextmanager
def quiet_sage_logs():
    """The Sage client and mapper log every row; that would bury a report."""
    log = logging.getLogger("apps.sage200")
    previous = log.level
    log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        log.setLevel(previous)


def broker_reachable(url=None, timeout=2.0) -> bool:
    """Can Celery's broker (Redis) be reached? Checked before a pull so a missing Redis
    does not stall every failed invoice for ~20 seconds."""
    try:
        from config.celery import app
        with app.connection_for_write(url) as conn:
            conn.ensure_connection(max_retries=1, timeout=timeout)
        return True
    except Exception:
        return False


def installed_sql_drivers():
    try:
        import pyodbc
        return [d for d in pyodbc.drivers() if "SQL Server" in d]
    except Exception:  # pyodbc missing, or too old to list drivers
        return []


def connection_help(company) -> str:
    """Extra advice for a failed connection, based on the drivers this machine has."""
    installed = installed_sql_drivers()
    if installed and company.mssql_driver not in installed:
        return (f"The driver '{company.mssql_driver}' is not installed on this machine. Installed SQL Server "
                f"drivers: {', '.join(installed)}. Put one of those names in the client's 'Mssql driver' field.")
    if not installed:
        return ("No SQL Server ODBC driver was found on this machine. Install 'ODBC Driver 18 for SQL Server' "
                "from Microsoft, then put that name in the client's 'Mssql driver' field.")
    return ""


def _as_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


# ── the bookmark ─────────────────────────────────────────────

def highest_imported_id(company) -> int:
    """Highest Sage 200 record number Eagle already holds for this client."""
    ids = Invoice.objects.filter(client=company, source_system="SAGE200").values_list("sage_invoice_id", flat=True)
    return max((int(i) for i in ids.iterator() if str(i).isdigit()), default=0)


def get_state(company) -> SagePullState:
    """The client's bookmark. A new one starts after whatever Eagle already has."""
    state, _ = SagePullState.objects.get_or_create(
        client=company, defaults={"last_record_id": highest_imported_id(company)}
    )
    return state


def describe(company) -> dict:
    """What the dashboard shows about a client's pulls."""
    state = get_state(company)
    running = bool(state.running_since and state.running_since > timezone.now() - STALE_AFTER)
    return {
        "bookmark": state.last_record_id,
        "imported": Invoice.objects.filter(client=company, source_system="SAGE200").count(),
        "last_pull_at": state.last_pull_at.isoformat() if state.last_pull_at else None,
        "last_pull": state.last_pull_summary or None,
        "running": running,
    }


# ── analysing rows (shared by preview, the command and the dashboard) ─────

def analyze(company, raw, since=None) -> dict:
    """Describe a list of raw Sage rows without saving anything."""
    kinds = Counter((r.get("tr_type_code") or "?", r.get("tr_code_id")) for r in raw)
    dates = sorted(str(r.get("invoice_date"))[:10] for r in raw if r.get("invoice_date"))
    debit = sum(1 for r in raw if r.get("transaction_type") == 1)

    ids = {str(r.get("invoice_id")) for r in raw}
    known = set(
        Invoice.objects.filter(client=company, sage_invoice_id__in=ids).values_list("sage_invoice_id", flat=True)
    )
    fresh = [r for r in raw if str(r.get("invoice_id")) not in known]

    problems, b2b, b2c = Counter(), 0, 0
    with quiet_sage_logs():
        for r in fresh:
            try:
                mapped = Sage200FieldMapper(company, r).map()
            except MappingError as exc:
                problems[str(exc)[:110]] += 1
                continue
            except Exception as exc:  # keep going even if one row is odd
                problems[f"{type(exc).__name__}: {str(exc)[:90]}"] += 1
                continue
            if mapped.get("buyer_tin"):
                b2b += 1
            else:
                b2c += 1

    stale = 0
    if since:
        stale = sum(1 for r in raw if (_as_datetime(r.get("invoice_date")) or datetime.max) <= since)

    return {
        "returned": len(raw),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "types": [{"code": c, "code_id": cid, "count": n} for (c, cid), n in kinds.most_common()],
        "debit_rows": debit,
        "credit_rows": len(raw) - debit,
        "already_in_eagle": len(known),
        "new": len(fresh),
        "b2b": b2b,
        "b2c": b2c,
        "problems": [{"reason": text, "count": n} for text, n in problems.most_common(5)],
        "problem_total": sum(problems.values()),
        "samples": [
            {
                "number": str(r.get("invoice_number") or "?"),
                "date": str(r.get("invoice_date"))[:10],
                "customer": str(r.get("customer_name") or "(no customer)"),
                "gross": float(r.get("gross_amount") or 0),
                "code": r.get("tr_type_code") or "?",
            }
            for r in fresh[:SAMPLE_ROWS]
        ],
        "older_than_since": stale,
        "last_record_id": max((int(i) for i in ids if i.isdigit()), default=None),
    }


# ── preview: read only ───────────────────────────────────────

def preview(company, count, *, since=None, after_id=None) -> dict:
    """What the next pull would take. Saves nothing and never contacts FIRS."""
    count = clamp_count(count)
    bookmark = get_state(company).last_record_id if after_id is None else int(after_id)
    try:
        with quiet_sage_logs():
            raw = Sage200ODBCClient(company).get_new_invoices(since=since, limit=count, after_id=bookmark)
    except Sage200ConnectionError as exc:
        raise PullError(f"Could not read Sage 200: {exc} {connection_help(company)}".strip())
    except Exception as exc:  # for example a table that does not exist
        raise PullError(f"Connected to Sage 200, but the query failed: {exc}")

    result = analyze(company, raw, since=since)
    result.update({"requested": count, "bookmark": bookmark, "environment": firs_environment()})
    return result


# ── pull: the real thing ─────────────────────────────────────

def _begin(company, force=False) -> SagePullState:
    state = get_state(company)
    if force:
        SagePullState.objects.filter(pk=state.pk).update(running_since=timezone.now())
        return state
    now = timezone.now()
    claimed = SagePullState.objects.filter(pk=state.pk).filter(
        Q(running_since__isnull=True) | Q(running_since__lt=now - STALE_AFTER)
    ).update(running_since=now)
    if not claimed:
        raise AlreadyRunning(f"A pull for {company.name} is already running. Wait for it to finish.")
    state.refresh_from_db()
    return state


def pull(company, count, *, since=None, after_id=None, force=False, schedule_retries=None) -> dict:
    """
    Read the next `count` invoices from Sage 200 and send them through to FIRS.

    schedule_retries: queue automatic retries for invoices FIRS fails. By default
    this is on only when the Celery broker can be reached; otherwise failed
    invoices simply stay FAILED and can be retried from the dashboard.
    """
    from apps.invoices.pipeline import InvoicePipeline  # imported late: it pulls in the FIRS client
    from utils.exceptions import PipelineError

    count = clamp_count(count)
    state = _begin(company, force=force)
    bookmark = state.last_record_id if after_id is None else int(after_id)
    try:
        pipeline = InvoicePipeline(company)
        pipeline.schedule_retries = broker_reachable() if schedule_retries is None else schedule_retries
        try:
            results = pipeline.run(since=since, limit=count, after_id=bookmark)
        except PipelineError as exc:
            raise PullError(f"Could not read Sage 200: {exc} {connection_help(company)}".strip())

        new_bookmark = pipeline.last_record_id if pipeline.last_record_id is not None else bookmark
        summary = {
            **{k: v for k, v in results.items() if isinstance(v, int)},
            "requested": count,
            "errors": getattr(pipeline, "errors", [])[:50],
            "bookmark_before": bookmark,
            "bookmark_after": new_bookmark,
            "environment": firs_environment(),
            "retries_scheduled": pipeline.schedule_retries,
        }
        SagePullState.objects.filter(pk=state.pk).update(
            last_record_id=new_bookmark, last_pull_at=timezone.now(), last_pull_summary=summary
        )
        return summary
    finally:
        SagePullState.objects.filter(pk=state.pk).update(running_since=None)


def pull_in_background(company, count) -> dict:
    """
    What the Celery task runs. Like pull(), but a failure is saved on the client's
    pull state so the dashboard can show it. Nobody is watching a worker's logs.
    """
    try:
        return pull(company, count)
    except AlreadyRunning as exc:
        return {"error": str(exc)}  # the pull that is running owns the saved state
    except PullError as exc:
        summary = {"error": str(exc), "requested": count, "environment": firs_environment()}
        state = get_state(company)  # creates it if this failed before any pull began
        SagePullState.objects.filter(pk=state.pk).update(last_pull_at=timezone.now(), last_pull_summary=summary)
        return summary
