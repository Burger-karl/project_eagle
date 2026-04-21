"""
Sage 200 Evolution (Pastel Evolution) ODBC Client
Reads invoice data directly from the client's MSSQL database
restored from the Link_options.bak file.

Tables confirmed from database inspection:
    _bvARTransactionsFull  — AR transactions enriched view (invoices + customer info)
    Client                 — Customer master data
    _btblInvoiceLines      — Invoice line items
    _etblSystem            — Company/system settings

Transaction types in Sage 200 Evolution:
    iTransactionType = 1  — Invoice
    iTransactionType = 2  — Credit Note
    iTransactionType = 3  — Debit Note
"""

import pyodbc
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional

logger = logging.getLogger("apps.sage200")


class Sage200ODBCClient:
    """
    ODBC client for Sage 200 Evolution databases.
    Connects using Windows Authentication (Trusted_Connection=yes).
    """

    def __init__(self, company):
        self.company   = company
        self._conn_str = company.odbc_connection_string

    # ── CONNECTION ────────────────────────────────────────────

    @contextmanager
    def get_connection(self):
        """Open ODBC connection, yield it, then close cleanly."""
        conn = None
        try:
            logger.debug(
                f"[ODBC] Connecting to "
                f"{self.company.mssql_server}/{self.company.mssql_database}"
            )
            conn = pyodbc.connect(self._conn_str, timeout=30)
            conn.autocommit = True
            yield conn
        except pyodbc.Error as e:
            logger.error(f"[ODBC] Connection failed for {self.company.name}: {e}")
            raise Sage200ConnectionError(
                f"Cannot connect to Sage 200 Evolution database "
                f"'{self.company.mssql_database}' on '{self.company.mssql_server}'.\n"
                f"Error: {e}\n"
                f"Check: Is SQL Server running? Is the database name correct? "
                f"Is Windows Authentication enabled?"
            )
        finally:
            if conn:
                conn.close()

    def test_connection(self) -> bool:
        """Quick connection health check."""
        try:
            with self.get_connection() as conn:
                conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    # ── COMPANY INFO ──────────────────────────────────────────

    def get_company_info(self) -> Dict:
        """
        Read the company details from _etblSystem.
        Used to populate the Supplier section of every UBL invoice.
        """
        sql = """
            SELECT TOP 1
                cOwnEntityName      AS company_name,
                cPhysicalAddress1   AS address1,
                cPhysicalAddress2   AS address2,
                cPhysicalAddress3   AS address3,
                cPhysicalAddress4   AS city,
                cPhysicalPostalCode AS postal_code,
                cTelephone          AS phone,
                cVATNo              AS vat_number,
                cRegNo              AS reg_number
            FROM _etblSystem
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row:
                    return self._row_to_dict(cursor, row)
            except Exception as e:
                logger.warning(f"[ODBC] Could not read _etblSystem: {e}")
        return {
            "company_name": self.company.name,
            "address1":     self.company.address,
        }

    # ── INVOICES — MAIN QUERY ─────────────────────────────────

    def get_new_invoices(
        self,
        since: Optional[datetime] = None,
        limit: int = 500
    ) -> List[Dict]:
        """
        Pull posted AR invoices from Sage 200 Evolution.

        Uses _bvARTransactionsFull which is an enriched view
        joining PostAR with customer, currency and transaction code data.

        Args:
            since: Only pull records with TxDate after this datetime
            limit: Max records per batch (default 500)

        Returns:
            List of invoice dicts, each with a 'lines' key
        """
        date_filter = ""
        params      = []

        if since:
            date_filter = "AND ar.TxDate > ?"
            params.append(since)

        sql = f"""
            SELECT TOP {limit}
                ar.AutoIdx              AS invoice_id,
                ar.InvNumber            AS invoice_number,
                ar.TxDate               AS invoice_date,
                ar.DTStamp              AS created_at,
                ar.iTransactionType     AS transaction_type,
                ar.TrCode               AS tr_code,
                ar.Description          AS description,
                ar.Reference            AS reference,
                ar.cReference2          AS reference2,

                ar.AccountLink          AS account_link,
                ar.Id                   AS account_code,
                c.Name                  AS customer_name,
                c.Physical1             AS buyer_address1,
                c.Physical2             AS buyer_address2,
                c.Physical3             AS buyer_address3,
                c.Physical4             AS buyer_city,
                c.Physical5             AS buyer_state,
                c.PhysicalPC            AS buyer_postal,
                c.EMail                 AS buyer_email,

                ar.Debit                AS debit_amount,
                ar.Credit               AS credit_amount,
                ar.Tax_Amount           AS vat_amount,
                ar.fForeignDebit        AS foreign_debit,
                ar.fForeignCredit       AS foreign_credit,
                ar.fForeignTax          AS foreign_vat,
                ar.CurrencyCode         AS currency_code,
                ar.fExchangeRate        AS exchange_rate,
                ar.InvNumKey            AS inv_num_key,
                ar.TaxCode              AS tax_code

            FROM _bvARTransactionsFull ar
            LEFT JOIN Client c ON ar.AccountLink = c.DCLink
            WHERE ar.iTransactionType IN (1, 2, 3)
            {date_filter}
            ORDER BY ar.TxDate ASC, ar.AutoIdx ASC
        """

        invoices = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]

            for row in rows:
                inv = dict(zip(columns, row))

                # Calculate net/gross from Debit/Credit columns
                debit  = float(inv.get("debit_amount")  or 0)
                credit = float(inv.get("credit_amount") or 0)
                vat    = float(inv.get("vat_amount")    or 0)

                inv["gross_amount"] = debit if debit > 0 else credit
                inv["vat_amount"]   = abs(vat)
                inv["net_amount"]   = round(inv["gross_amount"] - inv["vat_amount"], 2)

                # Pull lines via InvNumKey
                inv_num_key = inv.get("inv_num_key")
                inv["lines"] = self.get_invoice_lines(conn, inv_num_key) if inv_num_key else []

                invoices.append(inv)

        logger.info(
            f"[ODBC] Pulled {len(invoices)} invoices for {self.company.name}"
        )
        return invoices

    def get_invoice_by_id(self, invoice_id: str) -> Optional[Dict]:
        """Fetch a single invoice by its AutoIdx."""
        sql = """
            SELECT TOP 1
                ar.AutoIdx              AS invoice_id,
                ar.InvNumber            AS invoice_number,
                ar.TxDate               AS invoice_date,
                ar.DTStamp              AS created_at,
                ar.iTransactionType     AS transaction_type,
                ar.TrCode               AS tr_code,
                ar.Description          AS description,
                ar.Reference            AS reference,
                ar.cReference2          AS reference2,
                ar.AccountLink          AS account_link,
                ar.Id                   AS account_code,
                c.Name                  AS customer_name,
                c.Physical1             AS buyer_address1,
                c.Physical2             AS buyer_address2,
                c.Physical3             AS buyer_address3,
                c.Physical4             AS buyer_city,
                c.PhysicalPC            AS buyer_postal,
                c.EMail                 AS buyer_email,
                ar.Debit                AS debit_amount,
                ar.Credit               AS credit_amount,
                ar.Tax_Amount           AS vat_amount,
                ar.CurrencyCode         AS currency_code,
                ar.fExchangeRate        AS exchange_rate,
                ar.InvNumKey            AS inv_num_key
            FROM _bvARTransactionsFull ar
            LEFT JOIN Client c ON ar.AccountLink = c.DCLink
            WHERE ar.AutoIdx = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, [invoice_id])
            row = cursor.fetchone()
            if not row:
                return None
            inv    = self._row_to_dict(cursor, row)
            debit  = float(inv.get("debit_amount")  or 0)
            credit = float(inv.get("credit_amount") or 0)
            vat    = float(inv.get("vat_amount")    or 0)
            inv["gross_amount"] = debit if debit > 0 else credit
            inv["vat_amount"]   = abs(vat)
            inv["net_amount"]   = round(inv["gross_amount"] - inv["vat_amount"], 2)
            inv["lines"]        = self.get_invoice_lines(conn, inv.get("inv_num_key"))
            return inv

    # ── INVOICE LINES ─────────────────────────────────────────

    def get_invoice_lines(self, conn, inv_num_key) -> List[Dict]:
        """
        Pull line items from _btblInvoiceLines.
        Linked via iInvoiceID = PostAR.InvNumKey.
        """
        if not inv_num_key:
            return []

        sql = """
            SELECT
                ROW_NUMBER() OVER (ORDER BY idInvoiceLines) AS line_number,
                idInvoiceLines                  AS line_id,
                cDescription                    AS description,
                fQuantity                       AS quantity,
                fUnitPriceExcl                  AS unit_price_excl,
                fUnitPriceIncl                  AS unit_price_incl,
                fQuantityLineTotExcl            AS line_net_amount,
                fQuantityLineTotIncl            AS line_gross_amount,
                fQuantityLineTaxAmount          AS vat_amount,
                fTaxRate                        AS vat_rate,
                fLineDiscount                   AS line_discount,
                iStockCodeID                    AS stock_code_id,
                iUnitsOfMeasureID               AS unit_of_measure_id,
                fUnitPriceExclForeign           AS unit_price_excl_foreign,
                fQuantityLineTotExclForeign     AS line_net_foreign
            FROM _btblInvoiceLines
            WHERE iInvoiceID = ?
            ORDER BY idInvoiceLines ASC
        """
        cursor = conn.cursor()
        cursor.execute(sql, [inv_num_key])
        rows    = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # ── CUSTOMERS ─────────────────────────────────────────────

    def get_customer(self, account_link: int) -> Optional[Dict]:
        """Fetch customer details from Client table by DCLink."""
        sql = """
            SELECT
                DCLink          AS account_link,
                Account         AS account_code,
                Name            AS customer_name,
                Physical1       AS address1,
                Physical2       AS address2,
                Physical3       AS address3,
                Physical4       AS city,
                Physical5       AS state,
                PhysicalPC      AS postal_code,
                Contact         AS contact_person,
                EMail           AS email,
                Tel1            AS phone,
                VATNo           AS vat_number
            FROM Client
            WHERE DCLink = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, [account_link])
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_dict(cursor, row)

    def get_all_customers(self) -> List[Dict]:
        """Return all active customers — used to populate BuyerTINMapping."""
        sql = """
            SELECT
                DCLink      AS account_link,
                Account     AS account_code,
                Name        AS customer_name,
                EMail       AS email,
                VATNo       AS vat_number,
                Tel1        AS phone
            FROM Client
            WHERE bAccountActive = 1
            ORDER BY Name
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    # ── DIAGNOSTICS ───────────────────────────────────────────

    def get_database_info(self) -> Dict:
        """Basic DB info for health check endpoint."""
        sql = "SELECT DB_NAME() AS db_name, @@SERVERNAME AS server_name"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
            return self._row_to_dict(cursor, row)

    def count_invoices(self) -> int:
        """Count total AR transactions — useful for quick testing."""
        sql = """
            SELECT COUNT(*)
            FROM _bvARTransactionsFull
            WHERE iTransactionType IN (1, 2, 3)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            return cursor.fetchone()[0]

    def get_sample_invoices(self, limit: int = 3) -> List[Dict]:
        """
        Pull a small sample of the most recent invoices.
        Used for testing — no line items joined (faster).
        """
        sql = f"""
            SELECT TOP {limit}
                ar.AutoIdx          AS invoice_id,
                ar.InvNumber        AS invoice_number,
                ar.TxDate           AS invoice_date,
                ar.iTransactionType AS transaction_type,
                ar.Description      AS description,
                c.Name              AS customer_name,
                ar.Debit            AS debit_amount,
                ar.Credit           AS credit_amount,
                ar.Tax_Amount       AS vat_amount,
                ar.CurrencyCode     AS currency_code,
                ar.InvNumKey        AS inv_num_key
            FROM _bvARTransactionsFull ar
            LEFT JOIN Client c ON ar.AccountLink = c.DCLink
            WHERE ar.iTransactionType IN (1, 2, 3)
            ORDER BY ar.TxDate DESC, ar.AutoIdx DESC
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def _row_to_dict(cursor, row) -> Dict:
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))


# ── Exceptions ────────────────────────────────────────────────

class Sage200ConnectionError(Exception):
    """Raised when ODBC connection to Sage 200 Evolution fails."""
    pass


class Sage200DataError(Exception):
    """Raised when expected data is missing in Sage 200 Evolution."""
    pass