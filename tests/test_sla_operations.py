import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dongjiang_agent.operations import SLAService, case_sla
from dongjiang_agent.security import AuthStore


class SLAOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case_root = self.root / "cases"
        self.auth_path = self.root / "auth.sqlite"
        self.policy_path = self.root / "sla.json"
        self.policy = {
            "version": "test-v1",
            "tasks": {
                "credit_approval": {
                    "label": "信用审批",
                    "target_hours": 24,
                    "warning_before_hours": 6,
                },
                "sales_revision": {
                    "label": "修改合同",
                    "target_hours": 48,
                    "warning_before_hours": 12,
                },
            },
        }
        self.policy_path.write_text(
            json.dumps(self.policy, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def save_case(self, payload):
        self.case_root.mkdir(parents=True, exist_ok=True)
        target = self.case_root / f"{payload['case_id']}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_case_sla_uses_node_entry_trace_not_latest_unrelated_trace(self):
        case = {
            "case_id": "DJ-SLAENTRY1",
            "status": "blocked",
            "created_at": "2026-07-28T00:00:00+00:00",
            "trace": [
                {"ts": "2026-07-29T08:00:00+00:00", "stage": "decision.routed"},
                {"ts": "2026-07-30T10:00:00+00:00", "stage": "case.owner_assigned"},
            ],
        }
        result = case_sla(
            case,
            now=datetime(2026, 7, 31, 9, tzinfo=timezone.utc),
            policy=self.policy,
        )
        self.assertEqual(result["state"], "overdue")
        self.assertEqual(result["entered_at"], "2026-07-29T08:00:00+00:00")
        self.assertEqual(result["due_at"], "2026-07-31T08:00:00+00:00")
        self.assertEqual(result["remaining_hours"], -1.0)

    @patch("dongjiang_agent.operations.sla.SecurityEmailSender")
    def test_sweep_notifies_role_once_per_level_and_emails_non_blocking(self, sender_class):
        with AuthStore(self.auth_path) as store:
            credit = store.create_user(
                username="credit.sla",
                display_name="时效信用审批",
                email="credit-sla@example.com",
                password="CreditSla123",
                roles=["credit"],
                must_change_password=False,
            )
        self.save_case(
            {
                "case_id": "DJ-SLAROLE01",
                "status": "credit_pending_approval",
                "created_at": "2026-07-29T00:00:00+00:00",
                "customer": {"customer_name": "时效测试客户", "business_type": "TKP"},
                "owner": {"user_id": "sales-1", "display_name": "销售甲"},
                "trace": [
                    {
                        "ts": "2026-07-30T08:00:00+00:00",
                        "stage": "credit.approval_requested",
                    }
                ],
            }
        )
        sender = sender_class.return_value
        sender.configured = True
        sender.send_notification.side_effect = RuntimeError("SMTP unavailable")
        service = SLAService(
            policy_path=self.policy_path,
            case_root=self.case_root,
            auth_path=self.auth_path,
        )
        first = service.sweep(now=datetime(2026, 7, 31, 3, tzinfo=timezone.utc))
        second = service.sweep(now=datetime(2026, 7, 31, 9, tzinfo=timezone.utc))
        third = service.sweep(now=datetime(2026, 7, 31, 10, tzinfo=timezone.utc))
        self.assertEqual(first["due_soon"], 1)
        self.assertEqual(first["notifications_created"], 1)
        self.assertEqual(first["emails_sent"], 0)
        self.assertEqual(second["overdue"], 1)
        self.assertEqual(second["notifications_created"], 1)
        self.assertEqual(third["notifications_created"], 0)
        with AuthStore(self.auth_path) as store:
            notices = store.list_notifications(credit["user_id"])
            self.assertEqual(notices["unread"], 2)
            self.assertEqual(notices["items"][0]["category"], "sla")
            self.assertEqual(notices["items"][0]["link"], "/cases/DJ-SLAROLE01/action")

    def test_dashboard_groups_bottlenecks(self):
        self.save_case(
            {
                "case_id": "DJ-SLADASH01",
                "status": "credit_pending_approval",
                "created_at": "2026-07-30T08:00:00+00:00",
                "customer": {"customer_name": "看板客户", "business_type": "TKP"},
                "trace": [
                    {
                        "ts": "2026-07-30T08:00:00+00:00",
                        "stage": "credit.approval_requested",
                    }
                ],
            }
        )
        dashboard = SLAService(
            policy_path=self.policy_path,
            case_root=self.case_root,
            auth_path=self.auth_path,
        ).dashboard(now=datetime(2026, 7, 31, 3, tzinfo=timezone.utc))
        self.assertEqual(dashboard["metrics"]["total"], 1)
        self.assertEqual(dashboard["metrics"]["due_soon"], 1)
        self.assertEqual(dashboard["by_task"][0]["label"], "信用审批")
        self.assertEqual(dashboard["by_task"][0]["due_soon"], 1)


if __name__ == "__main__":
    unittest.main()
