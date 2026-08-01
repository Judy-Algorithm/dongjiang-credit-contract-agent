"""JSON-serializable shared state for the Dongjiang LangGraph workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class WorkflowState(TypedDict, total=False):
    case_id: str
    created_at: str
    stage: str
    status: str
    source_system: str
    actor: dict[str, Any]
    applicant: dict[str, Any]
    owner: dict[str, Any]

    customer: dict[str, Any]
    use_cached_credit: bool
    pending_files: list[str]
    pending_document_kind: str | None
    source_files: list[str]
    source_documents: list[dict[str, Any]]
    credit_source_files: list[str]
    contract_source_files: list[str]
    contract_facts: list[dict[str, Any]]

    credit_source: str
    credit_assessment: dict[str, Any] | None
    effective_credit_assessment: dict[str, Any] | None
    credit_status: str
    credit_approval: dict[str, Any]
    credit_approval_request: dict[str, Any] | None
    approval_evidence: list[dict[str, Any]]
    approval_chain: list[dict[str, Any]]
    credit_control: dict[str, Any]
    special_release: dict[str, Any] | None
    exception_approval: dict[str, Any] | None
    contract_reviews: list[dict[str, Any]]
    decision: str | None
    approval_route: str | None
    approval_request: dict[str, Any] | None
    human_decision: dict[str, Any] | None
    waiting_for: str | None

    reports: dict[str, str]
    writeback: dict[str, Any]
    oa_submission: dict[str, Any]
    workflow_plans: Annotated[list[dict[str, Any]], operator.add]
    agent_runs: Annotated[list[dict[str, Any]], operator.add]
    agent_task_results: Annotated[list[dict[str, Any]], operator.add]
    execution_audits: Annotated[list[dict[str, Any]], operator.add]
    agent_incidents: list[dict[str, Any]]
    agent_candidate_reviews: list[dict[str, Any]]
    structured_extractions: list[dict[str, Any]]
    agent_rerun_context: dict[str, Any] | None
    active_workflow_plan: dict[str, Any] | None
    active_agent_task: dict[str, Any] | None
    credit_analysis: dict[str, Any]
    credit_verification: dict[str, Any]
    contract_verifications: list[dict[str, Any]]
    trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
