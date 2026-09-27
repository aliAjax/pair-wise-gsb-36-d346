"""Metering reversal (冲正) support, split into storage and judgment layers.

ReversalStore only knows how to persist and read reversal documents and
net-usage figures. CorrectionService owns the rules: who may reverse a usage
record, how much can still be returned, and how the reversal moves account
balances inside one transaction.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from common import DomainError, utcnow

if TYPE_CHECKING:
    from app import Database


class ReversalStore:
    """Storage layer: usage records, reversal documents and net usage."""

    def __init__(self, db: "Database"):
        self.db = db

    def get_usage(self, conn: sqlite3.Connection, usage_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM usage_records WHERE id=?", (usage_id,)).fetchone()

    def reversed_total(self, conn: sqlite3.Connection, usage_id: int) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_reversals WHERE usage_id=?",
            (usage_id,),
        ).fetchone()
        return float(row["total"])

    def net_month_usage(self, conn: sqlite3.Connection, account_id: int, month: str) -> float:
        """Net amount booked against an account in YYYY-MM: 原取水 - 已冲正."""
        gross = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
            (account_id, month),
        ).fetchone()["total"]
        reversed_amount = conn.execute(
            """SELECT COALESCE(SUM(r.amount),0) total
               FROM usage_reversals r JOIN usage_records u ON u.id=r.usage_id
               WHERE r.account_id=? AND substr(u.occurred_at,1,7)=?""",
            (account_id, month),
        ).fetchone()["total"]
        return float(gross) - float(reversed_amount)

    def insert_reversal(self, conn: sqlite3.Connection, usage_id: int, account_id: int,
                        amount: float, reason: str, operator: str) -> int:
        cur = conn.execute(
            "INSERT INTO usage_reversals(usage_id,account_id,amount,reason,operator,created_at) VALUES(?,?,?,?,?,?)",
            (usage_id, account_id, amount, reason, operator, utcnow()),
        )
        return int(cur.lastrowid)

    def get_reversal(self, conn: sqlite3.Connection, reversal_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM usage_reversals WHERE id=?", (reversal_id,)).fetchone()

    def list_reversals(self, conn: sqlite3.Connection, usage_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM usage_reversals WHERE usage_id=? ORDER BY id",
            (usage_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def usage_ledger(self, account_id: int | None = None) -> list[dict[str, Any]]:
        """Original usage documents grouped by account, with reversals and
        the amount still available to reverse (剩余可冲量)."""
        with self.db.connect() as conn:
            sql = "SELECT * FROM usage_records"
            params: tuple[Any, ...] = ()
            if account_id is not None:
                sql += " WHERE account_id=?"
                params = (account_id,)
            sql += " ORDER BY account_id, id"
            usages = conn.execute(sql, params).fetchall()
            accounts = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")}

            grouped: dict[int, dict[str, Any]] = {}
            for usage in usages:
                acct_id = int(usage["account_id"])
                bucket = grouped.setdefault(acct_id, {
                    "account_id": acct_id,
                    "account_name": accounts.get(acct_id, {}).get("name", f"#{acct_id}"),
                    "region": accounts.get(acct_id, {}).get("region", ""),
                    "records": [],
                })
                reversed_total = self.reversed_total(conn, int(usage["id"]))
                original_amount = float(usage["amount"])
                bucket["records"].append({
                    "usage": dict(usage),
                    "reversals": self.list_reversals(conn, int(usage["id"])),
                    "reversed_total": reversed_total,
                    "remaining_reversible": max(0.0, original_amount - reversed_total),
                })
        return list(grouped.values())


class CorrectionService:
    """Judgment layer: rules for reversing an erroneously booked usage record."""

    def __init__(self, db: "Database"):
        self.db = db
        self.store = db.reversals

    def reverse_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以发起计量冲正", 403)
        try:
            usage_id = int(payload.get("usage_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("取水记录编号和冲正量必须是数值") from exc
        reason = str(payload.get("reason", "")).strip()
        operator = str(payload.get("operator", "")).strip() or actor
        if amount <= 0:
            raise DomainError("冲正量必须大于 0")
        if not reason:
            raise DomainError("冲正原因不能为空")
        if not operator:
            raise DomainError("经办人不能为空")

        store = self.store
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            usage = store.get_usage(conn, usage_id)
            if not usage:
                raise DomainError("原取水记录不存在，冲正必须从原取水记录发起", 404)
            already = store.reversed_total(conn, usage_id)
            original_amount = float(usage["amount"])
            remaining = original_amount - already
            if amount > remaining + 1e-9:
                raise DomainError(
                    f"累计冲正不能超过原取水量（原量 {original_amount:g}，已冲 {already:g}，剩余可冲 {max(0.0, remaining):g}）",
                    409,
                )
            account = conn.execute(
                "SELECT * FROM accounts WHERE id=?", (int(usage["account_id"]),)
            ).fetchone()
            if not account:
                raise DomainError("水权账户不存在", 404)

            reversal_id = store.insert_reversal(
                conn, usage_id, int(usage["account_id"]), amount, reason, operator
            )
            # Confirmation immediately returns the water to both the account
            # balance and the seasonal quota for the original record's month.
            conn.execute("UPDATE accounts SET used=used-? WHERE id=?", (amount, int(usage["account_id"])))
            new_reversed = already + amount
            self.db._audit(
                conn, actor, "usage.reversed", "usage_record", usage_id,
                {
                    "reversal_id": reversal_id,
                    "meter_event_id": usage["meter_event_id"],
                    "original_amount": original_amount,
                    "amount": amount,
                    "reversed_total": new_reversed,
                    "remaining_reversible": max(0.0, original_amount - new_reversed),
                    "occurred_at": usage["occurred_at"],
                    "reason": reason,
                    "operator": operator,
                },
            )
            row = store.get_reversal(conn, reversal_id)
        return dict(row)
