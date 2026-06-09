"""
Sage 300 ERP (Accpac) ODBC Client
Reads invoice data directly from the client's Sage 300 MSSQL database.

Sage 300 uses a completely different database schema from Sage 200 Evolution.
It follows the Accpac naming convention — short 6-character table prefixes.

Key confirmed Sage 300 tables:
    ARINVOICE   — AR Invoice Headers
    ARINVOICED  — AR Invoice Detail Lines
    ARCUSTOMER  — Customer master data
    ARITEM      — AR item codes
    GLTRANSACT  — General Ledger transactions (for reference)
    CSCURRENCY  — Currency codes
    ARTAXGROUP  — Tax group definitions
    OEORDD      — OE Order Detail (for OE-origin invoices)

Sage 300 transaction types (type field in ARINVOICE):
    1  — Invoice
    2  — Debit Note
    3  — Credit Note
    4  — Interest Charge
    5  — Prepayment
    11 — Unapplied Cash

Authentication:
    Uses Windows Authentication (Trusted_Connection=yes)
    Same ODBC approach as Sage 200 — read only, never write directly.

Write-back of IRN/CSID goes through Sage 300 Web API:
    POST/PATCH to /AR/ARInvoiceBatches
"""

import pyodbc
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional

logger = logging.getLogger("apps.sage300")


class Sage300ODBCClient:
    """
    ODBC client for Sage 300 ERP (Accpac) databases.
    Read-only connection using Windows Authentication.

    Usage:
        client = Sage300ODBCClient(company)
        invoices = client.get_new_invoices(limit=10)
    """

    def __init__(self, company):
        self.company   = company
        self._conn_str = self._build_connection_string()

    def _build_connection_string(self) -> str:
        """
        Build ODBC connection string for Sage 300 database.
        Sage 300 database names are typically short codes like
        'SAMLTD', 'SAMINC', or the company's abbreviation.
        """
        company = self.company
        db      = company.sage300_mssql_database or company.mssql_database
        server  = company.sage300_mssql_server   or company.mssql_server
        driver  = getattr(company, "sage300_mssql_driver", "ODBC Driver 17 for SQL Server")
        trusted = getattr(company, "sage300_mssql_trusted", True)

        if trusted:
            return (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={db};"
                f"Trusted_Connection=yes;"
                f"Connection Timeout=10;"
            )
        username = getattr(company, "sage300_mssql_username", "")
        password = getattr(company, "sage300_mssql_password", "")
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={db};"
            f"UID={username};"
            f"PWD={password};"
            f"Connection Timeout=10;"
        )

    # ── CONNECTION ────────────────────────────────────────────

    @contextmanager
    def get_connection(self):
        """Open ODBC connection, yield it, then close cleanly."""
        conn = None
        try:
            db = (
                getattr(self.company, "sage300_mssql_database", None)
                or self.company.mssql_database
            )
            server = (
                getattr(self.company, "sage300_mssql_server", None)
                or self.company.mssql_server
            )
            logger.debug(f"[SAGE300 ODBC] Connecting to {server}/{db}")
            conn = pyodbc.connect(self._conn_str, timeout=30)
            conn.autocommit = True
            yield conn
        except pyodbc.Error as e:
            logger.error(f"[SAGE300 ODBC] Connection failed: {e}")
            raise Sage300ConnectionError(
                f"Cannot connect to Sage 300 database. Error: {e}\n"
                f"Check: Is SQL Server running? Is the Sage 300 database name correct?\n"
                f"Typical Sage 300 DB names: SAMLTD, SAMINC, or your company code."
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
        Read company details from Sage 300.
        Sage 300 stores company info in the CSCOMPANY table.
        """
        sql = """
            SELECT TOP 1
                COMPANYNAME     AS company_name,
                ADDRESS1        AS address1,
                ADDRESS2        AS address2,
                ADDRESS3        AS address3,
                ADDRESS4        AS city,
                ZIPCODE         AS postal_code,
                PHONE1          AS phone,
                TAXID           AS vat_number,
                COUNTRY         AS country
            FROM CSCOMPANY
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row:
                    return self._row_to_dict(cursor, row)
            except Exception as e:
                logger.warning(f"[SAGE300 ODBC] Could not read CSCOMPANY: {e}")
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
        Pull posted AR invoices from Sage 300.

        Sage 300 AR Invoice table: ARINVOICE
        Key columns:
            CNTBTCH     — Batch number
            CNTITEM     — Invoice entry number
            TEXTTRX     — Transaction type (1=Invoice, 2=DebitNote, 3=CreditNote)
            IDTRX       — Invoice number (e.g. 'INV-2025-001')
            DATEINVC    — Invoice date (YYYYMMDD integer format in older versions)
            DATEDUE     — Due date
            IDCUST      — Customer ID (links to ARCUSTOMER)
            AMTGROSDOC  — Gross amount in document currency
            AMTTAXDOC   — Tax amount in document currency
            AMTDUEDOC   — Amount due (payable)
            CODECURN    — Currency code
            RATEXCHR    — Exchange rate
            TEXTREF     — Reference/description

        Args:
            since: Only pull records with AUDTDATE after this datetime
            limit: Max records per batch

        Returns:
            List of invoice dicts with 'lines' key
        """
        date_filter = ""
        params      = []

        if since:
            # Sage 300 stores audit datetime as string 'YYYYMMDD' or datetime
            date_filter = "AND ar.AUDTDATE > ?"
            params.append(since)

        sql = f"""
            SELECT TOP {limit}
                ar.CNTBTCH          AS batch_number,
                ar.CNTITEM          AS entry_number,
                CAST(ar.CNTBTCH AS VARCHAR) + '-' +
                CAST(ar.CNTITEM AS VARCHAR) AS invoice_id,
                ar.IDTRX            AS invoice_number,
                ar.TEXTTRX          AS transaction_type,
                ar.DATEINVC         AS invoice_date_raw,
                ar.DATEDUE          AS due_date_raw,
                ar.TEXTREF          AS description,
                ar.IDCUST           AS customer_id,

                -- Customer details from ARCUSTOMER
                c.NAMECUST          AS customer_name,
                c.TEXTSTRE1         AS buyer_address1,
                c.TEXTSTRE2         AS buyer_address2,
                c.TEXTSTRE3         AS buyer_address3,
                c.NAMECITY          AS buyer_city,
                c.CODEPSTL          AS buyer_postal,
                c.CODECTRY          AS buyer_country,
                c.TEXTEMAILTO       AS buyer_email,

                -- Amounts
                ar.AMTGROSDOC       AS gross_amount,
                ar.AMTTAXDOC        AS vat_amount,
                ar.AMTDUEDOC        AS payable_amount,

                -- Currency
                ar.CODECURN         AS currency_code,
                ar.RATEXCHR         AS exchange_rate,

                -- Audit
                ar.AUDTDATE         AS audit_date,
                ar.AUDTTIME         AS audit_time

            FROM ARINVOICE ar
            LEFT JOIN ARCUSTOMER c ON ar.IDCUST = c.IDCUST
            WHERE ar.TEXTTRX IN (1, 2, 3)
            {date_filter}
            ORDER BY ar.AUDTDATE ASC, ar.CNTBTCH ASC, ar.CNTITEM ASC
        """

        invoices = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]

            for row in rows:
                inv = dict(zip(columns, row))

                # Parse Sage 300 date format (stored as integer YYYYMMDD)
                inv["invoice_date"] = self._parse_date(inv.get("invoice_date_raw"))
                inv["due_date"]     = self._parse_date(inv.get("due_date_raw"))

                # Calculate net amount
                gross = float(inv.get("gross_amount") or 0)
                vat   = float(inv.get("vat_amount")   or 0)
                inv["gross_amount"] = gross
                inv["vat_amount"]   = abs(vat)
                inv["net_amount"]   = round(gross - abs(vat), 2)

                # Pull line items
                inv["lines"] = self.get_invoice_lines(
                    conn,
                    inv.get("batch_number"),
                    inv.get("entry_number")
                )

                invoices.append(inv)

        logger.info(
            f"[SAGE300 ODBC] Pulled {len(invoices)} invoices for {self.company.name}"
        )
        return invoices

    def get_invoice_by_id(self, invoice_id: str) -> Optional[Dict]:
        """
        Fetch a single invoice by its composite ID (batch-entry).
        invoice_id format: "BATCH-ENTRY" e.g. "12-3"
        """
        try:
            parts = invoice_id.split("-")
            batch = int(parts[0])
            entry = int(parts[1])
        except (ValueError, IndexError):
            logger.error(f"[SAGE300 ODBC] Invalid invoice_id format: {invoice_id}")
            return None

        sql = """
            SELECT TOP 1
                ar.CNTBTCH          AS batch_number,
                ar.CNTITEM          AS entry_number,
                CAST(ar.CNTBTCH AS VARCHAR) + '-' +
                CAST(ar.CNTITEM AS VARCHAR) AS invoice_id,
                ar.IDTRX            AS invoice_number,
                ar.TEXTTRX          AS transaction_type,
                ar.DATEINVC         AS invoice_date_raw,
                ar.DATEDUE          AS due_date_raw,
                ar.TEXTREF          AS description,
                ar.IDCUST           AS customer_id,
                c.NAMECUST          AS customer_name,
                c.TEXTSTRE1         AS buyer_address1,
                c.TEXTSTRE2         AS buyer_address2,
                c.NAMECITY          AS buyer_city,
                c.CODEPSTL          AS buyer_postal,
                c.CODECTRY          AS buyer_country,
                c.TEXTEMAILTO       AS buyer_email,
                ar.AMTGROSDOC       AS gross_amount,
                ar.AMTTAXDOC        AS vat_amount,
                ar.AMTDUEDOC        AS payable_amount,
                ar.CODECURN         AS currency_code,
                ar.RATEXCHR         AS exchange_rate
            FROM ARINVOICE ar
            LEFT JOIN ARCUSTOMER c ON ar.IDCUST = c.IDCUST
            WHERE ar.CNTBTCH = ? AND ar.CNTITEM = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, [batch, entry])
            row = cursor.fetchone()
            if not row:
                return None
            inv = self._row_to_dict(cursor, row)
            inv["invoice_date"] = self._parse_date(inv.get("invoice_date_raw"))
            inv["due_date"]     = self._parse_date(inv.get("due_date_raw"))
            gross = float(inv.get("gross_amount") or 0)
            vat   = float(inv.get("vat_amount")   or 0)
            inv["gross_amount"] = gross
            inv["vat_amount"]   = abs(vat)
            inv["net_amount"]   = round(gross - abs(vat), 2)
            inv["lines"]        = self.get_invoice_lines(conn, batch, entry)
            return inv

    # ── INVOICE LINES ─────────────────────────────────────────

    def get_invoice_lines(
        self,
        conn,
        batch_number,
        entry_number
    ) -> List[Dict]:
        """
        Pull line items from ARINVOICED (AR Invoice Detail).
        Linked via CNTBTCH + CNTITEM composite key.

        ARINVOICED key columns:
            CNTBTCH     — Batch number (links to ARINVOICE)
            CNTITEM     — Entry number (links to ARINVOICE)
            CNTLINE     — Line number within the invoice
            IDITEM      — Item/stock code
            TEXTDESC    — Line description
            QTYINVC     — Quantity invoiced
            AMTPRIC     — Unit price
            AMTEXTN     — Line extension (qty × price)
            AMTTAX      — Tax amount on this line
            RATETAX     — Tax rate %
            IDACCTREV   — Revenue account code
        """
        if not batch_number or not entry_number:
            return []

        sql = """
            SELECT
                d.CNTLINE           AS line_number,
                d.IDITEM            AS item_code,
                d.TEXTDESC          AS description,
                d.QTYINVC           AS quantity,
                d.AMTPRIC           AS unit_price,
                d.AMTEXTN           AS line_net_amount,
                d.AMTTAX            AS vat_amount,
                d.RATETAX           AS vat_rate,
                d.IDACCTREV         AS revenue_account,
                d.UNITMEAS          AS unit_of_measure
            FROM ARINVOICED d
            WHERE d.CNTBTCH = ? AND d.CNTITEM = ?
            ORDER BY d.CNTLINE ASC
        """
        cursor = conn.cursor()
        cursor.execute(sql, [batch_number, entry_number])
        rows    = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # ── CUSTOMERS ─────────────────────────────────────────────

    def get_customer(self, customer_id: str) -> Optional[Dict]:
        """Fetch customer details from ARCUSTOMER by IDCUST."""
        sql = """
            SELECT
                IDCUST          AS customer_id,
                NAMECUST        AS customer_name,
                TEXTSTRE1       AS address1,
                TEXTSTRE2       AS address2,
                TEXTSTRE3       AS address3,
                NAMECITY        AS city,
                CODEPSTL        AS postal_code,
                CODECTRY        AS country,
                TEXTEMAILTO     AS email,
                TEXTPHONE1      AS phone,
                TAXEXEMNO       AS vat_number,
                CODECURN        AS currency_code
            FROM ARCUSTOMER
            WHERE IDCUST = ?
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, [customer_id])
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_dict(cursor, row)

    def get_all_customers(self) -> List[Dict]:
        """Return all active customers — used to populate BuyerTINMapping."""
        sql = """
            SELECT
                IDCUST          AS customer_id,
                NAMECUST        AS customer_name,
                TEXTEMAILTO     AS email,
                TAXEXEMNO       AS vat_number,
                TEXTPHONE1      AS phone,
                CODECTRY        AS country
            FROM ARCUSTOMER
            WHERE INACTIVE = 0
            ORDER BY NAMECUST
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    # ── DIAGNOSTICS ───────────────────────────────────────────

    def get_database_info(self) -> Dict:
        """Basic DB info for health check."""
        sql = "SELECT DB_NAME() AS db_name, @@SERVERNAME AS server_name"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
            return self._row_to_dict(cursor, row)

    def count_invoices(self) -> int:
        """Count total AR invoices in Sage 300."""
        sql = "SELECT COUNT(*) FROM ARINVOICE WHERE TEXTTRX IN (1, 2, 3)"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            return cursor.fetchone()[0]

    def get_sample_invoices(self, limit: int = 3) -> List[Dict]:
        """Pull recent invoices without lines — fast diagnostic method."""
        sql = f"""
            SELECT TOP {limit}
                ar.CNTBTCH          AS batch_number,
                ar.CNTITEM          AS entry_number,
                ar.IDTRX            AS invoice_number,
                ar.TEXTTRX          AS transaction_type,
                ar.DATEINVC         AS invoice_date_raw,
                c.NAMECUST          AS customer_name,
                ar.AMTGROSDOC       AS gross_amount,
                ar.AMTTAXDOC        AS vat_amount,
                ar.CODECURN         AS currency_code
            FROM ARINVOICE ar
            LEFT JOIN ARCUSTOMER c ON ar.IDCUST = c.IDCUST
            WHERE ar.TEXTTRX IN (1, 2, 3)
            ORDER BY ar.AUDTDATE DESC, ar.CNTBTCH DESC
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows    = cursor.fetchall()
            columns = [col[0] for col in cursor.description]
            result  = []
            for row in rows:
                inv = dict(zip(columns, row))
                inv["invoice_date"] = self._parse_date(inv.get("invoice_date_raw"))
                inv["gross_amount"] = float(inv.get("gross_amount") or 0)
                result.append(inv)
            return result

    # ── HELPERS ───────────────────────────────────────────────

    @staticmethod
    def _parse_date(raw_date) -> Optional[str]:
        """
        Parse Sage 300 date format.
        Sage 300 stores dates as:
            - Integer: 20250314 (YYYYMMDD)
            - String:  "20250314"
            - datetime object (when queried via ODBC)
        Returns ISO format string: "2025-03-14"
        """
        if raw_date is None:
            return None
        if hasattr(raw_date, "date"):
            return raw_date.date().isoformat()
        if hasattr(raw_date, "isoformat"):
            return raw_date.isoformat()
        raw_str = str(raw_date).strip()
        if len(raw_str) == 8 and raw_str.isdigit():
            return f"{raw_str[:4]}-{raw_str[4:6]}-{raw_str[6:8]}"
        return raw_str[:10] if len(raw_str) >= 10 else raw_str

    @staticmethod
    def _row_to_dict(cursor, row) -> Dict:
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))


# ── Exceptions ────────────────────────────────────────────────

class Sage300ConnectionError(Exception):
    """Raised when ODBC connection to Sage 300 fails."""
    pass


class Sage300DataError(Exception):
    """Raised when expected data is missing in Sage 300."""
    pass
