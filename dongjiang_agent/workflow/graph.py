"""LangGraph topology for the end-to-end credit and contract workflow."""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from .nodes import WorkflowNodes
from .state import WorkflowState


def _credit_route(
    state: WorkflowState,
) -> Literal["credit_workflow", "contract_gate", "await_special_release"]:
    if (
        state.get("credit_status") == "effective"
        and state.get("effective_credit_assessment")
    ):
        assessment = state.get("effective_credit_assessment") or {}
        return (
            "await_special_release"
            if assessment.get("credit_locked") and not state.get("special_release")
            else "contract_gate"
        )
    return "credit_workflow"


def _contract_route(state: WorkflowState) -> Literal["contract_workflow", "await_contract"]:
    return "contract_workflow" if state.get("contract_facts") else "await_contract"


def _credit_assessment_route(
    state: WorkflowState,
) -> Literal["prepare_credit_approval", "await_credit_supplement"]:
    assessment = state.get("credit_assessment") or {}
    return (
        "await_credit_supplement"
        if assessment.get("requires_supplement")
        else "prepare_credit_approval"
    )


def _credit_control_route(
    state: WorkflowState,
) -> Literal["await_special_release", "contract_gate"]:
    assessment = state.get("effective_credit_assessment") or {}
    return (
        "await_special_release"
        if assessment.get("credit_locked") and not state.get("special_release")
        else "contract_gate"
    )


def _decision_route(
    state: WorkflowState,
) -> Literal[
    "await_sales_revision",
    "await_manager_approval",
    "await_finance_legal",
    "finalize",
]:
    decision = state.get("decision")
    if decision == "block":
        return "await_sales_revision"
    if decision == "special_approval":
        return "await_manager_approval"
    if decision == "manual_review":
        return "await_finance_legal"
    return "finalize"


def build_credit_subgraph(nodes: WorkflowNodes):
    builder = StateGraph(WorkflowState)
    builder.add_node("score_credit", nodes.score_credit)
    builder.add_edge(START, "score_credit")
    builder.add_edge("score_credit", END)
    return builder.compile()


def build_contract_subgraph(nodes: WorkflowNodes):
    builder = StateGraph(WorkflowState)
    builder.add_node("review_contracts", nodes.review_contracts)
    builder.add_edge(START, "review_contracts")
    builder.add_edge("review_contracts", END)
    return builder.compile()


def build_workflow(nodes: WorkflowNodes, *, checkpointer):
    """Build one parent graph with two bounded business subgraphs."""
    credit_subgraph = build_credit_subgraph(nodes)
    contract_subgraph = build_contract_subgraph(nodes)

    def call_credit_subgraph(state: WorkflowState) -> dict:
        result = credit_subgraph.invoke(state)
        trace_offset = len(state.get("trace") or [])
        error_offset = len(state.get("errors") or [])
        return {
            "stage": result.get("stage"),
            "status": result.get("status"),
            "credit_source": result.get("credit_source"),
            "credit_assessment": result.get("credit_assessment"),
            "effective_credit_assessment": result.get(
                "effective_credit_assessment"
            ),
            "credit_status": result.get("credit_status"),
            "waiting_for": result.get("waiting_for"),
            "trace": list(result.get("trace") or [])[trace_offset:],
            "errors": list(result.get("errors") or [])[error_offset:],
        }

    def call_contract_subgraph(state: WorkflowState) -> dict:
        result = contract_subgraph.invoke(state)
        trace_offset = len(state.get("trace") or [])
        error_offset = len(state.get("errors") or [])
        return {
            "stage": result.get("stage"),
            "contract_reviews": result.get("contract_reviews"),
            "trace": list(result.get("trace") or [])[trace_offset:],
            "errors": list(result.get("errors") or [])[error_offset:],
        }

    builder = StateGraph(WorkflowState)
    builder.add_node("create_case", nodes.create_case)
    builder.add_node("ingest_credit_documents", nodes.ingest_credit_documents)
    builder.add_node("ingest_contract_documents", nodes.ingest_contract_documents)
    builder.add_node("check_credit_cache", nodes.check_credit_cache)
    builder.add_node("credit_workflow", call_credit_subgraph)
    builder.add_node("prepare_credit_approval", nodes.prepare_credit_approval)
    builder.add_node("await_credit_approval", nodes.await_credit_approval)
    builder.add_node("await_credit_supplement", nodes.await_credit_supplement)
    builder.add_node("activate_credit", nodes.activate_credit)
    builder.add_node("await_special_release", nodes.await_special_release)
    builder.add_node("contract_gate", nodes.contract_gate)
    builder.add_node("await_contract", nodes.await_contract)
    builder.add_node("contract_workflow", call_contract_subgraph)
    builder.add_node("decide", nodes.decide)
    builder.add_node("await_sales_revision", nodes.await_sales_revision)
    builder.add_node("await_manager_approval", nodes.await_manager_approval)
    builder.add_node("await_finance_legal", nodes.await_finance_legal)
    builder.add_node("finalize", nodes.finalize)

    builder.add_edge(START, "create_case")
    builder.add_edge("create_case", "ingest_credit_documents")
    builder.add_edge("ingest_credit_documents", "check_credit_cache")
    builder.add_conditional_edges("check_credit_cache", _credit_route)
    builder.add_conditional_edges("credit_workflow", _credit_assessment_route)
    builder.add_edge("prepare_credit_approval", "await_credit_approval")
    builder.add_conditional_edges("activate_credit", _credit_control_route)
    builder.add_conditional_edges("contract_gate", _contract_route)
    builder.add_edge("ingest_contract_documents", "contract_workflow")
    builder.add_edge("contract_workflow", "decide")
    builder.add_conditional_edges("decide", _decision_route)
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
