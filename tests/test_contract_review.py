import unittest

from dongjiang_agent.contract import ContractFactExtractor, ContractReviewEngine
from dongjiang_agent.credit import CreditScoringEngine
from dongjiang_agent.domain.models import AuditDecision, CreditProfile


def credit(business_type: str = "TKP"):
    return CreditScoringEngine().assess(CreditProfile(
        customer_name="测试客户",
        customer_type="existing",
        business_type=business_type,
        monthly_order_amount=1_000_000,
        external_rating="A",
        cooperation_years=3,
        overdue_count_12m=0,
        max_overdue_days_12m=0,
        on_time_payment_rate=0.98,
    ))


def credit_with_occupancy():
    return CreditScoringEngine().assess(CreditProfile(
        customer_name="额度占用客户",
        customer_type="new",
        business_type="TKP",
        monthly_order_amount=1_000_000,
        external_rating="AA",
        asset_liability_ratio=0.45,
        current_ratio=1.5,
        outstanding_receivables_amount=1_500_000,
        open_order_amount=1_000_000,
    ))


def complete_contract(extra: str, business_type: str = "TKP"):
    text = (
        "甲方：东江；乙方：客户。合同标的：模具。付款：按约支付。"
        "违约责任：赔偿直接损失。知识产权：背景权利归原权利人。"
        "保密：不得披露。解除与终止：违约可解除。争议解决：深圳法院。" + extra
    )
    return ContractFactExtractor().extract(text, business_type=business_type)


class ContractReviewTests(unittest.TestCase):
    def test_tkp_over_90_days_requires_special_approval(self):
        facts = complete_contract("合同金额：100万元。信用额度：100万元。账期：120天。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertTrue(any(item.rule_id == "TKP-SPECIAL-TERM" for item in result.findings))

    def test_tkm_tail_ratio_over_40_percent_requires_special_approval(self):
        facts = complete_contract("合同金额：100万元。尾款比例为50%，尾款在3个月内支付。", "TKM")
        result = ContractReviewEngine().review(facts, credit("TKM"))
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertTrue(any(item.rule_id == "TKM-SPECIAL-TAIL-RATIO" for item in result.findings))

    def test_tkm_automotive_standard_accepts_60_percent_tail_within_one_year(self):
        assessment = CreditScoringEngine().assess(CreditProfile(
            customer_name="汽车模具客户",
            customer_type="new",
            business_type="TKM",
            tkm_business_subtype="automotive_standard",
            monthly_order_amount=1_000_000,
            external_rating="AA",
            asset_liability_ratio=0.45,
            current_ratio=1.5,
        ))
        facts = complete_contract(
            "合同金额：100万元。尾款比例为60%，尾款在12个月内支付。", "TKM"
        )
        result = ContractReviewEngine().review(facts, assessment)
        self.assertFalse(any(
            item.rule_id in {"TKM-SPECIAL-TAIL-RATIO", "TKM-SPECIAL-TAIL-TERM"}
            for item in result.findings
        ))

    def test_unapproved_tkm_purchase_exemption_requires_special_approval(self):
        assessment = CreditScoringEngine().assess(CreditProfile(
            customer_name="采购豁免客户",
            customer_type="new",
            business_type="TKM",
            tkm_business_subtype="precision",
            purchase_exemption_requested=True,
            monthly_order_amount=1_000_000,
            external_rating="AA",
            asset_liability_ratio=0.45,
            current_ratio=1.5,
        ))
        facts = complete_contract("无需收回首期款即可采购项目物料。", "TKM")
        result = ContractReviewEngine().review(facts, assessment)
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertTrue(any(
            item.rule_id == "TKM-PURCHASE-EXEMPTION-NOT-APPROVED"
            for item in result.findings
        ))

    def test_over_credit_but_under_hard_term_routes_special_approval(self):
        facts = complete_contract("合同金额：400万元。信用额度：400万元。账期：60天。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertTrue(any(item.rule_id == "CREDIT-OVER-LIMIT" for item in result.findings))

    def test_contract_credit_is_checked_against_remaining_available_credit(self):
        facts = complete_contract("合同金额：80万元。信用额度：80万元。账期：60天。")
        result = ContractReviewEngine().review(facts, credit_with_occupancy())
        finding = next(item for item in result.findings if item.rule_id == "CREDIT-OVER-LIMIT")
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertIn("当前可用仅 500,000.00", finding.message)

    def test_dongjiang_bottom_line_patterns_route_by_required_authority(self):
        facts = complete_contract(
            "付款方式：银行承兑汇票。供应商承担连带责任。"
            "采用VMI供应商管理库存模式。"
        )
        result = ContractReviewEngine().review(facts, credit())
        rule_ids = {item.rule_id for item in result.findings}
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertIn("DJ-BANK-ACCEPTANCE", rule_ids)
        self.assertIn("DJ-JOINT-LIABILITY", rule_ids)
        self.assertIn("DJ-VMI-JIT-DELIVERY", rule_ids)

    def test_customer_cancel_without_liability_is_returned_for_revision(self):
        facts = complete_contract("买方可随时取消订单且不承担任何责任。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(item.rule_id == "DJ-CANCEL-WITHOUT-LIABILITY" for item in result.findings))

    def test_exclusive_jurisdiction_is_not_misclassified_as_business_exclusivity(self):
        facts = complete_contract(
            "The courts of Hong Kong have exclusive jurisdiction. "
            "Each party retains its exclusive intellectual property rights."
        )
        result = ContractReviewEngine().review(facts, credit())
        self.assertFalse(any(item.rule_id == "DJ-EXCLUSIVITY" for item in result.findings))

    def test_tkp_only_delivery_modes_do_not_apply_to_tkm(self):
        facts = complete_contract("采用VMI供应商管理库存模式。", "TKM")
        result = ContractReviewEngine().review(facts, credit("TKM"))
        self.assertFalse(any(item.rule_id == "DJ-VMI-JIT-DELIVERY" for item in result.findings))

    def test_english_contract_completeness_markers_are_recognized(self):
        facts = ContractFactExtractor().extract(
            "Buyer and Seller agree to supply products. Payment is due by invoice. "
            "Breach and damages are limited. Intellectual property remains with each party. "
            "Confidential information shall not be disclosed. Either party may terminate. "
            "Disputes are subject to Hong Kong jurisdiction."
        )
        self.assertEqual(facts.language, "en")
        self.assertTrue(facts.has_parties)
        self.assertTrue(facts.has_subject)
        self.assertTrue(facts.has_payment)
        self.assertTrue(facts.has_dispute_resolution)

    def test_vietnamese_japanese_and_spanish_contract_languages_are_detected(self):
        extractor = ContractFactExtractor()
        vietnamese = extractor.extract(
            "Hợp đồng mua bán quy định các bên, điều khoản thanh toán, trách nhiệm "
            "và nghĩa vụ bảo mật thông tin."
        )
        japanese = extractor.extract(
            "本契約は商品の供給、支払条件、秘密保持および契約終了について定めるものとします。"
        )
        spanish = extractor.extract(
            "El comprador y el vendedor celebran este contrato. El pago, la "
            "responsabilidad, la confidencialidad y la jurisdicción quedan regulados."
        )
        self.assertEqual(vietnamese.language, "vi")
        self.assertEqual(japanese.language, "ja")
        self.assertEqual(spanish.language, "es")


if __name__ == "__main__":
    unittest.main()
