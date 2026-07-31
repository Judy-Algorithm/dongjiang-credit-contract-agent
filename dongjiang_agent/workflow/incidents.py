"""Deterministic detection and SLA policy for Agent execution incidents."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .dynamic import assert_plan_integrity


DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "agent_operations_policy.json"
)
ANALYSIS_PHASE = "analysis"


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_agent_operations_policy(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def _latest_plan_rows(
    rows: list[dict[str, Any]], plan_id: str
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        if str(row.get("plan_id") or "") != plan_id:
            continue
        task_id = str(row.get("task_id") or "")
        if task_id:
            latest[task_id] = row
    return latest


def _latest_audit(state: dict[str, Any], plan_id: str) -> dict[str, Any]:
    return next(
        (
            dict(item)
            for item in reversed(state.get("execution_audits") or [])
            if str(item.get("plan_id") or "") == plan_id
        ),
        {},
    )


def detect_plan_incident(
    state: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a stable operational signal, excluding harmless one-off retries."""
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id or not plan.get("frozen"):
        return None
    try:
        assert_plan_integrity(plan)
        integrity_failed = False
    except (TypeError, ValueError):
        integrity_failed = True
    audit = _latest_audit(state, plan_id)
    tasks = {
        str(item.get("task_id") or ""): dict(item)
        for item in plan.get("tasks") or []
        if item.get("task_id")
    }
    runs = _latest_plan_rows(list(state.get("agent_runs") or []), plan_id)
    failed_tasks = sorted(
        task_id
        for task_id, run in runs.items()
        if str(run.get("status") or "") == "failed"
    )
    degraded_analysis = sorted(
        task_id
        for task_id, run in runs.items()
        if (tasks.get(task_id) or {}).get("phase") == ANALYSIS_PHASE
        and str(run.get("status") or "") == "degraded"
    )
    repeated_degraded = sorted(
        task_id
        for task_id in degraded_analysis
        if int((runs.get(task_id) or {}).get("attempt_count") or 1) >= 2
    )
    repeated_degradation = bool(repeated_degraded or len(degraded_analysis) >= 2)
    issue_types: list[str] = []
    if integrity_failed:
        issue_types.append("plan_integrity")
    if str(audit.get("status") or "") == "non_conformant":
        issue_types.append("execution_deviation")
    if failed_tasks:
        issue_types.append("node_failed")
    if repeated_degradation:
        issue_types.append("repeated_degradation")
    if not issue_types:
        return None
    severity = (
        "critical"
        if any(
            issue in {"plan_integrity", "execution_deviation", "node_failed"}
            for issue in issue_types
        )
        else "warning"
    )
    fingerprint_source = {
        "plan_id": plan_id,
        "issue_types": issue_types,
        "failed_tasks": failed_tasks,
        "degraded_tasks": degraded_analysis,
        "missing_tasks": sorted(audit.get("missing_tasks") or []),
        "unexpected_tasks": sorted(audit.get("unexpected_tasks") or []),
        "duplicate_results": sorted(audit.get("duplicate_results") or []),
        "evidence_violations": sorted(audit.get("evidence_violations") or []),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "plan_id": plan_id,
        "agent": str(plan.get("agent") or ""),
        "severity": severity,
        "issue_types": issue_types,
        "affected_task_ids": sorted(set(failed_tasks + degraded_analysis)),
        "issue_fingerprint": fingerprint,
    }


def new_agent_incident(
    signal: dict[str, Any],
    *,
    source: str,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    selected_policy = policy or load_agent_operations_policy()
    severity = str(signal.get("severity") or "critical")
    rule = dict(selected_policy.get(severity) or selected_policy.get("critical") or {})
    response_hours = max(float(rule.get("response_hours") or 1), 1.0)
    resolution_hours = max(float(rule.get("resolution_hours") or 1), response_hours)
    opened_at = current.isoformat(timespec="seconds")
    return {
        "incident_id": f"AINC-{uuid4().hex[:12].upper()}",
        "plan_id": signal.get("plan_id"),
        "agent": signal.get("agent"),
        "source": source,
        "status": "open",
        "severity": severity,
        "issue_types": list(signal.get("issue_types") or []),
        "affected_task_ids": list(signal.get("affected_task_ids") or []),
        "issue_fingerprint": signal.get("issue_fingerprint") or "",
        "policy_version": selected_policy.get("version") or "",
        "opened_at": opened_at,
        "updated_at": opened_at,
        "response_due_at": (current + timedelta(hours=response_hours)).isoformat(
            timespec="seconds"
        ),
        "resolution_due_at": (current + timedelta(hours=resolution_hours)).isoformat(
            timespec="seconds"
        ),
        "responded_at": "",
        "resolved_at": "",
        "assignee": {},
        "history": [
            {
                "action": "auto_open" if source == "automatic" else "open",
                "actor_id": "agent-operations-monitor" if source == "automatic" else "",
                "actor_name": "Agent运维监控" if source == "automatic" else "",
                "note": "检测到受控Agent运行异常。" if source == "automatic" else "",
                "at": opened_at,
            }
        ],
        "rerun_history": [],
    }


def incident_sla(
    incident: dict[str, Any],
    *,
    now: datetime | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    selected_policy = policy or load_agent_operations_policy()
    severity = str(incident.get("severity") or "critical")
    rule = dict(selected_policy.get(severity) or selected_policy.get("critical") or {})
    warning_hours = max(float(rule.get("warning_before_hours") or 0), 0.0)
    response_due = _parse_datetime(incident.get("response_due_at"))
    resolution_due = _parse_datetime(incident.get("resolution_due_at"))
    responded = bool(incident.get("responded_at"))
    resolved = str(incident.get("status") or "") == "resolved"

    def phase(due_at: datetime | None, complete: bool) -> dict[str, Any]:
        if complete:
            return {"state": "completed", "state_label": "已完成", "remaining_hours": None}
        if due_at is None:
            return {"state": "unknown", "state_label": "未配置", "remaining_hours": None}
        remaining = (due_at - current).total_seconds() / 3600
        if remaining <= 0:
            state, label = "overdue", "已逾期"
        elif remaining <= warning_hours:
            state, label = "due_soon", "即将到期"
        else:
            state, label = "on_track", "时效正常"
        return {"state": state, "state_label": label, "remaining_hours": round(remaining, 1)}

    return {
        "policy_version": incident.get("policy_version") or selected_policy.get("version") or "",
        "response": {**phase(response_due, responded or resolved), "due_at": incident.get("response_due_at") or ""},
        "resolution": {**phase(resolution_due, resolved), "due_at": incident.get("resolution_due_at") or ""},
    }


def automatic_incidents(
    state: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open one incident per new issue fingerprint and return the new records."""
    incidents = [dict(item) for item in state.get("agent_incidents") or []]
    known_fingerprints = {
        str(item.get("issue_fingerprint") or "") for item in incidents
        if item.get("issue_fingerprint")
    }
    open_plan_ids = {
        str(item.get("plan_id") or "")
        for item in incidents
        if item.get("status") != "resolved"
    }
    opened: list[dict[str, Any]] = []
    seen_plans: set[str] = set()
    for raw_plan in reversed(list(state.get("workflow_plans") or [])):
        plan = dict(raw_plan)
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or plan_id in seen_plans or plan_id in open_plan_ids:
            continue
        seen_plans.add(plan_id)
        signal = detect_plan_incident(state, plan)
        if not signal:
            continue
        legacy_resolved = next(
            (
                item
                for item in reversed(incidents)
                if item.get("plan_id") == plan_id
                and item.get("status") == "resolved"
                and not item.get("issue_fingerprint")
            ),
            None,
        )
        if legacy_resolved is not None:
            legacy_resolved["issue_fingerprint"] = signal["issue_fingerprint"]
            legacy_resolved["severity"] = signal["severity"]
            legacy_resolved["issue_types"] = list(signal["issue_types"])
            legacy_resolved["affected_task_ids"] = list(
                signal["affected_task_ids"]
            )
            known_fingerprints.add(str(signal["issue_fingerprint"]))
            continue
        if signal["issue_fingerprint"] in known_fingerprints:
            continue
        incident = new_agent_incident(
            signal, source="automatic", policy=policy, now=now
        )
        incidents.append(incident)
        opened.append(incident)
        known_fingerprints.add(str(signal["issue_fingerprint"]))
    return incidents, opened
