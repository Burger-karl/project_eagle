"""
Management command: watch_folder
Start the folder watch agent from the command line.

Usage:
    python manage.py watch_folder
    python manage.py watch_folder --client "Link Options"
    python manage.py watch_folder --path "C:\\InvoiceDropbox\\"
    python manage.py watch_folder --dry-run
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start folder watch agent for Excel invoice auto-pickup"

    def add_arguments(self, parser):
        parser.add_argument("--client", type=str)
        parser.add_argument("--path",   type=str)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from apps.excel_intake.models import FolderWatchConfig
        from apps.excel_intake.folder_watcher import FolderWatchAgent

        qs = FolderWatchConfig.objects.filter(is_active=True)

        if options.get("client"):
            qs = qs.filter(client__name__icontains=options["client"])
        if options.get("path"):
            qs = qs.filter(watch_path=options["path"])

        if not qs.exists():
            self.stderr.write("No active folder watch configs found.")
            self.stderr.write(
                "Create one in Django admin: "
                "Excel Intake → Folder Watch Configs → Add"
            )
            return

        import threading
        threads = []
        for config in qs:
            self.stdout.write(
                f"Watching: {config.watch_path} "
                f"for {config.client.name}"
            )
            agent  = FolderWatchAgent(config, dry_run=options.get("dry_run", False))
            thread = threading.Thread(target=agent.start, daemon=True)
            thread.start()
            threads.append(thread)

        self.stdout.write(
            self.style.SUCCESS(
                f"Started {len(threads)} folder watcher(s). Press Ctrl+C to stop."
            )
        )
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            self.stdout.write("\nStopping folder watchers...")
