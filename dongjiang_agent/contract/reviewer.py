"""Contract review combines legal rules with the customer's credit decision."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import load_policy
from ..domain.models import (
    ApprovalRoute,
    AuditDecision,
    ContractFacts,
    ContractReview,
    CreditAssessment,
    RiskFinding,
    RiskLevel,
)


class ContractReviewEngine:
    def __init__(
        self,
        credit_policy: dict[str, Any] | None = None,
        contract_policy: dict[str, Any] | None = None,
    ) -> None:
        self.credit_policy = credit_policy or load_policy()
        if contract_policy is None:
            path = Path(__file__).parents[1] / "config" / "contract_rules.json"
            contract_policy = json.loads(path.read_text(encoding="utf-8"))
        self.contract_policy = contract_policy

    @staticmethod
    def _finding(
        rule_id: str,
        title: str,
        level: RiskLevel,
        message: str,
        suggestion: str,
        *,
        excerpt: str = "",
        approval: bool = False,
        hard_stop: bool = False,
    ) -> RiskFinding:
        return RiskFinding(
            rule_id=rule_id,
            title=title,
            level=level,
            message=message,
            suggestion=suggestion,
            clause_excerpt=excerpt,
            requires_special_approval=approval,
            hard_stop=hard_stop,
        )

    def review(self, contract: ContractFacts, credit: CreditAssessment) -> ContractReview:
        findings: list[RiskFinding] = []
        for field, label in self.contract_policy["required_clauses"].items():
            if not bool(getattr(contract, field, False)):
                findings.append(self._finding(
                    f"COMPLETE-{field.upper()}",
                    f"缺少{label}条款",
                    RiskLevel.MEDIUM,
                    f"未识别到完整的{label}约定。",
                    f"补充或人工定位{label}条款后再提交审批。",
                ))

        business_type = contract.business_type.strip().upper() or "TKP"
        business_cfg = self.credit_policy["business_rules"].get(
            business_type, self.credit_policy["business_rules"]["TKP"]
        )

        if contract.requested_credit is not None and contract.requested_credit > credit.approved_credit_limit:
            findings.append(self._finding(
                "CREDIT-OVER-LIMIT",
                "合同授信超过批准额度",
                RiskLevel.HIGH,
                f"合同要求授信 {contract.requested_credit:,.2f}，批准额度仅 {credit.approved_credit_limit:,.2f}。",
                "降低赊销额度、增加预付款/担保，或发起市场总监/财务特批。",
                approval=True,
            ))

        if contract.payment_term_days is not None:
            if business_type == "TKP" and contract.payment_term_days > int(business_cfg["hard_max_term_days"]):
                findings.append(self._finding(
                    "TKP-HARD-TERM",
                    "TKP账期突破90天死线",
                    RiskLevel.BLOCKER,
                    f"合同账期 {contract.payment_term_days} 天，超过 TKP 90 天硬底线。",
                    "原则阻断；与客户修改至90天以内，或按制度走底线例外审批。",
                    hard_stop=True,
                ))
            elif contract.payment_term_days > credit.recommended_term_days:
                findings.append(self._finding(
                    "CREDIT-OVER-TERM",
                    "合同账期超过信用建议",
                    RiskLevel.HIGH,
                    f"合同账期 {contract.payment_term_days} 天，当前风险等级建议不超过 {credit.recommended_term_days} 天。",
                    "缩短账期或提交财务/市场总监特别审批。",
                    approval=True,
                ))

        if business_type == "TKM":
            hard_ratio = float(business_cfg["hard_max_tail_ratio"])
            hard_days = int(business_cfg["hard_max_tail_term_days"])
            if contract.tail_payment_ratio is not None and contract.tail_payment_ratio > hard_ratio:
                findings.append(self._finding(
                    "TKM-HARD-TAIL-RATIO",
                    "TKM尾款比例突破40%底线",
                    RiskLevel.BLOCKER,
                    f"尾款比例 {contract.tail_payment_ratio:.0%}，超过 40% 硬底线。",
                    "原则阻断；提高前期回款比例或按制度申请底线例外。",
                    hard_stop=True,
                ))
            elif (
                contract.tail_payment_ratio is not None
                and credit.max_tail_payment_ratio is not None
                and contract.tail_payment_ratio > credit.max_tail_payment_ratio
            ):
                findings.append(self._finding(
                    "TKM-OVER-TAIL-RATIO",
                    "TKM尾款比例超过信用建议",
                    RiskLevel.HIGH,
                    f"尾款比例 {contract.tail_payment_ratio:.0%}，建议上限 {credit.max_tail_payment_ratio:.0%}。",
                    "调整付款节点或提交特别审批。",
                    approval=True,
                ))
            if contract.tail_payment_term_days is not None and contract.tail_payment_term_days > hard_days:
                findings.append(self._finding(
                    "TKM-HARD-TAIL-TERM",
                    "TKM尾款账期突破半年底线",
                    RiskLevel.BLOCKER,
                    f"尾款账期 {contract.tail_payment_term_days} 天，超过半年硬底线。",
                    "原则阻断；将尾款支付期限调整至180天以内。",
                    hard_stop=True,
                ))
            elif (
                contract.tail_payment_term_days is not None
                and credit.max_tail_term_days is not None
                and contract.tail_payment_term_days > credit.max_tail_term_days
            ):
                findings.append(self._finding(
                    "TKM-OVER-TAIL-TERM",
                    "TKM尾款账期超过信用建议",
                    RiskLevel.HIGH,
                    f"尾款账期 {contract.tail_payment_term_days} 天，建议上限 {credit.max_tail_term_days} 天。",
                    "缩短尾款期限或提交特别审批。",
                    approval=True,
                ))

        for rule in self.contract_policy["legal_patterns"]:
            match = re.search(rule["pattern"], contract.raw_text, flags=re.IGNORECASE)
            if not match:
                continue
            excerpt = re.sub(
                r"\s+", " ", contract.raw_text[max(0, match.start() - 60): match.end() + 80]
            ).strip()
            level = RiskLevel(str(rule["level"]))
            findings.append(self._finding(
                rule["rule_id"],
                rule["title"],
                level,
                f"合同文本命中法律风险模式：{match.group(0)}",
                rule["suggestion"],
                excerpt=excerpt,
                approval=level == RiskLevel.HIGH,
            ))

        hard_stop = any(item.hard_stop for item in findings)
        approval = any(item.requires_special_approval for item in findings)
        completeness_missing = any(item.rule_id.startswith("COMPLETE-") for item in findings)
        if hard_stop:
            decision = AuditDecision.BLOCK
            route = ApprovalRoute.RETURN_TO_OWNER
            risk = RiskLevel.BLOCKER
        elif approval:
            decision = AuditDecision.SPECIAL_APPROVAL
            route = ApprovalRoute.DIRECTOR_CEO
            risk = RiskLevel.HIGH
        elif completeness_missing:
            decision = AuditDecision.MANUAL_REVIEW
            route = ApprovalRoute.FINANCE_LEGAL
            risk = RiskLevel.MEDIUM
        else:
            decision = AuditDecision.PASS
            route = ApprovalRoute.NORMAL
            risk = RiskLevel.LOW

        cross_check = {
            "credit_score": credit.score,
            "credit_risk_level": credit.risk_level.value,
            "approved_credit_limit": credit.approved_credit_limit,
            "contract_requested_credit": contract.requested_credit,
            "recommended_term_days": credit.recommended_term_days,
            "contract_term_days": contract.payment_term_days,
            "policy_version": credit.policy_version,
        }
        summary = {
            AuditDecision.BLOCK: "命中东江底线条款，原则阻断并退回业务协商修改。",
            AuditDecision.SPECIAL_APPROVAL: "未突破硬底线，但存在超授信/超建议账期或重大法律风险，进入特批。",
            AuditDecision.MANUAL_REVIEW: "未命中硬风险，但关键信息或条款不完整，需要法务/财务复核。",
            AuditDecision.PASS: "未发现需要阻断或特批的风险，可进入正常审批。",
        }[decision]
        return ContractReview(decision, route, risk, findings, summary, cross_check)
