"""
The Sage 200 query can be read in batches: ask for N rows after a bookmark (AutoIdx).
Runs the real generated SQL against an in-memory SQLite copy of the tables.
"""
import sqlite3
from contextlib import contextmanager

import pytest

from apps.core.models import ClientCompany
from apps.sage200.odbc_client import Sage200ODBCClient
from tests.test_sage200.test_since_filter import _TopToLimit


def _client_over(db):
    client = Sage200ODBCClient(ClientCompany(name="T", mssql_server="s", mssql_database="d"))

    @contextmanager
    def connection():
        class Conn:
            def cursor(self):
                return _TopToLimit(db.cursor())
        yield Conn()

    client.get_connection = connection
    return client


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE Client (DCLink INT, Account TEXT, Name TEXT, Physical1 TEXT, Physical2 TEXT,
            Physical3 TEXT, Physical4 TEXT, Physical5 TEXT, PhysicalPC TEXT, Email TEXT);
        CREATE TABLE PostAR (AutoIdx INT, TxDate TEXT, DTStamp TEXT, Id TEXT, TrCodeID INT,
            Reference TEXT, cReference2 TEXT, AccountLink INT, Debit REAL, Credit REAL,
            Tax_Amount REAL, fExchangeRate REAL, iCurrencyID INT, InvNumKey INT);
        INSERT INTO Client (DCLink, Account, Name) VALUES (1, 'CUS001', 'Acme Customer');
        """
    )
    for i in range(1, 8):
        post(db, i, f"2026-09-{i:02d}")
    return db


def post(db, auto_idx, tx_date, debit=1000.0):
    db.execute(
        "INSERT INTO PostAR VALUES (?,?,?,'Inv',38,?, '',1,?,0,0,1,0,NULL)",
        (auto_idx, tx_date, tx_date + " 09:00:00", f"REF{auto_idx}", debit),
    )


def ids(rows):
    return [r["invoice_id"] for r in rows]


def test_limit_picks_how_many_rows_come_back(db):
    assert ids(_client_over(db).get_new_invoices(limit=3)) == [1, 2, 3]


def test_bookmark_returns_the_next_rows(db):
    client = _client_over(db)
    assert ids(client.get_new_invoices(limit=3, after_id=3)) == [4, 5, 6]
    assert ids(client.get_new_invoices(limit=3, after_id=6)) == [7]
    assert ids(client.get_new_invoices(limit=3, after_id=7)) == []


def test_walking_in_batches_visits_every_row_exactly_once(db):
    client, seen, bookmark = _client_over(db), [], 0
    while True:
        rows = client.get_new_invoices(limit=2, after_id=bookmark)
        if not rows:
            break
        seen += ids(rows)
        bookmark = max(ids(rows))
    assert seen == [1, 2, 3, 4, 5, 6, 7]


def test_an_invoice_posted_late_with_an_old_date_is_still_found(db):
    """Entered today, dated months ago: a date-ordered bookmark would skip it for good."""
    client = _client_over(db)
    bookmark = max(ids(client.get_new_invoices(limit=100)))          # caught up to row 7
    post(db, 8, "2025-01-15")                                          # backdated posting
    assert ids(client.get_new_invoices(limit=100, after_id=bookmark)) == [8]


def test_bookmark_and_since_can_be_combined(db):
    from datetime import datetime
    rows = _client_over(db).get_new_invoices(since=datetime(2026, 9, 3), limit=10, after_id=4)
    assert ids(rows) == [5, 6, 7]
