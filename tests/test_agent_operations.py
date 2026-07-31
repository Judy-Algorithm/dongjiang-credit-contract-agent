import json
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.operations import AgentOperationsService
from dongjiang_agent.workflow.dynamic import build_credit_plan


class AgentOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case_root = self.root / "cases"
        self.case_root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def save_case(self, payload):
        (self.case_root / f"{payload['case_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_report_aggregates_health_without_task_payloads(self):
        plan = build_credit_plan(
            "DJ-OPS-HEALTH",
            {"customer_type": "new", "business_type": "TKP"},
            [],
            runtime_snapshot={"credit_policy_version": "credit-v1"},
        )
        task = plan["tasks"][0]
        self.save_case(
            {
                "case_id": "DJ-OPS-HEALTH",
                "status": "credit_pending_approval",
                "customer": {
                    "customer_name": "运维客户",
                    "business_type": "TKP",
                },
                "owner": {"display_name": "信用甲"},
                "workflow_plans": [plan],
                "agent_runs": [
                    {
                        "plan_id": plan["plan_id"],
                        "task_id": task["task_id"],
                        "status": "degraded",
                        "attempt_count": 2,
                        "evidence_gate": "degraded",
                        "input_summary": "不要出现在运维聚合",
                        "output_summary": "不要出现在运维聚合",
                    }
                ],
                "execution_audits": [
                    {
                        "plan_id": plan["plan_id"],
                        "status": "non_conformant",
                        "integrity_valid": True,
                        "missing_tasks": ["credit.synthesis"],
                        "audited_at": "2026-07-31T08:00:00+00:00",
                    }
                ],
            }
        )

        report = AgentOperationsService(case_root=str(self.case_root)).report()
        self.assertEqual(report["metrics"]["plan_total"], 1)
        self.assertEqual(report["metrics"]["critical_plans"], 1)
        self.assertEqual(report["metrics"]["retry_count"], 1)
        self.assertEqual(report["plans"][0]["audit_status"], "non_conformant")
        self.assertIn("execution_deviation", report["plans"][0]["issue_types"])
        self.assertIn("evidence_degraded", report["plans"][0]["issue_types"])
        self.assertNotIn("不要出现在运维聚合", json.dumps(report, ensure_ascii=False))

    def test_legacy_plan_is_reported_separately_from_critical(self):
        self.save_case(
            {
                "case_id": "DJ-OPS-LEGACY",
                "status": "completed",
                "customer": {"customer_name": "历史客户", "business_type": "TKP"},
                "workflow_plans": [
                    {
                        "plan_id": "CREDIT-LEGACY",
                        "agent": "credit",
                        "label": "信用信审子 Agent",
                        "version": "1.0",
                        "tasks": [],
                    }
                ],
            }
        )
        report = AgentOperationsService(case_root=str(self.case_root)).report()
        self.assertEqual(report["metrics"]["legacy_plans"], 1)
        self.assertEqual(report["metrics"]["critical_plans"], 0)
        self.assertEqual(report["plans"][0]["severity"], "legacy")

    def test_tampered_plan_is_critical(self):
        plan = build_credit_plan(
            "DJ-OPS-TAMPER",
            {"customer_type": "new", "business_type": "TKP"},
            [],
        )
        plan["tasks"][0]["task_id"] = "tampered.task"
        self.save_case(
            {
                "case_id": "DJ-OPS-TAMPER",
                "status": "processing",
                "customer": {"customer_name": "篡改客户", "business_type": "TKP"},
                "workflow_plans": [plan],
            }
        )
        report = AgentOperationsService(case_root=str(self.case_root)).report()
        self.assertEqual(report["plans"][0]["integrity_status"], "invalid")
        self.assertEqual(report["plans"][0]["severity"], "critical")

    def test_reused_degraded_evidence_is_warning(self):
        plan = build_credit_plan(
            "DJ-OPS-EVIDENCE",
            {"customer_type": "new", "business_type": "TKP"},
            [],
        )
        task = plan["tasks"][0]
        self.save_case(
            {
                "case_id": "DJ-OPS-EVIDENCE",
                "status": "processing",
                "customer": {"customer_name": "证据降级客户", "business_type": "TKP"},
                "workflow_plans": [plan],
                "agent_runs": [
                    {
                        "plan_id": plan["plan_id"],
                        "task_id": task["task_id"],
                        "status": "reused",
                        "attempt_count": 1,
                        "evidence_gate": "degraded",
                    }
                ],
                "execution_audits": [
                    {
                        "plan_id": plan["plan_id"],
                        "status": "conformant",
                        "integrity_valid": True,
                    }
                ],
            }
        )
        report = AgentOperationsService(case_root=str(self.case_root)).report()
        self.assertEqual(report["plans"][0]["severity"], "warning")
        self.assertEqual(report["metrics"]["warning_plans"], 1)
        self.assertIn("evidence_degraded", report["plans"][0]["issue_types"])


if __name__ == "__main__":
    unittest.main()
