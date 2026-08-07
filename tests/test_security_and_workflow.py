import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from dongjiang_agent.credit import CreditScoringEngine
from dongjiang_agent.domain.models import AuditCase, CreditProfile, ExternalRating
from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.security import RedactionVault
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness


COMPLETE_CONTRACT = (
    "甲方：东江；乙方：客户。合同标的：组件。付款：月结60天。"
    "违约责任：直接损失。知识产权：各自所有。保密：不得披露。"
    "解除与终止：违约解除。争议解决：深圳法院。"
)


class SecurityAndWorkflowTests(unittest.TestCase):
    def test_repository_inactivates_latest_effective_credit_after_one_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = CaseRepository(Path(tmp))
            profile = CreditProfile(
                customer_name="失活客户",
                unified_social_credit_code="91440300INACTIVE001",
                customer_type="existing",
                customer_status="Active",
                business_type="TKP",
                monthly_order_amount=1_000_000,
                last_order_date="2024-01-01",
                outstanding_receivables_amount=0,
                open_order_amount=0,
                external_rating="AA",
                asset_liability_ratio=0.45,
                current_ratio=1.5,
            )
            assessment = CreditScoringEngine().assess(profile)
            case = AuditCase(
                customer=profile,
                contracts=[],
                credit_assessment=assessment,
                credit_status="effective",
                status="awaiting_contract",
            )
            repository.save(case)
            changed = repository.apply_inactivity_policy(
                as_of=datetime(2026, 7, 31, tzinfo=timezone.utc)
            )
            stored = repository.get_case(case.case_id)
            self.assertEqual(changed, [case.case_id])
            self.assertEqual(stored["status"], "inactive")
            self.assertEqual(stored["customer"]["customer_status"], "Inactive")
            self.assertEqual(stored["credit_assessment"]["approved_credit_limit"], 0)
            self.assertIsNone(repository.find_valid_credit(profile))

    def test_credit_cache_honors_custom_approval_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = CaseRepository(Path(tmp))
            profile = CreditProfile(
                customer_name="过期授信客户",
                unified_social_credit_code="91440300EXPIRED0001",
                customer_type="existing",
                monthly_order_amount=1_000_000,
                external_rating="AA",
                asset_liability_ratio=0.45,
                current_ratio=1.5,
            )
            assessment = CreditScoringEngine().assess(profile)
            repository.save(AuditCase(
                customer=profile,
                contracts=[],
                credit_assessment=assessment,
                credit_status="effective",
                credit_approval={"expires_at": "2020-01-01T00:00:00+00:00"},
            ))
            self.assertIsNone(repository.find_valid_credit(profile))

    def test_redaction_is_reversible_and_masks_money_party_phone(self):
        raw = "乙方：深圳示例科技有限公司，电话13800138000，合同金额：人民币1,200,000元。"
        vault = RedactionVault("CASE-1")
        safe = vault.redact(raw)
        self.assertNotIn("深圳示例科技有限公司", safe)
        self.assertNotIn("13800138000", safe)
        self.assertNotIn("1,200,000", safe)
        self.assertEqual(vault.restore(safe), raw)

    def test_redaction_masks_identifiers_adjacent_to_chinese_text(self):
        raw = (
            "收款账号6222021234567890123，"
            "统一社会信用代码91440300MA5F12345X。"
        )
        vault = RedactionVault("CASE-CJK-BOUNDARY")

        safe = vault.redact(raw)

        self.assertNotIn("6222021234567890123", safe)
        self.assertNotIn("91440300MA5F12345X", safe)
        self.assertIn("⟦BANK_", safe)
        self.assertIn("⟦USCC_", safe)
        self.assertEqual(vault.restore(safe), raw)

    def test_redaction_masks_multilingual_party_money_tech_and_contact_fields(self):
        raw = (
            "Atlas Mobility Systems Ltd. | Contract amount: USD 520,000 | "
            "Technical parameters: PPS-GF40 | alice@example.com | +84 912345678 | "
            "Công ty TNHH Sao Việt Mobility"
        )
        vault = RedactionVault("CASE-MULTILINGUAL")

        safe = vault.redact(raw)

        for value in (
            "Atlas Mobility Systems Ltd.",
            "USD 520,000",
            "PPS-GF40",
            "alice@example.com",
            "+84 912345678",
            "Công ty TNHH Sao Việt Mobility",
        ):
            self.assertNotIn(value, safe)
        self.assertEqual(vault.restore(safe), raw)

    def test_redaction_vault_merges_existing_case_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = RedactionVault("CASE-2", root)
            first.redact("电话13800138000")
            first.persist_local()
            second = RedactionVault("CASE-2", root)
            second.redact("邮箱test@example.com")
            second.persist_local()
            payload = json.loads(
                (root / "CASE-2.vault.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payload["mapping"]), 2)

    def test_credit_cache_does_not_cross_same_name_different_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = CaseRepository(Path(tmp))
            original = CreditProfile(
                customer_name="同名企业",
                unified_social_credit_code="91440300ORIGINAL001",
                customer_type="new",
                monthly_order_amount=1_000_000,
                external_rating="AA",
                asset_liability_ratio=0.45,
                current_ratio=1.5,
            )
            assessment = CreditScoringEngine().assess(original)
            repository.save(AuditCase(
                customer=original,
                contracts=[],
                credit_assessment=assessment,
                credit_status="effective",
            ))
            other = CreditProfile(
                customer_name="同名企业",
                unified_social_credit_code="91440300DIFFERENT02",
            )
            self.assertIsNone(repository.find_valid_credit(other))
            self.assertIsNone(repository.find_valid_credit(CreditProfile(
                customer_name="同名企业",
            )))
            self.assertIsNotNone(repository.find_valid_credit(original))

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
                        asset_liability_ratio=0.5,
                        current_ratio=1.4,
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
                        "contract_texts": [COMPLETE_CONTRACT + "买方可取消订单且不承担任何责任。"],
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
