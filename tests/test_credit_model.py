import unittest

from dongjiang_agent.credit import CreditScoringEngine
from dongjiang_agent.domain.models import CreditProfile, ExternalRating, RiskLevel


class CreditModelTests(unittest.TestCase):
    def test_new_customer_missing_cooperation_is_renormalized(self):
        profile = CreditProfile(
            customer_name="新客户",
            customer_type="new",
            business_type="TKP",
            registered_capital=80_000_000,
            years_in_business=15,
            asset_liability_ratio=0.42,
            net_margin=0.13,
            current_ratio=1.9,
            revenue_growth=0.16,
            monthly_order_amount=1_000_000,
            external_rating="AA",
        )
        result = CreditScoringEngine().assess(profile)
        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(result.approved_credit_limit, 3_000_000)
        self.assertIn("cooperation_history", result.missing_fields)

    def test_overdue_and_alert_cap_score(self):
        profile = CreditProfile(
            customer_name="风险客户",
            customer_type="existing",
            monthly_order_amount=1_000_000,
            external_rating="AA",
            max_overdue_days_12m=120,
            on_time_payment_rate=0.5,
            tax_or_enforcement_alert=True,
        )
        result = CreditScoringEngine().assess(profile)
        self.assertLessEqual(result.score, 30)
        self.assertEqual(result.risk_level, RiskLevel.HIGH)

    def test_multiple_agency_ratings_use_conservative_result_and_keep_conflict(self):
        profile = CreditProfile(
            customer_name="多评级客户",
            customer_type="new",
            monthly_order_amount=1_000_000,
            asset_liability_ratio=0.45,
            external_ratings=[
                ExternalRating("中诚信", "AAA", "稳定", "2026-06-24", source="ccxi.pdf"),
                ExternalRating("ChinaRatings", "AA+", "stable", "2026-07-03", source="chinaratings.html"),
            ],
        )
        result = CreditScoringEngine().assess(profile)
        resolution = result.rating_resolution
        self.assertEqual(result.dimension_scores["external_rating"], 95)
        self.assertTrue(resolution["conflict"])
        self.assertEqual(resolution["selected"]["agency"], "中债资信")
        self.assertEqual(resolution["selected"]["rating"], "AA+")
        self.assertIn("按保守策略采用AA+", "；".join(resolution["warnings"]))

    def test_missing_registered_capital_is_not_scored_as_zero(self):
        profile = CreditProfile(
            customer_name="资本缺失客户",
            customer_type="new",
            years_in_business=10,
            monthly_order_amount=1_000_000,
            external_rating="A",
        )
        result = CreditScoringEngine().assess(profile)
        self.assertEqual(result.dimension_scores["enterprise"], 75)
        self.assertIn("registered_capital", result.missing_fields)
        self.assertFalse(any("注册资本 0" in reason for reason in result.reasons))

    def test_partial_financial_metrics_are_renormalized(self):
        profile = CreditProfile(
            customer_name="财务字段缺失客户",
            customer_type="new",
            asset_liability_ratio=0.45,
            monthly_order_amount=1_000_000,
            external_rating="A",
        )
        result = CreditScoringEngine().assess(profile)
        self.assertEqual(result.dimension_scores["financial"], 100)
        self.assertIn("net_margin", result.missing_fields)
        self.assertIn("current_ratio", result.missing_fields)


if __name__ == "__main__":
    unittest.main()
