import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.web.presentation import case_view
from dongjiang_agent.workflow import DongjiangWorkflowHarness
from dongjiang_agent.workflow.dynamic import (
    build_contract_plan,
    build_credit_plan,
    validate_plan,
)
from dongjiang_agent.workflow.nodes import WorkflowNodes


SAFE_CONTRACT = """销售合同
甲方：东江集团
乙方：动态工作流客户
合同标的：精密组件。
合同金额：人民币1,000,000元，信用额度：1,000,000元。
付款及账期：月结60天。
知识产权：双方背景知识产权各自所有。
保密：双方不得披露商业秘密。
违约责任：违约方赔偿直接损失，累计不超过合同金额。
解除与终止：重大违约催告后可以解除。
争议解决：由深圳市人民法院管辖。
"""


class DynamicPlanTests(unittest.TestCase):
    def test_credit_plan_is_case_specific_and_allowlisted(self):
        plan = build_credit_plan(
            "DJ-PLAN-1",
            {
                "customer_type": "new",
                "business_type": "TKM",
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
            },
            [],
        )
        task_types = {item["task_type"] for item in plan["tasks"]}
        self.assertIn("credit_financial_analysis", task_types)
        self.assertIn("credit_rating_analysis", task_types)
        self.assertIn("credit_tkm_analysis", task_types)
        self.assertNotIn("credit_cooperation_analysis", task_types)
        self.assertEqual(plan["planner"], "controlled_runtime_planner")

    def test_contract_plan_adds_ai_only_when_available(self):
        contract = {"document_id": "DOC-1"}
        without_ai = build_contract_plan("DJ-PLAN-2", [contract], ai_available=False)
        with_ai = build_contract_plan("DJ-PLAN-3", [contract], ai_available=True)
        self.assertNotIn(
            "contract_ai_review",
            {item["task_type"] for item in without_ai["tasks"]},
        )
        self.assertIn(
            "contract_ai_review",
            {item["task_type"] for item in with_ai["tasks"]},
        )

    def test_unknown_task_and_cycle_are_rejected(self):
        base = {
            "agent": "credit",
            "tasks": [
                {
                    "task_id": "a",
                    "task_type": "untrusted_python",
                    "depends_on": [],
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "未授权"):
            validate_plan(base)
        cyclic = {
            "agent": "credit",
            "tasks": [
                {
                    "task_id": "a",
                    "task_type": "credit_data_completeness",
                    "depends_on": ["b"],
                },
                {
                    "task_id": "b",
                    "task_type": "credit_financial_analysis",
                    "depends_on": ["a"],
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "循环"):
            validate_plan(cyclic)


class DynamicWorkflowExecutionTests(unittest.TestCase):
    def test_credit_and_contract_agents_persist_visualizable_runs(self):
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
                        "customer_name": "动态运行测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )
                self.assertEqual(run.state["credit_verification"]["status"], "passed")
                run = harness.resume(run.case_id, {"action": "approve"})
                final = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [SAFE_CONTRACT]},
                )

            self.assertGreaterEqual(len(final.state["workflow_plans"]), 2)
            self.assertTrue(final.state["agent_runs"])
            self.assertEqual(
                final.state["contract_verifications"][0]["status"], "passed"
            )
            view = case_view(final.state)
            execution = view["agent_execution"]
            self.assertEqual(execution["mode"], "controlled_dynamic_workflow")
            self.assertEqual(
                {plan["agent"] for plan in execution["plans"]},
                {"credit", "contract"},
            )
            self.assertTrue(
                any(
                    node["task_type"] == "credit_verification"
                    for plan in execution["plans"]
                    for node in plan["nodes"]
                )
            )
            public_text = str(execution)
            self.assertNotIn("动态运行测试客户", public_text)
            self.assertNotIn("1,000,000", public_text)
            self.assertNotIn("trace", view)

    def test_failed_credit_verification_forces_supplement(self):
        nodes = WorkflowNodes.__new__(WorkflowNodes)
        plan = build_credit_plan(
            "DJ-VERIFY-FAIL",
            {"customer_type": "new", "business_type": "TKP"},
            [],
        )
        result = nodes.verify_credit(
            {
                "active_workflow_plan": plan,
                "credit_assessment": {
                    "score": 120,
                    "risk_level": "invalid",
                    "policy_version": "",
                    "requires_supplement": False,
                    "supplement_reasons": [],
                    "missing_fields": [],
                },
                "credit_analysis": {},
                "status": "credit_calculated",
                "credit_status": "calculated",
                "waiting_for": None,
            }
        )
        self.assertEqual(result["credit_verification"]["status"], "failed")
        self.assertEqual(result["credit_status"], "supplement_required")
        self.assertEqual(result["waiting_for"], "credit_supplement")
        self.assertTrue(result["credit_assessment"]["requires_supplement"])

    def test_failed_contract_verification_forces_manual_review(self):
        nodes = WorkflowNodes.__new__(WorkflowNodes)
        plan = build_contract_plan(
            "DJ-CONTRACT-VERIFY-FAIL",
            [{"document_id": "DOC-FAIL"}],
            ai_available=False,
        )
        result = nodes.verify_contract_reviews(
            {
                "active_workflow_plan": plan,
                "contract_facts": [{"document_id": "DOC-FAIL"}],
                "contract_reviews": [
                    {
                        "decision": "pass",
                        "approval_route": "normal",
                        "risk_level": "low",
                        "summary": "",
                        "credit_cross_check": {},
                        "findings": [],
                        "ai_assistance": {"findings": []},
                    }
                ],
            }
        )
        self.assertEqual(result["contract_verifications"][0]["status"], "failed")
        self.assertEqual(result["contract_reviews"][0]["decision"], "manual_review")
        self.assertEqual(
            result["contract_reviews"][0]["approval_route"], "finance_legal"
        )


if __name__ == "__main__":
    unittest.main()
