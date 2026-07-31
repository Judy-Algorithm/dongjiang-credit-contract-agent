import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dongjiang_agent.operations import AgentIncidentService
from dongjiang_agent.operations import AgentOperationsService
from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.security import AuthStore
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness
from dongjiang_agent.workflow.dynamic import audit_plan_execution
from dongjiang_agent.workflow.incidents import (
    automatic_incidents,
    detect_plan_incident,
    incident_sla,
)


class AgentIncidentAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = CaseRepository(self.root / "cases")
        self.checkpoint = self.root / "workflow.sqlite"
        self.auth_path = self.root / "auth.sqlite"
        self.admin = ActorContext("admin-1", ("admin",), "test", "管理员")
        self.harness = DongjiangWorkflowHarness(
            checkpoint_path=self.checkpoint,
            repository=self.repository,
            vault_dir=self.root / "vault",
            inbox_dir=self.root / "inbox",
            output_dir=self.root / "output",
            evidence_dir=self.root / "evidence",
            archive_dir=self.root / "archive",
            execution_dir=self.root / "executions",
        )
        with AuthStore(self.auth_path) as store:
            store.bootstrap_admin("admin", "管理员", "AdminPass123")

    def tearDown(self):
        self.harness.close()
        self.temp.cleanup()

    def _failed_state(self):
        run = self.harness.start(
            {
                "customer_name": "自动异常测试客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            },
            use_cached_credit=False,
            actor=self.admin,
        )
        plan = deepcopy(run.state["workflow_plans"][-1])
        results = [
            dict(item)
            for item in run.state["agent_task_results"]
            if item.get("plan_id") == plan["plan_id"]
            and item.get("task_id") != "credit.analysis.1"
        ]
        audit = audit_plan_execution(plan, run.state["agent_runs"], results)
        self.harness.graph.update_state(
            self.harness._config(run.case_id), {"execution_audits": [audit]}
        )
        return self.harness.get(run.case_id), plan

    def test_auto_open_is_deduplicated_and_persists_to_checkpoint(self):
        run, plan = self._failed_state()
        service = AgentIncidentService(
            case_root=self.root / "cases",
            auth_path=self.auth_path,
            checkpoint_path=self.checkpoint,
        )
        opened = service.sweep(now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        repeated = service.sweep(now=datetime(2026, 8, 1, 1, tzinfo=timezone.utc))

        self.assertEqual(opened["opened_incidents"], 1)
        self.assertEqual(repeated["opened_incidents"], 0)
        current = self.harness.get(run.case_id)
        incident = current.state["agent_incidents"][-1]
        self.assertEqual(incident["source"], "automatic")
        self.assertEqual(incident["severity"], "critical")
        self.assertEqual(incident["status"], "open")
        self.assertEqual(incident["policy_version"], "competition-agent-operations-v1")
        self.assertEqual(incident["response_due_at"], "2026-08-01T02:00:00+00:00")
        self.assertEqual(incident["resolution_due_at"], "2026-08-02T00:00:00+00:00")
        report = AgentOperationsService(case_root=self.root / "cases").report(
            now=datetime(2026, 8, 1, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(report["metrics"]["automatic_incidents"], 1)
        self.assertEqual(report["metrics"]["response_overdue"], 1)
        self.assertEqual(report["metrics"]["resolution_overdue"], 0)
        self.assertFalse(report["plans"][0]["incident_eligible"])
        with self.assertRaisesRegex(ValueError, "请先确认"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="assign",
                actor=self.admin,
                assignee={"user_id": "credit-1", "display_name": "信用甲"},
            )
        with AuthStore(self.auth_path) as store:
            events = store.list_audit(limit=100)
            admin = store.list_users()[0]
            notices = store.list_notifications(admin["user_id"])
        self.assertTrue(any(item["event_type"] == "agent.incident.auto_open" for item in events))
        self.assertTrue(any(item["category"] == "agent_incident" for item in notices["items"]))

    def test_resolved_same_fingerprint_is_not_reopened(self):
        run, plan = self._failed_state()
        state = dict(run.state)
        incidents, opened = automatic_incidents(state)
        self.assertEqual(len(opened), 1)
        incidents[-1]["status"] = "resolved"
        state["agent_incidents"] = incidents

        repeated_incidents, repeated = automatic_incidents(state)

        self.assertFalse(repeated)
        self.assertEqual(len(repeated_incidents), 1)

    def test_manual_action_cannot_reopen_resolved_same_fingerprint(self):
        run, plan = self._failed_state()
        self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="acknowledge",
            actor=self.admin,
        )
        self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="resolve",
            actor=self.admin,
            note="相同异常事实已完成处置",
        )
        with self.assertRaisesRegex(ValueError, "未发现新的异常变化"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="acknowledge",
                actor=self.admin,
            )

    def test_one_off_successful_retry_does_not_open_incident(self):
        run = self.harness.start(
            {
                "customer_name": "单次重试测试客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            },
            use_cached_credit=False,
            actor=self.admin,
        )
        state = deepcopy(run.state)
        plan = state["workflow_plans"][-1]
        target = next(
            item
            for item in state["agent_runs"]
            if item.get("plan_id") == plan["plan_id"]
        )
        target["attempt_count"] = 2
        target["status"] = "completed"

        self.assertIsNone(detect_plan_incident(state, plan))

    def test_repeated_analysis_degradation_opens_warning(self):
        run = self.harness.start(
            {
                "customer_name": "连续降级测试客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            },
            use_cached_credit=False,
            actor=self.admin,
        )
        state = deepcopy(run.state)
        plan = state["workflow_plans"][-1]
        analysis_ids = {
            item["task_id"] for item in plan["tasks"] if item["phase"] == "analysis"
        }
        first = next(
            item for item in state["agent_runs"] if item.get("task_id") in analysis_ids
        )
        first["status"] = "degraded"
        first["attempt_count"] = 2
        signal = detect_plan_incident(state, plan)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["severity"], "warning")
        self.assertEqual(signal["issue_types"], ["repeated_degradation"])

    def test_incident_sla_tracks_response_and_resolution_separately(self):
        incident = {
            "status": "open",
            "severity": "critical",
            "response_due_at": "2026-08-01T02:00:00+00:00",
            "resolution_due_at": "2026-08-02T00:00:00+00:00",
            "responded_at": "",
        }
        now = datetime(2026, 8, 1, 3, tzinfo=timezone.utc)
        sla = incident_sla(incident, now=now)
        self.assertEqual(sla["response"]["state"], "overdue")
        self.assertEqual(sla["resolution"]["state"], "on_track")
        incident["status"] = "acknowledged"
        incident["responded_at"] = "2026-08-01T01:00:00+00:00"
        acknowledged = incident_sla(incident, now=now)
        self.assertEqual(acknowledged["response"]["state"], "completed")
        self.assertEqual(acknowledged["resolution"]["state"], "on_track")

    def test_sla_alert_is_deduplicated_per_phase_and_level(self):
        run, _plan = self._failed_state()
        service = AgentIncidentService(
            case_root=self.root / "cases",
            auth_path=self.auth_path,
            checkpoint_path=self.checkpoint,
        )
        service.sweep(now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        first_overdue = service.sweep(
            now=datetime(2026, 8, 1, 3, tzinfo=timezone.utc)
        )
        repeated_overdue = service.sweep(
            now=datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
        )

        self.assertEqual(first_overdue["notifications_created"], 1)
        self.assertEqual(repeated_overdue["notifications_created"], 0)
        with AuthStore(self.auth_path) as store:
            events = store.list_audit(limit=100)
        alerts = [
            item for item in events
            if item["event_type"] == "agent.incident.sla_alert"
        ]
        self.assertEqual(len(alerts), 1)


if __name__ == "__main__":
    unittest.main()
