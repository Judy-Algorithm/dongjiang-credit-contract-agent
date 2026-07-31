from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository, TaskExecutionStore
from dongjiang_agent.web.presentation import case_view
from dongjiang_agent.workflow import DongjiangWorkflowHarness
from dongjiang_agent.workflow.dynamic import (
    assert_plan_integrity,
    audit_plan_execution,
    build_contract_plan,
    build_credit_plan,
    plan_task,
    task_idempotency_key,
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
    def test_frozen_plan_identity_is_stable_and_version_sensitive(self):
        customer = {"customer_type": "new", "business_type": "TKP"}
        snapshot = {
            "credit_policy_version": "credit-v1",
            "credit_policy_hash": "a" * 64,
        }
        first = build_credit_plan(
            "DJ-STABLE-1", customer, [], runtime_snapshot=snapshot
        )
        second = build_credit_plan(
            "DJ-STABLE-1", customer, [], runtime_snapshot=snapshot
        )
        changed = build_credit_plan(
            "DJ-STABLE-1",
            customer,
            [],
            runtime_snapshot={**snapshot, "credit_policy_version": "credit-v2"},
        )

        self.assertTrue(first["frozen"])
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["spec_hash"], second["spec_hash"])
        self.assertNotEqual(first["plan_id"], changed["plan_id"])

    def test_tampered_frozen_plan_is_rejected(self):
        plan = build_credit_plan(
            "DJ-TAMPER-1", {"customer_type": "new", "business_type": "TKP"}, []
        )
        tampered = deepcopy(plan)
        tampered["tasks"][0]["input_refs"].append("unapproved-input")

        with self.assertRaisesRegex(ValueError, "哈希不匹配"):
            assert_plan_integrity(tampered)

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

    def test_execution_audit_detects_missing_unexpected_and_duplicate_tasks(self):
        plan = build_contract_plan(
            "DJ-AUDIT-1", [{"document_id": "DOC-1"}], ai_available=False
        )
        results = [
            {
                "plan_id": plan["plan_id"],
                "task_id": task["task_id"],
                "evidence_gate": "passed",
            }
            for task in plan["tasks"]
        ]
        self.assertEqual(
            audit_plan_execution(plan, [], results)["status"], "conformant"
        )

        missing = audit_plan_execution(plan, [], results[1:])
        self.assertEqual(missing["status"], "non_conformant")
        self.assertEqual(missing["missing_tasks"], [results[0]["task_id"]])

        unexpected = audit_plan_execution(
            plan,
            [],
            results
            + [{"plan_id": plan["plan_id"], "task_id": "unapproved.task"}],
        )
        self.assertEqual(unexpected["unexpected_tasks"], ["unapproved.task"])

        duplicate = audit_plan_execution(plan, [], results + [dict(results[0])])
        self.assertEqual(duplicate["duplicate_results"], [results[0]["task_id"]])


class TaskExecutionStoreTests(unittest.TestCase):
    def test_result_is_reused_across_store_instances_by_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_credit_plan(
                "DJ-CACHE-1",
                {"customer_type": "new", "business_type": "TKP"},
                [],
            )
            task = plan["tasks"][0]
            key = task_idempotency_key(plan, task)
            result = {"idempotency_key": key, "payload": {"status": "complete"}}
            run = {"idempotency_key": key, "status": "completed"}

            TaskExecutionStore(tmp).save(
                "DJ-CACHE-1", plan["plan_id"], key, result=result, run=run
            )
            reopened = TaskExecutionStore(tmp)
            stored = reopened.load("DJ-CACHE-1", plan["plan_id"], key)

            self.assertEqual(stored["result"], result)
            self.assertIsNone(
                reopened.load("DJ-CACHE-1", plan["plan_id"], "f" * 64)
            )

    def test_reused_result_keeps_its_evidence_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_contract_plan(
                "DJ-CACHE-GATE",
                [{"document_id": "DOC-CACHE"}],
                ai_available=True,
            )
            task = plan_task(plan, "contract_ai_review", input_ref="DOC-CACHE")
            key = task_idempotency_key(plan, task)
            store = TaskExecutionStore(root)
            store.save(
                "DJ-CACHE-GATE",
                plan["plan_id"],
                key,
                result={
                    "plan_id": plan["plan_id"],
                    "task_id": task["task_id"],
                    "idempotency_key": key,
                    "evidence_gate": "degraded",
                    "payload": {},
                },
                run={"status": "degraded"},
            )
            nodes = WorkflowNodes.__new__(WorkflowNodes)
            nodes.executions = TaskExecutionStore(root)
            update = nodes.run_contract_analysis(
                {
                    "case_id": "DJ-CACHE-GATE",
                    "active_workflow_plan": plan,
                    "active_agent_task": task,
                    "agent_task_results": [],
                }
            )

        self.assertEqual(update["agent_runs"][0]["status"], "reused")
        self.assertEqual(update["agent_runs"][0]["evidence_gate"], "degraded")


class SequencedAssistant:
    enabled = True

    class Gateway:
        available = True
        model = "fake-retry-model"

    gateway = Gateway()

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def review(self, facts, *, redacted_text, fragments):
        response = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        return deepcopy(response)


class DynamicAIGovernanceTests(unittest.TestCase):
    @staticmethod
    def _run_ai_task(root: Path, assistant: SequencedAssistant):
        nodes = WorkflowNodes.__new__(WorkflowNodes)
        nodes.contract_ai = assistant
        nodes.executions = TaskExecutionStore(root / "executions")
        plan = build_contract_plan(
            "DJ-AI-GOV-1", [{"document_id": "DOC-AI"}], ai_available=True
        )
        task = plan_task(plan, "contract_ai_review", input_ref="DOC-AI")
        return nodes.run_contract_analysis(
            {
                "case_id": "DJ-AI-GOV-1",
                "active_workflow_plan": plan,
                "active_agent_task": task,
                "agent_task_results": [],
                "contract_facts": [
                    {
                        "document_id": "DOC-AI",
                        "contract_name": "已脱敏合同",
                        "raw_text": "⟦REDACTED_TEXT⟧付款应在验收后30日支付",
                    }
                ],
                "source_documents": [
                    {
                        "document_id": "DOC-AI",
                        "fragments": [
                            {
                                "fragment_id": "line-1",
                                "text": "付款应在验收后30日支付",
                                "location": {"line": 1},
                            }
                        ],
                    }
                ],
            }
        )

    def test_ai_failure_is_retried_once_before_success(self):
        assistant = SequencedAssistant(
            [
                {
                    "status": "failed",
                    "model": "fake-retry-model",
                    "findings": [],
                    "error_type": "TimeoutError",
                },
                {
                    "status": "succeeded",
                    "model": "fake-retry-model",
                    "findings": [],
                    "summary": "第二次尝试成功",
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            update = self._run_ai_task(Path(tmp), assistant)

        run = update["agent_runs"][0]
        self.assertEqual(assistant.call_count, 2)
        self.assertEqual(run["attempt_count"], 2)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(
            [item["status"] for item in run["attempt_history"]],
            ["failed", "succeeded"],
        )

    def test_unlocated_ai_findings_are_removed_and_node_is_degraded(self):
        located = {
            "finding_id": "AI-LOCATED",
            "title": "已定位",
            "document_id": "DOC-AI",
            "fragment_id": "line-1",
        }
        unlocated = {"finding_id": "AI-UNLOCATED", "title": "未定位"}
        assistant = SequencedAssistant(
            [
                {
                    "status": "succeeded",
                    "model": "fake-retry-model",
                    "findings": [located, unlocated],
                    "summary": "包含未定位发现",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            update = self._run_ai_task(Path(tmp), assistant)

        result = update["agent_task_results"][0]
        run = update["agent_runs"][0]
        assistance = result["payload"]["ai_assistance"]
        self.assertEqual(result["evidence_gate"], "degraded")
        self.assertEqual(run["status"], "degraded")
        self.assertEqual(assistance["findings"], [located])
        self.assertTrue(assistance["evidence_degraded"])


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
