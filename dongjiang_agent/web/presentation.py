"""Business-facing API views.

The workflow keeps technical state for persistence and diagnostics.  This
module is the only place where that state is translated for CRM/OA/Web users.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..contract.revisions import suggested_replacement
from ..ingestion import location_label
from ..llm.structured_extractor import FIELD_LABELS as EXTRACTION_FIELD_LABELS
from ..operations.sla import case_sla
from ..workflow.candidate_reviews import (
    CANDIDATE_WAITING_FOR,
    build_candidate_comparison,
    candidate_fingerprint,
    latest_candidate,
    safe_candidate_review_view,
)


STATUS_LABELS = {
    "created": "草稿",
    "processing": "审核中",
    "credit_calculated": "信用评估已完成",
    "credit_pending_approval": "等待信用审批",
    "credit_supplement_required": "等待补充信用资料",
    "credit_effective": "授信已生效",
    "credit_control_locked": "信用控制已锁定",
    "credit_control_rejected": "特别放行已拒绝",
    "credit_rejected": "信用申请已拒绝",
    "awaiting_contract": "等待上传合同",
    "blocked": "等待修改合同",
    "pending_special_approval": "等待管理层审批",
    "pending_manual_review": "等待财务法务复核",
    "pending_legal_approval": "等待合同法务批准",
    "approved": "已通过",
    "approved_by_exception": "已特批通过",
    "approved_after_manual_review": "已复核通过",
    "completed": "已完成",
    "rejected": "已驳回",
    "closed_without_contract": "已关闭",
    "inactive": "Inactive（授信已清零）",
}

RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
    "blocker": "阻断项",
}

FIELD_LABELS = {
    "registered_capital": "注册资本",
    "years_in_business": "成立年限",
    "asset_liability_ratio": "资产负债率",
    "net_margin": "净利率",
    "current_ratio": "流动比率",
    "revenue_growth": "营收增长率",
    "external_rating": "外部主体评级",
    "cooperation_history": "历史合作记录",
    "enterprise_basics": "企业基础信息",
    "tkm_business_subtype": "TKM业务子类型",
}

RECORD_LABELS = {
    "document.processing_failed": "资料已解析，后续字段提取失败",
    "workflow.started": "案件已提交",
    "document.extracted": "资料已读取",
    "document.failed": "部分资料读取失败",
    "privacy.redacted": "敏感资料已安全处理",
    "credit.cache_hit": "已读取有效信用结果",
    "credit.completed": "信用审核已完成",
    "credit.insufficient_data": "信用资料不足，等待补充",
    "credit.rating_conflict": "外部评级需要人工确认",
    "credit.approval_requested": "信用评估已提交审批",
    "credit.supplement_requested": "需要补充信用资料",
    "credit.supplemented": "补充信用资料已提交",
    "credit.effective": "正式授信已生效",
    "credit.special_release_approved": "特别放行已批准",
    "credit.special_release_rejected": "特别放行已拒绝",
    "credit.rejected": "信用申请已拒绝",
    "credit.inactivated": "客户已转Inactive并清零授信",
    "workflow.interrupt": "等待上传合同",
    "workflow.resumed": "合同已提交",
    "contract.completed": "合同审核已完成",
    "integration.writeback_retried": "企业系统回写已人工重试",
    "decision.routed": "审核结果已确定",
    "sales.revised": "修订合同已提交",
    "sales.closed": "案件已关闭",
    "manager.approved": "管理层已批准",
    "manager.rejected": "管理层已驳回",
    "manual.approved": "财务法务复核已完成",
    "contract_legal.approved": "合同法务已批准",
    "contract_legal.revision_requested": "合同法务已要求修改",
    "manual.supplemented": "补充资料已提交",
    "manual.revision_requested": "已要求修改合同",
    "workflow.closed": "案件已关闭",
    "workflow.finalized": "案件处理已完成",
    "case.owner_assigned": "案件负责人已调整",
    "agent.extraction.generated": "AI结构化字段候选已生成",
    "agent.extraction.adopted": "AI结构化字段已人工采纳",
    "agent.extraction.rejected": "AI结构化字段候选已拒绝",
}

LEGACY_ROLE_MAP = {
    "admin": "system_admin",
    "sales": "case_submitter",
    "credit": "credit_approver",
    "finance": "credit_approver",
    "legal": "legal_reviewer",
    "director": "exception_approver",
    "ceo": "exception_approver",
}


def _actor_roles(actor: dict[str, Any] | None) -> set[str]:
    return {
        LEGACY_ROLE_MAP.get(str(role), str(role))
        for role in (actor or {}).get("roles") or []
    }


def _structured_extractions(
    case: dict[str, Any], actor: dict[str, Any] | None
) -> list[dict[str, Any]]:
    roles = _actor_roles(actor)
    rows: list[dict[str, Any]] = []
    for raw in reversed(list(case.get("structured_extractions") or [])):
        item = dict(raw)
        decision = dict(item.get("decision") or {})
        review_roles = (
            {"credit_approver"}
            if item.get("document_kind") == "credit"
            else {"legal_reviewer", "exception_approver"}
        )
        rows.append(
            {
                "extraction_id": item.get("extraction_id"),
                "document_kind": item.get("document_kind"),
                "status": item.get("status"),
                "model": item.get("model"),
                "prompt_version": item.get("prompt_version"),
                "summary": item.get("summary"),
                "verification": dict(item.get("verification") or {}),
                "candidates": [
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "field": candidate.get("field"),
                        "label": candidate.get("label")
                        or EXTRACTION_FIELD_LABELS.get(
                            str(candidate.get("field") or ""),
                            candidate.get("field"),
                        ),
                        "value": candidate.get("value"),
                        "confidence": candidate.get("confidence"),
                        "evidence_query": candidate.get("evidence_query"),
                        "document_id": candidate.get("document_id"),
                        "fragment_id": candidate.get("fragment_id"),
                        "location": dict(candidate.get("location") or {}),
                        "location_label": location_label(candidate.get("location")),
                    }
                    for candidate in item.get("candidates") or []
                ],
                "created_by": dict(item.get("created_by") or {}),
                "created_at": item.get("created_at"),
                "decided_by": dict(item.get("decided_by") or {}),
                "decided_at": item.get("decided_at"),
                "decision": {
                    "action": decision.get("action"),
                    "candidate_count": len(decision.get("candidate_ids") or []),
                    "official_state_changed": bool(
                        decision.get("official_state_changed")
                    ),
                    "result_plan_id": decision.get("result_plan_id"),
                },
                "permissions": {
                    "can_review": bool(review_roles.intersection(roles))
                    and item.get("status") == "pending"
                },
            }
        )
    return rows


def _decision(reviews: list[dict[str, Any]]) -> str:
    values = {str(item.get("decision") or "") for item in reviews}
    if "block" in values:
        return "block"
    if "special_approval" in values:
        return "special_approval"
    if "manual_review" in values:
        return "manual_review"
    if "pass" in values:
        return "pass"
    return "credit_only"


def _waiting_for(case: dict[str, Any], explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    return {
        "credit_pending_approval": "credit_approval",
        "credit_supplement_required": "credit_supplement",
        "credit_control_locked": "special_release",
        "awaiting_contract": "contract_upload",
        "blocked": "sales_revision",
        "pending_special_approval": "manager_approval",
        "pending_manual_review": "finance_legal_review",
        "pending_legal_approval": "contract_approval",
    }.get(str(case.get("status") or ""))


def _next_action(waiting_for: str | None) -> dict[str, Any] | None:
    actions = {
        "credit_approval": {
            "type": "credit_approval",
            "label": "处理信用审批",
            "allowed_actions": [
                {"type": "approve", "label": "批准"},
                {"type": "adjust_and_approve", "label": "调整后批准"},
                {"type": "request_supplement", "label": "要求补充资料"},
                {"type": "reject", "label": "拒绝"},
            ],
        },
        "credit_supplement": {
            "type": "credit_supplement",
            "label": "补充信用资料",
            "allowed_actions": [
                {"type": "submit_supplement", "label": "提交补充资料"},
                {"type": "close_case", "label": "关闭申请"},
            ],
        },
        "special_release": {
            "type": "special_release",
            "label": "处理特别放行",
            "allowed_actions": [
                {"type": "approve", "label": "批准并归档证据"},
                {"type": "reject", "label": "拒绝放行"},
            ],
        },
        "contract_upload": {
            "type": "upload_contract",
            "label": "上传合同",
            "allowed_actions": [
                {"type": "upload_contract", "label": "提交合同审核"},
                {"type": "close_case", "label": "关闭案件"},
            ],
        },
        "sales_revision": {
            "type": "submit_revision",
            "label": "修改合同",
            "allowed_actions": [
                {"type": "submit_revision", "label": "重新提交"},
                {"type": "close_case", "label": "关闭案件"},
            ],
        },
        "manager_approval": {
            "type": "manager_review",
            "label": "处理审批",
            "allowed_actions": [
                {"type": "approve", "label": "批准"},
                {"type": "reject", "label": "驳回"},
            ],
        },
        "finance_legal_review": {
            "type": "manual_review",
            "label": "处理复核",
            "allowed_actions": [
                {"type": "approve", "label": "确认通过"},
                {"type": "supplement", "label": "补充资料"},
                {"type": "request_revision", "label": "要求修改合同"},
            ],
        },
        "contract_approval": {
            "type": "contract_approval",
            "label": "处理合同法务批准",
            "allowed_actions": [
                {"type": "approve", "label": "批准合同"},
                {"type": "request_revision", "label": "要求修改合同"},
            ],
        },
    }
    return actions.get(str(waiting_for or ""))


def _records(trace: list[dict[str, Any]]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for item in trace:
        label = RECORD_LABELS.get(str(item.get("stage") or ""))
        if not label:
            continue
        if records and records[-1]["label"] == label:
            continue
        records.append({"time": str(item.get("ts") or ""), "label": label})
    return records


def _agent_execution(case: dict[str, Any]) -> dict[str, Any]:
    runs = list(case.get("agent_runs") or [])
    invocations = list(case.get("agent_invocations") or [])
    tool_calls = list(case.get("tool_calls") or [])
    audits = list(case.get("execution_audits") or [])
    run_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        key = (str(run.get("plan_id") or ""), str(run.get("task_id") or ""))
        run_by_key[key] = dict(run)
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_plan in reversed(list(case.get("workflow_plans") or [])):
        plan_id = str(raw_plan.get("plan_id") or "")
        if not plan_id or plan_id in seen:
            continue
        seen.add(plan_id)
        raw_snapshot = dict(raw_plan.get("runtime_snapshot") or {})
        snapshot = {
            "credit_policy_version": raw_snapshot.get("credit_policy_version") or "",
            "credit_policy_hash": str(raw_snapshot.get("credit_policy_hash") or "")[:12],
            "contract_policy_version": raw_snapshot.get("contract_policy_version") or "",
            "contract_policy_hash": str(raw_snapshot.get("contract_policy_hash") or "")[:12],
            "model": raw_snapshot.get("model") or "",
            "ai_enabled": bool(raw_snapshot.get("ai_enabled")),
            "prompt_version": raw_snapshot.get("prompt_version") or "",
            "prompt_hash": str(raw_snapshot.get("prompt_hash") or "")[:12],
            "tool_registry_provider": raw_snapshot.get("tool_registry_provider") or "local",
            "available_tool_count": len(raw_snapshot.get("available_tools") or []),
        }
        audit = next(
            (
                dict(item)
                for item in reversed(audits)
                if str(item.get("plan_id") or "") == plan_id
            ),
            {},
        )
        nodes: list[dict[str, Any]] = []
        for task in raw_plan.get("tasks") or []:
            run = run_by_key.get((plan_id, str(task.get("task_id") or "")), {})
            nodes.append(
                {
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "label": task.get("label"),
                    "phase": task.get("phase"),
                    "depends_on": list(task.get("depends_on") or []),
                    "status": run.get("status") or "pending",
                    "duration_ms": run.get("duration_ms"),
                    "started_at": run.get("started_at"),
                    "completed_at": run.get("completed_at"),
                    "input_summary": run.get("input_summary") or "等待执行",
                    "output_summary": run.get("output_summary") or "尚无输出",
                    "model": run.get("model") or "",
                    "attempt_count": int(run.get("attempt_count") or 0),
                    "attempt_history": [
                        {
                            "attempt": item.get("attempt"),
                            "status": item.get("status"),
                            "duration_ms": item.get("duration_ms"),
                            "error_type": item.get("error_type") or "",
                        }
                        for item in run.get("attempt_history") or []
                    ],
                    "idempotency_key": str(run.get("idempotency_key") or "")[:12],
                    "execution_policy": dict(
                        run.get("execution_policy")
                        or task.get("execution_policy")
                        or {}
                    ),
                    "evidence_gate": run.get("evidence_gate") or "not_applicable",
                    "sensitive_input": run.get("sensitive_input") or "not_logged",
                }
            )
        plan_runs = [run for run in runs if run.get("plan_id") == plan_id]
        plan_invocation = next(
            (
                dict(item)
                for item in reversed(invocations)
                if str(item.get("plan_id") or "") == plan_id
            ),
            {},
        )
        plan_tools = [
            {
                "tool_call_id": item.get("tool_call_id"),
                "task_id": item.get("task_id"),
                "tool_name": item.get("tool_name"),
                "tool_label": item.get("tool_label"),
                "provider": item.get("provider"),
                "status": item.get("status"),
                "duration_ms": item.get("duration_ms"),
                "output_summary": item.get("output_summary"),
            }
            for item in tool_calls
            if str(item.get("plan_id") or "") == plan_id
        ]
        total_duration = sum(int(run.get("duration_ms") or 0) for run in plan_runs)
        plans.append(
            {
                "plan_id": plan_id,
                "agent": raw_plan.get("agent"),
                "label": raw_plan.get("label"),
                "version": raw_plan.get("version"),
                "planner": raw_plan.get("planner"),
                "frozen": bool(raw_plan.get("frozen")),
                "spec_hash": str(raw_plan.get("spec_hash") or "")[:12],
                "task_catalog_version": raw_plan.get("task_catalog_version") or "",
                "runtime_snapshot": snapshot,
                "execution_audit": {
                    "status": audit.get("status") or "pending",
                    "integrity_valid": audit.get("integrity_valid"),
                    "missing_tasks": list(audit.get("missing_tasks") or []),
                    "unexpected_tasks": list(audit.get("unexpected_tasks") or []),
                    "duplicate_results": list(audit.get("duplicate_results") or []),
                    "evidence_violations": list(audit.get("evidence_violations") or []),
                    "retry_count": int(audit.get("retry_count") or 0),
                    "fallback_count": int(audit.get("fallback_count") or 0),
                    "audited_at": audit.get("audited_at") or "",
                },
                "status": raw_plan.get("status") or (
                    "completed"
                    if nodes
                    and all(
                        node["status"] in {"completed", "degraded", "reused"}
                        for node in nodes
                    )
                    else "running"
                ),
                "created_at": raw_plan.get("created_at"),
                "completed_at": raw_plan.get("completed_at"),
                "task_count": len(nodes),
                "completed_count": sum(
                    node["status"] in {"completed", "degraded", "reused"}
                    for node in nodes
                ),
                "total_duration_ms": total_duration,
                "agent_invocation": {
                    "invocation_id": plan_invocation.get("invocation_id"),
                    "provider": plan_invocation.get("provider") or "local",
                    "caller": plan_invocation.get("caller") or "case_orchestrator",
                    "duration_ms": plan_invocation.get("duration_ms"),
                    "status": plan_invocation.get("status"),
                },
                "tool_calls": plan_tools,
                "nodes": nodes,
            }
        )
    plans.reverse()
    raw_orchestration = dict(case.get("orchestration_plan") or {})
    orchestration_run_rows = list(case.get("orchestration_runs") or [])
    latest_orchestration_runs: dict[str, dict[str, Any]] = {}
    for run in orchestration_run_rows:
        latest_orchestration_runs[str(run.get("task_type") or "")] = dict(run)
    orchestration_plan = {}
    if raw_orchestration:
        assistance = dict(raw_orchestration.get("planner_assistance") or {})
        orchestration_plan = {
            "plan_id": raw_orchestration.get("plan_id"),
            "label": raw_orchestration.get("label"),
            "version": raw_orchestration.get("version"),
            "planner": raw_orchestration.get("planner"),
            "frozen": bool(raw_orchestration.get("frozen")),
            "spec_hash": str(raw_orchestration.get("spec_hash") or "")[:12],
            "status": raw_orchestration.get("status") or "running",
            "planner_assistance": {
                "status": assistance.get("status") or "not_configured",
                "model": assistance.get("model") or "",
                "proposal_adopted": bool(assistance.get("proposal_adopted")),
                "rationale": assistance.get("rationale") or "",
                "fallback_reason": assistance.get("fallback_reason") or "",
            },
            "nodes": [
                {
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "label": task.get("label"),
                    "executor": task.get("executor"),
                    "executor_type": task.get("executor_type"),
                    "condition": task.get("condition") or "",
                    "depends_on": list(task.get("depends_on") or []),
                    "status": latest_orchestration_runs.get(
                        str(task.get("task_type") or ""), {}
                    ).get("status") or "pending",
                    "output_summary": latest_orchestration_runs.get(
                        str(task.get("task_type") or ""), {}
                    ).get("output_summary") or "等待主 Agent 调度",
                    "duration_ms": latest_orchestration_runs.get(
                        str(task.get("task_type") or ""), {}
                    ).get("duration_ms"),
                }
                for task in raw_orchestration.get("tasks") or []
            ],
        }
    return {
        "mode": "controlled_dynamic_workflow",
        "parent_label": "信审与合同评审主流程",
        "orchestration_plan": orchestration_plan,
        "plans": plans,
        "run_count": len(runs),
        "agent_invocation_count": len(invocations),
        "tool_call_count": len(tool_calls),
        "unscoped_tool_calls": [
            {
                "tool_name": item.get("tool_name"),
                "tool_label": item.get("tool_label"),
                "provider": item.get("provider"),
                "status": item.get("status"),
                "duration_ms": item.get("duration_ms"),
                "output_summary": item.get("output_summary"),
            }
            for item in tool_calls
            if not item.get("plan_id")
        ],
        "security_notice": "运行记录仅展示结构化摘要；敏感原文与脱敏映射不会写入执行日志。",
    }


def _agent_candidates(
    case: dict[str, Any],
    waiting_for: str | None,
    actor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    reviews = list(case.get("agent_candidate_reviews") or [])
    actor_roles = _actor_roles(actor)
    can_manage = bool(
        {"credit_approver", "legal_reviewer", "exception_approver"}.intersection(actor_roles)
    )
    rows: list[dict[str, Any]] = []
    for incident in reversed(list(case.get("agent_incidents") or [])):
        candidate = latest_candidate(incident)
        if not candidate:
            continue
        fingerprint = candidate_fingerprint(candidate)
        linked = [
            item
            for item in reviews
            if item.get("incident_id") == incident.get("incident_id")
            and item.get("candidate_fingerprint") == fingerprint
        ]
        review = linked[-1] if linked else None
        status = str((review or {}).get("status") or "candidate")
        agent = str(incident.get("agent") or "")
        incident_status = str(incident.get("status") or "")
        can_request = bool(
            can_manage
            and incident_status != "open"
            and incident_status != "resolved"
            and status != "pending"
            and waiting_for in CANDIDATE_WAITING_FOR.get(agent, set())
        )
        can_reject = bool(
            can_manage
            and incident_status != "open"
            and status in {"candidate", "pending"}
        )
        rows.append(
            {
                "incident_id": incident.get("incident_id"),
                "plan_id": incident.get("plan_id"),
                "agent": agent,
                "agent_label": "信用信审子 Agent" if agent == "credit" else "合同审查子 Agent",
                "status": status,
                "incident_status": incident_status,
                "candidate_ref": fingerprint[:12],
                "completed_at": candidate.get("completed_at"),
                "comparison": build_candidate_comparison(case, incident, candidate),
                "review": safe_candidate_review_view(review) if review else None,
                "permissions": {
                    "can_request_adoption": can_request,
                    "can_reject_candidate": can_reject,
                },
                "eligibility_message": (
                    "可提交至当前正式审批节点"
                    if can_request
                    else "案件不在兼容审批节点，仅可查看差异"
                    if waiting_for not in CANDIDATE_WAITING_FOR.get(agent, set())
                    else "该候选正在等待或已经完成处置"
                    if review
                    else "请先确认Agent运行异常"
                ),
            }
        )
    return rows


def case_view(
    case: dict[str, Any],
    *,
    waiting_for: str | None = None,
    actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    customer = dict(case.get("customer") or {})
    credit_status = str(case.get("credit_status") or "draft")
    model_credit = dict(
        case.get("model_credit_assessment")
        or case.get("credit_assessment")
        or {}
    )
    effective_credit = dict(case.get("effective_credit_assessment") or {})
    if not effective_credit and credit_status == "effective":
        effective_credit = dict(case.get("credit_assessment") or {})
    displayed_credit = effective_credit or model_credit
    credit_approval = dict(case.get("credit_approval") or {})
    reviews = list(case.get("contract_reviews") or [])
    contracts = list(case.get("contract_facts") or case.get("contracts") or [])
    resolved_waiting = _waiting_for(case, waiting_for)
    next_action = _next_action(resolved_waiting)
    applicant = dict(case.get("applicant") or {})
    owner = dict(case.get("owner") or applicant)
    can_act = True
    if actor is not None and next_action:
        actor_roles = _actor_roles(actor)
        required = {
            "credit_approval": {"credit_approver"},
            "credit_supplement": {"case_submitter"},
            "special_release": {"exception_approver"},
            "contract_upload": {"case_submitter"},
            "sales_revision": {"case_submitter"},
            "manager_approval": {"exception_approver"},
            "finance_legal_review": {"legal_reviewer"},
            "contract_approval": {"legal_reviewer"},
        }.get(str(resolved_waiting or ""), set())
        can_act = bool(required.intersection(actor_roles))
        if can_act and resolved_waiting in {
            "credit_supplement",
            "contract_upload",
            "sales_revision",
        }:
            owner_id = str(owner.get("user_id") or "")
            can_act = not owner_id or owner_id == str(actor.get("user_id") or "")
    records = _records(list(case.get("trace") or []))
    sla = case_sla(case)
    status = str(case.get("status") or "created")
    findings = [
        {
            "finding_key": (
                f"{item.get('rule_id')}:{item.get('document_id')}:{item.get('fragment_id')}"
                if item.get("document_id") and item.get("fragment_id")
                else ""
            ),
            "rule_id": item.get("rule_id"),
            "title": item.get("title"),
            "level": item.get("level"),
            "message": item.get("message"),
            "suggestion": item.get("suggestion"),
            "clause_excerpt": item.get("clause_excerpt"),
            "evidence_query": item.get("evidence_query"),
            "document_id": item.get("document_id"),
            "fragment_id": item.get("fragment_id"),
            "location": dict(item.get("location") or {}),
            "location_label": location_label(item.get("location"))
            if item.get("document_id")
            else "",
            "suggested_replacement": suggested_replacement(
                str(item.get("rule_id") or "")
            ),
        }
        for review in reviews
        for item in review.get("findings") or []
    ]
    ai_assistance = [
        {
            "status": str(review.get("ai_assistance", {}).get("status") or "not_configured"),
            "model": str(review.get("ai_assistance", {}).get("model") or ""),
            "summary": str(review.get("ai_assistance", {}).get("summary") or ""),
            "error": str(review.get("ai_assistance", {}).get("error") or ""),
            "findings": [
                {
                    **dict(item),
                    "location_label": location_label(item.get("location"))
                    if item.get("location")
                    else "",
                }
                for item in review.get("ai_assistance", {}).get("findings") or []
            ],
        }
        for review in reviews
        if review.get("ai_assistance")
    ]
    documents = [
        Path(str(item)).name for item in case.get("source_files") or []
    ]
    return {
        "case_id": case.get("case_id"),
        "customer": customer,
        "status": status,
        "status_label": STATUS_LABELS.get(status, "处理中"),
        "risk_level": displayed_credit.get("risk_level"),
        "risk_label": RISK_LABELS.get(str(displayed_credit.get("risk_level") or ""), "待评估"),
        "decision": _decision(reviews),
        "next_action": next_action if can_act else None,
        "pending_action": next_action,
        "is_my_task": bool(next_action and can_act),
        "sla": sla,
        "applicant": applicant,
        "owner": owner,
        "phase": "contract" if credit_status == "effective" else "credit",
        "permissions": {
            "can_assign_owner": bool(actor and "system_admin" in _actor_roles(actor)),
            "can_upload_contract": bool(
                credit_status == "effective"
                and resolved_waiting in {"contract_upload", "sales_revision"}
                and can_act
            ),
            "can_approve_credit": resolved_waiting == "credit_approval" and can_act,
            "can_approve_contract": resolved_waiting == "contract_approval" and can_act,
            "can_upload_credit_documents": resolved_waiting == "credit_supplement" and can_act,
            "can_approve_special_release": resolved_waiting == "special_release" and can_act,
            "can_retry_writeback": bool(actor and "system_admin" in _actor_roles(actor)),
            "can_run_mock_approval": bool(
                actor
                and "credit_approver" in _actor_roles(actor)
                and resolved_waiting == "credit_approval"
                and os.getenv("DONGJIANG_INTEGRATION_MODE", "").strip().lower()
                == "mock"
            ),
        },
        "credit": {
            "status": credit_status,
            "effective": credit_status == "effective",
            "model_result": {
                "score": model_credit.get("score"),
                "risk_level": model_credit.get("risk_level"),
                "risk_label": RISK_LABELS.get(
                    str(model_credit.get("risk_level") or ""),
                    "待评估",
                ),
                "credit_limit": model_credit.get("approved_credit_limit"),
                "total_credit_limit": model_credit.get("total_credit_limit"),
                "term_days": model_credit.get("recommended_term_days"),
                "hard_term_limit_days": model_credit.get("hard_term_limit_days"),
                "occupied_credit_amount": model_credit.get("occupied_credit_amount"),
                "available_credit_amount": model_credit.get("available_credit_amount"),
                "credit_locked": bool(model_credit.get("credit_locked")),
                "purchase_exemption_requested": bool(
                    model_credit.get("purchase_exemption_requested")
                ),
                "purchase_exemption_approved": bool(
                    model_credit.get("purchase_exemption_approved")
                ),
                "max_tail_payment_ratio": model_credit.get(
                    "max_tail_payment_ratio"
                ),
                "max_tail_term_days": model_credit.get("max_tail_term_days"),
                "credit_lock_reasons": list(model_credit.get("credit_lock_reasons") or []),
            },
            "approved_result": (
                {
                    "score": effective_credit.get("score"),
                    "risk_level": effective_credit.get("risk_level"),
                    "risk_label": RISK_LABELS.get(
                        str(effective_credit.get("risk_level") or ""),
                        "待评估",
                    ),
                    "credit_limit": effective_credit.get("approved_credit_limit"),
                    "total_credit_limit": effective_credit.get("total_credit_limit"),
                    "term_days": effective_credit.get("recommended_term_days"),
                    "effective_at": credit_approval.get("effective_at"),
                    "expires_at": credit_approval.get("expires_at"),
                    "occupied_credit_amount": effective_credit.get("occupied_credit_amount"),
                    "available_credit_amount": effective_credit.get("available_credit_amount"),
                    "credit_locked": bool(effective_credit.get("credit_locked")),
                    "purchase_exemption_approved": bool(
                        effective_credit.get("purchase_exemption_approved")
                    ),
                    "credit_lock_reasons": list(effective_credit.get("credit_lock_reasons") or []),
                }
                if effective_credit
                else None
            ),
            "approval": credit_approval,
            "missing_fields": [
                {
                    "field": field,
                    "label": FIELD_LABELS.get(str(field), str(field)),
                }
                for field in model_credit.get("missing_fields") or []
            ],
            "data_coverage_ratio": model_credit.get("data_coverage_ratio"),
            "available_dimensions": list(model_credit.get("available_dimensions") or []),
            "requires_supplement": bool(model_credit.get("requires_supplement")),
            "supplement_reasons": list(model_credit.get("supplement_reasons") or []),
            "reasons": list(model_credit.get("reasons") or []),
            "rating_resolution": dict(model_credit.get("rating_resolution") or {}),
        },
        "credit_control": dict(case.get("credit_control") or {}),
        "special_release": dict(case.get("special_release") or {}),
        "exception_approval": dict(case.get("exception_approval") or {}),
        "approval_evidence": [
            {
                "purpose": item.get("purpose"),
                "name": item.get("name"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
                "actor_id": item.get("actor_id"),
                "archived_at": item.get("archived_at"),
            }
            for item in case.get("approval_evidence") or []
        ],
        "approval_chain": list(case.get("approval_chain") or []),
        "writeback": dict(case.get("writeback") or {}),
        "contracts": [
            {
                "name": item.get("contract_name"),
                "amount": item.get("amount"),
                "requested_credit": item.get("requested_credit"),
                "payment_term_days": item.get("payment_term_days"),
                "tail_payment_ratio": item.get("tail_payment_ratio"),
                "tail_payment_term_days": item.get("tail_payment_term_days"),
                "document_id": item.get("document_id"),
                "evidence": [
                    {
                        "field": evidence.get("field"),
                        "value": evidence.get("value"),
                        "excerpt": evidence.get("excerpt"),
                        "document_id": evidence.get("document_id"),
                        "fragment_id": evidence.get("fragment_id"),
                        "location": dict(evidence.get("location") or {}),
                        "location_label": location_label(evidence.get("location")),
                    }
                    for evidence in item.get("evidence") or []
                ],
            }
            for item in contracts
        ],
        "findings": findings,
        "ai_assistance": ai_assistance,
        "contract_approval": dict(case.get("contract_approval") or {}),
        "agent_execution": _agent_execution(case),
        "agent_candidates": _agent_candidates(case, resolved_waiting, actor),
        "structured_extractions": _structured_extractions(case, actor),
        "structured_extraction_permissions": {
            "can_generate_credit": bool(
                actor
                and {"credit_approver"}.intersection(
                    _actor_roles(actor)
                )
                and resolved_waiting == "credit_approval"
                and any(
                    item.get("document_kind") == "credit"
                    and item.get("parse_status") == "parsed"
                    for item in case.get("source_documents") or []
                )
            ),
            "can_generate_contract": bool(
                actor
                and {"legal_reviewer", "exception_approver"}.intersection(
                    _actor_roles(actor)
                )
                and resolved_waiting
                in {"contract_approval", "manager_approval", "finance_legal_review"}
                and any(
                    item.get("document_kind") == "contract"
                    and item.get("parse_status") == "parsed"
                    for item in case.get("source_documents") or []
                )
            ),
        },
        "translation_permissions": {
            "can_manage": bool(
                actor
                and {"case_submitter", "legal_reviewer"}.intersection(
                    _actor_roles(actor)
                )
                and any(
                    item.get("document_kind") == "contract"
                    and item.get("parse_status") == "parsed"
                    and item.get("fragments")
                    for item in case.get("source_documents") or []
                )
            )
        },
        "records": records,
        "documents": documents,
        "source_documents": [
            {
                "document_kind": item.get("document_kind"),
                "document_id": item.get("document_id"),
                "name": item.get("name"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
                "archived_at": item.get("archived_at"),
                "parse_status": (
                    "parsed"
                    if item.get("parse_status") == "failed" and item.get("fragments")
                    else item.get("parse_status")
                ),
                "processing_error": item.get("processing_error")
                or (
                    item.get("error")
                    if item.get("parse_status") == "failed" and item.get("fragments")
                    else None
                ),
                "semantic_status": item.get("semantic_status")
                or (
                    "failed"
                    if item.get("parse_status") == "failed" and item.get("fragments")
                    else None
                ),
                "media_type": item.get("media_type"),
                "extractor": item.get("extractor"),
                "warnings": list(item.get("warnings") or []),
                "features": dict(item.get("features") or {}),
                "quality": dict(item.get("quality") or {}),
                "text_enhancement": {
                    "status": (item.get("text_enhancement") or {}).get("status"),
                    "mode": (item.get("text_enhancement") or {}).get("mode"),
                    "model": (item.get("text_enhancement") or {}).get("model"),
                    "summary": (item.get("text_enhancement") or {}).get("summary"),
                    "candidate_count": len(
                        (item.get("text_enhancement") or {}).get("candidates") or []
                    ),
                    "limitation": (item.get("text_enhancement") or {}).get("limitation"),
                },
                "fragment_count": len(item.get("fragments") or []),
            }
            for item in case.get("source_documents") or []
        ],
        "created_at": case.get("created_at"),
        "updated_at": records[-1]["time"] if records else case.get("created_at"),
    }


def case_summary(
    case: dict[str, Any], *, actor: dict[str, Any] | None = None
) -> dict[str, Any]:
    view = case_view(case, actor=actor)
    customer = view["customer"]
    return {
        "case_id": view["case_id"],
        "customer_name": customer.get("customer_name"),
        "customer_type": customer.get("customer_type"),
        "business_type": customer.get("business_type"),
        "status": view["status"],
        "status_label": view["status_label"],
        "score": view["credit"]["model_result"]["score"],
        "risk_level": view["risk_level"],
        "risk_label": view["risk_label"],
        "decision": view["decision"],
        "next_action": view["next_action"],
        "pending_action": view["pending_action"],
        "is_my_task": view["is_my_task"],
        "sla": view["sla"],
        "applicant": view["applicant"],
        "owner": view["owner"],
        "created_at": view["created_at"],
        "updated_at": view["updated_at"],
    }
