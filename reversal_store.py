"""计量冲正存储层：只负责冲正表的建表与 SQL 存取，不含任何业务判定。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_id INTEGER NOT NULL REFERENCES usage_records(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount REAL NOT NULL CHECK(amount > 0),
    reason TEXT NOT NULL,
    handler TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_reversals_usage ON usage_reversals(usage_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert_reversal(conn: sqlite3.Connection, *, usage_id: int, account_id: int, amount: float,
                    reason: str, handler: str, created_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO usage_reversals(usage_id,account_id,amount,reason,handler,status,created_at)"
        " VALUES(?,?,?,?,?,'confirmed',?)",
        (usage_id, account_id, amount, reason, handler, created_at),
    )
    return int(cur.lastrowid)


def get_reversal(conn: sqlite3.Connection, reversal_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM usage_reversals WHERE id=?", (reversal_id,)).fetchone()


def reversed_total(conn: sqlite3.Connection, usage_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM usage_reversals WHERE usage_id=? AND status='confirmed'",
        (usage_id,),
    ).fetchone()
    return float(row["total"])


def month_net_usage(conn: sqlite3.Connection, account_id: int, year_month: str) -> float:
    """某账户某月的净取水量：已登记取水减去该月记录上已确认的冲正。"""
    gross = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
        (account_id, year_month),
    ).fetchone()["total"]
    reversed_ = conn.execute(
        """SELECT COALESCE(SUM(r.amount),0) total
           FROM usage_reversals r JOIN usage_records u ON u.id=r.usage_id
           WHERE r.account_id=? AND substr(u.occurred_at,1,7)=? AND r.status='confirmed'""",
        (account_id, year_month),
    ).fetchone()["total"]
    return float(gross) - float(reversed_)


def list_usage_with_reversals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT u.*, COALESCE((
               SELECT SUM(r.amount) FROM usage_reversals r
               WHERE r.usage_id=u.id AND r.status='confirmed'),0) AS reversed_total
           FROM usage_records u ORDER BY u.account_id, u.occurred_at, u.id"""
    ).fetchall()


def list_reversals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM usage_reversals ORDER BY usage_id,id").fetchall()
