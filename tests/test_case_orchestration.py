from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.llm import CasePlanningAssistant
from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.web.presentation import case_view
from dongjiang_agent.workflow import DongjiangWorkflowHarness
from dongjiang_agent.workflow.orchestration import (
    assert_case_plan_integrity,
    build_case_plan,
    case_plan_snapshot,
)


class StubGateway:
    available = True
    model = "planner-test-model"

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete_json(self, instruction, *, system_prompt, operation):
        self.calls.append((instruction, system_prompt, operation))
        return self.response


class CaseOrchestrationPlanTests(unittest.TestCase):
    def snapshot(self, business_type="TKP"):
        return case_plan_snapshot(
            customer={"business_type": business_type, "customer_type": "new"},
            credit_file_count=1,
            use_cached_credit=True,
            runtime_snapshot={
                "ai_enabled": True,
                "tool_registry_provider": "local",
                "available_tools": ["document.extract"],
            },
        )

    def test_deterministic_plan_is_frozen_and_keeps_governance_tasks(self):
        plan = build_case_plan("DJ-CASE-PLAN", self.snapshot())
        task_types = {item["task_type"] for item in plan["tasks"]}

        self.assertTrue(plan["frozen"])
        self.assertEqual(plan["planner"], "controlled_case_planner")
        self.assertIn("credit_human_approval", task_types)
        self.assertIn("contract_human_review", task_types)
        self.assertIn("enterprise_writeback", task_types)
        self.assertNotIn("tkm_governance", task_types)
        assert_case_plan_integrity(plan)

    def test_tkm_plan_adds_special_governance(self):
        plan = build_case_plan("DJ-CASE-TKM", self.snapshot("TKM"))
        self.assertIn(
            "tkm_governance", {item["task_type"] for item in plan["tasks"]}
        )

    def test_incomplete_model_proposal_is_rejected_and_falls_back(self):
        proposal = {
            "status": "succeeded",
            "model": "planner-test-model",
            "selected_task_types": ["case_intake", "credit_agent_dispatch"],
            "rationale": "尝试省略审批",
        }
        plan = build_case_plan("DJ-CASE-FALLBACK", self.snapshot(), proposal=proposal)

        self.assertEqual(plan["planner"], "controlled_case_planner")
        self.assertFalse(plan["planner_assistance"]["proposal_adopted"])
        self.assertIn(
            "enterprise_writeback",
            {item["task_type"] for item in plan["tasks"]},
        )

    def test_unknown_model_task_rejects_the_whole_proposal(self):
        base = build_case_plan("DJ-CASE-BASE", self.snapshot())
        proposal = {
            "status": "succeeded",
            "model": "planner-test-model",
            "selected_task_types": [
                *[item["task_type"] for item in base["tasks"]],
                "approve_without_human",
            ],
        }
        plan = build_case_plan("DJ-CASE-UNKNOWN", self.snapshot(), proposal=proposal)

        self.assertEqual(plan["planner"], "controlled_case_planner")
        self.assertFalse(plan["planner_assistance"]["proposal_adopted"])
        self.assertNotIn(
            "approve_without_human",
            {item["task_type"] for item in plan["tasks"]},
        )

    def test_valid_model_proposal_can_omit_optional_supervision(self):
        base = build_case_plan("DJ-CASE-OPTIONAL-BASE", self.snapshot())
        core = [
            item["task_type"]
            for item in base["tasks"]
            if item["task_type"]
            not in {"document_quality_supervision", "enterprise_identity_supervision"}
        ]
        plan = build_case_plan(
            "DJ-CASE-OPTIONAL",
            self.snapshot(),
            proposal={
                "status": "succeeded",
                "model": "planner-test-model",
                "selected_task_types": core,
                "rationale": "仅保留核心治理链",
            },
        )

        self.assertEqual(plan["planner"], "llm_proposal_guarded")
        self.assertTrue(plan["planner_assistance"]["proposal_adopted"])
        self.assertNotIn(
            "enterprise_identity_supervision",
            {item["task_type"] for item in plan["tasks"]},
        )

    def test_tampered_plan_is_rejected(self):
        plan = build_case_plan("DJ-CASE-TAMPER", self.snapshot())
        tampered = deepcopy(plan)
        tampered["tasks"][0]["label"] = "篡改"
        with self.assertRaisesRegex(ValueError, "哈希不匹配"):
            assert_case_plan_integrity(tampered)

    def test_llm_planner_only_receives_privacy_safe_snapshot(self):
        gateway = StubGateway(
            '{"selected_task_types":["case_intake"],"rationale":"测试"}'
        )
        assistant = CasePlanningAssistant(gateway=gateway, enabled=True)
        result = assistant.propose(
            self.snapshot(),
            allowed_task_types=["case_intake"],
            required_task_types=["case_intake"],
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(gateway.calls[0][2], "case_planning")
        self.assertNotIn("customer_name", gateway.calls[0][0])


class CaseOrchestrationWorkflowTests(unittest.TestCase):
    def test_parent_plan_is_persisted_and_visualized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with DongjiangWorkflowHarness(
                checkpoint_path=root / "workflow.sqlite",
                repository=CaseRepository(root / "cases"),
                vault_dir=root / "vault",
                inbox_dir=root / "inbox",
                output_dir=root / "output",
            ) as harness:
                run = harness.start(
                    {
                        "customer_name": "主Agent计划测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )

            plan = run.state["orchestration_plan"]
            latest = {
                item["task_type"]: item
                for item in run.state["orchestration_runs"]
            }
            view = case_view(run.state)["agent_execution"]["orchestration_plan"]
            self.assertEqual(plan["agent"], "case_orchestrator")
            self.assertEqual(latest["credit_agent_dispatch"]["status"], "completed")
            self.assertEqual(latest["credit_human_approval"]["status"], "waiting")
            self.assertEqual(view["plan_id"], plan["plan_id"])
            self.assertTrue(any(item["executor_type"] == "human" for item in view["nodes"]))

    def test_completed_case_marks_parent_plan_and_persists_terminal_tasks(self):
        contract = """销售合同
甲方：东江集团
乙方：主Agent终态测试客户
合同标的：精密组件。
合同金额：人民币1,000,000元，信用额度：1,000,000元。
付款及账期：月结60天。
知识产权：双方背景知识产权各自所有。
保密：双方不得披露商业秘密。
违约责任：违约方赔偿直接损失，累计不超过合同金额。
解除与终止：重大违约催告后可以解除。
争议解决：由深圳市人民法院管辖。
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = CaseRepository(root / "cases")
            with DongjiangWorkflowHarness(
                checkpoint_path=root / "workflow.sqlite",
                repository=repository,
                vault_dir=root / "vault",
                inbox_dir=root / "inbox",
                output_dir=root / "output",
            ) as harness:
                run = harness.start(
                    {
                        "customer_name": "主Agent终态测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )
                run = harness.resume(run.case_id, {"action": "approve"})
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [contract]},
                )
                if run.waiting_for in {"finance_legal_review", "contract_approval"}:
                    run = harness.resume(run.case_id, {"action": "approve"})

            latest = {
                item["task_type"]: item["status"]
                for item in run.state["orchestration_runs"]
            }
            stored = repository.get_case(run.case_id)
            self.assertEqual(run.state["orchestration_plan"]["status"], "completed")
            self.assertIn(latest["contract_human_review"], {"completed", "skipped"})
            self.assertEqual(latest["enterprise_writeback"], "completed")
            self.assertEqual(latest["case_archive"], "completed")
            self.assertEqual(stored["orchestration_plan"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
