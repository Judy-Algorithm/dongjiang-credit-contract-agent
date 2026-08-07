"""Single case-level decision policy shared by workflow and presentation layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import ApprovalRoute, AuditDecision


@dataclass(frozen=True, slots=True)
class CaseDecision:
    decision: AuditDecision
    approval_route: ApprovalRoute
    waiting_for: str | None
    status: str


def resolve_case_decision(
    decisions: Iterable[AuditDecision | str],
    *,
    rating_requires_review: bool = False,
) -> CaseDecision:
    values = {
        item.value if isinstance(item, AuditDecision) else str(item)
        for item in decisions
    }
    if not values:
        return CaseDecision(
            AuditDecision.BLOCK,
            ApprovalRoute.RETURN_TO_OWNER,
            "sales_revision",
            "blocked",
        )
    if AuditDecision.BLOCK.value in values:
        return CaseDecision(
            AuditDecision.BLOCK,
            ApprovalRoute.RETURN_TO_OWNER,
            "sales_revision",
            "blocked",
        )
    if AuditDecision.SPECIAL_APPROVAL.value in values:
        return CaseDecision(
            AuditDecision.SPECIAL_APPROVAL,
            ApprovalRoute.DIRECTOR_CEO,
            "manager_approval",
            "pending_special_approval",
        )
    if AuditDecision.MANUAL_REVIEW.value in values or rating_requires_review:
        return CaseDecision(
            AuditDecision.MANUAL_REVIEW,
            ApprovalRoute.FINANCE_LEGAL,
            "finance_legal_review",
            "pending_manual_review",
        )
    return CaseDecision(
        AuditDecision.PASS,
        ApprovalRoute.NORMAL,
        "contract_approval",
        "pending_legal_approval",
    )
