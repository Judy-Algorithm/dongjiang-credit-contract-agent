"""Cross-case Agent incident detection, SLA reporting and notifications."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any

from ..persistence import CaseRepository
from ..security import AuthStore, SecurityEmailSender
from ..workflow import DongjiangWorkflowHarness
from ..workflow.incidents import (
    automatic_incidents,
    incident_sla,
    load_agent_operations_policy,
)


SWEEP_LOCK = threading.Lock()


def _send_emails(
    recipients: list[dict[str, Any]], title: str, body: str, link: str
) -> int:
    sender = SecurityEmailSender()
    if not sender.configured:
        return 0
    sent = 0
    for user in recipients:
        email = str(user.get("email") or "").strip()
        if not email:
            continue
        try:
            sender.send_notification(email, title=title, body=body, link=link)
            sent += 1
        except Exception:
            pass
    return sent


class AgentIncidentService:
    """Open incidents for new signals and monitor operational response SLAs."""

    def __init__(
        self,
        *,
        policy_path: str | Path | None = None,
        case_root: str | Path = "data/cases",
        auth_path: str | Path = "data/auth/auth.sqlite",
        checkpoint_path: str | Path = "data/workflow/checkpoints.sqlite",
    ) -> None:
        self.policy = load_agent_operations_policy(policy_path)
        self.repository = CaseRepository(case_root)
        self.auth_path = Path(auth_path)
        self.checkpoint_path = Path(checkpoint_path)

    def sweep(self, *, now: datetime | None = None) -> dict[str, Any]:
        with SWEEP_LOCK:
            return self._sweep(now=now)

    def _sweep(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        opened: list[dict[str, Any]] = []
        notification_count = 0
        email_count = 0
        audit_count = 0
        cases = self.repository.list_cases()
        runtime_root = self.checkpoint_path.parent
        default_layout = self.checkpoint_path == Path("data/workflow/checkpoints.sqlite")
        with DongjiangWorkflowHarness(
            checkpoint_path=self.checkpoint_path,
            repository=self.repository,
            vault_dir=Path("data/vault") if default_layout else runtime_root / "vault",
            inbox_dir=runtime_root / "inbox",
            output_dir=Path("output") if default_layout else runtime_root / "output",
            evidence_dir=(
                Path("data/evidence") if default_layout else runtime_root / "evidence"
            ),
            archive_dir=(
                Path("data/archive") if default_layout else runtime_root / "archive"
            ),
            execution_dir=(
                Path("data/executions")
                if default_layout
                else runtime_root / "executions"
            ),
        ) as harness, AuthStore(self.auth_path) as store:
            current_cases: list[dict[str, Any]] = []
            for projected_case in cases:
                case_id = str(projected_case.get("case_id") or "")
                try:
                    case = dict(harness.get(case_id).state)
                except KeyError:
                    case = projected_case
                incidents, new_records = automatic_incidents(
                    case, policy=self.policy, now=current
                )
                if incidents != list(case.get("agent_incidents") or []):
                    case["agent_incidents"] = incidents
                    try:
                        harness.graph.update_state(
                            harness._config(case_id), {"agent_incidents": incidents}
                        )
                        case = dict(harness.get(case_id).state)
                    except KeyError:
                        self.repository.save_dict(case)
                current_cases.append(case)
                for incident in new_records:
                    case_id = str(case.get("case_id") or "")
                    link = f"/cases/{case_id}?tab=agents"
                    title = "Agent运行异常已自动发现"
                    body = f"案件 {case_id} 的Agent运行出现受控异常，请进入运维中心确认。"
                    recipients = store.notify_roles(
                        ["system_admin"],
                        category="agent_incident",
                        title=title,
                        body=body,
                        link=link,
                    )
                    notification_count += len(recipients)
                    email_count += _send_emails(recipients, title, body, link)
                    store.audit(
                        "agent.incident.auto_open",
                        username="agent-operations-monitor",
                        target_type="agent_incident",
                        target_id=str(incident.get("incident_id") or ""),
                        detail={
                            "case_id": case_id,
                            "plan_id": incident.get("plan_id"),
                            "severity": incident.get("severity"),
                            "issue_types": list(incident.get("issue_types") or []),
                        },
                    )
                    audit_count += 1
                    opened.append(
                        {
                            "case_id": case_id,
                            "incident_id": incident.get("incident_id"),
                            "plan_id": incident.get("plan_id"),
                            "severity": incident.get("severity"),
                            "issue_types": list(incident.get("issue_types") or []),
                        }
                    )

            for case in current_cases:
                case_id = str(case.get("case_id") or "")
                for incident in case.get("agent_incidents") or []:
                    if incident.get("status") == "resolved":
                        continue
                    sla = incident_sla(incident, now=current, policy=self.policy)
                    for phase in ("response", "resolution"):
                        phase_sla = dict(sla.get(phase) or {})
                        level = str(phase_sla.get("state") or "")
                        if level not in {"due_soon", "overdue"}:
                            continue
                        phase_label = "响应" if phase == "response" else "解决"
                        level_label = "已逾期" if level == "overdue" else "即将到期"
                        title = f"Agent异常{phase_label}{level_label}"
                        body = f"案件 {case_id} 的Agent异常{phase_label}时限{level_label}，请及时处理。"
                        link = "/agent-operations"
                        recipients = store.users_for_roles(["system_admin"])
                        dedupe_key = (
                            f"agent-incident-sla:{incident.get('incident_id')}:{phase}:{level}"
                        )
                        delivered: list[dict[str, Any]] = []
                        for user in recipients:
                            notification = store.create_notification(
                                str(user["user_id"]),
                                category="agent_incident",
                                title=title,
                                body=body,
                                link=link,
                                dedupe_key=dedupe_key,
                            )
                            if notification:
                                delivered.append(user)
                        notification_count += len(delivered)
                        email_count += _send_emails(delivered, title, body, link)
                        if delivered:
                            store.audit(
                                "agent.incident.sla_alert",
                                username="agent-operations-monitor",
                                target_type="agent_incident",
                                target_id=str(incident.get("incident_id") or ""),
                                detail={
                                    "case_id": case_id,
                                    "phase": phase,
                                    "level": level,
                                    "recipient_count": len(delivered),
                                },
                            )
                            audit_count += 1
        return {
            "ok": True,
            "examined_cases": len(cases),
            "opened_incidents": len(opened),
            "opened": opened,
            "notifications_created": notification_count,
            "emails_sent": email_count,
            "audit_events": audit_count,
            "policy_version": self.policy.get("version") or "",
            "generated_at": current.isoformat(timespec="seconds"),
        }
