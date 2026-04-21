"""
FIRS UBL Invoice Schema Builder
Converts a mapped invoice dict into the exact JSON payload
FIRS expects — BIS Billing 3.0, 55 mandatory fields.

Input:  mapped dict from Sage200FieldMapper.map()
Output: UBL JSON payload ready to POST to FIRS MBS API
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("apps.firs")


class UBLInvoiceBuilder:
    """
    Builds the complete FIRS UBL invoice payload.

    Usage:
        builder = UBLInvoiceBuilder(mapped_invoice)
        payload = builder.build_b2b()   # or build_b2c()
    """

    def __init__(self, mapped: Dict):
        """
        Args:
            mapped: Output of Sage200FieldMapper.map()
        """
        self.m = mapped

    # ──────────────────────────────────────────────────────────
    # B2B INVOICE (requires FIRS clearance before sending to buyer)
    # ──────────────────────────────────────────────────────────

    def build_b2b(self) -> Dict:
        """Build a complete B2B clearance invoice payload."""
        payload = {
            # ── Category 1: Invoice Header ─────────────────────
            "UBLVersionID":    "2.1",
            "CustomizationID": self.m["customization_id"],
            "ProfileID":       self.m["profile_id"],
            "ID":              self.m["invoice_number"],
            "IssueDate":       self.m["invoice_date"],
            "InvoiceTypeCode": self.m["document_type"],
            "DocumentCurrencyCode": "NGN",
            "TaxCurrencyCode":      "NGN",

            # ── Category 2: Supplier ────────────────────────────
            "AccountingSupplierParty": self._build_party(
                tin=self.m["supplier_tin"],
                name=self.m["supplier_name"],
                address=self.m["supplier_address"],
                city=self.m.get("supplier_city", ""),
                country="NG",
                postal=self.m.get("supplier_postal", ""),
            ),

            # ── Category 3: Buyer ───────────────────────────────
            "AccountingCustomerParty": self._build_party(
                tin=self.m["buyer_tin"],
                name=self.m["buyer_name"],
                address=self.m["buyer_address"],
                city=self.m.get("buyer_city", ""),
                country=self.m.get("buyer_country", "NG"),
                postal=self.m.get("buyer_postal", ""),
            ),

            # ── Category 4: Tax Total ───────────────────────────
            "TaxTotal": [self._build_tax_total()],

            # ── Category 5: Monetary Totals ─────────────────────
            "LegalMonetaryTotal": self._build_monetary_totals(),

            # ── Category 6: Payment ─────────────────────────────
            "PaymentMeans": [{
                "PaymentMeansCode": self.m.get("payment_means_code", "30"),
            }],

            # ── Category 7: Line Items ──────────────────────────
            "InvoiceLine": self._build_lines(),
        }

        # Add due date only if present
        if self.m.get("due_date"):
            payload["DueDate"] = self.m["due_date"]

        # Add billing reference for credit/debit notes
        if self.m.get("original_irn"):
            payload["BillingReference"] = [{
                "InvoiceDocumentReference": {
                    "ID": self.m["original_irn"]
                }
            }]

        return payload

    # ──────────────────────────────────────────────────────────
    # B2C INVOICE (report to FIRS within 24 hours — no pre-clearance)
    # ──────────────────────────────────────────────────────────

    def build_b2c(self) -> Dict:
        """
        Build a B2C invoice report payload.
        Simpler than B2B — no buyer TIN required.
        """
        payload = {
            "UBLVersionID":    "2.1",
            "CustomizationID": self.m["customization_id"],
            "ProfileID":       self.m["profile_id"],
            "ID":              self.m["invoice_number"],
            "IssueDate":       self.m["invoice_date"],
            "InvoiceTypeCode": "380",
            "InvoiceSubtype":  "B2C",
            "DocumentCurrencyCode": "NGN",
            "TaxCurrencyCode":      "NGN",

            "AccountingSupplierParty": self._build_party(
                tin=self.m["supplier_tin"],
                name=self.m["supplier_name"],
                address=self.m["supplier_address"],
                city=self.m.get("supplier_city", ""),
                country="NG",
                postal=self.m.get("supplier_postal", ""),
            ),

            # B2C: buyer has no TIN — just a name
            "AccountingCustomerParty": {
                "Party": {
                    "PartyLegalEntity": {
                        "RegistrationName": self.m.get("buyer_name", "Consumer")
                    }
                }
            },

            "TaxTotal": [self._build_tax_total()],
            "LegalMonetaryTotal": self._build_monetary_totals(),
            "InvoiceLine": self._build_lines(),
        }
        return payload

    # ──────────────────────────────────────────────────────────
    # PARTY BUILDER (used for both supplier and buyer)
    # ──────────────────────────────────────────────────────────

    def _build_party(
        self,
        tin: str,
        name: str,
        address: str,
        city: str = "",
        country: str = "NG",
        postal: str = "",
    ) -> Dict:
        return {
            "Party": {
                "PartyTaxScheme": {
                    "CompanyID": tin,
                    "TaxScheme": {"ID": "VAT"}
                },
                "PartyLegalEntity": {
                    "RegistrationName": name
                },
                "PostalAddress": {
                    "StreetName":  address,
                    "CityName":    city,
                    "PostalZone":  postal,
                    "Country": {
                        "IdentificationCode": country
                    }
                }
            }
        }

    # ──────────────────────────────────────────────────────────
    # TAX TOTAL BUILDER
    # ──────────────────────────────────────────────────────────

    def _build_tax_total(self) -> Dict:
        total_vat = float(self.m["vat_amount"])
        net       = float(self.m["net_amount"])
        lines     = self.m.get("lines", [])

        # Group lines by tax category for the subtotal breakdown
        tax_subtotals = {}
        for line in lines:
            cat   = line.get("tax_category", "S")
            rate  = float(line.get("vat_rate", 7.5))
            key   = f"{cat}_{rate}"
            if key not in tax_subtotals:
                tax_subtotals[key] = {
                    "category": cat,
                    "rate":     rate,
                    "taxable":  0.0,
                    "tax":      0.0,
                }
            tax_subtotals[key]["taxable"] += float(line.get("line_net_amount", 0))
            tax_subtotals[key]["tax"]     += float(line.get("vat_amount", 0))

        subtotal_list = []
        for sub in tax_subtotals.values():
            subtotal_list.append({
                "TaxableAmount": {"currencyID": "NGN", "value": round(sub["taxable"], 2)},
                "TaxAmount":     {"currencyID": "NGN", "value": round(sub["tax"], 2)},
                "TaxCategory": {
                    "ID":      sub["category"],
                    "Percent": sub["rate"],
                    "TaxScheme": {"ID": "VAT"}
                }
            })

        return {
            "TaxAmount": {"currencyID": "NGN", "value": round(total_vat, 2)},
            "TaxSubtotal": subtotal_list,
        }

    # ──────────────────────────────────────────────────────────
    # MONETARY TOTALS BUILDER
    # ──────────────────────────────────────────────────────────

    def _build_monetary_totals(self) -> Dict:
        net   = round(float(self.m["net_amount"]),   2)
        vat   = round(float(self.m["vat_amount"]),   2)
        gross = round(float(self.m["gross_amount"]), 2)

        return {
            "LineExtensionAmount": {"currencyID": "NGN", "value": net},
            "TaxExclusiveAmount":  {"currencyID": "NGN", "value": net},
            "TaxInclusiveAmount":  {"currencyID": "NGN", "value": gross},
            "PayableAmount":       {"currencyID": "NGN", "value": gross},
        }

    # ──────────────────────────────────────────────────────────
    # LINE ITEMS BUILDER
    # ──────────────────────────────────────────────────────────

    def _build_lines(self) -> List[Dict]:
        lines = self.m.get("lines", [])
        ubl_lines = []

        for line in lines:
            net = round(float(line.get("line_net_amount", 0)), 2)
            vat = round(float(line.get("vat_amount", 0)), 2)
            qty = float(line.get("quantity", 1))
            price = round(float(line.get("unit_price", 0)), 4)

            ubl_lines.append({
                "ID": str(line["line_number"]),
                "InvoicedQuantity": {
                    "unitCode": line.get("unit_code", "EA"),
                    "value":    qty,
                },
                "LineExtensionAmount": {
                    "currencyID": "NGN",
                    "value":      net,
                },
                "TaxTotal": {
                    "TaxAmount": {"currencyID": "NGN", "value": vat},
                    "TaxSubtotal": [{
                        "TaxableAmount": {"currencyID": "NGN", "value": net},
                        "TaxAmount":     {"currencyID": "NGN", "value": vat},
                        "TaxCategory": {
                            "ID":      line.get("tax_category", "S"),
                            "Percent": float(line.get("vat_rate", 7.5)),
                            "TaxScheme": {"ID": "VAT"},
                        }
                    }]
                },
                "Item": {
                    "Description": line.get("description", ""),
                    "ClassifiedTaxCategory": {
                        "ID":      line.get("tax_category", "S"),
                        "Percent": float(line.get("vat_rate", 7.5)),
                        "TaxScheme": {"ID": "VAT"},
                    }
                },
                "Price": {
                    "PriceAmount": {
                        "currencyID": "NGN",
                        "value":      price,
                    }
                }
            })

        return ubl_lines
