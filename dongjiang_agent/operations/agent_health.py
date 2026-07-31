"""Cross-case operational health for controlled dynamic agent plans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..persistence import CaseRepository
from ..workflow.dynamic import assert_plan_integrity


SEVERITY_ORDER = {
    "critical": 0,
    "warning": 1,
    "legacy": 2,
    "pending": 3,
    "healthy": 4,
}


def _latest_plans(case: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in reversed(list(case.get("workflow_plans") or [])):
        plan = dict(raw)
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or plan_id in seen:
            continue
        seen.add(plan_id)
        plans.append(plan)
    plans.reverse()
    return plans


def _last_by_key(
    rows: list[dict[str, Any]], key_fields: tuple[str, ...]
) -> dict[tuple[str, ...], dict[str, Any]]:
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        key = tuple(str(row.get(field) or "") for field in key_fields)
        if all(key):
            result[key] = row
    return result


def _integrity_status(plan: dict[str, Any]) -> str:
    if not plan.get("frozen") or not plan.get("spec_hash"):
        return "legacy"
    try:
        assert_plan_integrity(plan)
    except (TypeError, ValueError):
        return "invalid"
    return "valid"


def _timestamp(value: object) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _issue_types(
    *,
    integrity_status: str,
    audit: dict[str, Any],
    failed: int,
    degraded: int,
    evidence_degraded: int,
    retries: int,
) -> list[str]:
    issues: list[str] = []
    if integrity_status == "invalid":
        issues.append("plan_integrity")
    if str(audit.get("status") or "") == "non_conformant":
        issues.append("execution_deviation")
    if failed:
        issues.append("node_failed")
    if evidence_degraded:
        issues.append("evidence_degraded")
    if degraded:
        issues.append("fallback_degraded")
    if retries:
        issues.append("retried")
    if integrity_status == "legacy":
        issues.append("legacy_plan")
    return issues


def _severity(
    *,
    integrity_status: str,
    audit_status: str,
    failed: int,
    degraded: int,
    evidence_degraded: int,
    retries: int,
) -> str:
    if integrity_status == "invalid" or audit_status == "non_conformant" or failed:
        return "critical"
    if degraded or evidence_degraded or retries:
        return "warning"
    if integrity_status == "legacy":
        return "legacy"
    if audit_status != "conformant":
        return "pending"
    return "healthy"


class AgentOperationsService:
    """Aggregate plan integrity and execution health without exposing task payloads."""

    def __init__(self, *, case_root: str = "data/cases") -> None:
        self.repository = CaseRepository(case_root)

    def report(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        case_ids: set[str] = set()
        for case in self.repository.list_cases():
            case_id = str(case.get("case_id") or "")
            customer = dict(case.get("customer") or {})
            owner = dict(case.get("owner") or case.get("applicant") or {})
            runs_by_task = _last_by_key(
                list(case.get("agent_runs") or []), ("plan_id", "task_id")
            )
            audits_by_plan = _last_by_key(
                list(case.get("execution_audits") or []), ("plan_id",)
            )
            for plan in _latest_plans(case):
                plan_id = str(plan.get("plan_id") or "")
                plan_runs = [
                    run for (run_plan_id, _), run in runs_by_task.items()
                    if run_plan_id == plan_id
                ]
                statuses = [str(run.get("status") or "pending") for run in plan_runs]
                failed = statuses.count("failed")
                degraded = statuses.count("degraded")
                reused = statuses.count("reused")
                retries = sum(
                    max(0, int(run.get("attempt_count") or 1) - 1)
                    for run in plan_runs
                )
                evidence_degraded = sum(
                    str(run.get("evidence_gate") or "") in {"degraded", "failed"}
                    for run in plan_runs
                )
                audit = audits_by_plan.get((plan_id,), {})
                audit_status = str(audit.get("status") or "pending")
                integrity_status = _integrity_status(plan)
                issues = _issue_types(
                    integrity_status=integrity_status,
                    audit=audit,
                    failed=failed,
                    degraded=degraded,
                    evidence_degraded=evidence_degraded,
                    retries=retries,
                )
                severity = _severity(
                    integrity_status=integrity_status,
                    audit_status=audit_status,
                    failed=failed,
                    degraded=degraded,
                    evidence_degraded=evidence_degraded,
                    retries=retries,
                )
                timestamps = [
                    str(plan.get("completed_at") or plan.get("created_at") or ""),
                    str(audit.get("audited_at") or ""),
                    *[
                        str(run.get("completed_at") or run.get("started_at") or "")
                        for run in plan_runs
                    ],
                ]
                rows.append(
                    {
                        "case_id": case_id,
                        "customer_name": str(customer.get("customer_name") or ""),
                        "business_type": str(customer.get("business_type") or ""),
                        "owner": str(
                            owner.get("display_name")
                            or owner.get("username")
                            or "未分配"
                        ),
                        "case_status": str(case.get("status") or ""),
                        "plan_id": plan_id,
                        "agent": str(plan.get("agent") or ""),
                        "label": str(plan.get("label") or plan.get("agent") or "Agent"),
                        "version": str(plan.get("version") or ""),
                        "spec_hash": str(plan.get("spec_hash") or "")[:12],
                        "task_catalog_version": str(
                            plan.get("task_catalog_version") or ""
                        ),
                        "integrity_status": integrity_status,
                        "audit_status": audit_status,
                        "severity": severity,
                        "issue_types": issues,
                        "task_count": len(plan.get("tasks") or []),
                        "executed_count": len(plan_runs),
                        "failed_count": failed,
                        "degraded_count": degraded,
                        "reused_count": reused,
                        "retry_count": retries,
                        "evidence_degraded_count": evidence_degraded,
                        "missing_tasks": list(audit.get("missing_tasks") or []),
                        "unexpected_tasks": list(audit.get("unexpected_tasks") or []),
                        "duplicate_results": list(audit.get("duplicate_results") or []),
                        "evidence_violations": list(audit.get("evidence_violations") or []),
                        "updated_at": max(timestamps),
                    }
                )
                case_ids.add(case_id)

        rows.sort(
            key=lambda row: (
                SEVERITY_ORDER.get(str(row["severity"]), 9),
                -_timestamp(row["updated_at"]),
            )
        )
        plan_total = len(rows)
        governed = sum(row["integrity_status"] != "legacy" for row in rows)
        critical = sum(row["severity"] == "critical" for row in rows)
        warning = sum(row["severity"] == "warning" for row in rows)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": {
                "case_total": len(case_ids),
                "plan_total": plan_total,
                "governed_plans": governed,
                "governance_coverage": round(governed / plan_total, 4)
                if plan_total
                else None,
                "healthy_plans": sum(row["severity"] == "healthy" for row in rows),
                "attention_plans": critical + warning,
                "critical_plans": critical,
                "warning_plans": warning,
                "legacy_plans": sum(row["severity"] == "legacy" for row in rows),
                "non_conformant_plans": sum(
                    row["audit_status"] == "non_conformant" for row in rows
                ),
                "failed_nodes": sum(int(row["failed_count"]) for row in rows),
                "degraded_nodes": sum(int(row["degraded_count"]) for row in rows),
                "retry_count": sum(int(row["retry_count"]) for row in rows),
                "reused_nodes": sum(int(row["reused_count"]) for row in rows),
            },
            "plans": rows,
            "security_notice": (
                "运维聚合只包含计划状态、任务计数和截断哈希，不包含客户资料、"
                "合同正文、节点输入输出、完整哈希或密钥。"
            ),
        }
