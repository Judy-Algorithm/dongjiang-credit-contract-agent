import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness
from dongjiang_agent.workflow.dynamic import audit_plan_execution


SAFE_CONTRACT = """销售合同
甲方：东江集团
乙方：异常处置测试客户
合同标的：精密注塑组件。
合同金额：人民币1,000,000元，信用额度：1,000,000元。
付款及账期：月结60天。
知识产权：双方背景知识产权各自所有。
保密：双方不得披露商业秘密。
违约责任：违约方赔偿直接损失，累计不超过合同金额。
解除与终止：重大违约催告后可以解除。
争议解决：由深圳市人民法院管辖。
"""


class AgentIncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.harness = DongjiangWorkflowHarness(
            checkpoint_path=self.root / "workflow.sqlite",
            repository=CaseRepository(self.root / "cases"),
            vault_dir=self.root / "vault",
            inbox_dir=self.root / "inbox",
            output_dir=self.root / "output",
        )
        self.admin = ActorContext("admin-1", ("admin",), "web", "管理员")

    def tearDown(self):
        self.harness.close()
        self.temp.cleanup()

    def _failed_credit_run(self):
        run = self.harness.start(
            {
                "customer_name": "异常处置测试客户",
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
            self.harness._config(run.case_id),
            {"execution_audits": [audit]},
        )
        return self.harness.get(run.case_id), plan

    def test_healthy_plan_cannot_open_incident(self):
        run = self.harness.start(
            {
                "customer_name": "健康计划客户",
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
        plan = run.state["workflow_plans"][-1]
        with self.assertRaisesRegex(ValueError, "未发现可处置"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="acknowledge",
                actor=self.admin,
            )

    def test_incident_assignment_rerun_and_resolution_preserve_official_decision(self):
        run, plan = self._failed_credit_run()
        official_assessment = deepcopy(run.state["credit_assessment"])
        official_status = run.status
        official_waiting = run.waiting_for

        run, incident = self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="acknowledge",
            actor=self.admin,
            note="确认存在执行结果缺失",
        )
        self.assertEqual(incident["status"], "acknowledged")
        self.assertTrue(incident["responded_at"])

        run, incident = self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="assign",
            actor=self.admin,
            assignee={"user_id": "credit-1", "display_name": "信用甲"},
            note="交由信用团队核查",
        )
        self.assertEqual(incident["status"], "assigned")

        run, incident = self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="rerun",
            actor=self.admin,
            task_id="credit.analysis.1",
            note="在隔离副本重新执行缺失节点",
        )
        rerun = incident["rerun_history"][-1]
        self.assertEqual(incident["status"], "rerun_completed")
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual(rerun["execution_audit"], "conformant")
        self.assertFalse(rerun["official_state_changed"])
        self.assertEqual(run.state["credit_assessment"], official_assessment)
        self.assertEqual(run.status, official_status)
        self.assertEqual(run.waiting_for, official_waiting)

        with self.assertRaisesRegex(ValueError, "处理说明"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="resolve",
                actor=self.admin,
            )
        run, incident = self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="resolve",
            actor=self.admin,
            note="候选核验通过，保留原审批状态并关闭异常",
        )
        self.assertEqual(incident["status"], "resolved")
        self.assertEqual(run.state["credit_assessment"], official_assessment)
        self.assertTrue(
            any(
                item.get("stage") == "agent.incident.resolve"
                for item in run.state["trace"]
            )
        )

    def test_decision_node_cannot_be_rerun(self):
        run, plan = self._failed_credit_run()
        self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="acknowledge",
            actor=self.admin,
        )
        with self.assertRaisesRegex(ValueError, "禁止直接重跑"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="rerun",
                actor=self.admin,
                task_id="credit.scoring",
                note="尝试重跑决策节点",
            )

    def test_incident_must_be_acknowledged_before_other_actions(self):
        run, plan = self._failed_credit_run()
        with self.assertRaisesRegex(ValueError, "请先确认"):
            self.harness.manage_agent_incident(
                run.case_id,
                plan_id=plan["plan_id"],
                action="rerun",
                actor=self.admin,
                task_id="credit.analysis.1",
                note="跳过确认直接重跑",
            )

    def test_contract_candidate_rerun_preserves_official_review(self):
        run = self.harness.start(
            {
                "customer_name": "合同异常处置客户",
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
        run = self.harness.resume(
            run.case_id,
            {"action": "approve", "comment": "同意授信建议"},
            actor=self.admin,
        )
        run = self.harness.resume(
            run.case_id,
            {"action": "submit_contract", "contract_texts": [SAFE_CONTRACT]},
            actor=self.admin,
        )
        plan = next(
            deepcopy(item)
            for item in reversed(run.state["workflow_plans"])
            if item.get("agent") == "contract"
        )
        task = next(item for item in plan["tasks"] if item["phase"] == "analysis")
        official_reviews = deepcopy(run.state["contract_reviews"])
        official_status = run.status
        official_waiting = run.waiting_for
        results = [
            dict(item)
            for item in run.state["agent_task_results"]
            if item.get("plan_id") == plan["plan_id"]
            and item.get("task_id") != task["task_id"]
        ]
        audit = audit_plan_execution(plan, run.state["agent_runs"], results)
        self.harness.graph.update_state(
            self.harness._config(run.case_id),
            {"execution_audits": [audit]},
        )
        self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="acknowledge",
            actor=self.admin,
        )

        rerun, incident = self.harness.manage_agent_incident(
            run.case_id,
            plan_id=plan["plan_id"],
            action="rerun",
            actor=self.admin,
            task_id=task["task_id"],
            note="在隔离副本重新执行合同分析节点",
        )

        candidate = incident["rerun_history"][-1]
        self.assertEqual(candidate["status"], "completed")
        self.assertEqual(candidate["execution_audit"], "conformant")
        self.assertFalse(candidate["official_state_changed"])
        self.assertEqual(rerun.state["contract_reviews"], official_reviews)
        self.assertEqual(rerun.status, official_status)
        self.assertEqual(rerun.waiting_for, official_waiting)


if __name__ == "__main__":
    unittest.main()
