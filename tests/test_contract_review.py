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

    def test_combined_penalty_and_damages_over_half_is_blocked(self):
        facts = complete_contract(
            "合同金额：100万元。违约金为合同金额30%，损失赔偿金上限为合同金额25%。"
        )
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(facts.combined_liability_ratio, 0.55)
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(
            item.rule_id == "DJ-COMBINED-LIABILITY-OVER-50PCT"
            for item in result.findings
        ))

    def test_explicit_combined_liability_at_half_is_not_double_counted(self):
        facts = complete_contract(
            "合同金额：100万元。违约金与损失赔偿金合计不得超过合同金额的50%。"
        )
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(facts.combined_liability_ratio, 0.5)
        self.assertFalse(any(
            item.rule_id == "DJ-COMBINED-LIABILITY-OVER-50PCT"
            for item in result.findings
        ))

    def test_shared_penalty_and_damages_percentage_is_only_counted_once(self):
        facts = complete_contract(
            "合同金额：100万元。违约金包括损失赔偿，累计上限为合同金额的30%。"
        )
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(facts.combined_liability_ratio, 0.3)
        self.assertFalse(any(
            item.rule_id == "DJ-COMBINED-LIABILITY-OVER-50PCT"
            for item in result.findings
        ))

    def test_no_transaction_contract_hkd_one_million_cap_is_blocked(self):
        facts = complete_contract("累计赔偿责任上限为港币100万元。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertIsNone(facts.amount)
        self.assertEqual(facts.liability_cap_hkd, 1_000_000)
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(
            item.rule_id == "DJ-NO-AMOUNT-LIABILITY-OVER-HKD-1M"
            for item in result.findings
        ))

    def test_warranty_requires_shots_term_and_first_expiry(self):
        incomplete = complete_contract("模具质保期为交付后12个月。")
        result = ContractReviewEngine().review(incomplete, credit())
        self.assertTrue(any(
            item.rule_id == "DJ-WARRANTY-DUAL-LIMIT-INCOMPLETE"
            for item in result.findings
        ))
        complete = complete_contract("模具质保期为100万啤或交付后12个月，两者先到为准。")
        result = ContractReviewEngine().review(complete, credit())
        self.assertFalse(any(
            item.rule_id == "DJ-WARRANTY-DUAL-LIMIT-INCOMPLETE"
            for item in result.findings
        ))

    def test_replacement_warranty_reset_is_blocked(self):
        facts = complete_contract("替代品的质保期应自替换之日起重新起算。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(
            item.rule_id == "DJ-REPLACEMENT-WARRANTY-RESET"
            for item in result.findings
        ))

    def test_sales_country_compliance_cannot_be_shifted_entirely(self):
        facts = complete_contract(
            "产品销售国的法律法规识别及合规义务全部由供应商承担。"
        )
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(
            item.rule_id == "DJ-SALES-COUNTRY-COMPLIANCE-SHIFT"
            for item in result.findings
        ))

    def test_ip_license_requires_all_four_boundaries(self):
        incomplete = complete_contract("知识产权由东江所有，并许可客户使用。")
        result = ContractReviewEngine().review(incomplete, credit())
        self.assertTrue(any(
            item.rule_id == "DJ-IP-LICENSE-BOUNDARY-INCOMPLETE"
            for item in result.findings
        ))
        complete = complete_contract(
            "知识产权由东江所有，仅许可客户为履行本合同目的使用，许可期限为本合同有效期，免费且不可转让。"
        )
        result = ContractReviewEngine().review(complete, credit())
        self.assertFalse(any(
            item.rule_id == "DJ-IP-LICENSE-BOUNDARY-INCOMPLETE"
            for item in result.findings
        ))

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

    def test_english_and_vietnamese_amount_credit_term_and_bottom_line_are_detected(self):
        extractor = ContractFactExtractor()
        english = extractor.extract(
            "Buyer and Seller supply products. Contract amount: USD 520,000. "
            "Credit limit: USD 480,000. Payment term: Net 120 days. "
            "Breach and damages. Intellectual property. Confidential information. "
            "Termination. Governing law and dispute arbitration."
        )
        vietnamese = extractor.extract(
            "Bên mua và Bên bán ký hợp đồng hàng hóa. Giá trị hợp đồng: "
            "VND 8,000,000,000. Hạn mức tín dụng: VND 6,000,000,000. "
            "Thời hạn thanh toán: 90 ngày. Trách nhiệm vi phạm và bồi thường. "
            "Sở hữu trí tuệ. Bảo mật. Chấm dứt. Giải quyết tranh chấp. "
            "Bên mua có thể hủy đơn hàng mà không chịu trách nhiệm hoặc bồi thường."
        )

        self.assertEqual(english.amount, 520_000)
        self.assertEqual(english.requested_credit, 480_000)
        self.assertEqual(english.payment_term_days, 120)
        self.assertEqual(vietnamese.amount, 8_000_000_000)
        self.assertEqual(vietnamese.requested_credit, 6_000_000_000)
        self.assertEqual(vietnamese.payment_term_days, 90)
        result = ContractReviewEngine().review(vietnamese, credit())
        self.assertTrue(any(
            item.rule_id == "DJ-CANCEL-WITHOUT-LIABILITY"
            for item in result.findings
        ))

    def test_ocr_money_group_separators_are_normalized(self):
        facts = ContractFactExtractor().extract(
            "采购合同。合同金额人民币 3 600 000 元；"
            "授信额度人民币 3.000,000 元；付款期限：月结 120 天。",
            contract_name="scan.pdf",
        )

        self.assertEqual(facts.amount, 3_600_000)
        self.assertEqual(facts.requested_credit, 3_000_000)
        self.assertEqual(facts.payment_term_days, 120)


if __name__ == "__main__":
    unittest.main()
