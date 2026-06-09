"""
Sage 200 Evolution (Pastel Evolution) ODBC Client
Updated based on confirmed database structure from Link Options - Yemi.

Confirmed tables and columns:
    PostAR              — AR transaction headers
                          AutoIdx, TxDate, Id, AccountLink, TrCodeID,
                          Debit, Credit, Tax_Amount, fExchangeRate,
                          iCurrencyID, InvNumKey, Reference, cReference2
    Client              — Customer master (DCLink, Account, Name, Physical1-5)
    _btblInvoiceLines   — Line items (iInvoiceID links to PostAR.AutoIdx)
    TrCodes             — Transaction type codes (ID, Code, Description)

From sample data:
    TrCodeID = 38 confirmed as invoice type (Id='OInv', Debit > 0)
    InvNumKey links PostAR to _btblInvoiceLines.iInvoiceID
"""

import pyodbc
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional

logger = logging.getLogger("apps.sage200")


class Sage200ODBCClient:

    def __init__(self, company):
        self.company   = company
        self._conn_str = company.odbc_connection_string

    # ── CONNECTION ────────────────────────────────────────────

    @contextmanager
    def get_connection(self):
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
                f"Error: {e}"
            )
        finally:
            if conn:
                conn.close()

    def test_connection(self) -> bool:
        try:
            with self.get_connection() as conn:
                conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    # ── COMPANY INFO ──────────────────────────────────────────

    def get_company_info(self) -> Dict:
        """Read company details from _etblSystem."""
        sql = """
            SELECT TOP 1
                cOwnEntityName      AS company_name,
                cPhysicalAddress1   AS address1,
                cPhysicalAddress2   AS address2,
                cPhysicalAddress3   AS address3,
                cPhysicalAddress4   AS city,
                cPhysicalPostalCode AS postal_code,
                cTelephone          AS phone,
                cVATNo              AS vat_number
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
        Pull posted AR invoices directly from PostAR table.

        PostAR confirmed columns:
            AutoIdx, TxDate, Id, AccountLink, TrCodeID,
            Debit, Credit, Tax_Amount, fExchangeRate,
            iCurrencyID, InvNumKey, Reference, cReference2, DTStamp

        TrCodeID=38 confirmed as invoice type from sample data.
        We also pull credit notes (Credit > 0) and debit notes.
        """
        date_filter = ""
        params      = []

        if since:
            date_filter = "AND ar.TxDate > ?"
            params.append(since)

        sql = f"""
            SELECT TOP {limit}
                ar.AutoIdx          AS invoice_id,
                ar.TxDate           AS invoice_date,
                ar.DTStamp          AS created_at,
                ar.Id               AS tr_type_code,
                ar.TrCodeID         AS tr_code_id,
                ar.Reference        AS invoice_number,
                ar.cReference2      AS reference2,

                ar.AccountLink      AS account_link,
                c.Account           AS account_code,
                c.Name              AS customer_name,
                c.Physical1         AS buyer_address1,
                c.Physical2         AS buyer_address2,
                c.Physical3         AS buyer_address3,
                c.Physical4         AS buyer_city,
                c.Physical5         AS buyer_state,
                c.PhysicalPC        AS buyer_postal,
                c.Email             AS buyer_email,

                ar.Debit            AS debit_amount,
                ar.Credit           AS credit_amount,
                ar.Tax_Amount       AS vat_amount,
                ar.fExchangeRate    AS exchange_rate,
                ar.iCurrencyID      AS currency_id,
                ar.InvNumKey        AS inv_num_key

            FROM PostAR ar
            LEFT JOIN Client c ON ar.AccountLink = c.DCLink
            WHERE ar.Debit > 0 OR ar.Credit > 0
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

                # Calculate amounts
                debit  = float(inv.get("debit_amount")  or 0)
                credit = float(inv.get("credit_amount") or 0)
                vat    = float(inv.get("vat_amount")    or 0)

                inv["gross_amount"]       = debit if debit > 0 else credit
                inv["vat_amount"]         = abs(vat)
                inv["net_amount"]         = round(
                    inv["gross_amount"] - inv["vat_amount"], 2
                )
                inv["transaction_type"]   = 1 if debit > 0 else 2
                inv["currency_code"]      = "NGN"

                # Pull line items
                inv_num_key = inv.get("inv_num_key")
                inv["lines"] = (
                    self.get_invoice_lines(conn, inv_num_key)
                    if inv_num_key else []
                )

                invoices.append(inv)

        logger.info(
            f"[ODBC] Pulled {len(invoices)} invoices for {self.company.name}"
        )
        return invoices

    def get_invoice_by_id(self, invoice_id: str) -> Optional[Dict]:
        """Fetch a single invoice by PostAR.AutoIdx."""
        sql = """
            SELECT TOP 1
                ar.AutoIdx          AS invoice_id,
                ar.TxDate           AS invoice_date,
                ar.DTStamp          AS created_at,
                ar.Id               AS tr_type_code,
                ar.TrCodeID         AS tr_code_id,
                ar.Reference        AS invoice_number,
                ar.cReference2      AS reference2,
                ar.AccountLink      AS account_link,
                c.Account           AS account_code,
                c.Name              AS customer_name,
                c.Physical1         AS buyer_address1,
                c.Physical2         AS buyer_address2,
                c.Physical3         AS buyer_address3,
                c.Physical4         AS buyer_city,
                c.PhysicalPC        AS buyer_postal,
                c.Email             AS buyer_email,
                ar.Debit            AS debit_amount,
                ar.Credit           AS credit_amount,
                ar.Tax_Amount       AS vat_amount,
                ar.fExchangeRate    AS exchange_rate,
                ar.iCurrencyID      AS currency_id,
                ar.InvNumKey        AS inv_num_key
            FROM PostAR ar
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
            inv["gross_amount"]     = debit if debit > 0 else credit
            inv["vat_amount"]       = abs(vat)
            inv["net_amount"]       = round(inv["gross_amount"] - inv["vat_amount"], 2)
            inv["transaction_type"] = 1 if debit > 0 else 2
            inv["currency_code"]    = "NGN"
            inv["lines"]            = self.get_invoice_lines(
                conn, inv.get("inv_num_key")
            )
            return inv

    # ── INVOICE LINES ─────────────────────────────────────────

    def get_invoice_lines(self, conn, inv_num_key) -> List[Dict]:
        """
        Pull line items from _btblInvoiceLines.
        iInvoiceID links to PostAR.AutoIdx (confirmed from InvNumKey).
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
                iUnitsOfMeasureID               AS unit_of_measure_id
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
        sql = """
            SELECT
                DCLink      AS account_link,
                Account     AS account_code,
                Name        AS customer_name,
                Physical1   AS address1,
                Physical2   AS address2,
                Physical3   AS address3,
                Physical4   AS city,
                PhysicalPC  AS postal_code,
                Email       AS email
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
        """Return all customers — used to populate BuyerTINMapping."""
        sql = """
            SELECT
                DCLink      AS account_link,
                Account     AS account_code,
                Name        AS customer_name,
                Email       AS email
            FROM Client
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
        sql = "SELECT DB_NAME() AS db_name, @@SERVERNAME AS server_name"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
            return self._row_to_dict(cursor, row)

    def count_invoices(self) -> int:
        """Count AR transactions with amounts."""
        sql = "SELECT COUNT(*) FROM PostAR WHERE Debit > 0 OR Credit > 0"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            return cursor.fetchone()[0]

    def get_sample_invoices(self, limit: int = 3) -> List[Dict]:
        """Pull recent invoices without lines — fast diagnostic method."""
        sql = f"""
            SELECT TOP {limit}
                ar.AutoIdx          AS invoice_id,
                ar.Reference        AS invoice_number,
                ar.TxDate           AS invoice_date,
                ar.Id               AS tr_type_code,
                c.Name              AS customer_name,
                ar.Debit            AS debit_amount,
                ar.Credit           AS credit_amount,
                ar.Tax_Amount       AS vat_amount,
                ar.InvNumKey        AS inv_num_key
            FROM PostAR ar
            LEFT JOIN Client c ON ar.AccountLink = c.DCLink
            WHERE ar.Debit > 0 OR ar.Credit > 0
            ORDER BY ar.TxDate DESC, ar.AutoIdx DESC
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            result  = []
            for row in rows:
                inv = dict(zip(columns, row))
                debit  = float(inv.get("debit_amount")  or 0)
                credit = float(inv.get("credit_amount") or 0)
                inv["gross_amount"] = debit if debit > 0 else credit
                result.append(inv)
            return result

    @staticmethod
    def _row_to_dict(cursor, row) -> Dict:
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))


class Sage200ConnectionError(Exception):
    pass


class Sage200DataError(Exception):
    pass