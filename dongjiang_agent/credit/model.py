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

    def _data_coverage(
        self,
        profile: CreditProfile,
        enabled: dict[str, float],
        available_dimensions: list[str],
    ) -> float:
        covered_weight = 0.0
        financial = self.policy["dimensions"]["financial"]
        if "financial" in enabled:
            metrics = financial["metrics"]
            metric_total = sum(float(item["weight"]) for item in metrics.values())
            metric_covered = sum(
                float(item["weight"])
                for name, item in metrics.items()
                if getattr(profile, name, None) is not None
            )
            if metric_total:
                covered_weight += enabled["financial"] * metric_covered / metric_total
        if "external_rating" in enabled and "external_rating" in available_dimensions:
            covered_weight += enabled["external_rating"]
        if "cooperation" in enabled:
            cooperation_fields = (
                profile.cooperation_years,
                profile.overdue_count_12m,
                profile.max_overdue_days_12m,
                profile.on_time_payment_rate,
            )
            covered_weight += enabled["cooperation"] * (
                sum(value is not None for value in cooperation_fields) / len(cooperation_fields)
            )
        if "enterprise" in enabled:
            enterprise_coverage = 0.0
            if profile.registered_capital is not None:
                enterprise_coverage += 0.55
            if profile.years_in_business is not None:
                enterprise_coverage += 0.45
            covered_weight += enabled["enterprise"] * enterprise_coverage
        total_weight = sum(enabled.values())
        return round(covered_weight / total_weight, 4) if total_weight else 0.0

    def credit_control(
        self,
        profile: CreditProfile,
        approved_credit_limit: float,
    ) -> dict[str, Any]:
        receivables = max(0.0, float(profile.outstanding_receivables_amount or 0))
        open_orders = max(0.0, float(profile.open_order_amount or 0))
        occupied = round(receivables + open_orders, 2)
        limit = max(0.0, float(approved_credit_limit))
        overdue_days = max(0, int(profile.current_overdue_days or 0))
        max_overdue_days = int(
            (self.policy.get("credit_control") or {}).get(
                "max_current_overdue_days", 30
            )
        )
        lock_reasons: list[str] = []
        if occupied > limit:
            lock_reasons.append(
                f"当前授信占用 {occupied:,.2f} 元，超过批准额度 {limit:,.2f} 元"
            )
        if overdue_days > max_overdue_days:
            lock_reasons.append(
                f"当前未收款已逾期 {overdue_days} 天，超过 {max_overdue_days} 天锁定阈值"
            )
        return {
            "outstanding_receivables_amount": receivables,
            "open_order_amount": open_orders,
            "occupied_credit_amount": occupied,
            "available_credit_amount": round(max(0.0, limit - occupied), 2),
            "current_overdue_days": overdue_days,
            "max_current_overdue_days": max_overdue_days,
            "credit_locked": bool(lock_reasons),
            "credit_lock_reasons": lock_reasons,
        }

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
        is_inactive = profile.customer_status.strip().lower() == "inactive"
        enabled: dict[str, float] = {}
        for dimension, cfg in self.policy["dimensions"].items():
            if bool(cfg.get("enabled", True)):
                enabled[dimension] = float(cfg["weight"])
        if is_new and not self.policy["new_customer"]["cooperation_dimension_enabled"]:
            enabled.pop("cooperation", None)

        available = {key: value for key, value in dimension_values.items() if key in enabled and value is not None}
        available_dimensions = sorted(available)
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
        business_cfg = self.policy["business_rules"].get(
            business_type, self.policy["business_rules"]["TKP"]
        )
        tkm_subtype = ""
        if business_type == "TKM":
            subtypes = business_cfg.get("subtypes") or {}
            tkm_subtype = (
                profile.tkm_business_subtype.strip().lower()
                or str(business_cfg.get("default_subtype") or "precision")
            )
            if tkm_subtype not in subtypes:
                missing.append("tkm_business_subtype")
                tkm_subtype = str(business_cfg.get("default_subtype") or "precision")
                reasons.append("未识别TKM业务子类型，按精密模具业务的保守条件评估")
            business_cfg = subtypes.get(tkm_subtype) or business_cfg
        grade_cfg = business_cfg["grades"][risk_level.value]
        monthly_amount = max(0.0, float(profile.monthly_order_amount or 0))
        credit_limit = monthly_amount * float(grade_cfg["credit_months"])
        if monthly_amount <= 0:
            missing.append("monthly_order_amount")
            reasons.append("缺少月度订单额，暂不能形成可用授信额度")

        sufficiency = self.policy.get("data_sufficiency") or {}
        coverage = self._data_coverage(profile, enabled, available_dimensions)
        supplement_reasons: list[str] = []
        minimum_coverage = float(sufficiency.get("minimum_coverage_ratio", 0))
        minimum_dimensions = int(sufficiency.get("minimum_dimension_count", 1))
        if coverage < minimum_coverage:
            supplement_reasons.append(
                f"资料覆盖率 {coverage:.0%}，低于最低要求 {minimum_coverage:.0%}"
            )
        if len(available_dimensions) < minimum_dimensions:
            supplement_reasons.append(
                f"仅有 {len(available_dimensions)} 个有效评分维度，至少需要 {minimum_dimensions} 个"
            )
        if bool(sufficiency.get("require_monthly_order_amount")) and monthly_amount <= 0:
            supplement_reasons.append("缺少月度订单额，无法形成可执行的授信额度")
        if (
            (is_new or is_inactive)
            and bool(self.policy.get("new_customer", {}).get("manual_review_if_missing_financial_and_rating"))
            and dimension_values["financial"] is None
            and dimension_values["external_rating"] is None
        ):
            supplement_reasons.append("新客户同时缺少财务数据和外部信用资料")
        if is_inactive and dimension_values["cooperation"] is None:
            supplement_reasons.append("Inactive客户重新申请时必须补充历史交易与付款记录")
        if supplement_reasons:
            reasons.append("资料不足：" + "；".join(supplement_reasons))

        credit_control = self.credit_control(profile, credit_limit)
        if business_type == "TKM":
            subtype_label = str(business_cfg.get("label") or tkm_subtype)
            reasons.append(
                f"TKM业务子类型：{subtype_label}；"
                f'{business_cfg.get("payment_baseline") or "按批核条件执行"}'
            )
            if profile.purchase_exemption_requested:
                reasons.append("已申请首期采购款豁免，须随正式信用条件单独批核")
        if credit_control["occupied_credit_amount"]:
            reasons.append(
                "授信占用：未收款及在手订单合计 "
                f'{credit_control["occupied_credit_amount"]:,.2f} 元，'
                f'当前可用 {credit_control["available_credit_amount"]:,.2f} 元'
            )
        if credit_control["credit_lock_reasons"]:
            reasons.append("信用控制锁定：" + "；".join(credit_control["credit_lock_reasons"]))

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
            tkm_business_subtype=tkm_subtype,
            purchase_exemption_requested=(
                bool(profile.purchase_exemption_requested)
                if business_type == "TKM"
                else False
            ),
            purchase_exemption_approved=False,
            dimension_scores=dimension_scores,
            missing_fields=sorted(set(missing)),
            reasons=reasons,
            policy_version=str(self.policy["version"]),
            total_credit_limit=round(credit_limit, 2),
            data_coverage_ratio=coverage,
            available_dimensions=available_dimensions,
            requires_supplement=bool(supplement_reasons),
            supplement_reasons=supplement_reasons,
            occupied_credit_amount=credit_control["occupied_credit_amount"],
            available_credit_amount=credit_control["available_credit_amount"],
            credit_locked=credit_control["credit_locked"],
            credit_lock_reasons=credit_control["credit_lock_reasons"],
            rating_resolution=rating_resolution,
        )
