import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.domain.models import CreditProfile, ExternalRating
from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.security import RedactionVault
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness


COMPLETE_CONTRACT = (
    "甲方：东江；乙方：客户。合同标的：组件。付款：月结60天。"
    "违约责任：直接损失。知识产权：各自所有。保密：不得披露。"
    "解除与终止：违约解除。争议解决：深圳法院。"
)


class SecurityAndWorkflowTests(unittest.TestCase):
    def test_redaction_is_reversible_and_masks_money_party_phone(self):
        raw = "乙方：深圳示例科技有限公司，电话13800138000，合同金额：人民币1,200,000元。"
        vault = RedactionVault("CASE-1")
        safe = vault.redact(raw)
        self.assertNotIn("深圳示例科技有限公司", safe)
        self.assertNotIn("13800138000", safe)
        self.assertNotIn("1,200,000", safe)
        self.assertEqual(vault.restore(safe), raw)

    def test_workflow_runs_complete_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with DongjiangWorkflowHarness(
                checkpoint_path=tmp_path / "workflow.sqlite",
                repository=CaseRepository(tmp_path / "cases"),
                vault_dir=tmp_path / "vault",
                inbox_dir=tmp_path / "inbox",
                output_dir=tmp_path / "output",
            ) as harness:
                run = harness.start(
                    CreditProfile(
                        customer_name="流程测试客户",
                        customer_type="new",
                        monthly_order_amount=500_000,
                        external_rating="A",
                    ),
                    use_cached_credit=False,
                    actor=ActorContext("test", ("system",), "test"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve"},
                    actor=ActorContext("test", ("system",), "test"),
                )
                run = harness.resume(
                    run.case_id,
                    {
                        "action": "submit_contract",
                        "contract_texts": [COMPLETE_CONTRACT.replace("60天", "120天")],
                    },
                    actor=ActorContext("test", ("system",), "test"),
                )
            stages = [item["stage"] for item in run.state["trace"]]
            self.assertIn("agent.plan", stages)
            self.assertIn("privacy.redacted", stages)
            self.assertIn("credit.completed", stages)
            self.assertIn("contract.completed", stages)
            self.assertEqual(run.status, "blocked")

    def test_material_rating_conflict_routes_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with DongjiangWorkflowHarness(
                checkpoint_path=tmp_path / "workflow.sqlite",
                repository=CaseRepository(tmp_path / "cases"),
                vault_dir=tmp_path / "vault",
                inbox_dir=tmp_path / "inbox",
                output_dir=tmp_path / "output",
            ) as harness:
                run = harness.start(
                    CreditProfile(
                        customer_name="评级冲突客户",
                        customer_type="new",
                        monthly_order_amount=500_000,
                        asset_liability_ratio=0.5,
                        external_ratings=[
                            ExternalRating("中诚信国际", "AAA", "稳定", "2026-06-24"),
                            ExternalRating("联合资信", "A", "稳定", "2026-07-03"),
                        ],
                    ),
                    use_cached_credit=False,
                    actor=ActorContext("test", ("system",), "test"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve"},
                    actor=ActorContext("test", ("system",), "test"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [COMPLETE_CONTRACT]},
                    actor=ActorContext("test", ("system",), "test"),
                )
            self.assertEqual(run.status, "pending_manual_review")
            self.assertTrue(
                run.state["credit_assessment"]["rating_resolution"]["material_conflict"]
            )
            self.assertIn(
                "credit.rating_conflict",
                [item["stage"] for item in run.state["trace"]],
            )


if __name__ == "__main__":
    unittest.main()
