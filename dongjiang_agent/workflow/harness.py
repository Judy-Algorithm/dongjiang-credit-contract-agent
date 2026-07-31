"""Execution harness around LangGraph: identity, permissions, staging and recovery."""

from __future__ import annotations

import sqlite3
import hashlib
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..domain.codec import profile_from_dict
from ..domain.models import AuditCase, CreditProfile, utc_now
from ..integrations import IntegrationBundle
from ..persistence import CaseRepository
from .codec import case_from_state, checkpoint_dict
from .graph import build_workflow
from .nodes import WorkflowNodes


@dataclass(slots=True, frozen=True)
class ActorContext:
    actor_id: str = "local-system"
    roles: tuple[str, ...] = ("system",)
    source_system: str = "local"
    display_name: str = ""


@dataclass(slots=True)
class WorkflowRun:
    case_id: str
    status: str
    stage: str
    waiting_for: str | None
    next_nodes: list[str]
    interrupt: dict[str, Any] | None
    state: dict[str, Any]

    @property
    def paused(self) -> bool:
        return bool(self.next_nodes)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["paused"] = self.paused
        return payload


class DongjiangWorkflowHarness:
    """Stable API used by Web/CRM/OA; LangGraph stays an internal detail."""

    ROLE_REQUIREMENTS = {
        "credit_approval": {"credit", "finance"},
        "credit_supplement": {"sales", "finance"},
        "special_release": {"director"},
        "contract_upload": {"sales"},
        "sales_revision": {"sales"},
        "manager_approval": {"director", "ceo"},
        "finance_legal_review": {"finance", "legal"},
    }

    def __init__(
        self,
        *,
        checkpoint_path: str | Path = "data/workflow/checkpoints.sqlite",
        repository: CaseRepository | None = None,
        vault_dir: str | Path = "data/vault",
        inbox_dir: str | Path = "data/workflow/inbox",
        output_dir: str | Path = "output",
        evidence_dir: str | Path | None = None,
        archive_dir: str | Path | None = None,
        integrations: IntegrationBundle | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.repository = repository or CaseRepository()
        self.checkpoint_path = Path(checkpoint_path)
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        default_layout = self.inbox_dir == Path("data/workflow/inbox")
        self.evidence_dir = (
            Path(evidence_dir)
            if evidence_dir is not None
            else Path("data/evidence")
            if default_layout
            else self.inbox_dir.parent / "evidence"
        )
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.checkpoint_path,
            check_same_thread=False,
        )
        self.checkpointer = SqliteSaver(self._connection)
        self.checkpointer.setup()
        integration_bundle = integrations or IntegrationBundle.from_environment(
            audit_root=(
                Path("data/integrations")
                if default_layout
                else self.inbox_dir.parent / "integrations"
            )
        )
        self.nodes = WorkflowNodes(
            repository=self.repository,
            vault_dir=vault_dir,
            inbox_dir=self.inbox_dir,
            output_dir=output_dir,
            archive_dir=(
                Path(archive_dir)
                if archive_dir is not None
                else Path("data/archive")
                if default_layout
                else self.inbox_dir.parent / "archive"
            ),
            integrations=integration_bundle,
            policy=policy,
        )
        self.graph = build_workflow(self.nodes, checkpointer=self.checkpointer)

    @staticmethod
    def _config(case_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": case_id}}

    @staticmethod
    def _profile(payload: CreditProfile | dict[str, Any]) -> CreditProfile:
        if isinstance(payload, CreditProfile):
            return payload
        return profile_from_dict(payload)

    def _stage_contract_texts(self, case_id: str, texts: list[str]) -> list[str]:
        if not texts:
            return []
        case_dir = self.inbox_dir / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for index, text in enumerate(texts, start=1):
            if not str(text or "").strip():
                continue
            target = case_dir / f"合同粘贴-{index}-{uuid4().hex[:8]}.txt"
            target.write_text(str(text), encoding="utf-8")
            paths.append(str(target))
        return paths

    @staticmethod
    def _interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
        for task in getattr(snapshot, "tasks", ()):
            for item in getattr(task, "interrupts", ()):
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    return value
        return None

    def _run_result(self, case_id: str) -> WorkflowRun:
        snapshot = self.graph.get_state(self._config(case_id))
        state = dict(snapshot.values or {})
        if not state:
            raise KeyError(f"未找到工作流案件：{case_id}")
        run = WorkflowRun(
            case_id=case_id,
            status=str(state.get("status") or "unknown"),
            stage=str(state.get("stage") or "unknown"),
            waiting_for=state.get("waiting_for"),
            next_nodes=list(snapshot.next or ()),
            interrupt=self._interrupt_payload(snapshot),
            state=state,
        )
        if state.get("customer"):
            self.repository.save(case_from_state(state))
        return run

    def start(
        self,
        customer: CreditProfile | dict[str, Any],
        *,
        file_paths: list[str] | tuple[str, ...] = (),
        contract_texts: list[str] | tuple[str, ...] = (),
        use_cached_credit: bool = True,
        case_id: str | None = None,
        actor: ActorContext | None = None,
    ) -> WorkflowRun:
        profile = self._profile(customer)
        actor = actor or ActorContext()
        if contract_texts:
            raise ValueError("发起信审时不能同时提交合同；请等待授信审批生效。")
        resolved_case_id = case_id or AuditCase(customer=profile, contracts=[]).case_id
        try:
            existing = self.graph.get_state(self._config(resolved_case_id))
            if existing.values:
                raise ValueError(f"工作流案件已存在：{resolved_case_id}")
        except KeyError:
            pass
        pending = [str(Path(item).expanduser().resolve()) for item in file_paths]
        initial = {
            "case_id": resolved_case_id,
            "created_at": utc_now(),
            "stage": "created",
            "status": "created",
            "source_system": actor.source_system,
            "actor": asdict(actor),
            "applicant": {
                "user_id": actor.actor_id,
                "display_name": actor.display_name or actor.actor_id,
            },
            "owner": {
                "user_id": actor.actor_id,
                "display_name": actor.display_name or actor.actor_id,
            },
            "customer": checkpoint_dict(profile),
            "use_cached_credit": use_cached_credit,
            "pending_files": pending,
            "pending_document_kind": "credit",
            "source_files": [],
            "source_documents": [],
            "credit_source_files": [],
            "contract_source_files": [],
            "contract_facts": [],
            "credit_assessment": None,
            "effective_credit_assessment": None,
            "credit_status": "collecting",
            "credit_approval": {},
            "credit_approval_request": None,
            "approval_evidence": [],
            "approval_chain": [],
            "credit_control": {},
            "special_release": None,
            "exception_approval": None,
            "contract_reviews": [],
            "decision": None,
            "approval_route": None,
            "approval_request": None,
            "human_decision": None,
            "waiting_for": None,
            "reports": {},
            "writeback": {},
            "oa_submission": {},
            "trace": [],
            "errors": [],
        }
        self.graph.invoke(initial, config=self._config(resolved_case_id))
        return self._run_result(resolved_case_id)

    def get(self, case_id: str) -> WorkflowRun:
        return self._run_result(case_id)

    def assign_owner(self, case_id: str, owner: dict[str, Any]) -> WorkflowRun:
        current = self.get(case_id)
        normalized = {
            "user_id": str(owner.get("user_id") or ""),
            "display_name": str(owner.get("display_name") or ""),
        }
        if not normalized["user_id"] or not normalized["display_name"]:
            raise ValueError("负责人信息不完整。")
        self.graph.update_state(
            self._config(case_id),
            {
                "owner": normalized,
                "trace": [
                    {
                        "ts": utc_now(),
                        "stage": "case.owner_assigned",
                        "message": f"案件负责人已调整为{normalized['display_name']}。",
                        "data": normalized,
                    }
                ],
            },
        )
        return self._run_result(current.case_id)

    @staticmethod
    def _writeback_payload(state: dict[str, Any], phase: str) -> tuple[str, dict[str, Any]]:
        customer = dict(state.get("customer") or {})
        credit = dict(state.get("effective_credit_assessment") or {})
        approval = dict(state.get("credit_approval") or {})
        customer_id = str(
            customer.get("crm_customer_id")
            or customer.get("unified_social_credit_code")
            or ""
        )
        status = (
            "credit_effective"
            if phase == "credit_activation"
            else "inactive"
            if phase == "inactivation"
            else str(state.get("status") or "completed")
        )
        payload = {
            "case_id": state.get("case_id"),
            "status": status,
            "customer_status": customer.get("customer_status") or "Active",
            "business_type": customer.get("business_type"),
            "tkm_business_subtype": customer.get("tkm_business_subtype"),
            "approved_total_credit_limit": credit.get("approved_credit_limit"),
            "approved_term_days": credit.get("recommended_term_days"),
            "purchase_exemption_approved": credit.get(
                "purchase_exemption_approved", False
            ),
            "max_tail_payment_ratio": credit.get("max_tail_payment_ratio"),
            "max_tail_term_days": credit.get("max_tail_term_days"),
            "approval_scope": approval.get("approval_scope"),
            "effective_at": approval.get("effective_at"),
            "expires_at": approval.get("expires_at"),
            "oa_evidence_id": approval.get("oa_evidence_id"),
            "approval_chain": list(state.get("approval_chain") or []),
        }
        if phase == "inactivation":
            payload.update(
                {
                    "customer_status": "Inactive",
                    "approved_total_credit_limit": 0,
                    "approved_term_days": 0,
                    "inactivation_reason": approval.get("inactivation_reason"),
                    "inactivated_at": approval.get("inactivated_at"),
                }
            )
        return customer_id, payload

    def retry_writeback(
        self,
        case_id: str,
        *,
        phase: str,
        system: str,
        actor: ActorContext | None = None,
    ) -> WorkflowRun:
        current = self.get(case_id)
        state = dict(current.state)
        writeback = dict(state.get("writeback") or {})
        phase_result = dict(writeback.get(phase) or {})
        existing = dict(phase_result.get(system) or {})
        if not phase_result:
            raise KeyError("回写阶段不存在。")
        if str(existing.get("status") or "") != "failed":
            raise ValueError("只有失败的回写记录可以重试。")
        customer_id, payload = self._writeback_payload(state, phase)
        result = self.nodes.integrations.retry_writeback(
            case_id,
            system,
            customer_id,
            payload,
            phase=phase,
        )
        actor = actor or ActorContext()
        retry_history = list(existing.get("retry_history") or [])
        retry_history.append(
            {
                **dict(result),
                "actor_id": actor.actor_id,
                "actor_name": actor.display_name or actor.actor_id,
            }
        )
        phase_result[system] = {**dict(result), "retry_history": retry_history}
        writeback[phase] = phase_result
        self.graph.update_state(
            self._config(case_id),
            {
                "writeback": writeback,
                "trace": [
                    {
                        "ts": utc_now(),
                        "stage": "integration.writeback_retried",
                        "message": f"{system.upper()} {phase} 回写已人工重试。",
                        "data": {
                            "phase": phase,
                            "system": system,
                            "status": result.get("status"),
                            "actor_id": actor.actor_id,
                        },
                    }
                ],
            },
        )
        return self._run_result(case_id)

    def _authorize(self, waiting_for: str | None, actor: ActorContext) -> None:
        if "system" in actor.roles or "admin" in actor.roles:
            return
        required = self.ROLE_REQUIREMENTS.get(str(waiting_for or ""), set())
        if required and not required.intersection(actor.roles):
            raise PermissionError(
                f"{waiting_for} 需要角色 {sorted(required)}，当前角色为 {sorted(actor.roles)}。"
            )

    def _archive_approval_evidence(
        self,
        case_id: str,
        paths: list[str],
        actor: ActorContext,
        purpose: str,
    ) -> list[dict[str, Any]]:
        if not paths:
            return []
        target_dir = self.evidence_dir / case_id
        target_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for raw_path in paths:
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError(f"审批证据文件不存在：{source.name}")
            display_name = (
                source.name[3:]
                if len(source.name) > 3
                and source.name[:2].isdigit()
                and source.name[2] == "-"
                else source.name
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = target_dir / f"{digest[:12]}-{display_name}"
            if not target.exists():
                shutil.copy2(source, target)
            records.append(
                {
                    "purpose": purpose,
                    "name": display_name,
                    "sha256": digest,
                    "size_bytes": source.stat().st_size,
                    "archived_path": str(target.resolve()),
                    "actor_id": actor.actor_id,
                    "source_system": actor.source_system,
                    "archived_at": utc_now(),
                }
            )
        return records

    def resume(
        self,
        case_id: str,
        decision: dict[str, Any],
        *,
        actor: ActorContext | None = None,
    ) -> WorkflowRun:
        actor = actor or ActorContext()
        current = self.get(case_id)
        if not current.paused:
            raise ValueError(f"案件 {case_id} 当前不在等待状态。")
        self._authorize(current.waiting_for, actor)
        payload = dict(decision)
        texts = list(payload.pop("contract_texts", []) or [])
        if texts and current.waiting_for not in {"contract_upload", "sales_revision"}:
            raise ValueError("当前节点不接受合同正文。")
        files = [
            str(Path(item).expanduser().resolve())
            for item in payload.get("file_paths") or []
        ]
        files.extend(self._stage_contract_texts(case_id, texts))
        if (
            str(payload.get("action") or "") == "approve"
            and current.waiting_for in {"manager_approval", "special_release"}
        ):
            evidence = self._archive_approval_evidence(
                case_id,
                files,
                actor,
                current.waiting_for,
            )
            if not evidence:
                raise ValueError("批准例外或特别放行时必须上传审批证据附件。")
            payload["approval_evidence"] = evidence
            payload.pop("file_paths", None)
        elif current.waiting_for in {"manager_approval", "special_release"}:
            payload.pop("file_paths", None)
        elif files:
            payload["file_paths"] = files
        payload["actor"] = asdict(actor)
        self.graph.invoke(
            Command(resume=payload, update={"actor": asdict(actor)}),
            config=self._config(case_id),
        )
        return self._run_result(case_id)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DongjiangWorkflowHarness":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
