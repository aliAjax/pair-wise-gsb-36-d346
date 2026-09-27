"""计量冲正判定层：校验冲正请求，并在同一事务内写冲正单、扣减已用水量。

从原取水记录发起；同一记录可分次冲回，累计不能超过原取水量。
确认后同步减少账户已用水量，当月季节用量由存储层按净额实时计算。
"""
from __future__ import annotations

from typing import Any

import reversal_store
from common import DomainError, utcnow

EPS = 1e-9


class ReversalService:
    def __init__(self, db: Any):
        self.db = db

    def create_reversal(self, usage_id: int, actor: str, payload: dict[str, Any],
                        role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员或配额管理员可以发起冲正", 403)
        try:
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("冲正量必须是数值") from exc
        if amount <= 0:
            raise DomainError("冲正量必须大于 0")
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise DomainError("冲正原因不能为空")
        handler = str(payload.get("handler", "") or actor).strip()
        if not handler:
            raise DomainError("经办人不能为空")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            usage = conn.execute("SELECT * FROM usage_records WHERE id=?", (usage_id,)).fetchone()
            if not usage:
                raise DomainError("原取水记录不存在", 404)
            original = float(usage["amount"])
            already = reversal_store.reversed_total(conn, usage_id)
            remaining = original - already
            if amount > remaining + EPS:
                raise DomainError(
                    f"累计冲正量不能超过原取水量，该记录剩余可冲 {remaining:g}", 409
                )
            reversal_id = reversal_store.insert_reversal(
                conn, usage_id=usage_id, account_id=int(usage["account_id"]),
                amount=amount, reason=reason, handler=handler, created_at=utcnow(),
            )
            # 冲正确认：同步减少已用水量（可用额度随之恢复）；
            # 季节用量不单独记账，按取水-冲正净额判定，因此该月季节额度同步释放。
            conn.execute("UPDATE accounts SET used=used-? WHERE id=?",
                         (amount, usage["account_id"]))
            left = remaining - amount
            self.db._audit(conn, actor, "usage.reversed", "usage", usage_id, {
                "reversal_id": reversal_id,
                "account_id": int(usage["account_id"]),
                "meter_event_id": usage["meter_event_id"],
                "occurred_at": usage["occurred_at"],
                "original_amount": original,
                "reversal_amount": amount,
                "reversed_total": already + amount,
                "remaining_reversible": left,
                "reason": reason,
                "handler": handler,
            })
            row = reversal_store.get_reversal(conn, reversal_id)
        result = dict(row)
        result["original_amount"] = original
        result["reversed_total"] = already + amount
        result["remaining_reversible"] = left
        return result

    def list_by_account(self) -> list[dict[str, Any]]:
        """按账户组装：原单、冲正单、已冲回与剩余可冲量，供页面展开。"""
        with self.db.connect() as conn:
            accounts = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            usage_rows = reversal_store.list_usage_with_reversals(conn)
            reversal_rows = reversal_store.list_reversals(conn)
        reversals_by_usage: dict[int, list[dict[str, Any]]] = {}
        for row in reversal_rows:
            reversals_by_usage.setdefault(int(row["usage_id"]), []).append(dict(row))
        usage_by_account: dict[int, list[dict[str, Any]]] = {}
        for row in usage_rows:
            item = dict(row)
            reversed_total = float(row["reversed_total"])
            item["reversed_total"] = reversed_total
            item["remaining_reversible"] = float(row["amount"]) - reversed_total
            item["reversals"] = reversals_by_usage.get(int(row["id"]), [])
            usage_by_account.setdefault(int(row["account_id"]), []).append(item)
        groups = []
        for account in accounts:
            records = usage_by_account.get(int(account["id"]))
            if not records:
                continue
            groups.append({
                "account_id": int(account["id"]),
                "name": account["name"],
                "region": account["region"],
                "holder": account["holder"],
                "usage": records,
            })
        return groups
