import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
from reversal_service import ReversalService


class ReversalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.db_path)
        self.accounts = seed_demo(self.db)
        self.service = ReversalService(self.db)
        self.source = self.accounts["北区水库"]  # quota 1000，7 月季节上限 0.35

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, amount, event, day):
        return self.db.record_usage(
            "meter-01",
            {"account_id": self.source, "amount": amount, "meter_event_id": event, "occurred_at": day},
            "meter",
        )

    def account(self):
        return next(a for a in self.db.list_accounts() if a["id"] == self.source)

    def test_partial_reversals_capped_by_original(self):
        usage = self.record(200, "M-REV-1", "2026-08-05")
        first = self.service.create_reversal(
            usage["id"], "meter-02", {"amount": 80, "reason": "同一水泵重复登记", "handler": "张三"}, "meter")
        self.assertEqual(first["remaining_reversible"], 120)
        second = self.service.create_reversal(
            usage["id"], "meter-02", {"amount": 80, "reason": "同一水泵重复登记", "handler": "张三"}, "meter")
        self.assertEqual(second["remaining_reversible"], 40)
        # 累计冲正不能超过原取水量
        with self.assertRaisesRegex(DomainError, "不能超过原取水量"):
            self.service.create_reversal(
                usage["id"], "meter-02", {"amount": 41, "reason": "再冲一次", "handler": "张三"}, "meter")
        # 已用水量同步减少（种子已用 100 + 200 - 160 = 140），可用额度同步恢复
        self.assertAlmostEqual(self.account()["used"], 140)
        self.assertAlmostEqual(self.db.available(self.source)["available"], 860)

    def test_reversal_frees_monthly_seasonal_quota(self):
        usage = self.record(300, "M-JUL-1", "2026-07-05")  # 7 月季节上限 350
        with self.assertRaisesRegex(DomainError, "季节配额"):
            self.record(100, "M-JUL-2", "2026-07-06")
        self.service.create_reversal(
            usage["id"], "meter-02", {"amount": 100, "reason": "水泵重复登记", "handler": "李四"}, "meter")
        again = self.record(100, "M-JUL-2", "2026-07-06")  # 净额 200+100=300，未超 350
        self.assertEqual(again["amount"], 100)

    def test_reason_handler_required_and_audit_trail(self):
        usage = self.record(50, "M-AUD-1", "2026-08-10")
        with self.assertRaisesRegex(DomainError, "原因"):
            self.service.create_reversal(
                usage["id"], "meter-02", {"amount": 10, "reason": "  ", "handler": "王五"}, "meter")
        with self.assertRaisesRegex(DomainError, "冲正量"):
            self.service.create_reversal(
                usage["id"], "meter-02", {"amount": 0, "reason": "重复", "handler": "王五"}, "meter")
        reversal = self.service.create_reversal(
            usage["id"], "meter-02", {"amount": 10, "reason": "事件编号填错", "handler": "王五"}, "meter")
        entry = self.db.audit()[0]
        self.assertEqual(entry["action"], "usage.reversed")
        details = json.loads(entry["details"])
        # 审计保留原单、冲正单与剩余可冲量
        self.assertEqual(details["meter_event_id"], "M-AUD-1")
        self.assertEqual(details["original_amount"], 50)
        self.assertEqual(details["reversal_id"], reversal["id"])
        self.assertEqual(details["reversal_amount"], 10)
        self.assertEqual(details["remaining_reversible"], 40)
        self.assertEqual(details["handler"], "王五")

    def test_grouped_records_survive_service_restart(self):
        usage = self.record(120, "M-PER-1", "2026-08-15")
        self.service.create_reversal(
            usage["id"], "meter-02", {"amount": 30, "reason": "重复登记", "handler": "赵六"}, "meter")
        # 重开服务（同一数据库文件重新实例化）
        reopened = ReversalService(Database(self.db_path))
        groups = reopened.list_by_account()
        north = next(g for g in groups if g["account_id"] == self.source)
        record = next(u for u in north["usage"] if u["meter_event_id"] == "M-PER-1")
        self.assertEqual(record["reversed_total"], 30)
        self.assertEqual(record["remaining_reversible"], 90)
        self.assertEqual(record["reversals"][0]["reason"], "重复登记")
        self.assertEqual(record["reversals"][0]["handler"], "赵六")

    def test_role_and_missing_record_guards(self):
        usage = self.record(20, "M-SEC-1", "2026-08-20")
        with self.assertRaises(DomainError) as ctx:
            self.service.create_reversal(
                usage["id"], "guest", {"amount": 5, "reason": "重复", "handler": "钱七"}, "viewer")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(DomainError) as ctx:
            self.service.create_reversal(
                9999, "meter-02", {"amount": 5, "reason": "重复", "handler": "钱七"}, "meter")
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
