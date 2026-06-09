"""
Method A — Folder Watch Agent
Monitors a folder for new Excel files and processes them automatically.

Run as a background process:
    python manage.py watch_folder
    python manage.py watch_folder --client "Link Options"
    python manage.py watch_folder --path "C:\\InvoiceDropbox\\"

Uses Python watchdog library for real-time file system events.
Falls back to polling if watchdog events are not available.
"""

import os
import time
import shutil
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("apps.excel_intake")

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}


class FolderWatchAgent:
    """
    Watches a folder for new Excel invoice files and
    submits them through the FIRS pipeline automatically.

    Usage:
        agent = FolderWatchAgent(config)
        agent.start()  # blocks — run in background thread/process
    """

    def __init__(self, config, dry_run: bool = False):
        """
        Args:
            config:   FolderWatchConfig model instance
            dry_run:  If True, parse but don't submit to FIRS
        """
        self.config    = config
        self.company   = config.client
        self.dry_run   = dry_run
        self.watch_dir = Path(config.watch_path)
        self.done_dir  = Path(config.processed_path) if config.processed_path else None
        self.fail_dir  = Path(config.failed_path)    if config.failed_path    else None
        self._running  = False

    def start(self):
        """
        Start watching the folder.
        Tries watchdog for real-time events, falls back to polling.
        """
        if not self.watch_dir.exists():
            raise FileNotFoundError(
                f"Watch folder does not exist: {self.watch_dir}\n"
                f"Create it on the client's server and try again."
            )

        logger.info(
            f"[FolderWatch] Starting for {self.company.name} "
            f"→ {self.watch_dir}"
        )
        self._running = True

        # Ensure output directories exist
        if self.done_dir:
            self.done_dir.mkdir(parents=True, exist_ok=True)
        if self.fail_dir:
            self.fail_dir.mkdir(parents=True, exist_ok=True)

        # Try watchdog first, fall back to polling
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class Handler(FileSystemEventHandler):
                def __init__(self_, agent):
                    self_.agent = agent

                def on_created(self_, event):
                    if not event.is_directory:
                        path = Path(event.src_path)
                        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                            # Small delay to ensure file is fully written
                            time.sleep(1)
                            self_.agent._process_file(path)

            observer = Observer()
            observer.schedule(Handler(self), str(self.watch_dir), recursive=False)
            observer.start()
            logger.info(f"[FolderWatch] Watchdog active on {self.watch_dir}")

            # Also process any files already in the folder
            self._scan_existing()

            try:
                while self._running:
                    time.sleep(self.config.poll_interval)
            finally:
                observer.stop()
                observer.join()

        except ImportError:
            logger.warning(
                "[FolderWatch] watchdog not installed — using polling mode. "
                "Install with: pip install watchdog"
            )
            self._poll_loop()

    def stop(self):
        """Stop the folder watcher."""
        self._running = False
        logger.info(f"[FolderWatch] Stopped for {self.company.name}")

    def _poll_loop(self):
        """Fallback polling mode — scans folder every N seconds."""
        seen_files = set()
        while self._running:
            self._scan_existing(seen_files)
            time.sleep(self.config.poll_interval)

    def _scan_existing(self, seen_files: Optional[set] = None):
        """Scan folder for unprocessed Excel files."""
        for path in sorted(self.watch_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if seen_files is not None:
                if str(path) in seen_files:
                    continue
                seen_files.add(str(path))
            self._process_file(path)

    def _process_file(self, path: Path):
        """Process one Excel file through the full pipeline."""
        logger.info(f"[FolderWatch] New file detected: {path.name}")

        from django.utils import timezone
        from apps.excel_intake.models import ExcelUpload, UploadStatus, IntakeMethod
        from apps.excel_intake.excel_parser import ExcelParser, ExcelParseError
        from apps.excel_intake.pipeline import ExcelPipeline

        upload = None
        try:
            # Read file
            file_bytes = path.read_bytes()

            # Create upload record
            upload = ExcelUpload.objects.create(
                client=self.company,
                method=IntakeMethod.FOLDER_WATCH,
                status=UploadStatus.RECEIVED,
                original_filename=path.name,
                file_path=str(path),
                file_size_bytes=len(file_bytes),
            )

            # Check for duplicate
            parser = ExcelParser(file_bytes=file_bytes, filename=path.name)
            result = parser.parse()

            upload.file_hash = result.file_hash
            upload.save(update_fields=["file_hash"])

            if upload.is_duplicate:
                logger.warning(
                    f"[FolderWatch] Duplicate file skipped: {path.name}"
                )
                self._move_file(path, self.fail_dir, suffix="_DUPLICATE")
                upload.status = UploadStatus.FAILED
                upload.parse_error = "Duplicate file — already processed"
                upload.save(update_fields=["status", "parse_error"])
                return

            # Run pipeline
            pipeline = ExcelPipeline(upload, result, dry_run=self.dry_run)
            pipeline.run()

            # Move to processed folder
            self._move_file(path, self.done_dir)
            logger.info(f"[FolderWatch] ✅ Processed: {path.name}")

        except ExcelParseError as e:
            logger.error(f"[FolderWatch] Parse error for {path.name}: {e}")
            self._move_file(path, self.fail_dir, suffix="_PARSE_ERROR")
            if upload:
                upload.status = UploadStatus.FAILED
                upload.parse_error = str(e)
                upload.save(update_fields=["status", "parse_error"])

        except Exception as e:
            logger.exception(f"[FolderWatch] Error processing {path.name}: {e}")
            self._move_file(path, self.fail_dir, suffix="_ERROR")
            if upload:
                upload.status = UploadStatus.FAILED
                upload.parse_error = str(e)
                upload.save(update_fields=["status", "parse_error"])

    def _move_file(self, path: Path, dest_dir: Optional[Path], suffix: str = ""):
        """Move a file to the destination directory."""
        if not dest_dir:
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            new_name = path.stem + suffix + path.suffix
            dest     = dest_dir / new_name
            # Avoid overwrite
            counter = 1
            while dest.exists():
                new_name = f"{path.stem}{suffix}_{counter}{path.suffix}"
                dest     = dest_dir / new_name
                counter += 1
            shutil.move(str(path), str(dest))
            logger.debug(f"[FolderWatch] Moved {path.name} → {dest}")
        except Exception as e:
            logger.warning(f"[FolderWatch] Could not move {path.name}: {e}")
