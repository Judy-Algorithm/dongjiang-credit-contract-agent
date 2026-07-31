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

    def test_incident_metrics_retry_allowlist_and_sensitive_history_are_redacted(self):
        plan = build_credit_plan(
            "DJ-OPS-INCIDENT",
            {"customer_type": "new", "business_type": "TKP"},
            [],
        )
        analysis_task = next(
            item for item in plan["tasks"] if item["phase"] == "analysis"
        )
        self.save_case(
            {
                "case_id": "DJ-OPS-INCIDENT",
                "status": "credit_pending_approval",
                "customer": {
                    "customer_name": "异常聚合客户",
                    "business_type": "TKP",
                },
                "workflow_plans": [plan],
                "execution_audits": [
                    {
                        "plan_id": plan["plan_id"],
                        "status": "non_conformant",
                        "integrity_valid": True,
                    }
                ],
                "agent_incidents": [
                    {
                        "incident_id": "AINC-OPS-1",
                        "plan_id": plan["plan_id"],
                        "agent": "credit",
                        "status": "rerun_completed",
                        "assignee": {
                            "user_id": "credit-1",
                            "display_name": "信用甲",
                            "email": "secret@example.com",
                        },
                        "latest_note": "敏感处理备注不得出现在运维报表",
                        "history": [{"note": "敏感历史备注不得出现在运维报表"}],
                        "rerun_history": [
                            {
                                "task_id": analysis_task["task_id"],
                                "task_type": analysis_task["task_type"],
                                "status": "completed",
                                "attempt_count": 1,
                                "evidence_gate": "passed",
                                "execution_audit": "conformant",
                                "candidate_summary": {
                                    "score": 82,
                                    "risk_level": "low",
                                    "secret_payload": "候选敏感原文不得出现",
                                },
                                "note": "重跑备注不得出现在运维报表",
                                "official_state_changed": False,
                            }
                        ],
                    }
                ],
            }
        )

        report = AgentOperationsService(case_root=str(self.case_root)).report()
        row = report["plans"][0]
        self.assertEqual(report["metrics"]["open_incidents"], 1)
        self.assertEqual(report["metrics"]["resolved_incidents"], 0)
        self.assertEqual(row["incident"]["assignee"]["display_name"], "信用甲")
        self.assertEqual(row["incident"]["rerun_count"], 1)
        retryable_types = {item["task_type"] for item in row["retryable_tasks"]}
        self.assertIn(analysis_task["task_type"], retryable_types)
        self.assertNotIn("credit_scoring", retryable_types)
        self.assertNotIn("credit_verification", retryable_types)
        report_text = json.dumps(report, ensure_ascii=False)
        for secret in (
            "secret@example.com",
            "敏感处理备注不得出现在运维报表",
            "敏感历史备注不得出现在运维报表",
            "候选敏感原文不得出现",
            "重跑备注不得出现在运维报表",
        ):
            self.assertNotIn(secret, report_text)

        case = json.loads(
            (self.case_root / "DJ-OPS-INCIDENT.json").read_text(encoding="utf-8")
        )
        case["agent_incidents"][0]["status"] = "resolved"
        self.save_case(case)
        metrics = AgentOperationsService(case_root=str(self.case_root)).report()[
            "metrics"
        ]
        self.assertEqual(metrics["open_incidents"], 0)
        self.assertEqual(metrics["resolved_incidents"], 1)


if __name__ == "__main__":
    unittest.main()
