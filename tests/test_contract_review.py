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


def complete_contract(extra: str, business_type: str = "TKP"):
    text = (
        "甲方：东江；乙方：客户。合同标的：模具。付款：按约支付。"
        "违约责任：赔偿直接损失。知识产权：背景权利归原权利人。"
        "保密：不得披露。解除与终止：违约可解除。争议解决：深圳法院。" + extra
    )
    return ContractFactExtractor().extract(text, business_type=business_type)


class ContractReviewTests(unittest.TestCase):
    def test_tkp_over_90_days_is_hard_block(self):
        facts = complete_contract("合同金额：100万元。信用额度：100万元。账期：120天。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(item.rule_id == "TKP-HARD-TERM" and item.hard_stop for item in result.findings))

    def test_tkm_tail_ratio_over_40_percent_is_hard_block(self):
        facts = complete_contract("合同金额：100万元。尾款比例为50%，尾款在3个月内支付。", "TKM")
        result = ContractReviewEngine().review(facts, credit("TKM"))
        self.assertEqual(result.decision, AuditDecision.BLOCK)
        self.assertTrue(any(item.rule_id == "TKM-HARD-TAIL-RATIO" for item in result.findings))

    def test_over_credit_but_under_hard_term_routes_special_approval(self):
        facts = complete_contract("合同金额：400万元。信用额度：400万元。账期：60天。")
        result = ContractReviewEngine().review(facts, credit())
        self.assertEqual(result.decision, AuditDecision.SPECIAL_APPROVAL)
        self.assertTrue(any(item.rule_id == "CREDIT-OVER-LIMIT" for item in result.findings))


if __name__ == "__main__":
    unittest.main()
