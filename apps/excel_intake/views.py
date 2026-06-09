"""
Method C — Web Upload Portal
REST API endpoints for browser-based Excel invoice uploads.

Security:
    - Token authentication (DRF)
    - Rate limiting (django-ratelimit + Cloudflare)
    - File type validation (magic bytes, not just extension)
    - File size limit (configurable, default 10MB)
    - CSRF protection
    - Real IP extraction from Cloudflare headers

Endpoints:
    POST /api/excel/upload/          — Upload Excel file
    GET  /api/excel/uploads/         — List all uploads for this client
    GET  /api/excel/uploads/{id}/    — Upload detail + invoice rows
    GET  /api/excel/uploads/{id}/status/ — Upload status (poll)
    POST /api/excel/template/download/   — Download standard template
    GET  /api/excel/health/          — Health check
"""

import hashlib
import logging
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django_ratelimit.decorators import ratelimit
from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.excel_intake.models import (
    ExcelUpload, ExcelInvoice, UploadStatus, IntakeMethod
)
from apps.excel_intake.serializers import (
    ExcelUploadSerializer,
    ExcelUploadDetailSerializer,
    ExcelUploadStatusSerializer,
)
from apps.excel_intake.excel_parser import ExcelParser, ExcelParseError
from apps.excel_intake.tasks import process_excel_upload

logger = logging.getLogger("apps.excel_intake")

# ── Constants ─────────────────────────────────────────────────
MAX_FILE_SIZE_MB  = getattr(settings, "EXCEL_MAX_FILE_SIZE_MB", 10)
MAX_FILE_SIZE     = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_MIMETYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.ms-excel",                                            # .xls
    "application/vnd.ms-excel.sheet.macroEnabled.12",                      # .xlsm
    "text/csv",                                                             # .csv
    "application/csv",
}
ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}

# Excel file magic bytes (first bytes identify file type)
MAGIC_BYTES = {
    b"\x50\x4b\x03\x04": "xlsx/xlsm",  # ZIP header (xlsx is a zip)
    b"\xd0\xcf\x11\xe0": "xls",         # OLE2 compound document (old xls)
}


def get_real_ip(request) -> str:
    """Extract real client IP (handles Cloudflare proxy)."""
    return (
        request.META.get("HTTP_CF_CONNECTING_IP")
        or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
    )


def validate_excel_file(file) -> tuple:
    """
    Validate an uploaded file.
    Returns (is_valid: bool, error: str)
    Checks: size, extension, magic bytes.
    """
    # Size check
    if file.size > MAX_FILE_SIZE:
        return False, (
            f"File too large. Maximum size is {MAX_FILE_SIZE_MB}MB. "
            f"Your file is {round(file.size / 1024 / 1024, 1)}MB."
        )

    # Extension check
    ext = Path(file.name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, (
            f"File type not supported. "
            f"Please upload an Excel file (.xlsx, .xls, .xlsm) or .csv. "
            f"Received: {ext}"
        )

    # Magic bytes check (prevents renamed files)
    if ext in {".xlsx", ".xls", ".xlsm"}:
        first_bytes = file.read(4)
        file.seek(0)  # Reset for later reading
        if first_bytes not in MAGIC_BYTES:
            return False, (
                "File content does not match an Excel file. "
                "Please ensure you are uploading a genuine Excel file."
            )

    return True, ""


# ── VIEWS ─────────────────────────────────────────────────────

class ExcelUploadView(APIView):
    """
    POST /api/excel/upload/
    Upload an Excel invoice file for FIRS processing.

    Rate limited: 20 uploads per hour per IP.
    Max file size: 10MB.
    Accepted formats: .xlsx, .xls, .xlsm, .csv

    Request:
        multipart/form-data
        file: <excel_file>
        client_id: <uuid>  (optional if auth token maps to one client)

    Response 202:
        {
            "upload_id": "uuid",
            "status": "RECEIVED",
            "message": "File received. Processing started.",
            "poll_url": "/api/excel/uploads/{id}/status/"
        }
    """
    parser_classes     = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Rate limiting (defence-in-depth on top of Cloudflare)
        from django_ratelimit.core import is_ratelimited
        real_ip = get_real_ip(request)
        limited = is_ratelimited(
            request,
            group="excel_upload",
            key=f"ip:{real_ip}",
            rate="20/h",
            increment=True,
        )
        if limited:
            logger.warning(
                f"[WebUpload] Rate limit exceeded for IP {real_ip}"
            )
            return Response(
                {
                    "error": "Upload rate limit exceeded.",
                    "detail": "Maximum 20 uploads per hour. Please try again later.",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Get uploaded file
        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response(
                {"error": "No file provided. Include a 'file' field in the request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate file
        is_valid, error_msg = validate_excel_file(uploaded_file)
        if not is_valid:
            return Response(
                {"error": error_msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get client company
        from apps.core.models import ClientCompany
        try:
            client_id = request.data.get("client_id")
            if client_id:
                company = ClientCompany.objects.get(
                    id=client_id, is_active=True
                )
            else:
                company = ClientCompany.objects.filter(
                    is_active=True
                ).first()
                if not company:
                    return Response(
                        {"error": "No active client company configured."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        except ClientCompany.DoesNotExist:
            return Response(
                {"error": "Client company not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Read file bytes
        file_bytes = uploaded_file.read()
        file_hash  = hashlib.sha256(file_bytes).hexdigest()

        # Check for duplicate
        existing = ExcelUpload.objects.filter(
            client=company,
            file_hash=file_hash,
            status=UploadStatus.COMPLETE,
        ).first()
        if existing:
            return Response(
                {
                    "error": "Duplicate file detected.",
                    "detail": (
                        f"This exact file was already processed on "
                        f"{existing.created_at.strftime('%d %b %Y at %H:%M')}."
                    ),
                    "previous_upload_id": str(existing.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Save file to disk
        upload_dir = Path(
            getattr(settings, "EXCEL_UPLOAD_DIR", "media/excel_uploads")
        ) / str(company.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{file_hash[:16]}_{uploaded_file.name}"
        file_path.write_bytes(file_bytes)

        # Create upload record
        upload = ExcelUpload.objects.create(
            client=company,
            method=IntakeMethod.WEB_UPLOAD,
            status=UploadStatus.RECEIVED,
            original_filename=uploaded_file.name,
            file_path=str(file_path),
            file_size_bytes=len(file_bytes),
            file_hash=file_hash,
            uploaded_by_ip=real_ip,
            uploaded_by_user=str(request.user),
        )

        # Queue async processing
        process_excel_upload.delay(
            str(upload.id),
            file_bytes.hex(),
        )

        logger.info(
            f"[WebUpload] File received: {uploaded_file.name} "
            f"from {real_ip} for {company.name} → upload {upload.id}"
        )

        return Response(
            {
                "upload_id":  str(upload.id),
                "status":     upload.status,
                "filename":   upload.original_filename,
                "size_bytes": upload.file_size_bytes,
                "message":    (
                    "File received and queued for processing. "
                    "Poll the status URL for updates."
                ),
                "poll_url":   f"/api/excel/uploads/{upload.id}/status/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ExcelUploadListView(generics.ListAPIView):
    """
    GET /api/excel/uploads/
    List all Excel uploads with filtering.

    Query params:
        status  — RECEIVED, PARSING, COMPLETE, FAILED etc.
        method  — FOLDER, EMAIL, UPLOAD
        date_from, date_to
    """
    serializer_class   = ExcelUploadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ExcelUpload.objects.select_related("client").order_by("-created_at")
        s = self.request.query_params.get("status")
        m = self.request.query_params.get("method")
        if s:
            qs = qs.filter(status=s)
        if m:
            qs = qs.filter(method=m)
        return qs


class ExcelUploadDetailView(generics.RetrieveAPIView):
    """
    GET /api/excel/uploads/{id}/
    Full detail of one upload including all invoice rows.
    """
    serializer_class   = ExcelUploadDetailSerializer
    permission_classes = [IsAuthenticated]
    queryset           = ExcelUpload.objects.prefetch_related("invoices")


class ExcelUploadStatusView(APIView):
    """
    GET /api/excel/uploads/{id}/status/
    Lightweight status poll — returns just the counts and status.
    Used by the frontend to poll without loading all invoice rows.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            upload = ExcelUpload.objects.get(id=pk)
        except ExcelUpload.DoesNotExist:
            return Response({"error": "Upload not found"}, status=404)

        return Response({
            "id":              str(upload.id),
            "status":          upload.status,
            "filename":        upload.original_filename,
            "total_rows":      upload.total_rows,
            "valid_rows":      upload.valid_rows,
            "cleared_rows":    upload.cleared_rows,
            "failed_rows":     upload.failed_rows,
            "completion_rate": upload.completion_rate,
            "created_at":      upload.created_at,
            "completed_at":    upload.completed_at,
        })


class DownloadTemplateView(APIView):
    """
    GET /api/excel/template/
    Download the Link Options standard Excel invoice template.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            template_path = self._generate_template()
            response = FileResponse(
                open(template_path, "rb"),
                content_type=(
                    "application/vnd.openxmlformats-"
                    "officedocument.spreadsheetml.sheet"
                ),
            )
            response["Content-Disposition"] = (
                'attachment; filename="LinkOptions_Invoice_Template.xlsx"'
            )
            return response
        except Exception as e:
            logger.error(f"[WebUpload] Template generation failed: {e}")
            return Response(
                {"error": "Template generation failed"},
                status=500
            )

    def _generate_template(self) -> str:
        """Generate the standard Excel template and return its path."""
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        import tempfile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoices"

        # Header style
        header_fill = PatternFill(
            start_color="1B3A6B",
            end_color="1B3A6B",
            fill_type="solid"
        )
        header_font  = Font(color="FFFFFF", bold=True, size=11)
        header_align = Alignment(horizontal="center", vertical="center")
        thin_border  = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        headers = [
            ("A", "Invoice Number *",    18),
            ("B", "Invoice Date *",      15),
            ("C", "Buyer Name *",        25),
            ("D", "Buyer TIN",           18),
            ("E", "Buyer Address",       30),
            ("F", "Item Description *",  35),
            ("G", "Quantity *",          12),
            ("H", "Unit Price (excl VAT)*", 20),
            ("I", "VAT Rate %",          12),
            ("J", "VAT Amount",          15),
            ("K", "Gross Amount *",      18),
            ("L", "Currency",            10),
            ("M", "Notes",               20),
        ]

        for col, header, width in headers:
            cell          = ws[f"{col}1"]
            cell.value    = header
            cell.font     = header_font
            cell.fill     = header_fill
            cell.alignment = header_align
            cell.border   = thin_border
            ws.column_dimensions[col].width = width

        ws.row_dimensions[1].height = 20

        # Sample row
        sample_fill = PatternFill(
            start_color="EEF4FB", end_color="EEF4FB", fill_type="solid"
        )
        sample_data = [
            "INV-2025-001", "14/03/2025", "Financial Reporting Council",
            "31569955-0001", "Plot 17 Dar-es-Salaam St, Abuja",
            "Revenue Software License", 1, 1440000, 7.5, 108000, 1548000,
            "NGN", "Annual license fee"
        ]
        for i, val in enumerate(sample_data, start=1):
            cell          = ws.cell(row=2, column=i)
            cell.value    = val
            cell.fill     = sample_fill
            cell.border   = thin_border
            cell.alignment = Alignment(vertical="center")

        # Freeze header row
        ws.freeze_panes = "A2"

        # Add instructions sheet
        ws2        = wb.create_sheet("Instructions")
        ws2["A1"]  = "Link Options Invoice Template — Instructions"
        ws2["A1"].font = Font(bold=True, size=14, color="1B3A6B")
        instructions = [
            ("A3",  "MANDATORY FIELDS (marked with *)"),
            ("A4",  "Invoice Number:  Your own invoice reference number"),
            ("A5",  "Invoice Date:    Format DD/MM/YYYY or YYYY-MM-DD"),
            ("A6",  "Buyer Name:      Full company or individual name"),
            ("A7",  "Item Description: What was sold or provided"),
            ("A8",  "Quantity:        Number of units (use 1 for services)"),
            ("A9",  "Unit Price:      Price per unit EXCLUDING VAT (in NGN)"),
            ("A10", "Gross Amount:    Total amount INCLUDING VAT (in NGN)"),
            ("A12", "OPTIONAL FIELDS"),
            ("A13", "Buyer TIN:       Required for B2B invoices. Leave blank for B2C."),
            ("A14", "VAT Rate %:      Defaults to 7.5% if blank"),
            ("A15", "VAT Amount:      Calculated automatically if blank"),
            ("A16", "Currency:        Defaults to NGN if blank"),
            ("A18", "TIPS"),
            ("A19", "- One invoice per row"),
            ("A20", "- Do not change column headers"),
            ("A21", "- Save as .xlsx format"),
            ("A22", "- Maximum 10MB file size"),
            ("A23", "- Maximum 1000 rows per file"),
        ]
        for cell_ref, text in instructions:
            ws2[cell_ref] = text
        ws2.column_dimensions["A"].width = 65

        # Save to temp file
        tmp = tempfile.NamedTemporaryFile(
            suffix=".xlsx", delete=False
        )
        wb.save(tmp.name)
        tmp.close()
        return tmp.name


class ExcelHealthView(APIView):
    """GET /api/excel/health/ — Health check for Excel intake system."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.excel_intake.models import (
            FolderWatchConfig, EmailIngestConfig
        )
        return Response({
            "status":           "ok",
            "folder_watches":   FolderWatchConfig.objects.filter(is_active=True).count(),
            "email_configs":    EmailIngestConfig.objects.filter(is_active=True).count(),
            "total_uploads":    ExcelUpload.objects.count(),
            "pending_uploads":  ExcelUpload.objects.filter(
                status__in=[UploadStatus.RECEIVED, UploadStatus.PARSING]
            ).count(),
            "failed_uploads":   ExcelUpload.objects.filter(
                status=UploadStatus.FAILED
            ).count(),
            "max_file_size_mb": MAX_FILE_SIZE_MB,
        })
