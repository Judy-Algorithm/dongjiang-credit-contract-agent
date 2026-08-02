"""Governed case-level plan owned by the parent orchestration Agent."""

from __future__ import annotations

from collections import deque
from typing import Any
from uuid import uuid4

from ..domain.models import utc_now
from .dynamic import canonical_hash


CASE_PLAN_VERSION = "1.0"
CASE_TASK_CATALOG_VERSION = "dongjiang-case-orchestration-v1"

CASE_TASK_CATALOG: dict[str, dict[str, Any]] = {
    "case_intake": {
        "label": "案件受理与身份登记",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": True,
        "depends_on": [],
    },
    "credit_document_processing": {
        "label": "信用资料解析与归档",
        "executor": "case_orchestrator",
        "executor_type": "tool",
        "required": True,
        "depends_on": ["case_intake"],
    },
    "document_quality_supervision": {
        "label": "文档解析质量监督",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": False,
        "condition": "案件上传信用文件时可选执行",
        "depends_on": ["credit_document_processing"],
    },
    "enterprise_identity_supervision": {
        "label": "客户唯一标识监督",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": False,
        "condition": "缺少CRM客户号和统一社会信用代码时可选执行",
        "depends_on": ["case_intake"],
    },
    "credit_cache_check": {
        "label": "历史有效授信检查",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": True,
        "depends_on": ["credit_document_processing"],
    },
    "credit_agent_dispatch": {
        "label": "分配信用评审子 Agent",
        "executor": "credit_review",
        "executor_type": "agent",
        "required": True,
        "condition": "无可复用的有效授信时执行",
        "depends_on": ["credit_cache_check"],
    },
    "tkm_governance": {
        "label": "TKM专项条件监督",
        "executor": "credit_review",
        "executor_type": "agent",
        "required_for": "TKM",
        "condition": "业务类型为TKM时执行",
        "depends_on": ["credit_agent_dispatch"],
    },
    "credit_human_approval": {
        "label": "正式授信人工审批",
        "executor": "credit_approver",
        "executor_type": "human",
        "required": True,
        "condition": "新信用评估形成后执行",
        "depends_on": ["credit_agent_dispatch"],
    },
    "credit_control_gate": {
        "label": "额度占用与逾期门禁",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": True,
        "depends_on": ["credit_cache_check"],
    },
    "exception_authorization": {
        "label": "信用例外授权",
        "executor": "exception_approver",
        "executor_type": "human",
        "required": True,
        "condition": "信用锁定或特殊放行时执行",
        "depends_on": ["credit_control_gate"],
    },
    "contract_collection": {
        "label": "合同资料收集",
        "executor": "case_submitter",
        "executor_type": "human",
        "required": True,
        "depends_on": ["credit_control_gate"],
    },
    "contract_document_processing": {
        "label": "合同解析、脱敏与归档",
        "executor": "case_orchestrator",
        "executor_type": "tool",
        "required": True,
        "depends_on": ["contract_collection"],
    },
    "contract_agent_dispatch": {
        "label": "分配合同评审子 Agent",
        "executor": "contract_review",
        "executor_type": "agent",
        "required": True,
        "depends_on": ["contract_document_processing"],
    },
    "result_synthesis": {
        "label": "信用与合同结果汇总路由",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": True,
        "depends_on": ["contract_agent_dispatch"],
    },
    "contract_human_review": {
        "label": "合同人工复核或修改",
        "executor": "legal_reviewer",
        "executor_type": "human",
        "required": True,
        "condition": "规则或AI发现需要人工处理时执行",
        "depends_on": ["result_synthesis"],
    },
    "enterprise_writeback": {
        "label": "OA、CRM与SAP回写",
        "executor": "case_orchestrator",
        "executor_type": "integration",
        "required": True,
        "depends_on": ["result_synthesis"],
    },
    "case_archive": {
        "label": "报告生成与案件归档",
        "executor": "case_orchestrator",
        "executor_type": "system",
        "required": True,
        "depends_on": ["enterprise_writeback"],
    },
}


def case_plan_snapshot(
    *,
    customer: dict[str, Any],
    credit_file_count: int,
    use_cached_credit: bool,
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build a privacy-safe snapshot suitable for optional model planning."""
    return {
        "business_type": str(customer.get("business_type") or "").upper(),
        "customer_type": str(customer.get("customer_type") or ""),
        "has_enterprise_identifier": bool(
            customer.get("crm_customer_id")
            or customer.get("unified_social_credit_code")
        ),
        "credit_file_count": max(0, int(credit_file_count)),
        "use_cached_credit": bool(use_cached_credit),
        "has_current_overdue": int(customer.get("current_overdue_days") or 0) > 0,
        "purchase_exemption_requested": bool(
            customer.get("purchase_exemption_requested")
        ),
        "ai_assistance_available": bool(runtime_snapshot.get("ai_enabled")),
        "tool_registry_provider": str(
            runtime_snapshot.get("tool_registry_provider") or "unknown"
        ),
        "available_tool_count": len(runtime_snapshot.get("available_tools") or []),
    }


def allowed_case_task_types(snapshot: dict[str, Any]) -> list[str]:
    allowed = list(CASE_TASK_CATALOG)
    if snapshot.get("business_type") != "TKM":
        allowed.remove("tkm_governance")
    if int(snapshot.get("credit_file_count") or 0) <= 0:
        allowed.remove("document_quality_supervision")
    if snapshot.get("has_enterprise_identifier"):
        allowed.remove("enterprise_identity_supervision")
    return allowed


def required_case_task_types(snapshot: dict[str, Any]) -> list[str]:
    required = [
        task_type
        for task_type, item in CASE_TASK_CATALOG.items()
        if item.get("required")
    ]
    if snapshot.get("business_type") == "TKM":
        required.append("tkm_governance")
    return required


def build_case_plan(
    case_id: str,
    snapshot: dict[str, Any],
    *,
    proposal: dict[str, Any] | None = None,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed = allowed_case_task_types(snapshot)
    required = required_case_task_types(snapshot)
    proposal = dict(proposal or {})
    raw_selected = [
        str(item) for item in proposal.get("selected_task_types") or []
    ]
    selected = [item for item in raw_selected if item in allowed]
    proposal_valid = bool(proposal.get("status") == "succeeded")
    if proposal_valid:
        missing = sorted(set(required) - set(selected))
        unexpected = sorted(set(raw_selected) - set(allowed))
        duplicate = len(raw_selected) != len(set(raw_selected))
        proposal_valid = not missing and not unexpected and not duplicate
    if not proposal_valid:
        selected = allowed

    selected_set = set(selected)
    tasks = []
    for index, task_type in enumerate(allowed, start=1):
        if task_type not in selected_set:
            continue
        catalog = CASE_TASK_CATALOG[task_type]
        dependencies = [
            item for item in catalog.get("depends_on") or [] if item in selected_set
        ]
        tasks.append(
            {
                "task_id": f"case.{index:02d}.{task_type}",
                "task_type": task_type,
                "label": catalog["label"],
                "executor": catalog["executor"],
                "executor_type": catalog["executor_type"],
                "depends_on_types": dependencies,
                "condition": str(catalog.get("condition") or ""),
            }
        )
    by_type = {item["task_type"]: item["task_id"] for item in tasks}
    for task in tasks:
        task["depends_on"] = [
            by_type[item]
            for item in task.pop("depends_on_types")
            if item in by_type
        ]

    assistance = {
        "status": str(proposal.get("status") or "not_configured"),
        "model": str(proposal.get("model") or ""),
        "prompt_version": str(proposal.get("prompt_version") or ""),
        "proposal_adopted": proposal_valid,
        "rationale": str(proposal.get("rationale") or "")[:500],
        "fallback_reason": (
            ""
            if proposal_valid
            else str(
                proposal.get("fallback_reason")
                or (
                    "模型提议包含未知、重复或缺失的治理任务，使用确定性案件计划。"
                    if proposal.get("status") == "succeeded"
                    else "使用确定性案件计划"
                )
            )[:300]
        ),
    }
    plan = {
        "plan_id": "",
        "case_id": case_id,
        "agent": "case_orchestrator",
        "label": "案件主 Agent 动态计划",
        "version": CASE_PLAN_VERSION,
        "planner": (
            "llm_proposal_guarded"
            if proposal_valid
            else "controlled_case_planner"
        ),
        "task_catalog_version": CASE_TASK_CATALOG_VERSION,
        "status": "running",
        "created_at": utc_now(),
        "planning_snapshot": dict(snapshot),
        "runtime_snapshot": dict(runtime_snapshot or {}),
        "planner_assistance": assistance,
        "tasks": tasks,
    }
    plan["plan_id"] = f"CASE-{canonical_hash(_case_plan_spec(plan))[:12].upper()}"
    validate_case_plan(plan)
    plan["frozen"] = True
    plan["frozen_at"] = utc_now()
    plan["spec_hash"] = canonical_hash(_case_plan_spec(plan))
    return plan


def _case_plan_spec(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": plan.get("case_id"),
        "agent": plan.get("agent"),
        "label": plan.get("label"),
        "version": plan.get("version"),
        "planner": plan.get("planner"),
        "task_catalog_version": plan.get("task_catalog_version"),
        "planning_snapshot": dict(plan.get("planning_snapshot") or {}),
        "runtime_snapshot": dict(plan.get("runtime_snapshot") or {}),
        "planner_assistance": dict(plan.get("planner_assistance") or {}),
        "tasks": list(plan.get("tasks") or []),
    }


def validate_case_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("agent") != "case_orchestrator":
        raise ValueError("案件动态计划的主Agent类型无效。")
    tasks = list(plan.get("tasks") or [])
    if not tasks or len(tasks) > len(CASE_TASK_CATALOG):
        raise ValueError("案件动态计划任务数量无效。")
    ids = [str(item.get("task_id") or "") for item in tasks]
    types = [str(item.get("task_type") or "") for item in tasks]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("案件动态计划任务ID为空或重复。")
    snapshot = dict(plan.get("planning_snapshot") or {})
    allowed = set(allowed_case_task_types(snapshot))
    required = set(required_case_task_types(snapshot))
    if not set(types).issubset(allowed):
        raise ValueError("案件动态计划包含未授权任务。")
    if not required.issubset(set(types)):
        raise ValueError("案件动态计划缺少必需任务。")
    known = set(ids)
    indegree = {item: 0 for item in ids}
    outgoing: dict[str, list[str]] = {item: [] for item in ids}
    for task in tasks:
        task_id = str(task["task_id"])
        dependencies = [str(item) for item in task.get("depends_on") or []]
        if task_id in dependencies or not set(dependencies).issubset(known):
            raise ValueError(f"案件动态计划依赖无效：{task_id}")
        for dependency in dependencies:
            indegree[task_id] += 1
            outgoing[dependency].append(task_id)
    queue = deque(item for item, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(tasks):
        raise ValueError("案件动态计划存在循环依赖。")
    return plan


def assert_case_plan_integrity(plan: dict[str, Any]) -> None:
    validate_case_plan(plan)
    if not plan.get("frozen") or not plan.get("spec_hash"):
        raise ValueError("案件动态计划尚未冻结。")
    if str(plan["spec_hash"]) != canonical_hash(_case_plan_spec(plan)):
        raise ValueError("案件动态计划规范哈希不匹配。")


def case_task(plan: dict[str, Any], task_type: str) -> dict[str, Any]:
    assert_case_plan_integrity(plan)
    for item in plan.get("tasks") or []:
        if item.get("task_type") == task_type:
            return dict(item)
    raise KeyError(f"案件动态计划中缺少任务：{task_type}")


def case_plan_has_task(plan: dict[str, Any], task_type: str) -> bool:
    return any(
        item.get("task_type") == task_type for item in plan.get("tasks") or []
    )


def orchestration_run(
    plan: dict[str, Any],
    task_type: str,
    *,
    status: str,
    output_summary: str,
    duration_ms: int = 0,
) -> dict[str, Any]:
    task = case_task(plan, task_type)
    return {
        "run_id": f"{plan.get('plan_id')}:{task.get('task_id')}:{uuid4().hex[:8]}",
        "plan_id": plan.get("plan_id"),
        "task_id": task.get("task_id"),
        "task_type": task_type,
        "label": task.get("label"),
        "executor": task.get("executor"),
        "executor_type": task.get("executor_type"),
        "status": status,
        "started_at": utc_now(),
        "completed_at": utc_now() if status not in {"waiting", "running"} else "",
        "duration_ms": max(0, int(duration_ms)),
        "output_summary": str(output_summary)[:500],
        "sensitive_input": "not_logged",
    }


def orchestration_waiting_run(
    plan: dict[str, Any],
    task_type: str,
    *,
    output_summary: str,
) -> dict[str, Any]:
    task = case_task(plan, task_type)
    return {
        "run_id": f"{plan.get('plan_id')}:{task.get('task_id')}:waiting",
        "plan_id": plan.get("plan_id"),
        "task_id": task.get("task_id"),
        "task_type": task_type,
        "label": task.get("label"),
        "executor": task.get("executor"),
        "executor_type": task.get("executor_type"),
        "status": "waiting",
        "started_at": utc_now(),
        "completed_at": "",
        "duration_ms": 0,
        "output_summary": str(output_summary)[:500],
        "sensitive_input": "not_logged",
    }
