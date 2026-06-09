"""
Method B — Email Ingestion Agent
Monitors a dedicated email inbox for Excel invoice attachments.

Checks the inbox every 5 minutes via Celery Beat.
Downloads attachments, processes them through the FIRS pipeline,
and replies to the sender with the IRN confirmation.

Supports Gmail, Outlook, and any IMAP-compatible inbox.
"""

import email
import imaplib
import logging
import os
import tempfile
from email.header import decode_header
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("apps.excel_intake")

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}


class EmailIngestAgent:
    """
    Connects to an IMAP inbox and processes Excel invoice attachments.

    Usage:
        config = EmailIngestConfig.objects.get(is_active=True)
        agent  = EmailIngestAgent(config)
        agent.process_new_emails()
    """

    def __init__(self, config):
        self.config  = config
        self.company = config.client
        self._imap   = None

    # ── CONNECTION ────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect and authenticate to IMAP server."""
        try:
            if self.config.imap_use_ssl:
                self._imap = imaplib.IMAP4_SSL(
                    self.config.imap_host,
                    self.config.imap_port
                )
            else:
                self._imap = imaplib.IMAP4(
                    self.config.imap_host,
                    self.config.imap_port
                )
            self._imap.login(
                self.config.email_address,
                self.config.email_password
            )
            logger.info(
                f"[EmailIngest] Connected to {self.config.imap_host} "
                f"as {self.config.email_address}"
            )
            return True
        except imaplib.IMAP4.error as e:
            logger.error(f"[EmailIngest] IMAP login failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[EmailIngest] Connection error: {e}")
            return False

    def disconnect(self):
        """Close IMAP connection cleanly."""
        if self._imap:
            try:
                self._imap.close()
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    # ── MAIN ENTRY POINT ──────────────────────────────────────

    def process_new_emails(self) -> dict:
        """
        Check inbox for new emails with Excel attachments.
        Returns summary of processed emails.
        """
        from django.utils import timezone
        from apps.excel_intake.models import EmailIngestConfig

        results = {"checked": 0, "processed": 0, "skipped": 0, "errors": 0}

        if not self.connect():
            return {"error": "Cannot connect to email inbox"}

        try:
            # Select inbox folder
            status, _ = self._imap.select(
                f'"{self.config.inbox_folder}"'
            )
            if status != "OK":
                return {"error": f"Cannot select folder: {self.config.inbox_folder}"}

            # Search for unread emails with attachments
            # UNSEEN = unread, since last UID if available
            last_uid = self.config.last_uid
            if last_uid:
                search_criteria = f"(UID {int(last_uid)+1}:*)"
                _, uid_data = self._imap.uid("search", None, search_criteria)
            else:
                _, uid_data = self._imap.uid("search", None, "UNSEEN")

            uids = uid_data[0].split() if uid_data[0] else []
            results["checked"] = len(uids)
            logger.info(
                f"[EmailIngest] Found {len(uids)} emails to check in "
                f"{self.config.email_address}"
            )

            for uid in uids:
                uid_str = uid.decode()
                try:
                    processed = self._process_email(uid_str)
                    if processed:
                        results["processed"] += 1
                        # Update last processed UID
                        self.config.last_uid = uid_str
                    else:
                        results["skipped"] += 1
                except Exception as e:
                    logger.error(
                        f"[EmailIngest] Error processing UID {uid_str}: {e}"
                    )
                    results["errors"] += 1

            # Save last check time
            self.config.last_checked_at = timezone.now()
            self.config.save(update_fields=["last_checked_at", "last_uid"])

        finally:
            self.disconnect()

        logger.info(f"[EmailIngest] Results: {results}")
        return results

    # ── EMAIL PROCESSING ──────────────────────────────────────

    def _process_email(self, uid: str) -> bool:
        """
        Process one email. Returns True if it had Excel attachments.
        """
        _, msg_data = self._imap.uid("fetch", uid, "(RFC822)")
        raw_email   = msg_data[0][1]
        msg         = email.message_from_bytes(raw_email)

        sender  = self._decode_header(msg.get("From", ""))
        subject = self._decode_header(msg.get("Subject", ""))
        msg_id  = msg.get("Message-ID", uid)

        logger.info(f"[EmailIngest] Processing email from {sender}: {subject}")

        # Find Excel attachments
        attachments = self._extract_attachments(msg)
        if not attachments:
            logger.debug(f"[EmailIngest] No Excel attachments in email from {sender}")
            return False

        logger.info(
            f"[EmailIngest] Found {len(attachments)} attachment(s) "
            f"from {sender}"
        )

        # Process each attachment
        processed_irns = []
        for filename, file_bytes in attachments:
            irns = self._process_attachment(
                filename, file_bytes, sender, subject, msg_id
            )
            processed_irns.extend(irns)

        # Send confirmation reply
        if processed_irns:
            self._send_confirmation_reply(sender, subject, processed_irns)

        # Mark email as read and move to processed folder
        self._imap.uid("store", uid, "+FLAGS", "\\Seen")

        return True

    def _extract_attachments(
        self, msg
    ) -> List[Tuple[str, bytes]]:
        """Extract Excel attachments from email message."""
        attachments = []
        for part in msg.walk():
            content_disposition = str(
                part.get("Content-Disposition", "")
            ).lower()

            if "attachment" not in content_disposition:
                continue

            filename = part.get_filename()
            if not filename:
                continue

            filename = self._decode_header(filename)
            ext      = Path(filename).suffix.lower()

            if ext not in SUPPORTED_EXTENSIONS:
                logger.debug(
                    f"[EmailIngest] Skipping non-Excel attachment: {filename}"
                )
                continue

            try:
                file_bytes = part.get_payload(decode=True)
                if file_bytes:
                    attachments.append((filename, file_bytes))
                    logger.debug(
                        f"[EmailIngest] Extracted attachment: "
                        f"{filename} ({len(file_bytes)} bytes)"
                    )
            except Exception as e:
                logger.warning(
                    f"[EmailIngest] Cannot extract {filename}: {e}"
                )

        return attachments

    def _process_attachment(
        self,
        filename:   str,
        file_bytes: bytes,
        sender:     str,
        subject:    str,
        msg_id:     str,
    ) -> List[str]:
        """
        Process one Excel attachment through the full FIRS pipeline.
        Returns list of IRNs generated.
        """
        from apps.excel_intake.models import (
            ExcelUpload, UploadStatus, IntakeMethod
        )
        from apps.excel_intake.excel_parser import ExcelParser, ExcelParseError
        from apps.excel_intake.pipeline import ExcelPipeline

        upload = ExcelUpload.objects.create(
            client=self.company,
            method=IntakeMethod.EMAIL_INGESTION,
            status=UploadStatus.RECEIVED,
            original_filename=filename,
            file_size_bytes=len(file_bytes),
            sender_email=sender[:254],
            email_subject=subject[:499],
            email_message_id=msg_id[:254],
        )

        try:
            parser = ExcelParser(file_bytes=file_bytes, filename=filename)
            result = parser.parse()

            upload.file_hash = result.file_hash
            upload.save(update_fields=["file_hash"])

            if upload.is_duplicate:
                logger.warning(
                    f"[EmailIngest] Duplicate attachment skipped: {filename}"
                )
                upload.status = UploadStatus.FAILED
                upload.parse_error = "Duplicate file"
                upload.save(update_fields=["status", "parse_error"])
                return []

            pipeline = ExcelPipeline(upload, result)
            pipeline.run()

            # Collect IRNs from cleared invoices
            irns = list(
                upload.invoices.filter(irn__gt="").values_list("irn", flat=True)
            )
            return irns

        except ExcelParseError as e:
            logger.error(
                f"[EmailIngest] Parse error for {filename}: {e}"
            )
            upload.status = UploadStatus.FAILED
            upload.parse_error = str(e)
            upload.save(update_fields=["status", "parse_error"])
            return []

        except Exception as e:
            logger.exception(
                f"[EmailIngest] Error for {filename}: {e}"
            )
            upload.status = UploadStatus.FAILED
            upload.parse_error = str(e)
            upload.save(update_fields=["status", "parse_error"])
            return []

    def _send_confirmation_reply(
        self,
        to_email: str,
        original_subject: str,
        irns: List[str],
    ):
        """Send IRN confirmation reply to the sender."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from django.conf import settings

        try:
            irn_list = "\n".join(f"  • {irn}" for irn in irns)
            body = (
                f"Your invoice submission has been processed successfully.\n\n"
                f"Invoice Reference Numbers (IRNs) issued by FIRS NRS:\n"
                f"{irn_list}\n\n"
                f"These IRNs confirm your invoices have been cleared by FIRS.\n"
                f"Please attach the IRN to the corresponding invoice records.\n\n"
                f"— Project Eagle Middleware\n"
                f"  Link Options & Systems Limited"
            )

            msg          = MIMEMultipart()
            msg["From"]  = self.config.email_address
            msg["To"]    = to_email
            msg["Subject"] = f"Re: {original_subject} — IRN Confirmation"
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(
                    self.config.email_address,
                    self.config.email_password
                )
                smtp.send_message(msg)

            logger.info(
                f"[EmailIngest] IRN confirmation sent to {to_email}"
            )
        except Exception as e:
            logger.warning(
                f"[EmailIngest] Could not send confirmation email: {e}"
            )

    # ── HELPERS ───────────────────────────────────────────────

    @staticmethod
    def _decode_header(raw: str) -> str:
        """Decode email header which may be encoded."""
        try:
            decoded_parts = decode_header(raw)
            parts = []
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    parts.append(
                        part.decode(encoding or "utf-8", errors="replace")
                    )
                else:
                    parts.append(str(part))
            return "".join(parts)
        except Exception:
            return str(raw)
