"""Validated construction of domain objects from untrusted dictionaries."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from .models import (
    ApprovalRoute,
    AuditDecision,
    ContractFacts,
    ContractReview,
    CreditAssessment,
    CreditProfile,
    Evidence,
    ExternalRating,
    RiskFinding,
    RiskLevel,
)


def profile_from_dict(payload: dict[str, Any]) -> CreditProfile:
    allowed = {item.name for item in fields(CreditProfile)}
    clean = {key: value for key, value in dict(payload or {}).items() if key in allowed}
    clean["external_ratings"] = [
        item if isinstance(item, ExternalRating) else ExternalRating(**item)
        for item in clean.get("external_ratings") or []
        if isinstance(item, (dict, ExternalRating))
    ]
    clean["evidence"] = [
        item if isinstance(item, Evidence) else Evidence(**item)
        for item in clean.get("evidence") or []
        if isinstance(item, (dict, Evidence))
    ]
    if not str(clean.get("customer_name") or "").strip():
        raise ValueError("customer_name 为必填字段。")
    return CreditProfile(**clean)


def assessment_from_dict(payload: dict[str, Any] | None) -> CreditAssessment | None:
    if not payload:
        return None
    return CreditAssessment(
        score=float(payload["score"]),
        risk_level=RiskLevel(payload["risk_level"]),
        approved_credit_limit=float(payload["approved_credit_limit"]),
        recommended_term_days=int(payload["recommended_term_days"]),
        hard_term_limit_days=int(payload["hard_term_limit_days"]),
        max_tail_payment_ratio=payload.get("max_tail_payment_ratio"),
        max_tail_term_days=payload.get("max_tail_term_days"),
        tkm_business_subtype=str(payload.get("tkm_business_subtype") or ""),
        purchase_exemption_requested=bool(
            payload.get("purchase_exemption_requested")
        ),
        purchase_exemption_approved=bool(
            payload.get("purchase_exemption_approved")
        ),
        dimension_scores=dict(payload.get("dimension_scores") or {}),
        missing_fields=list(payload.get("missing_fields") or []),
        reasons=list(payload.get("reasons") or []),
        policy_version=str(payload.get("policy_version") or ""),
        total_credit_limit=(
            float(payload["total_credit_limit"])
            if payload.get("total_credit_limit") is not None
            else float(payload["approved_credit_limit"])
        ),
        data_coverage_ratio=float(payload.get("data_coverage_ratio") or 0),
        available_dimensions=list(payload.get("available_dimensions") or []),
        requires_supplement=bool(payload.get("requires_supplement")),
        supplement_reasons=list(payload.get("supplement_reasons") or []),
        occupied_credit_amount=float(payload.get("occupied_credit_amount") or 0),
        available_credit_amount=(
            float(payload["available_credit_amount"])
            if payload.get("available_credit_amount") is not None
            else None
        ),
        credit_locked=bool(payload.get("credit_locked")),
        credit_lock_reasons=list(payload.get("credit_lock_reasons") or []),
        rating_resolution=dict(payload.get("rating_resolution") or {}),
        assessed_at=str(payload.get("assessed_at") or ""),
    )


def contract_facts_from_dict(payload: dict[str, Any]) -> ContractFacts:
    clean = dict(payload)
    clean["evidence"] = [
        item if isinstance(item, Evidence) else Evidence(**item)
        for item in clean.get("evidence") or []
        if isinstance(item, (dict, Evidence))
    ]
    return ContractFacts(**clean)


def review_from_dict(payload: dict[str, Any]) -> ContractReview:
    findings = [
        RiskFinding(
            rule_id=str(item["rule_id"]),
            title=str(item["title"]),
            level=RiskLevel(item["level"]),
            message=str(item["message"]),
            suggestion=str(item["suggestion"]),
            clause_excerpt=str(item.get("clause_excerpt") or ""),
            requires_special_approval=bool(item.get("requires_special_approval")),
            hard_stop=bool(item.get("hard_stop")),
        )
        for item in payload.get("findings") or []
    ]
    return ContractReview(
        decision=AuditDecision(payload["decision"]),
        approval_route=ApprovalRoute(payload["approval_route"]),
        risk_level=RiskLevel(payload["risk_level"]),
        findings=findings,
        summary=str(payload.get("summary") or ""),
        credit_cross_check=dict(payload.get("credit_cross_check") or {}),
        reviewed_at=str(payload.get("reviewed_at") or ""),
    )
