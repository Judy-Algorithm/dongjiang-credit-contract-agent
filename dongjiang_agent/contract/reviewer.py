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

        available_credit = (
            credit.available_credit_amount
            if credit.available_credit_amount is not None
            else credit.approved_credit_limit
        )
        if contract.requested_credit is not None and contract.requested_credit > available_credit:
            findings.append(self._finding(
                "CREDIT-OVER-LIMIT",
                "合同授信超过当前可用额度",
                RiskLevel.HIGH,
                f"合同要求授信 {contract.requested_credit:,.2f}，总批准额度 "
                f"{credit.approved_credit_limit:,.2f}，已占用 {credit.occupied_credit_amount:,.2f}，"
                f"当前可用仅 {available_credit:,.2f}。",
                "降低赊销额度、增加预付款/担保，或发起市场总监/财务特批。",
                approval=True,
            ))

        if contract.payment_term_days is not None:
            if business_type == "TKP" and contract.payment_term_days > int(business_cfg["hard_max_term_days"]):
                findings.append(self._finding(
                    "TKP-SPECIAL-TERM",
                    "TKP账期超过Net 90天",
                    RiskLevel.HIGH,
                    f"合同账期 {contract.payment_term_days} 天，超过 TKP 一般不超过 Net 90 天的条件。",
                    "核验终端项目统一账期批核或上传市场总监特别账期批准材料。",
                    approval=True,
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
            subtype = (
                credit.tkm_business_subtype
                or str(business_cfg.get("default_subtype") or "precision")
            )
            subtype_cfg = (business_cfg.get("subtypes") or {}).get(subtype) or business_cfg
            hard_ratio = float(subtype_cfg["hard_max_tail_ratio"])
            hard_days = int(subtype_cfg["hard_max_tail_term_days"])
            if contract.tail_payment_ratio is not None and contract.tail_payment_ratio > hard_ratio:
                findings.append(self._finding(
                    "TKM-SPECIAL-TAIL-RATIO",
                    f"TKM走模后放账比例超过{hard_ratio:.0%}",
                    RiskLevel.HIGH,
                    f"尾款比例 {contract.tail_payment_ratio:.0%}，超过该TKM子类型一般走模后放账比例 {hard_ratio:.0%}。",
                    "调整走模前收款比例，或上传市场总监特别比例批准材料。",
                    approval=True,
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
                    "TKM-SPECIAL-TAIL-TERM",
                    f"TKM尾款账期超过{hard_days}天",
                    RiskLevel.HIGH,
                    f"尾款账期 {contract.tail_payment_term_days} 天，超过该TKM子类型一般条件 {hard_days} 天。",
                    "缩短尾款期限，或上传市场总监特别账期批准材料。",
                    approval=True,
                ))
            if contract.uses_purchase_exemption and not credit.purchase_exemption_approved:
                findings.append(self._finding(
                    "TKM-PURCHASE-EXEMPTION-NOT-APPROVED",
                    "首期采购款豁免尚未获批",
                    RiskLevel.HIGH,
                    "合同或订单要求未收回首期款即采购项目物料，但正式信用条件未批准采购豁免权。",
                    "走模或采购前收妥首期款，或取得首期采购款豁免批核并回写SAP/PM。",
                    approval=True,
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

        if contract.contract_term_years is not None and contract.contract_term_years > 5:
            findings.append(self._finding(
                "DJ-CONTRACT-TERM-OVER-5Y",
                "合同有效期超过5年",
                RiskLevel.MEDIUM,
                f"识别到合同有效期约 {contract.contract_term_years:g} 年。",
                "将合同有效期调整至5年以内，或由法务核验续期和退出机制。",
            ))

        if contract.max_penalty_ratio is not None and contract.max_penalty_ratio > 0.5:
            findings.append(self._finding(
                "DJ-PENALTY-OVER-50PCT",
                "违约金比例超过合同金额50%",
                RiskLevel.HIGH,
                f"识别到最高违约金比例 {contract.max_penalty_ratio:.0%}。",
                "核对违约金与损失赔偿的合计口径并调整至合同金额50%以内。",
                approval=True,
            ))

        for rule in self.contract_policy["legal_patterns"]:
            applies_to = {
                str(item).upper() for item in rule.get("business_types") or []
            }
            if applies_to and business_type not in applies_to:
                continue
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
                hard_stop=str(rule.get("route") or "") == "block",
            ))

        hard_stop = any(item.hard_stop for item in findings)
        approval = any(item.requires_special_approval for item in findings)
        manual_review = any(
            item.rule_id.startswith("COMPLETE-")
            or (item.level == RiskLevel.MEDIUM and not item.requires_special_approval)
            for item in findings
        )
        if hard_stop:
            decision = AuditDecision.BLOCK
            route = ApprovalRoute.RETURN_TO_OWNER
            risk = RiskLevel.BLOCKER
        elif approval:
            decision = AuditDecision.SPECIAL_APPROVAL
            route = ApprovalRoute.DIRECTOR_CEO
            risk = RiskLevel.HIGH
        elif manual_review:
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
            "occupied_credit_amount": credit.occupied_credit_amount,
            "available_credit_amount": available_credit,
            "contract_requested_credit": contract.requested_credit,
            "recommended_term_days": credit.recommended_term_days,
            "contract_term_days": contract.payment_term_days,
            "tkm_business_subtype": credit.tkm_business_subtype,
            "purchase_exemption_approved": credit.purchase_exemption_approved,
            "policy_version": credit.policy_version,
        }
        summary = {
            AuditDecision.BLOCK: "命中东江底线条款，原则阻断并退回业务协商修改。",
            AuditDecision.SPECIAL_APPROVAL: "未突破硬底线，但存在超授信/超建议账期或重大法律风险，进入特批。",
            AuditDecision.MANUAL_REVIEW: "未命中硬风险，但关键信息或条款不完整，需要法务/财务复核。",
            AuditDecision.PASS: "未发现需要阻断或特批的风险，可进入正常审批。",
        }[decision]
        return ContractReview(decision, route, risk, findings, summary, cross_check)
