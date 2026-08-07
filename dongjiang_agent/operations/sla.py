"""Approval-task SLA calculation, dashboards and deduplicated reminders."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..persistence import CaseRepository
from ..security import AuthStore, SecurityEmailSender


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "sla_policy.json"
WAITING_BY_STATUS = {
    "credit_pending_approval": "credit_approval",
    "credit_supplement_required": "credit_supplement",
    "credit_control_locked": "special_release",
    "awaiting_contract": "contract_upload",
    "blocked": "sales_revision",
    "pending_special_approval": "manager_approval",
    "pending_manual_review": "finance_legal_review",
    "pending_legal_approval": "contract_approval",
}
WAITING_ROLES = {
    "credit_approval": ["credit_approver"],
    "special_release": ["exception_approver"],
    "manager_approval": ["exception_approver"],
    "finance_legal_review": ["legal_reviewer"],
    "contract_approval": ["legal_reviewer"],
}
OWNER_WAITING = {"credit_supplement", "contract_upload", "sales_revision"}
ENTRY_STAGES = {
    "credit_approval": {"credit.approval_requested"},
    "credit_supplement": {
        "credit.insufficient_data",
        "credit.supplement_requested",
    },
    "special_release": {"credit.effective"},
    "contract_upload": {
        "workflow.interrupt",
        "credit.special_release_approved",
    },
    "sales_revision": {
        "decision.routed",
        "manager.rejected",
        "manual.revision_requested",
        "contract_legal.revision_requested",
    },
    "manager_approval": {"decision.routed"},
    "finance_legal_review": {"decision.routed"},
    "contract_approval": {"decision.routed"},
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_sla_policy(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def _task_entered_at(case: dict[str, Any], waiting_for: str) -> datetime:
    accepted = ENTRY_STAGES.get(waiting_for, set())
    trace = list(case.get("trace") or [])
    for item in reversed(trace):
        if str(item.get("stage") or "") in accepted:
            parsed = _parse_datetime(item.get("ts"))
            if parsed:
                return parsed
    for item in reversed(trace):
        parsed = _parse_datetime(item.get("ts"))
        if parsed:
            return parsed
    return _parse_datetime(case.get("created_at")) or _utc_now()


def case_sla(
    case: dict[str, Any],
    *,
    now: datetime | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    waiting_for = WAITING_BY_STATUS.get(str(case.get("status") or ""))
    rules = (policy or load_sla_policy()).get("tasks") or {}
    rule = dict(rules.get(str(waiting_for or "")) or {})
    if not waiting_for or not rule:
        return {
            "state": "not_applicable",
            "state_label": "无需处理",
            "waiting_for": waiting_for or "",
        }
    target_hours = max(float(rule.get("target_hours") or 1), 1.0)
    warning_hours = max(float(rule.get("warning_before_hours") or 0), 0.0)
    entered_at = _task_entered_at(case, waiting_for)
    due_at = entered_at + timedelta(hours=target_hours)
    remaining_hours = (due_at - current).total_seconds() / 3600
    elapsed_hours = max((current - entered_at).total_seconds() / 3600, 0.0)
    if remaining_hours <= 0:
        state, state_label = "overdue", "已逾期"
    elif remaining_hours <= warning_hours:
        state, state_label = "due_soon", "即将到期"
    else:
        state, state_label = "on_track", "时效正常"
    return {
        "state": state,
        "state_label": state_label,
        "waiting_for": waiting_for,
        "task_label": str(rule.get("label") or waiting_for),
        "target_hours": target_hours,
        "warning_before_hours": warning_hours,
        "entered_at": entered_at.isoformat(timespec="seconds"),
        "due_at": due_at.isoformat(timespec="seconds"),
        "elapsed_hours": round(elapsed_hours, 1),
        "remaining_hours": round(remaining_hours, 1),
        "progress_percent": round(min(elapsed_hours / target_hours * 100, 999), 1),
    }


class SLAService:
    def __init__(
        self,
        *,
        policy_path: str | Path | None = None,
        case_root: str | Path = "data/cases",
        auth_path: str | Path = "data/auth/auth.sqlite",
    ) -> None:
        self.policy = load_sla_policy(policy_path)
        self.repository = CaseRepository(case_root)
        self.auth_path = Path(auth_path)

    def dashboard(self, *, now: datetime | None = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for case in self.repository.list_cases():
            sla = case_sla(case, now=now, policy=self.policy)
            if sla["state"] == "not_applicable":
                continue
            customer = dict(case.get("customer") or {})
            owner = dict(case.get("owner") or case.get("applicant") or {})
            items.append(
                {
                    "case_id": str(case.get("case_id") or ""),
                    "customer_name": str(customer.get("customer_name") or ""),
                    "business_type": str(customer.get("business_type") or ""),
                    "status": str(case.get("status") or ""),
                    "owner": owner,
                    "sla": sla,
                }
            )
        state_order = {"overdue": 0, "due_soon": 1, "on_track": 2}
        items.sort(
            key=lambda item: (
                state_order.get(str(item["sla"]["state"]), 9),
                float(item["sla"].get("remaining_hours") or 0),
            )
        )
        by_task: dict[str, dict[str, Any]] = {}
        for item in items:
            sla = item["sla"]
            task = str(sla["waiting_for"])
            row = by_task.setdefault(
                task,
                {
                    "waiting_for": task,
                    "label": sla["task_label"],
                    "total": 0,
                    "due_soon": 0,
                    "overdue": 0,
                },
            )
            row["total"] += 1
            if sla["state"] in {"due_soon", "overdue"}:
                row[sla["state"]] += 1
        return {
            "items": items,
            "metrics": {
                "total": len(items),
                "on_track": sum(item["sla"]["state"] == "on_track" for item in items),
                "due_soon": sum(item["sla"]["state"] == "due_soon" for item in items),
                "overdue": sum(item["sla"]["state"] == "overdue" for item in items),
            },
            "by_task": list(by_task.values()),
            "policy_version": self.policy.get("version"),
            "generated_at": (
                (now.replace(tzinfo=timezone.utc) if now and now.tzinfo is None else now)
                or _utc_now()
            ).isoformat(timespec="seconds"),
        }

    def sweep(self, *, now: datetime | None = None) -> dict[str, Any]:
        dashboard = self.dashboard(now=now)
        created = 0
        emailed = 0
        sender = SecurityEmailSender()
        with AuthStore(self.auth_path) as store:
            for item in dashboard["items"]:
                sla = item["sla"]
                if sla["state"] not in {"due_soon", "overdue"}:
                    continue
                recipients = self._recipients(store, item)
                level = str(sla["state"])
                title = "待办已逾期" if level == "overdue" else "待办即将到期"
                hours = abs(float(sla["remaining_hours"]))
                timing = (
                    f"已逾期 {hours:.1f} 小时"
                    if level == "overdue"
                    else f"剩余 {hours:.1f} 小时"
                )
                body = (
                    f"案件 {item['case_id']}（{item['customer_name']}）的"
                    f"{sla['task_label']}{timing}，请及时处理。"
                )
                entered_key = str(sla["entered_at"]).replace(":", "")
                for user in recipients:
                    notification = store.create_notification(
                        str(user["user_id"]),
                        category="sla",
                        title=title,
                        body=body,
                        link=f"/cases/{item['case_id']}/action",
                        dedupe_key=(
                            f"sla:{item['case_id']}:{sla['waiting_for']}:{entered_key}:{level}"
                        ),
                    )
                    if not notification:
                        continue
                    created += 1
                    email = str(user.get("email") or "").strip()
                    if sender.configured and email:
                        try:
                            sender.send_notification(
                                email,
                                title=title,
                                body=body,
                                link=f"/cases/{item['case_id']}/action",
                            )
                            emailed += 1
                        except Exception:
                            pass
        return {
            "ok": True,
            "examined": len(dashboard["items"]),
            "due_soon": dashboard["metrics"]["due_soon"],
            "overdue": dashboard["metrics"]["overdue"],
            "notifications_created": created,
            "emails_sent": emailed,
        }

    @staticmethod
    def _recipients(store: AuthStore, item: dict[str, Any]) -> list[dict[str, Any]]:
        waiting_for = str(item["sla"]["waiting_for"])
        if waiting_for in OWNER_WAITING:
            owner_id = str((item.get("owner") or {}).get("user_id") or "")
            owner = store.get_user(owner_id) if owner_id else None
            return [owner] if owner and owner.get("active") else []
        roles = WAITING_ROLES.get(waiting_for) or []
        return store.users_for_roles(roles) if roles else []


class SLAMonitor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="dongjiang-sla-monitor", daemon=True)
        self.interval_seconds = max(
            int(os.getenv("DONGJIANG_SLA_SCAN_INTERVAL_SECONDS", "900")), 60
        )
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.wait(self.interval_seconds):
            try:
                SLAService().sweep()
            except Exception as exc:
                print(f"[sla] scan failed: {exc}")
            try:
                from .agent_incidents import AgentIncidentService

                AgentIncidentService().sweep()
            except Exception as exc:
                print(f"[agent-incident] scan failed: {exc}")

    def stop(self) -> None:
        self.stopped.set()
