import json
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness


COMPLETE_SAFE_CONTRACT = """销售合同
甲方：东江集团
乙方：测试客户有限公司
合同标的：精密注塑组件。
合同金额：人民币2,000,000元，信用额度：2,000,000元。
付款及账期：月结60天。
知识产权：双方背景知识产权各自所有。
保密：双方不得披露商业秘密。
违约责任：违约方赔偿直接损失，累计不超过合同金额。
解除与终止：重大违约催告后可以解除。
争议解决：由深圳市人民法院管辖。
"""


class LangGraphWorkflowTests(unittest.TestCase):
    def harness(self, root: Path) -> DongjiangWorkflowHarness:
        return DongjiangWorkflowHarness(
            checkpoint_path=root / "workflow.sqlite",
            repository=CaseRepository(root / "cases"),
            vault_dir=root / "vault",
            inbox_dir=root / "inbox",
            output_dir=root / "output",
        )

    def test_blocked_contract_can_be_revised_and_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "阻断流程测试客户",
                "customer_type": "existing",
                "business_type": "TKP",
                "monthly_order_amount": 2_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
            }
            blocked_contract = COMPLETE_SAFE_CONTRACT.replace(
                "月结60天",
                "月结120天",
            )
            with self.harness(root) as harness:
                run = harness.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertEqual(run.waiting_for, "credit_approval")
                run = harness.resume(
                    run.case_id,
                    {"action": "approve", "comment": "同意授信建议"},
                    actor=ActorContext("credit-1", ("credit",), "oa"),
                )
                self.assertEqual(run.waiting_for, "contract_upload")
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [blocked_contract]},
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertTrue(run.paused)
                self.assertEqual(run.waiting_for, "sales_revision")
                self.assertEqual(run.interrupt["type"], "sales_revision")
                checkpoint_text = json.dumps(run.state, ensure_ascii=False)
                self.assertNotIn("2,000,000", checkpoint_text)
                self.assertNotIn("东江集团", checkpoint_text)
                self.assertFalse(any((root / "inbox").rglob("*.txt")))
                stages = [item["stage"] for item in run.state["trace"]]
                self.assertEqual(stages.count("workflow.started"), 1)

                final = harness.resume(
                    run.case_id,
                    {
                        "action": "submit_revision",
                        "contract_texts": [COMPLETE_SAFE_CONTRACT],
                    },
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.state["decision"], "pass")
                self.assertEqual(final.status, "approved")
                self.assertTrue(Path(final.state["reports"]["json"]).is_file())

    def test_checkpoint_survives_process_boundary_and_roles_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "等待合同客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
            }
            with self.harness(root) as first:
                run = first.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-2", ("sales",), "crm"),
                )
                case_id = run.case_id
                self.assertEqual(run.waiting_for, "credit_approval")

            with self.harness(root) as second:
                recovered = second.get(case_id)
                self.assertEqual(recovered.waiting_for, "credit_approval")
                with self.assertRaises(PermissionError):
                    second.resume(
                        case_id,
                        {"action": "approve"},
                        actor=ActorContext("sales-2", ("sales",), "crm"),
                    )
                approved = second.resume(
                    case_id,
                    {"action": "approve", "comment": "审批通过"},
                    actor=ActorContext("finance-1", ("finance",), "oa"),
                )
                self.assertEqual(approved.waiting_for, "contract_upload")
                with self.assertRaises(PermissionError):
                    second.resume(
                        case_id,
                        {"action": "submit_contract", "contract_texts": [COMPLETE_SAFE_CONTRACT]},
                        actor=ActorContext("finance-1", ("finance",), "oa"),
                    )
                final = second.resume(
                    case_id,
                    {"action": "submit_contract", "contract_texts": [COMPLETE_SAFE_CONTRACT]},
                    actor=ActorContext("sales-2", ("sales",), "crm"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.state["decision"], "pass")

    def test_special_approval_requires_director_or_ceo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "特批客户",
                "customer_type": "new",
                "business_type": "TKP",
                "registered_capital": 100_000_000,
                "years_in_business": 20,
                "asset_liability_ratio": 0.4,
                "net_margin": 0.15,
                "current_ratio": 2.0,
                "revenue_growth": 0.2,
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
            }
            over_limit = COMPLETE_SAFE_CONTRACT.replace(
                "信用额度：2,000,000元",
                "信用额度：5,000,000元",
            )
            with self.harness(root) as harness:
                run = harness.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-3", ("sales",), "crm"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve"},
                    actor=ActorContext("credit-3", ("credit",), "oa"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [over_limit]},
                    actor=ActorContext("sales-3", ("sales",), "crm"),
                )
                self.assertEqual(run.waiting_for, "manager_approval")
                with self.assertRaises(PermissionError):
                    harness.resume(
                        run.case_id,
                        {"action": "approve"},
                        actor=ActorContext("sales-3", ("sales",), "crm"),
                    )
                final = harness.resume(
                    run.case_id,
                    {"action": "approve", "comment": "同意本次例外"},
                    actor=ActorContext("director-1", ("director",), "oa"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.status, "approved_by_exception")

    def test_contract_cannot_be_submitted_before_credit_is_effective(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "门禁测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-gate", ("sales",), "crm"),
                )
                self.assertEqual(run.status, "credit_pending_approval")
                self.assertEqual(run.state["credit_status"], "pending_approval")
                self.assertIsNone(run.state["effective_credit_assessment"])
                with self.assertRaises(PermissionError):
                    harness.resume(
                        run.case_id,
                        {
                            "action": "submit_contract",
                            "contract_texts": [COMPLETE_SAFE_CONTRACT],
                        },
                        actor=ActorContext("sales-gate", ("sales",), "crm"),
                    )

    def test_credit_adjustment_becomes_the_effective_contract_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "授信调整测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-adjust", ("sales",), "crm"),
                )
                approved = harness.resume(
                    run.case_id,
                    {
                        "action": "adjust_and_approve",
                        "approved_credit_limit": 1_500_000,
                        "approved_term_days": 45,
                        "comment": "依据担保条件下调额度和账期",
                    },
                    actor=ActorContext("credit-adjust", ("credit",), "oa"),
                )
                self.assertEqual(approved.state["credit_status"], "effective")
                effective = approved.state["effective_credit_assessment"]
                self.assertEqual(effective["approved_credit_limit"], 1_500_000)
                self.assertEqual(effective["recommended_term_days"], 45)
                self.assertEqual(approved.waiting_for, "contract_upload")


if __name__ == "__main__":
    unittest.main()
