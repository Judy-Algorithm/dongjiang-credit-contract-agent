"""Deterministic and explainable Dongjiang credit scoring proposal."""

from __future__ import annotations

from typing import Any

from ..config import load_policy
from ..domain.models import CreditAssessment, CreditProfile, RiskLevel
from .ratings import resolve_ratings


class CreditScoringEngine:
    def __init__(self, policy: dict[str, Any] | None = None) -> None:
        self.policy = policy or load_policy()

    @staticmethod
    def _linear_score(value: float, good: float, bad: float, direction: str) -> float:
        if good == bad:
            return 50.0
        if direction == "lower":
            raw = (bad - value) / (bad - good)
        else:
            raw = (value - bad) / (good - bad)
        return round(max(0.0, min(1.0, raw)) * 100, 2)

    def _financial_score(self, profile: CreditProfile, missing: list[str]) -> tuple[float | None, list[str]]:
        metrics = self.policy["dimensions"]["financial"]["metrics"]
        weighted = 0.0
        available_weight = 0.0
        reasons: list[str] = []
        for name, cfg in metrics.items():
            value = getattr(profile, name, None)
            if value is None:
                missing.append(name)
                continue
            score = self._linear_score(float(value), float(cfg["good"]), float(cfg["bad"]), str(cfg["direction"]))
            metric_weight = float(cfg["weight"])
            weighted += score * metric_weight
            available_weight += metric_weight
            reasons.append(f"{name}={value:g}，指标分 {score:.1f}")
        return (round(weighted / available_weight, 2) if available_weight else None), reasons

    def _rating_score(
        self, profile: CreditProfile, missing: list[str]
    ) -> tuple[float | None, list[str], dict[str, Any]]:
        resolution = resolve_ratings(
            profile.external_ratings,
            self.policy,
            fallback_rating=profile.external_rating,
            fallback_outlook=profile.rating_outlook,
        )
        selected = resolution.get("selected")
        if not selected:
            missing.append("external_rating")
            return None, list(resolution["warnings"]), resolution
        reasons = [
            f'{selected["agency"]}主体评级 {selected["rating"]}'
            f'（{selected["outlook"] or "展望未知"}），映射分 {selected["score"]:.1f}'
        ]
        reasons.extend(resolution["warnings"])
        return float(selected["score"]), reasons, resolution

    @staticmethod
    def _cooperation_score(profile: CreditProfile, missing: list[str]) -> tuple[float | None, list[str]]:
        fields = (
            profile.cooperation_years,
            profile.overdue_count_12m,
            profile.max_overdue_days_12m,
            profile.on_time_payment_rate,
        )
        if all(value is None for value in fields):
            missing.append("cooperation_history")
            return None, []
        years = max(0.0, float(profile.cooperation_years or 0))
        overdue_count = max(0, int(profile.overdue_count_12m or 0))
        max_days = max(0, int(profile.max_overdue_days_12m or 0))
        on_time = profile.on_time_payment_rate
        on_time_score = 70.0 if on_time is None else max(0.0, min(1.0, float(on_time))) * 100
        score = min(100.0, 55 + years * 5)
        score = score * 0.35 + on_time_score * 0.45
        score += max(0.0, 20 - overdue_count * 5 - max_days * 0.35)
        return max(0.0, min(100.0, round(score, 2))), [
            f"合作 {years:g} 年，近12月逾期 {overdue_count} 次，最长 {max_days} 天"
        ]

    @staticmethod
    def _enterprise_score(profile: CreditProfile, missing: list[str]) -> tuple[float | None, list[str]]:
        if profile.registered_capital is None and profile.years_in_business is None:
            missing.append("enterprise_basics")
            missing.extend(["registered_capital", "years_in_business"])
            return None, []
        weighted = 0.0
        available_weight = 0.0
        reasons: list[str] = []
        if profile.registered_capital is None:
            missing.append("registered_capital")
        else:
            capital = max(0.0, float(profile.registered_capital))
            weighted += min(100.0, 30 + capital / 1_000_000 * 3.5) * 0.55
            available_weight += 0.55
            reasons.append(f"注册资本 {capital:,.0f}")
        if profile.years_in_business is None:
            missing.append("years_in_business")
        else:
            years = max(0, int(profile.years_in_business))
            weighted += min(100.0, 25 + years * 5) * 0.45
            available_weight += 0.45
            reasons.append(f"成立 {years} 年")
        return round(weighted / available_weight, 2), reasons

    def assess(self, profile: CreditProfile) -> CreditAssessment:
        missing: list[str] = []
        reasons: list[str] = []
        dimension_values: dict[str, float | None] = {}

        dimension_values["financial"], detail = self._financial_score(profile, missing)
        reasons.extend(detail)
        dimension_values["external_rating"], detail, rating_resolution = self._rating_score(profile, missing)
        reasons.extend(detail)
        dimension_values["cooperation"], detail = self._cooperation_score(profile, missing)
        reasons.extend(detail)
        dimension_values["enterprise"], detail = self._enterprise_score(profile, missing)
        reasons.extend(detail)

        is_new = profile.customer_type.strip().lower() in {"new", "新客户", "new_customer"}
        enabled: dict[str, float] = {}
        for dimension, cfg in self.policy["dimensions"].items():
            if bool(cfg.get("enabled", True)):
                enabled[dimension] = float(cfg["weight"])
        if is_new and not self.policy["new_customer"]["cooperation_dimension_enabled"]:
            enabled.pop("cooperation", None)

        available = {key: value for key, value in dimension_values.items() if key in enabled and value is not None}
        weight_sum = sum(enabled[key] for key in available)
        score = (
            sum(float(value) * enabled[key] for key, value in available.items()) / weight_sum
            if weight_sum
            else 0.0
        )

        hard_stops = self.policy["hard_stops"]
        if profile.major_litigation and hard_stops.get("major_litigation"):
            score = min(score, 35)
            reasons.append("存在重大诉讼，触发评分封顶")
        if profile.tax_or_enforcement_alert and hard_stops.get("tax_or_enforcement_alert"):
            score = min(score, 30)
            reasons.append("存在税务或被执行告警，触发评分封顶")
        if int(profile.max_overdue_days_12m or 0) > int(hard_stops["max_overdue_days"]):
            score = min(score, 30)
            reasons.append("历史最长逾期超过政策底线，触发评分封顶")

        low = float(self.policy["risk_thresholds"]["low"])
        medium = float(self.policy["risk_thresholds"]["medium"])
        risk_level = RiskLevel.LOW if score >= low else RiskLevel.MEDIUM if score >= medium else RiskLevel.HIGH

        business_type = profile.business_type.strip().upper() or "TKP"
        business_cfg = self.policy["business_rules"].get(business_type, self.policy["business_rules"]["TKP"])
        grade_cfg = business_cfg["grades"][risk_level.value]
        monthly_amount = max(0.0, float(profile.monthly_order_amount or 0))
        credit_limit = monthly_amount * float(grade_cfg["credit_months"])
        if monthly_amount <= 0:
            missing.append("monthly_order_amount")
            reasons.append("缺少月度订单额，暂不能形成可用授信额度")

        dimension_scores = {
            key: round(float(value), 2)
            for key, value in dimension_values.items()
            if value is not None
        }
        return CreditAssessment(
            score=round(score, 2),
            risk_level=risk_level,
            approved_credit_limit=round(credit_limit, 2),
            recommended_term_days=int(grade_cfg["recommended_term_days"]),
            hard_term_limit_days=int(business_cfg.get("hard_max_term_days", grade_cfg["recommended_term_days"])),
            max_tail_payment_ratio=(
                float(grade_cfg["max_tail_ratio"]) if business_type == "TKM" else None
            ),
            max_tail_term_days=(
                int(grade_cfg["max_tail_term_days"]) if business_type == "TKM" else None
            ),
            dimension_scores=dimension_scores,
            missing_fields=sorted(set(missing)),
            reasons=reasons,
            policy_version=str(self.policy["version"]),
            rating_resolution=rating_resolution,
        )
