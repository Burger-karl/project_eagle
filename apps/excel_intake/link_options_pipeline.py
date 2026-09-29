"""
Link Options XLS → FIRS UBL Pipeline
======================================
Chains three steps for Link Options custom .xls invoice files:

    1. LinkOptionsInvoiceParser  — reads the custom XLS template,
                                   extracts invoice fields per sheet
    2. ExcelFieldMapper          — maps to the 55 FIRS UBL fields
    3. UBLInvoiceBuilder         — builds the exact JSON payload
                                   ready to POST to FIRS via the APP

Usage (in Django shell or Celery task):

    from apps.excel_intake.link_options_pipeline import run_link_options_file

    results = run_link_options_file(
        file_path=r"C:\\invoices\\236-Alitheia.xls",
        company=company,   # ClientCompany instance
        dry_run=True,      # True = map only, don't submit to FIRS
    )

    for r in results:
        print(r['invoice_number'], r['status'], r.get('irn') or r.get('error'))
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("apps.excel_intake")


def run_link_options_file(
    company,
    file_path: str = None,
    file_bytes: bytes = None,
    filename:   str   = "",
    dry_run:    bool  = True,
) -> List[Dict]:
    """
    Parse a Link Options .xls invoice file and map every invoice
    to the FIRS UBL format.  In dry_run mode (default) the mapped
    payload is returned without submitting to FIRS — useful for
    verifying field mapping before going live.

    Args:
        company:    ClientCompany instance (supplies supplier TIN/name)
        file_path:  Path to the .xls file on disk
        file_bytes: Raw bytes (use when reading from memory/upload)
        filename:   Original filename for logging
        dry_run:    If True, stop after mapping — do not call FIRS API

    Returns:
        List of result dicts, one per invoice sheet:
        {
            "invoice_number": "236",
            "sheet":          "sage - international",
            "status":         "mapped" | "submitted" | "error",
            "mapped":         { ... all 55 FIRS UBL fields ... },
            "ubl_payload":    { ... exact JSON for FIRS API ... },
            "irn":            "IRN-xxx" | None,
            "error":          "error message" | None,
        }
    """
    from apps.excel_intake.link_options_parser import (
        LinkOptionsInvoiceParser, LinkOptionsParseError
    )
    from apps.excel_intake.excel_field_mapper import ExcelFieldMapper, MappingError
    from apps.firs.invoice_schema import UBLInvoiceBuilder
    from apps.invoices.models import InvoiceType

    # ── Step 1: Parse XLS file ────────────────────────────────
    try:
        parser = LinkOptionsInvoiceParser(
            file_path=file_path,
            file_bytes=file_bytes,
            filename=filename or (file_path or "upload"),
        )
        invoices = parser.parse()
        logger.info(
            f"[LOPipeline] Parsed {len(invoices)} invoice(s) "
            f"from '{filename or file_path}'"
        )
    except LinkOptionsParseError as e:
        logger.error(f"[LOPipeline] Parse failed: {e}")
        return [{"status": "error", "error": str(e), "invoice_number": None}]

    results = []

    for raw in invoices:
        inv_num = raw.get("invoice_number", "?")
        sheet   = raw.get("source_sheet", "")
        result  = {
            "invoice_number": inv_num,
            "sheet":          sheet,
            "status":         "error",
            "mapped":         None,
            "ubl_payload":    None,
            "irn":            None,
            "error":          None,
        }

        # ── Step 2: Map to FIRS UBL fields ───────────────────
        try:
            mapper = ExcelFieldMapper(company, raw)
            mapped = mapper.map()
            result["mapped"] = mapped
        except MappingError as e:
            result["error"] = f"Field mapping failed: {e}"
            logger.error(f"[LOPipeline] Invoice {inv_num}: {result['error']}")
            results.append(result)
            continue

        # ── Step 3: Build UBL JSON payload ────────────────────
        try:
            builder = UBLInvoiceBuilder(mapped)
            if mapped["invoice_type"] == InvoiceType.B2B:
                payload = builder.build_b2b()
            else:
                payload = builder.build_b2c()
            result["ubl_payload"] = payload
        except Exception as e:
            result["error"] = f"UBL build failed: {e}"
            logger.error(f"[LOPipeline] Invoice {inv_num}: {result['error']}")
            results.append(result)
            continue

        # ── Step 4: Submit to FIRS (skipped in dry_run) ───────
        if dry_run:
            result["status"] = "mapped"
            logger.info(
                f"[LOPipeline] Invoice {inv_num} mapped OK "
                f"(dry_run — not submitted to FIRS)"
            )
        else:
            try:
                from apps.firs.client import FIRSClient
                firs = FIRSClient(
                    api_key=getattr(company, "firs_api_key", None),
                    secret_key=getattr(company, "firs_secret_key", None),
                )
                response = firs.submit_invoice(payload)
                result["irn"]    = response.get("irn") or response.get("IRN")
                result["status"] = "submitted"
                logger.info(
                    f"[LOPipeline] Invoice {inv_num} submitted — "
                    f"IRN: {result['irn']}"
                )
            except Exception as e:
                result["status"] = "error"
                result["error"]  = f"FIRS submission failed: {e}"
                logger.error(f"[LOPipeline] Invoice {inv_num}: {result['error']}")

        results.append(result)

    return results


def print_results(results: List[Dict], show_full_payload: bool = False):
    """
    Pretty-print pipeline results to the terminal.
    Call this from the Django shell after run_link_options_file().
    """
    print(f"\n{'='*65}")
    print(f"  LINK OPTIONS PIPELINE RESULTS — {len(results)} invoice(s)")
    print(f"{'='*65}")

    for r in results:
        status_icon = {"mapped": "✓", "submitted": "🚀", "error": "✗"}.get(
            r["status"], "?"
        )
        print(f"\n  {status_icon}  Invoice {r['invoice_number']}  "
              f"[sheet: {r['sheet']}]  →  {r['status'].upper()}")

        if r["error"]:
            print(f"     ERROR: {r['error']}")
            continue

        m = r["mapped"]
        if m:
            print(f"     Supplier:   {m['supplier_name']}  TIN: {m['supplier_tin']}")
            print(f"     Buyer:      {m['buyer_name']}")
            print(f"     Type:       {m['invoice_type']}")
            print(f"     Date:       {m['invoice_date']}")
            print(f"     Net:        ₦{float(m['net_amount']):>14,.2f}")
            print(f"     VAT:        ₦{float(m['vat_amount']):>14,.2f}")
            print(f"     Gross:      ₦{float(m['gross_amount']):>14,.2f}")
            print(f"     Lines:      {len(m['lines'])}")
            for i, line in enumerate(m["lines"], 1):
                print(f"       Line {i}: {line['description'][:45]!r}")
                print(f"               qty={line['quantity']} | "
                      f"price=₦{line['unit_price']:,.2f} | "
                      f"net=₦{line['line_net_amount']:,.2f} | "
                      f"VAT {line['vat_rate']}% → {line['tax_category']}")

        if r["irn"]:
            print(f"     IRN:        {r['irn']}")

        if show_full_payload and r["ubl_payload"]:
            import json
            print(f"\n     UBL Payload:")
            print(json.dumps(r["ubl_payload"], indent=6, default=str))

    print(f"\n{'='*65}")
    passed = sum(1 for r in results if r["status"] != "error")
    failed = len(results) - passed
    print(f"  Summary: {passed} OK, {failed} failed")
    print(f"{'='*65}\n")