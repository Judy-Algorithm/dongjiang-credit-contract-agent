"""LangGraph topology for the end-to-end credit and contract workflow."""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..runtime import AgentDescriptor, AgentRegistry, InvocationContext, LocalAgentRegistry
from .dynamic import assert_plan_integrity
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
    "await_contract_approval",
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
    if state.get("waiting_for") == "contract_approval":
        return "await_contract_approval"
    return "finalize"


def build_credit_subgraph(nodes: WorkflowNodes):
    builder = StateGraph(WorkflowState)
    builder.add_node("plan_credit", nodes.plan_credit_workflow)
    builder.add_node("run_credit_analysis", nodes.run_credit_analysis)
    builder.add_node("synthesize_credit", nodes.synthesize_credit_analysis)
    builder.add_node("score_credit", nodes.score_credit)
    builder.add_node("verify_credit", nodes.verify_credit)
    builder.add_edge(START, "plan_credit")

    def dispatch_credit(state: WorkflowState):
        plan = dict(state.get("active_workflow_plan") or {})
        assert_plan_integrity(plan)
        return [
            Send("run_credit_analysis", {**state, "active_agent_task": task})
            for task in plan.get("tasks") or []
            if task.get("phase") == "analysis"
        ]

    builder.add_conditional_edges("plan_credit", dispatch_credit)
    builder.add_edge("run_credit_analysis", "synthesize_credit")
    builder.add_edge("synthesize_credit", "score_credit")
    builder.add_edge("score_credit", "verify_credit")
    builder.add_edge("verify_credit", END)
    return builder.compile()


def build_contract_subgraph(nodes: WorkflowNodes):
    builder = StateGraph(WorkflowState)
    builder.add_node("plan_contract", nodes.plan_contract_workflow)
    builder.add_node("run_contract_analysis", nodes.run_contract_analysis)
    builder.add_node("synthesize_contracts", nodes.synthesize_contract_reviews)
    builder.add_node("verify_contracts", nodes.verify_contract_reviews)
    builder.add_edge(START, "plan_contract")

    def dispatch_contract(state: WorkflowState):
        plan = dict(state.get("active_workflow_plan") or {})
        assert_plan_integrity(plan)
        return [
            Send("run_contract_analysis", {**state, "active_agent_task": task})
            for task in plan.get("tasks") or []
            if task.get("phase") == "analysis"
        ]

    builder.add_conditional_edges("plan_contract", dispatch_contract)
    builder.add_edge("run_contract_analysis", "synthesize_contracts")
    builder.add_edge("synthesize_contracts", "verify_contracts")
    builder.add_edge("verify_contracts", END)
    return builder.compile()


def build_workflow(
    nodes: WorkflowNodes,
    *,
    checkpointer,
    agent_registry: AgentRegistry | None = None,
):
    """Build one parent graph with two bounded business subgraphs."""
    credit_subgraph = build_credit_subgraph(nodes)
    contract_subgraph = build_contract_subgraph(nodes)
    registry = agent_registry or LocalAgentRegistry()
    if isinstance(registry, LocalAgentRegistry):
        registered = {item["agent_id"] for item in registry.discover()}
        if "credit_review" not in registered:
            registry.register(
                AgentDescriptor(
                    agent_id="credit_review",
                    name="信用评审子 Agent",
                    description="动态分析信用资料并输出评分、额度、账期与独立核验结果。",
                    capabilities=("credit_review", "credit_dynamic_workflow"),
                ),
                credit_subgraph.invoke,
            )
        if "contract_review" not in registered:
            registry.register(
                AgentDescriptor(
                    agent_id="contract_review",
                    name="合同评审子 Agent",
                    description="动态调用合同规则与AI工具，输出风险、证据和审批路由。",
                    capabilities=("contract_review", "contract_dynamic_workflow"),
                ),
                contract_subgraph.invoke,
            )

    def call_credit_subgraph(state: WorkflowState) -> dict:
        trace_offset = len(state.get("trace") or [])
        error_offset = len(state.get("errors") or [])
        plan_offset = len(state.get("workflow_plans") or [])
        run_offset = len(state.get("agent_runs") or [])
        result_offset = len(state.get("agent_task_results") or [])
        audit_offset = len(state.get("execution_audits") or [])
        tool_offset = len(state.get("tool_calls") or [])
        invocation = registry.invoke(
            "credit_review",
            dict(state),
            context=InvocationContext(
                case_id=str(state.get("case_id") or ""),
                caller="case_orchestrator",
                agent_id="credit_review",
                input_summary="客户档案、信用资料引用与受控运行快照",
            ),
        )
        result = invocation.output
        orchestration_runs = []
        orchestration_plan = dict(state.get("orchestration_plan") or {})
        if orchestration_plan:
            orchestration_runs.append(
                nodes.orchestration_agent_dispatch_run(
                    orchestration_plan,
                    "credit_agent_dispatch",
                    invocation.record,
                )
            )
            if str((state.get("customer") or {}).get("business_type") or "").upper() == "TKM":
                orchestration_runs.append(
                    nodes.orchestration_agent_dispatch_run(
                        orchestration_plan,
                        "tkm_governance",
                        invocation.record,
                    )
                )
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
            "workflow_plans": list(result.get("workflow_plans") or [])[plan_offset:],
            "agent_runs": list(result.get("agent_runs") or [])[run_offset:],
            "agent_task_results": list(result.get("agent_task_results") or [])[result_offset:],
            "execution_audits": list(result.get("execution_audits") or [])[audit_offset:],
            "agent_invocations": [invocation.record],
            "orchestration_runs": orchestration_runs,
            "tool_calls": list(result.get("tool_calls") or [])[tool_offset:],
            "active_workflow_plan": result.get("active_workflow_plan"),
            "credit_analysis": result.get("credit_analysis"),
            "credit_verification": result.get("credit_verification"),
            "trace": list(result.get("trace") or [])[trace_offset:],
            "errors": list(result.get("errors") or [])[error_offset:],
        }

    def call_contract_subgraph(state: WorkflowState) -> dict:
        trace_offset = len(state.get("trace") or [])
        error_offset = len(state.get("errors") or [])
        plan_offset = len(state.get("workflow_plans") or [])
        run_offset = len(state.get("agent_runs") or [])
        result_offset = len(state.get("agent_task_results") or [])
        audit_offset = len(state.get("execution_audits") or [])
        tool_offset = len(state.get("tool_calls") or [])
        invocation = registry.invoke(
            "contract_review",
            dict(state),
            context=InvocationContext(
                case_id=str(state.get("case_id") or ""),
                caller="case_orchestrator",
                agent_id="contract_review",
                input_summary="正式授信结果、合同结构和证据引用",
            ),
        )
        result = invocation.output
        orchestration_runs = []
        orchestration_plan = dict(state.get("orchestration_plan") or {})
        if orchestration_plan:
            orchestration_runs.append(
                nodes.orchestration_agent_dispatch_run(
                    orchestration_plan,
                    "contract_agent_dispatch",
                    invocation.record,
                )
            )
        return {
            "stage": result.get("stage"),
            "contract_reviews": result.get("contract_reviews"),
            "workflow_plans": list(result.get("workflow_plans") or [])[plan_offset:],
            "agent_runs": list(result.get("agent_runs") or [])[run_offset:],
            "agent_task_results": list(result.get("agent_task_results") or [])[result_offset:],
            "execution_audits": list(result.get("execution_audits") or [])[audit_offset:],
            "agent_invocations": [invocation.record],
            "orchestration_runs": orchestration_runs,
            "tool_calls": list(result.get("tool_calls") or [])[tool_offset:],
            "active_workflow_plan": result.get("active_workflow_plan"),
            "contract_verifications": result.get("contract_verifications"),
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
    builder.add_node("await_contract_approval", nodes.await_contract_approval)
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
