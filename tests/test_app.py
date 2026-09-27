import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class WaterRightsFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_transfer_approval_usage_and_drought(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 400, "effective_date": "2026-06-01"}, "editor")
        self.assertEqual(self.db.available(source)["reserved_outgoing"], 400)
        approved = self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")
        usage = self.db.record_usage("meter-01", {"account_id": source, "amount": 150, "meter_event_id": "UP-JUL-1", "occurred_at": "2026-07-10"}, "meter")
        self.assertEqual(usage["amount"], 150)
        simulation = self.db.simulate_drought(1000, 0.3)
        self.assertAlmostEqual(sum(x["allocation"] for x in simulation["allocations"]) + simulation["unallocated"], 700)

    def test_duplicate_meter_event_and_pending_reservation(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 500, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaises(DomainError):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 1, "effective_date": "2026-06-01"}, "editor")
        self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能重复计水"):
            self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")

    def test_third_party_and_self_approval_conflicts(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        with self.assertRaisesRegex(DomainError, "最小留存"):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 501, "effective_date": "2026-06-01"}, "editor")
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 100, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            self.db.approve_transfer(transfer["id"], "alice", "reviewer")

    def test_usage_reversal_partial_and_limits(self):
        source = self.accounts["北区水库"]
        # July seasonal cap for upstream is 35% of 1000 = 350.
        first = self.db.record_usage("meter-01", {"account_id": source, "amount": 300, "meter_event_id": "UP-JUL-A", "occurred_at": "2026-07-05"}, "meter")
        before = self.db.available(source)
        self.assertEqual(before["used"], 400)  # 100 seeded in March + 300
        # Another booking under the same month is blocked by the seasonal cap.
        with self.assertRaisesRegex(DomainError, "季节配额"):
            self.db.record_usage("meter-01", {"account_id": source, "amount": 100, "meter_event_id": "UP-JUL-B", "occurred_at": "2026-07-20"}, "meter")

        # Only meter/editor may reverse, and reason + operator are mandatory.
        with self.assertRaisesRegex(DomainError, "只有计量员"):
            self.db.reverse_usage("carl", {"usage_id": first["id"], "amount": 100, "reason": "重复登记", "operator": "meter-01"}, "reviewer")
        with self.assertRaisesRegex(DomainError, "原因"):
            self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 100, "operator": "meter-01"}, "meter")

        rev1 = self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 100, "reason": "同一水泵事件重复登记", "operator": "张三"}, "meter")
        self.assertEqual(rev1["usage_id"], first["id"])
        self.assertEqual(self.db.available(source)["used"], 300)

        # Seasonal capacity is released: the 100 booking now fits.
        booked = self.db.record_usage("meter-01", {"account_id": source, "amount": 100, "meter_event_id": "UP-JUL-B", "occurred_at": "2026-07-20"}, "meter")
        self.assertEqual(booked["amount"], 100)

        # Second partial reversal; cumulative reversals cannot exceed the original.
        self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 50, "reason": "再次核对扣减", "operator": "张三"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能超过原取水量"):
            self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 151, "reason": "超额冲正", "operator": "张三"}, "meter")
        # Exactly the remaining reversible amount is allowed.
        self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 150, "reason": "冲完剩余部分", "operator": "张三"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能超过原取水量"):
            self.db.reverse_usage("meter-01", {"usage_id": first["id"], "amount": 0.01, "reason": "已冲完", "operator": "张三"}, "meter")

        # Used must never go negative: all 300 of the original were returned.
        self.assertEqual(self.db.available(source)["used"], 200)  # 100 March + 100 July-B

        ledger = {rec["usage"]["id"]: rec for acct in self.db.usage_ledger(source) for rec in acct["records"]}
        entry = ledger[first["id"]]
        self.assertEqual(len(entry["reversals"]), 3)
        self.assertAlmostEqual(entry["reversed_total"], 300)
        self.assertAlmostEqual(entry["remaining_reversible"], 0.0)

        actions = [a["action"] for a in self.db.audit()]
        self.assertEqual(actions.count("usage.reversed"), 3)
        detail = next(a for a in self.db.audit() if a["action"] == "usage.reversed")
        payload = json.loads(detail["details"])
        self.assertAlmostEqual(payload["remaining_reversible"], 0.0)
        self.assertEqual(payload["reason"], "冲完剩余部分")

        # Reversal must originate from an existing usage record.
        with self.assertRaisesRegex(DomainError, "原取水记录不存在"):
            self.db.reverse_usage("meter-01", {"usage_id": 99999, "amount": 1, "reason": "x", "operator": "张三"}, "meter")

    def test_reversal_persists_after_reopen(self):
        source = self.accounts["北区水库"]
        db_path = Path(self.tmp.name) / "test.db"
        usage = self.db.record_usage("meter-01", {"account_id": source, "amount": 80, "meter_event_id": "UP-AUG-1", "occurred_at": "2026-08-11"}, "meter")
        self.db.reverse_usage("meter-01", {"usage_id": usage["id"], "amount": 30, "reason": "重复登记", "operator": "李四"}, "meter")

        reopened = Database(db_path)
        self.assertEqual(reopened.available(source)["used"], 150)  # 100 seed + 80 - 30
        ledger = {rec["usage"]["id"]: rec for acct in reopened.usage_ledger(source) for rec in acct["records"]}
        entry = ledger[usage["id"]]
        self.assertAlmostEqual(entry["reversed_total"], 30)
        self.assertAlmostEqual(entry["remaining_reversible"], 50)
        self.assertEqual(entry["reversals"][0]["reason"], "重复登记")
        self.assertTrue(any(a["action"] == "usage.reversed" for a in reopened.audit()))


if __name__ == "__main__":
    unittest.main()
