"""Explicit domain contracts shared by all agent tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"


class AuditDecision(StrEnum):
    PASS = "pass"
    SPECIAL_APPROVAL = "special_approval"
    BLOCK = "block"
    MANUAL_REVIEW = "manual_review"


class ApprovalRoute(StrEnum):
    NORMAL = "normal"
    FINANCE_LEGAL = "finance_legal"
    DIRECTOR_CEO = "director_ceo"
    RETURN_TO_OWNER = "return_to_owner"


@dataclass(slots=True)
class Evidence:
    source: str
    field: str
    value: Any
    confidence: float = 1.0
    excerpt: str = ""
    document_id: str = ""
    fragment_id: str = ""
    location: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExternalRating:
    agency: str
    rating: str
    outlook: str = ""
    rating_date: str = ""
    report_type: str = "主体评级"
    source: str = ""
    confidence: float = 1.0


@dataclass(slots=True)
class CreditProfile:
    customer_name: str
    unified_social_credit_code: str = ""
    crm_customer_id: str = ""
    customer_type: str = "new"
    customer_status: str = "Active"
    business_type: str = "TKP"
    tkm_business_subtype: str = ""
    project_name: str = ""
    contract_amount: float | None = None
    requested_credit_limit: float | None = None
    requested_term_days: int | None = None
    purchase_exemption_requested: bool = False
    currency: str = "CNY"
    application_reason: str = ""
    registered_capital: float | None = None
    years_in_business: int | None = None
    asset_liability_ratio: float | None = None
    net_margin: float | None = None
    current_ratio: float | None = None
    revenue_growth: float | None = None
    monthly_order_amount: float | None = None
    external_rating: str = ""
    rating_outlook: str = ""
    external_ratings: list[ExternalRating] = field(default_factory=list)
    cooperation_years: float | None = None
    overdue_count_12m: int | None = None
    max_overdue_days_12m: int | None = None
    on_time_payment_rate: float | None = None
    outstanding_receivables_amount: float | None = None
    open_order_amount: float | None = None
    current_overdue_days: int | None = None
    last_order_date: str = ""
    major_litigation: bool = False
    tax_or_enforcement_alert: bool = False
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class CreditAssessment:
    score: float
    risk_level: RiskLevel
    approved_credit_limit: float
    recommended_term_days: int
    hard_term_limit_days: int
    max_tail_payment_ratio: float | None
    max_tail_term_days: int | None
    dimension_scores: dict[str, float]
    missing_fields: list[str]
    reasons: list[str]
    policy_version: str
    total_credit_limit: float | None = None
    tkm_business_subtype: str = ""
    purchase_exemption_requested: bool = False
    purchase_exemption_approved: bool = False
    data_coverage_ratio: float = 0.0
    available_dimensions: list[str] = field(default_factory=list)
    requires_supplement: bool = False
    supplement_reasons: list[str] = field(default_factory=list)
    occupied_credit_amount: float = 0.0
    available_credit_amount: float | None = None
    credit_locked: bool = False
    credit_lock_reasons: list[str] = field(default_factory=list)
    rating_resolution: dict[str, Any] = field(default_factory=dict)
    assessed_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ContractFacts:
    contract_name: str = ""
    customer_name: str = ""
    business_type: str = "TKP"
    amount: float | None = None
    requested_credit: float | None = None
    payment_term_days: int | None = None
    tail_payment_ratio: float | None = None
    tail_payment_term_days: int | None = None
    uses_purchase_exemption: bool = False
    contract_term_years: float | None = None
    max_penalty_ratio: float | None = None
    currency: str = "CNY"
    language: str = "zh"
    has_parties: bool = False
    has_subject: bool = False
    has_payment: bool = False
    has_breach: bool = False
    has_ip: bool = False
    has_confidentiality: bool = False
    has_termination: bool = False
    has_dispute_resolution: bool = False
    raw_text: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    document_id: str = ""


@dataclass(slots=True)
class RiskFinding:
    rule_id: str
    title: str
    level: RiskLevel
    message: str
    suggestion: str
    clause_excerpt: str = ""
    requires_special_approval: bool = False
    hard_stop: bool = False
    document_id: str = ""
    fragment_id: str = ""
    location: dict[str, Any] = field(default_factory=dict)
    evidence_query: str = ""


@dataclass(slots=True)
class ContractReview:
    decision: AuditDecision
    approval_route: ApprovalRoute
    risk_level: RiskLevel
    findings: list[RiskFinding]
    summary: str
    credit_cross_check: dict[str, Any]
    ai_assistance: dict[str, Any] = field(default_factory=dict)
    reviewed_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AuditCase:
    customer: CreditProfile
    contracts: list[ContractFacts]
    case_id: str = field(default_factory=lambda: f"DJ-{uuid4().hex[:10].upper()}")
    source_files: list[str] = field(default_factory=list)
    source_documents: list[dict[str, Any]] = field(default_factory=list)
    credit_assessment: CreditAssessment | None = None
    model_credit_assessment: CreditAssessment | None = None
    credit_status: str = "draft"
    credit_approval: dict[str, Any] = field(default_factory=dict)
    approval_evidence: list[dict[str, Any]] = field(default_factory=list)
    approval_chain: list[dict[str, Any]] = field(default_factory=list)
    credit_control: dict[str, Any] = field(default_factory=dict)
    special_release: dict[str, Any] = field(default_factory=dict)
    exception_approval: dict[str, Any] = field(default_factory=dict)
    writeback: dict[str, Any] = field(default_factory=dict)
    contract_reviews: list[ContractReview] = field(default_factory=list)
    status: str = "created"
    trace: list[dict[str, Any]] = field(default_factory=list)
    workflow_plans: list[dict[str, Any]] = field(default_factory=list)
    agent_runs: list[dict[str, Any]] = field(default_factory=list)
    credit_verification: dict[str, Any] = field(default_factory=dict)
    contract_verifications: list[dict[str, Any]] = field(default_factory=list)
    applicant: dict[str, Any] = field(default_factory=dict)
    owner: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def add_trace(self, stage: str, message: str, **data: Any) -> None:
        self.trace.append({"ts": utc_now(), "stage": stage, "message": message, "data": data})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
