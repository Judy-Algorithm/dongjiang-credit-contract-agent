"""Controlled runtime plans for the credit and contract business subgraphs.

Plans are data, not executable model output.  Every task type is selected from
an allowlist and the graph remains responsible for ordering and permissions.
"""

from __future__ import annotations

from collections import deque
import hashlib
import json
from typing import Any

from ..domain.models import utc_now


PLAN_VERSION = "2.0"
TASK_CATALOG_VERSION = "dongjiang-controlled-tasks-v2"
MAX_PLAN_TASKS = 48
RETRYABLE_ANALYSIS_TASKS = {
    "credit_data_completeness",
    "credit_financial_analysis",
    "credit_rating_analysis",
    "credit_cooperation_analysis",
    "credit_enterprise_analysis",
    "credit_control_analysis",
    "credit_tkm_analysis",
    "contract_policy_review",
    "contract_ai_review",
}

TASK_CATALOG: dict[str, dict[str, Any]] = {
    "credit_data_completeness": {"agent": "credit", "label": "资料完整性分析"},
    "credit_financial_analysis": {"agent": "credit", "label": "财务指标分析"},
    "credit_rating_analysis": {"agent": "credit", "label": "外部评级分析"},
    "credit_cooperation_analysis": {"agent": "credit", "label": "历史交易分析"},
    "credit_enterprise_analysis": {"agent": "credit", "label": "企业基础分析"},
    "credit_control_analysis": {"agent": "credit", "label": "额度占用与逾期分析"},
    "credit_tkm_analysis": {"agent": "credit", "label": "TKM专项条件分析"},
    "credit_synthesis": {"agent": "credit", "label": "信用分析汇总"},
    "credit_scoring": {"agent": "credit", "label": "确定性信用评分"},
    "credit_verification": {"agent": "credit", "label": "信用结论独立核验"},
    "contract_policy_review": {"agent": "contract", "label": "合同制度规则审查"},
    "contract_ai_review": {
        "agent": "contract",
        "label": "合同AI辅助审查",
        "max_attempts": 2,
        "fallback": "rule_only",
        "minimum_evidence_coverage": 0.8,
    },
    "contract_synthesis": {"agent": "contract", "label": "合同风险汇总"},
    "contract_verification": {"agent": "contract", "label": "合同结论独立核验"},
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_spec(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": plan.get("case_id"),
        "agent": plan.get("agent"),
        "label": plan.get("label"),
        "version": plan.get("version"),
        "planner": plan.get("planner"),
        "task_catalog_version": plan.get("task_catalog_version"),
        "runtime_snapshot": dict(plan.get("runtime_snapshot") or {}),
        "submission_number": int(plan.get("submission_number") or 0),
        "tasks": list(plan.get("tasks") or []),
    }


def freeze_plan(
    plan: dict[str, Any], runtime_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    frozen = dict(plan)
    frozen["runtime_snapshot"] = dict(runtime_snapshot or {})
    frozen["task_catalog_version"] = TASK_CATALOG_VERSION
    frozen["frozen"] = True
    frozen["frozen_at"] = utc_now()
    frozen["spec_hash"] = canonical_hash(_plan_spec(frozen))
    return frozen


def assert_plan_integrity(plan: dict[str, Any]) -> None:
    validate_plan(plan)
    if not plan.get("frozen") or not plan.get("spec_hash"):
        raise ValueError("动态计划尚未冻结，拒绝执行。")
    if str(plan.get("spec_hash")) != canonical_hash(_plan_spec(plan)):
        raise ValueError("动态计划规范哈希不匹配，拒绝执行。")


def task_idempotency_key(plan: dict[str, Any], task: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "plan_id": plan.get("plan_id"),
            "spec_hash": plan.get("spec_hash"),
            "task_id": task.get("task_id"),
            "task_type": task.get("task_type"),
            "input_refs": list(task.get("input_refs") or []),
        }
    )


def _task(
    task_id: str,
    task_type: str,
    *,
    depends_on: list[str] | None = None,
    input_refs: list[str] | None = None,
    required_evidence: bool = False,
    phase: str = "analysis",
) -> dict[str, Any]:
    catalog = TASK_CATALOG[task_type]
    return {
        "task_id": task_id,
        "task_type": task_type,
        "label": catalog["label"],
        "agent": catalog["agent"],
        "phase": phase,
        "depends_on": list(depends_on or []),
        "input_refs": list(input_refs or []),
        "required_evidence": bool(required_evidence),
        "execution_policy": {
            "max_attempts": int(catalog.get("max_attempts") or 1),
            "fallback": str(catalog.get("fallback") or "fail_closed"),
            "minimum_evidence_coverage": float(
                catalog.get("minimum_evidence_coverage") or 0
            ),
        },
    }


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate a runtime plan before any task is dispatched."""
    agent = str(plan.get("agent") or "")
    if agent not in {"credit", "contract"}:
        raise ValueError("动态计划的 Agent 类型无效。")
    tasks = list(plan.get("tasks") or [])
    if not tasks or len(tasks) > MAX_PLAN_TASKS:
        raise ValueError(f"动态计划任务数必须在1至{MAX_PLAN_TASKS}之间。")
    identifiers = [str(item.get("task_id") or "") for item in tasks]
    if any(not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("动态计划任务ID不能为空或重复。")
    known = set(identifiers)
    for item in tasks:
        task_type = str(item.get("task_type") or "")
        catalog = TASK_CATALOG.get(task_type)
        if not catalog or catalog["agent"] != agent:
            raise ValueError(f"动态计划包含未授权任务类型：{task_type or 'empty'}")
        dependencies = {str(value) for value in item.get("depends_on") or []}
        if not dependencies.issubset(known) or str(item["task_id"]) in dependencies:
            raise ValueError(f"动态计划任务依赖无效：{item['task_id']}")
        policy = dict(item.get("execution_policy") or {})
        attempts = int(policy.get("max_attempts") or 1)
        if attempts < 1 or attempts > 5:
            raise ValueError(f"动态计划任务重试次数无效：{item['task_id']}")

    indegree = {identifier: 0 for identifier in identifiers}
    outgoing: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    for item in tasks:
        task_id = str(item["task_id"])
        for dependency in item.get("depends_on") or []:
            dep = str(dependency)
            indegree[task_id] += 1
            outgoing[dep].append(task_id)
    queue = deque(identifier for identifier, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(tasks):
        raise ValueError("动态计划存在循环依赖。")
    return plan


def build_credit_plan(
    case_id: str,
    customer: dict[str, Any],
    source_documents: list[dict[str, Any]],
    *,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analysis_types = ["credit_data_completeness"]
    if any(
        customer.get(field) is not None
        for field in (
            "asset_liability_ratio",
            "net_margin",
            "current_ratio",
            "revenue_growth",
        )
    ):
        analysis_types.append("credit_financial_analysis")
    if customer.get("external_rating") or customer.get("external_ratings"):
        analysis_types.append("credit_rating_analysis")
    if str(customer.get("customer_type") or "").lower() not in {
        "new",
        "new_customer",
        "新客户",
    } or any(
        customer.get(field) is not None
        for field in (
            "cooperation_years",
            "overdue_count_12m",
            "max_overdue_days_12m",
            "on_time_payment_rate",
        )
    ):
        analysis_types.append("credit_cooperation_analysis")
    if any(
        customer.get(field) is not None
        for field in ("registered_capital", "years_in_business")
    ):
        analysis_types.append("credit_enterprise_analysis")
    if any(
        customer.get(field) is not None
        for field in (
            "outstanding_receivables_amount",
            "open_order_amount",
            "current_overdue_days",
        )
    ):
        analysis_types.append("credit_control_analysis")
    if str(customer.get("business_type") or "").upper() == "TKM":
        analysis_types.append("credit_tkm_analysis")
    document_refs = [
        str(item.get("document_id") or "")
        for item in source_documents
        if item.get("document_kind") == "credit" and item.get("document_id")
    ]
    tasks = [
        _task(
            f"credit.analysis.{index}",
            task_type,
            input_refs=document_refs or ["customer-profile"],
            required_evidence=task_type
            in {"credit_data_completeness", "credit_rating_analysis"},
        )
        for index, task_type in enumerate(analysis_types, start=1)
    ]
    analysis_ids = [str(item["task_id"]) for item in tasks]
    tasks.append(
        _task(
            "credit.synthesis",
            "credit_synthesis",
            depends_on=analysis_ids,
            phase="synthesis",
        )
    )
    tasks.append(
        _task(
            "credit.scoring",
            "credit_scoring",
            depends_on=["credit.synthesis"],
            phase="decision",
        )
    )
    tasks.append(
        _task(
            "credit.verification",
            "credit_verification",
            depends_on=["credit.scoring"],
            required_evidence=True,
            phase="verification",
        )
    )
    plan = {
        "plan_id": "",
        "case_id": case_id,
        "agent": "credit",
        "label": "信用信审子 Agent",
        "version": PLAN_VERSION,
        "planner": "controlled_runtime_planner",
        "status": "running",
        "created_at": utc_now(),
        "tasks": tasks,
    }
    plan["runtime_snapshot"] = dict(runtime_snapshot or {})
    plan["task_catalog_version"] = TASK_CATALOG_VERSION
    plan["plan_id"] = f"CREDIT-{canonical_hash(_plan_spec(plan))[:12].upper()}"
    validate_plan(plan)
    return freeze_plan(plan, runtime_snapshot)


def build_contract_plan(
    case_id: str,
    contracts: list[dict[str, Any]],
    *,
    ai_available: bool,
    submission_number: int = 0,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for index, contract in enumerate(contracts, start=1):
        document_ref = str(contract.get("document_id") or f"contract-{index}")
        policy_id = f"contract.{index}.policy"
        tasks.append(
            _task(
                policy_id,
                "contract_policy_review",
                input_refs=[document_ref],
                required_evidence=True,
            )
        )
        analysis_ids = [policy_id]
        if ai_available:
            ai_id = f"contract.{index}.ai"
            tasks.append(
                _task(
                    ai_id,
                    "contract_ai_review",
                    input_refs=[document_ref],
                    required_evidence=True,
                )
            )
            analysis_ids.append(ai_id)
        synthesis_id = f"contract.{index}.synthesis"
        verification_id = f"contract.{index}.verification"
        tasks.append(
            _task(
                synthesis_id,
                "contract_synthesis",
                depends_on=analysis_ids,
                input_refs=[document_ref],
                phase="synthesis",
            )
        )
        tasks.append(
            _task(
                verification_id,
                "contract_verification",
                depends_on=[synthesis_id],
                input_refs=[document_ref],
                required_evidence=True,
                phase="verification",
            )
        )
    if not tasks:
        tasks.extend(
            [
                _task(
                    "contract.1.policy",
                    "contract_policy_review",
                    input_refs=["unparsed-contract"],
                    required_evidence=True,
                ),
                _task(
                    "contract.1.synthesis",
                    "contract_synthesis",
                    depends_on=["contract.1.policy"],
                    input_refs=["unparsed-contract"],
                    phase="synthesis",
                ),
                _task(
                    "contract.1.verification",
                    "contract_verification",
                    depends_on=["contract.1.synthesis"],
                    input_refs=["unparsed-contract"],
                    required_evidence=True,
                    phase="verification",
                ),
            ]
        )
    plan = {
        "plan_id": "",
        "case_id": case_id,
        "agent": "contract",
        "label": "合同审查子 Agent",
        "version": PLAN_VERSION,
        "planner": "controlled_runtime_planner",
        "status": "running",
        "created_at": utc_now(),
        "submission_number": int(submission_number or 0),
        "tasks": tasks,
    }
    plan["runtime_snapshot"] = dict(runtime_snapshot or {})
    plan["task_catalog_version"] = TASK_CATALOG_VERSION
    plan["plan_id"] = f"CONTRACT-{canonical_hash(_plan_spec(plan))[:12].upper()}"
    validate_plan(plan)
    return freeze_plan(plan, runtime_snapshot)


def plan_task(plan: dict[str, Any], task_type: str, *, input_ref: str = "") -> dict[str, Any]:
    assert_plan_integrity(plan)
    candidates = [
        item
        for item in plan.get("tasks") or []
        if item.get("task_type") == task_type
        and (not input_ref or input_ref in (item.get("input_refs") or []))
    ]
    if not candidates:
        raise KeyError(f"动态计划中缺少任务：{task_type}")
    return dict(candidates[0])


def audit_plan_execution(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    planned = {str(item.get("task_id") or "") for item in plan.get("tasks") or []}
    relevant_results = [
        item for item in results if item.get("plan_id") == plan.get("plan_id")
    ]
    executed = {str(item.get("task_id") or "") for item in relevant_results}
    result_counts: dict[str, int] = {}
    for item in relevant_results:
        task_id = str(item.get("task_id") or "")
        result_counts[task_id] = result_counts.get(task_id, 0) + 1
    evidence_violations = [
        str(item.get("task_id") or "")
        for item in relevant_results
        if item.get("evidence_gate") == "failed"
    ]
    relevant_runs = [item for item in runs if item.get("plan_id") == plan.get("plan_id")]
    retry_count = sum(max(0, int(item.get("attempt_count") or 1) - 1) for item in relevant_runs)
    integrity_valid = bool(plan.get("frozen")) and str(
        plan.get("spec_hash") or ""
    ) == canonical_hash(_plan_spec(plan))
    missing = sorted(planned - executed)
    unexpected = sorted(executed - planned)
    duplicate_results = sorted(
        task_id for task_id, count in result_counts.items() if count > 1
    )
    conformant = (
        integrity_valid
        and not missing
        and not unexpected
        and not duplicate_results
        and not evidence_violations
    )
    return {
        "plan_id": plan.get("plan_id"),
        "status": "conformant" if conformant else "non_conformant",
        "audited_at": utc_now(),
        "integrity_valid": integrity_valid,
        "planned_count": len(planned),
        "executed_count": len(executed),
        "missing_tasks": missing,
        "unexpected_tasks": unexpected,
        "duplicate_results": duplicate_results,
        "evidence_violations": evidence_violations,
        "retry_count": retry_count,
        "fallback_count": sum(
            str(item.get("status") or "") == "degraded" for item in relevant_runs
        ),
    }
