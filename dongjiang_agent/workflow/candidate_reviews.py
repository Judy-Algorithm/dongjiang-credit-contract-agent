"""Safe comparison and lifecycle helpers for isolated Agent rerun candidates."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


CANDIDATE_WAITING_FOR = {
    "credit": {"credit_approval"},
    "contract": {"contract_approval", "finance_legal_review", "manager_approval"},
}

_CREDIT_FIELDS = (
    ("score", "信用分"),
    ("risk_level", "风险等级"),
    ("approved_credit_limit", "建议授信额度"),
    ("recommended_term_days", "建议账期"),
    ("requires_supplement", "需要补件"),
    ("credit_locked", "信用控制锁定"),
    ("verification_status", "独立核验"),
)


def candidate_fingerprint(rerun: dict[str, Any]) -> str:
    """Bind a review request to one immutable, safe candidate result."""
    payload = {
        "incident_id": str(rerun.get("incident_id") or ""),
        "plan_id": str(rerun.get("plan_id") or ""),
        "task_id": str(rerun.get("task_id") or ""),
        "status": str(rerun.get("status") or ""),
        "completed_at": str(rerun.get("completed_at") or ""),
        "candidate_summary": rerun.get("candidate_summary") or {},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest_candidate(incident: dict[str, Any]) -> dict[str, Any] | None:
    for rerun in reversed(list(incident.get("rerun_history") or [])):
        if str(rerun.get("status") or "") in {"completed", "degraded", "reused"}:
            return deepcopy(rerun)
    return None


def _official_credit(state: dict[str, Any]) -> dict[str, Any]:
    assessment = dict(
        state.get("effective_credit_assessment")
        or state.get("credit_assessment")
        or {}
    )
    verification = dict(state.get("credit_verification") or {})
    return {
        "score": assessment.get("score"),
        "risk_level": assessment.get("risk_level"),
        "approved_credit_limit": assessment.get("approved_credit_limit"),
        "recommended_term_days": assessment.get("recommended_term_days"),
        "requires_supplement": bool(assessment.get("requires_supplement")),
        "credit_locked": bool(assessment.get("credit_locked")),
        "verification_status": verification.get("status"),
    }


def _contract_rows_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    contracts = list(state.get("contract_facts") or [])
    verifications = {
        str(item.get("document_id") or ""): dict(item)
        for item in state.get("contract_verifications") or []
    }
    rows: list[dict[str, Any]] = []
    for index, review in enumerate(state.get("contract_reviews") or []):
        document_id = str(
            review.get("document_id")
            or (contracts[index] if index < len(contracts) else {}).get("document_id")
            or f"contract-{index + 1}"
        )
        findings = list(review.get("findings") or [])
        assistance = dict(review.get("ai_assistance") or {})
        verification = verifications.get(document_id) or {}
        rows.append(
            {
                "document_id": document_id,
                "decision": review.get("decision"),
                "risk_level": review.get("risk_level"),
                "rule_finding_count": len(findings),
                "ai_finding_count": len(assistance.get("findings") or []),
                "rule_ids": sorted(
                    {
                        str(item.get("rule_id") or "")
                        for item in findings
                        if item.get("rule_id")
                    }
                ),
                "verification_status": verification.get("status"),
                "evidence_coverage": verification.get("evidence_coverage"),
            }
        )
    return rows


def build_candidate_comparison(
    state: dict[str, Any], incident: dict[str, Any], rerun: dict[str, Any] | None = None
) -> dict[str, Any]:
    candidate = rerun or latest_candidate(incident)
    if not candidate:
        raise ValueError("该异常尚未形成可比较的候选结果。")
    agent = str(incident.get("agent") or "")
    summary = dict(candidate.get("candidate_summary") or {})
    if agent == "credit":
        official = _official_credit(state)
        candidate_values = {key: summary.get(key) for key, _ in _CREDIT_FIELDS}
        differences = [
            {
                "field": key,
                "label": label,
                "official": official.get(key),
                "candidate": candidate_values.get(key),
                "changed": official.get(key) != candidate_values.get(key),
            }
            for key, label in _CREDIT_FIELDS
        ]
        return {
            "agent": agent,
            "mode": "field",
            "differences": differences,
            "changed_count": sum(bool(item["changed"]) for item in differences),
        }
    if agent == "contract":
        official_rows = _contract_rows_from_state(state)
        candidate_rows = [dict(item) for item in summary.get("documents") or []]
        official_by_id = {str(item.get("document_id") or ""): item for item in official_rows}
        candidate_by_id = {str(item.get("document_id") or ""): item for item in candidate_rows}
        documents: list[dict[str, Any]] = []
        for document_id in sorted(set(official_by_id) | set(candidate_by_id)):
            official = official_by_id.get(document_id) or {}
            proposed = candidate_by_id.get(document_id) or {}
            changed_fields = [
                key
                for key in (
                    "decision",
                    "risk_level",
                    "rule_finding_count",
                    "ai_finding_count",
                    "rule_ids",
                    "verification_status",
                    "evidence_coverage",
                )
                if official.get(key) != proposed.get(key)
            ]
            documents.append(
                {
                    "document_id": document_id,
                    "official": official,
                    "candidate": proposed,
                    "changed_fields": changed_fields,
                    "changed": bool(changed_fields),
                }
            )
        return {
            "agent": agent,
            "mode": "document",
            "documents": documents,
            "changed_count": sum(bool(item["changed"]) for item in documents),
        }
    raise ValueError("未知的Agent候选类型。")


def safe_candidate_review_view(review: dict[str, Any]) -> dict[str, Any]:
    requested_by = dict(review.get("requested_by") or {})
    decided_by = dict(review.get("decided_by") or {})
    return {
        "request_id": review.get("request_id"),
        "incident_id": review.get("incident_id"),
        "plan_id": review.get("plan_id"),
        "agent": review.get("agent"),
        "status": review.get("status"),
        "eligible_waiting_for": review.get("eligible_waiting_for"),
        "candidate_ref": str(review.get("candidate_fingerprint") or "")[:12],
        "requested_by": {
            "user_id": requested_by.get("user_id"),
            "display_name": requested_by.get("display_name"),
        },
        "requested_at": review.get("requested_at"),
        "reason_recorded": bool(str(review.get("reason") or "").strip()),
        "decision": {
            key: (review.get("decision") or {}).get(key)
            for key in (
                "action",
                "waiting_for",
                "official_state_changed",
                "reason_recorded",
            )
            if key in (review.get("decision") or {})
        },
        "decided_by": {
            "user_id": decided_by.get("user_id"),
            "display_name": decided_by.get("display_name"),
        },
        "decided_at": review.get("decided_at"),
    }


def apply_contract_candidate(
    reviews: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    candidate_summary: dict[str, Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Adopt only decision metadata; keep official evidence and finding text intact."""
    proposed = {
        str(item.get("document_id") or ""): dict(item)
        for item in candidate_summary.get("documents") or []
    }
    adopted: list[dict[str, Any]] = []
    for index, raw in enumerate(reviews):
        review = deepcopy(raw)
        document_id = str(
            review.get("document_id")
            or (contracts[index] if index < len(contracts) else {}).get("document_id")
            or f"contract-{index + 1}"
        )
        candidate = proposed.get(document_id)
        if candidate:
            review["decision"] = candidate.get("decision") or review.get("decision")
            review["risk_level"] = candidate.get("risk_level") or review.get("risk_level")
            review["candidate_adoption"] = {
                "request_id": request_id,
                "rule_finding_count": candidate.get("rule_finding_count"),
                "ai_finding_count": candidate.get("ai_finding_count"),
                "rule_ids": list(candidate.get("rule_ids") or []),
                "verification_status": candidate.get("verification_status"),
                "evidence_coverage": candidate.get("evidence_coverage"),
            }
        adopted.append(review)
    return adopted
