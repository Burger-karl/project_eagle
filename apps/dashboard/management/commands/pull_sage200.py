"""
Pull the next batch of invoices from Sage 200 and send them to FIRS.

    python manage.py pull_sage200 --limit 10            # try 10 first
    python manage.py pull_sage200 --limit 200 --yes     # no confirmation question
    python manage.py pull_sage200 --client "Acme" --limit 50
    python manage.py pull_sage200 --after 0 --limit 5   # start again from Sage's first record

You choose how many invoices. Each pull continues after the last one, so running
it twice with --limit 50 gives 100 different invoices. A preview of exactly what
will be sent is shown first, and you are asked to confirm.

This runs in your terminal and needs no Celery worker. (The dashboard button does
the same thing in the background and does need Redis and a worker.)
"""
from django.core.management.base import BaseCommand, CommandError

from apps.dashboard import sage_batch
from apps.dashboard.management.commands.preview_sage200 import Command as PreviewCommand
from apps.dashboard.management.commands.preview_sage200 import active_clients, parse_since
from apps.dashboard.sage_batch import AlreadyRunning, PullError


class Command(BaseCommand):
    help = "Pull the next batch of invoices from Sage 200 and send them to FIRS."

    def add_arguments(self, parser):
        parser.add_argument("--client", help="Client name (or part of it). Needed when several clients are active.")
        parser.add_argument("--limit", type=int, default=sage_batch.DEFAULT_BATCH,
                            help=f"How many invoices (default {sage_batch.DEFAULT_BATCH}, most {sage_batch.MAX_BATCH}).")
        parser.add_argument("--after", type=int, help="Start after this Sage record number instead of the bookmark.")
        parser.add_argument("--since", help="Only rows with a transaction date after this (2026-09-01).")
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
        parser.add_argument("--force", action="store_true", help="Ignore a stuck 'already running' marker.")

    def handle(self, *args, **opts):
        clients = list(active_clients(opts["client"]))
        if len(clients) > 1:
            names = ", ".join(c.name for c in clients)
            raise CommandError(f"Several clients are active ({names}). Choose one with --client.")
        company, since = clients[0], parse_since(opts["since"])

        # 1. Show exactly what would be sent. Reads Sage only.
        try:
            preview = sage_batch.preview(company, opts["limit"], since=since, after_id=opts["after"])
        except PullError as exc:
            raise CommandError(str(exc))
        shown = PreviewCommand()  # reuse its printing on the result we already have
        shown.stdout, shown.stderr, shown.style = self.stdout, self.stderr, self.style
        shown._header(company)
        shown._position(preview, opts["after"], since)
        shown._report(preview, opts["limit"])
        if not preview["new"]:
            self.stdout.write("\nNothing new to send.")
            return

        # 2. Ask, with a stronger question for the live FIRS system.
        env = preview["environment"]
        if not opts["yes"]:
            word = "PRODUCTION" if env == "PRODUCTION" else "yes"
            self.stdout.write(self.style.WARNING(
                f"\nThis will send {preview['new']} invoice(s) to the {env} FIRS system."
                + (" These are REAL tax submissions." if env == "PRODUCTION" else "")
            ))
            if input(f"Type {word} to continue: ").strip() != word:
                raise CommandError("Cancelled. Nothing was sent.")

        # 3. Pull for real.
        try:
            summary = sage_batch.pull(company, opts["limit"], since=since, after_id=opts["after"], force=opts["force"])
        except AlreadyRunning as exc:
            raise CommandError(f"{exc} If it crashed, run again with --force.")
        except PullError as exc:
            raise CommandError(str(exc))
        self._summarise(summary)

    def _summarise(self, s):
        out = self.stdout.write
        out(self.style.SUCCESS(f"\nDone. Read {s['pulled']} row(s) from Sage 200 ({s['environment']})."))
        out(f"  Cleared by FIRS : {s.get('cleared', 0)}")
        out(f"  Failed          : {s.get('failed', 0)}")
        out(f"  Already in Eagle: {s.get('skipped', 0)}")
        out(f"  Bookmark        : Sage record #{s['bookmark_before']:,}  ->  #{s['bookmark_after']:,}")
        if s["errors"]:
            out(self.style.ERROR(f"\n  {len(s['errors'])} invoice(s) could not be read and were NOT saved:"))
            for e in s["errors"]:
                out(f"    {e['number']:<14} {e['reason']}")
        if s.get("failed") and not s["errors"]:
            out("\n  Failed invoices are saved. See them in the dashboard and retry once the cause is fixed.")
        if not s["retries_scheduled"]:
            out(self.style.WARNING("  Redis was not reachable, so automatic retries were not queued."))
