"""
DigiTax Invoice Schema Builder
Converts a mapped invoice dict (from Sage200FieldMapper) into
the exact payload DigiTax API expects.

DigiTax uses its own schema — NOT raw UBL/XML.
Their API handles UBL conversion and NRS submission internally.

Reference: https://ng.docs.digitax.tech/reference
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("apps.firs")


class DigiTaxInvoiceBuilder:
    """
    Builds DigiTax-compatible invoice payloads from mapped invoice data.

    Usage:
        builder = DigiTaxInvoiceBuilder(mapped, party_id, line_ids)
        payload = builder.build()
    """

    def __init__(
        self,
        mapped: Dict,
        party_id: Optional[str] = None,
        line_ids: Optional[List[Dict]] = None,
    ):
        self.mapped   = mapped
        self.party_id = party_id
        self.line_ids = line_ids or []

    def build(self) -> Dict:
        """Build the complete DigiTax invoice payload."""
        payload = {
            "invoice_number": self.mapped["invoice_number"],
            "invoice_date":   str(self.mapped["invoice_date"]),
            "currency":       "NGN",
            "invoice_type":   self.mapped.get("invoice_type", "B2B").lower(),
            "supplier_tin":   self.mapped["supplier_tin"],

            # Financial totals
            "net_amount":    float(self.mapped["net_amount"]),
            "vat_amount":    float(self.mapped["vat_amount"]),
            "gross_amount":  float(self.mapped["gross_amount"]),

            # Lines
            "lines": self.line_ids,
        }

        # Add buyer info for B2B
        if self.party_id:
            payload["buyer_party_id"] = self.party_id
        if self.mapped.get("buyer_tin"):
            payload["buyer_tin"] = self.mapped["buyer_tin"]
        if self.mapped.get("buyer_name") and not self.party_id:
            payload["buyer_name"] = self.mapped["buyer_name"]

        # Add due date if present
        if self.mapped.get("due_date"):
            payload["due_date"] = str(self.mapped["due_date"])

        # Add original IRN reference for credit/debit notes
        if self.mapped.get("original_irn"):
            payload["original_irn"] = self.mapped["original_irn"]
            payload["invoice_type"] = self.mapped.get("document_type", "381")

        return payload

    @staticmethod
    def build_party(mapped: Dict) -> Dict:
        """Build DigiTax party payload from mapped invoice data."""
        return {
            "name":    mapped.get("buyer_name", "Consumer"),
            "tin":     mapped.get("buyer_tin"),
            "address": mapped.get("buyer_address", ""),
            "email":   mapped.get("buyer_email", ""),
            "type":    "business" if mapped.get("buyer_tin") else "individual",
        }

    @staticmethod
    def build_item(line: Dict) -> Dict:
        """Build DigiTax item payload from a mapped invoice line."""
        return {
            "name":        line.get("description", "Item"),
            "description": line.get("description", "Item"),
            "unit_price":  float(line.get("unit_price", 0)),
            "tax_rate":    float(line.get("vat_rate", 7.5)),
            "unit_code":   line.get("unit_code", "EA"),
            "currency":    "NGN",
        }
