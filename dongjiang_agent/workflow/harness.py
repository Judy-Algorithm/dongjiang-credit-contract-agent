"""Execution harness around LangGraph: identity, permissions, staging and recovery."""

from __future__ import annotations

from copy import deepcopy
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
from ..llm import StructuredFieldExtractor
from ..persistence import CaseRepository
from ..runtime import AgentRegistry, LocalAgentRegistry, ToolRegistry
from .codec import case_from_state, checkpoint_dict
from .dynamic import (
    RETRYABLE_ANALYSIS_TASKS,
    assert_plan_integrity,
    build_contract_plan,
    build_credit_plan,
)
from .graph import build_workflow
from .incidents import detect_plan_incident, new_agent_incident
from .candidate_reviews import (
    CANDIDATE_WAITING_FOR,
    apply_contract_candidate,
    build_candidate_comparison,
    candidate_fingerprint,
    latest_candidate,
)
from .nodes import WorkflowNodes


LEGACY_ACTOR_ROLE_MAP = {
    "admin": "system_admin",
    "sales": "case_submitter",
    "credit": "credit_approver",
    "finance": "credit_approver",
    "legal": "legal_reviewer",
    "director": "exception_approver",
    "ceo": "exception_approver",
}


def _actor_roles(actor: "ActorContext") -> set[str]:
    return {
        LEGACY_ACTOR_ROLE_MAP.get(str(role), str(role)) for role in actor.roles
    }


@dataclass(slots=True, frozen=True)
class ActorContext:
    actor_id: str = "local-system"
    roles: tuple[str, ...] = ("system",)
    source_system: str = "local"
    display_name: str = ""
    administrator_id: str = ""
    administrator_display_name: str = ""


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
        # Some LangGraph/SQLite combinations expose an interrupted task while
        # omitting it from snapshot.next. The interrupt is still resumable.
        return bool(self.next_nodes or self.interrupt)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["paused"] = self.paused
        return payload


class DongjiangWorkflowHarness:
    """Stable API used by Web/CRM/OA; LangGraph stays an internal detail."""

    ROLE_REQUIREMENTS = {
        "credit_approval": {"credit_approver"},
        "credit_supplement": {"case_submitter"},
        "special_release": {"exception_approver"},
        "contract_upload": {"case_submitter"},
        "sales_revision": {"case_submitter"},
        "manager_approval": {"exception_approver"},
        "finance_legal_review": {"legal_reviewer"},
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
        execution_dir: str | Path | None = None,
        integrations: IntegrationBundle | None = None,
        policy: dict[str, Any] | None = None,
        agent_registry: AgentRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
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
            execution_dir=(
                Path(execution_dir)
                if execution_dir is not None
                else Path("data/executions")
                if default_layout
                else self.inbox_dir.parent / "executions"
            ),
            integrations=integration_bundle,
            policy=policy,
            tool_registry=tool_registry,
        )
        self.agent_registry = agent_registry or LocalAgentRegistry()
        self.tool_registry = self.nodes.tool_registry
        self.graph = build_workflow(
            self.nodes,
            checkpointer=self.checkpointer,
            agent_registry=self.agent_registry,
        )

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
            "workflow_plans": [],
            "orchestration_plan": None,
            "orchestration_runs": [],
            "agent_runs": [],
            "agent_task_results": [],
            "execution_audits": [],
            "agent_incidents": [],
            "agent_candidate_reviews": [],
            "structured_extractions": [],
            "agent_rerun_context": None,
            "active_workflow_plan": None,
            "active_agent_task": None,
            "credit_analysis": {},
            "credit_verification": {},
            "contract_verifications": [],
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

    def generate_structured_extraction(
        self,
        case_id: str,
        *,
        document_kind: str,
        actor: ActorContext,
        extractor: StructuredFieldExtractor | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        allowed_roles = (
            {"credit_approver", "system"}
            if document_kind == "credit"
            else {"legal_reviewer", "exception_approver", "system"}
            if document_kind == "contract"
            else set()
        )
        if not allowed_roles.intersection(_actor_roles(actor)):
            raise PermissionError("当前角色不能发起AI结构化提取。")
        current = self.get(case_id)
        if document_kind == "credit" and current.waiting_for != "credit_approval":
            raise ValueError("信用结构化提取仅支持信用审批节点。")
        if document_kind == "contract" and current.waiting_for not in {
            "manager_approval",
            "finance_legal_review",
        }:
            raise ValueError("合同结构化提取仅支持合同审批节点。")
        documents = [
            deepcopy(item)
            for item in current.state.get("source_documents") or []
            if item.get("document_kind") == document_kind
            and item.get("parse_status") == "parsed"
            and item.get("fragments")
        ]
        if not documents:
            raise ValueError("当前案件没有可定位的已解析资料。")
        redacted_text = "\n\n".join(
            f"[DOCUMENT {item.get('document_id')}]\n"
            + "\n".join(
                str(fragment.get("text") or "")
                for fragment in item.get("fragments") or []
            )
            for item in documents
        )
        result = (extractor or StructuredFieldExtractor()).extract(
            document_kind,
            redacted_text=redacted_text,
            documents=documents,
        )
        extraction = {
            "extraction_id": f"XTR-{uuid4().hex[:12].upper()}",
            "document_kind": document_kind,
            "status": (
                "pending"
                if (result.get("verification") or {}).get("status") == "passed"
                else "verification_failed"
            ),
            "model": result.get("model"),
            "prompt_version": result.get("prompt_version"),
            "summary": result.get("summary"),
            "candidates": list(result.get("candidates") or []),
            "verification": dict(result.get("verification") or {}),
            "created_by": {
                "user_id": actor.actor_id,
                "display_name": actor.display_name or actor.actor_id,
            },
            "created_at": utc_now(),
            "decision": {},
        }
        rows = [
            deepcopy(item)
            for item in current.state.get("structured_extractions") or []
        ]
        rows.append(extraction)
        self.graph.update_state(
            self._config(case_id),
            {
                "structured_extractions": rows,
                "trace": [
                    {
                        "ts": utc_now(),
                        "stage": "agent.extraction.generated",
                        "message": "AI结构化字段候选已生成并完成独立证据核验。",
                        "data": {
                            "extraction_id": extraction["extraction_id"],
                            "document_kind": document_kind,
                            "candidate_count": len(extraction["candidates"]),
                            "verification_status": extraction["verification"].get(
                                "status"
                            ),
                            "official_state_changed": False,
                        },
                    }
                ],
            },
        )
        return self._run_result(case_id), deepcopy(extraction)

    def manage_structured_extraction(
        self,
        case_id: str,
        *,
        extraction_id: str,
        action: str,
        actor: ActorContext,
        candidate_ids: list[str] | None = None,
        reason: str = "",
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        if action not in {"adopt", "reject"}:
            raise ValueError("结构化提取处置动作必须是adopt或reject。")
        clean_reason = " ".join(str(reason or "").split())[:500]
        if len(clean_reason) < 2:
            raise ValueError("采纳或拒绝结构化候选时必须填写理由。")
        current = self.get(case_id)
        state = deepcopy(current.state)
        rows = [deepcopy(item) for item in state.get("structured_extractions") or []]
        extraction = next(
            (
                item
                for item in rows
                if str(item.get("extraction_id") or "") == extraction_id
            ),
            None,
        )
        if extraction is None:
            raise KeyError("AI结构化提取记录不存在。")
        allowed_roles = (
            {"credit_approver", "system"}
            if extraction.get("document_kind") == "credit"
            else {"legal_reviewer", "exception_approver", "system"}
        )
        if not allowed_roles.intersection(_actor_roles(actor)):
            raise PermissionError("当前角色不能处置AI结构化提取候选。")
        if extraction.get("status") != "pending":
            raise ValueError("该结构化提取候选已完成处置或核验失败。")
        selected_ids = {str(item) for item in candidate_ids or []}
        selected = [
            deepcopy(item)
            for item in extraction.get("candidates") or []
            if not selected_ids or str(item.get("candidate_id") or "") in selected_ids
        ]
        now = utc_now()
        if action == "reject":
            extraction.update(
                {
                    "status": "rejected",
                    "decision": {
                        "action": action,
                        "reason": clean_reason,
                        "official_state_changed": False,
                    },
                    "decided_by": {
                        "user_id": actor.actor_id,
                        "display_name": actor.display_name or actor.actor_id,
                    },
                    "decided_at": now,
                }
            )
            self.graph.update_state(
                self._config(case_id),
                {
                    "structured_extractions": rows,
                    "trace": [
                        {
                            "ts": now,
                            "stage": "agent.extraction.rejected",
                            "message": "AI结构化字段候选已被人工拒绝。",
                            "data": {
                                "extraction_id": extraction_id,
                                "official_state_changed": False,
                            },
                        }
                    ],
                },
            )
            return self._run_result(case_id), deepcopy(extraction)
        if not selected:
            raise ValueError("请至少选择一个待采纳字段。")
        if (extraction.get("verification") or {}).get("status") != "passed":
            raise ValueError("结构化候选独立核验未通过，不能采纳。")
        update = self._adopt_extraction_candidates(
            state, extraction, selected, current.waiting_for
        )
        extraction.update(
            {
                "status": "adopted",
                "decision": {
                    "action": action,
                    "reason": clean_reason,
                    "candidate_ids": [item["candidate_id"] for item in selected],
                    "official_state_changed": True,
                    "result_plan_id": update.get("active_workflow_plan", {}).get(
                        "plan_id"
                    ),
                },
                "decided_by": {
                    "user_id": actor.actor_id,
                    "display_name": actor.display_name or actor.actor_id,
                },
                "decided_at": now,
            }
        )
        update["structured_extractions"] = rows
        update["trace"] = list(update.get("trace") or []) + [
            {
                "ts": now,
                "stage": "agent.extraction.adopted",
                "message": "人工采纳AI结构化字段后已重新执行受控Agent计划。",
                "data": {
                    "extraction_id": extraction_id,
                    "candidate_count": len(selected),
                    "plan_id": extraction["decision"].get("result_plan_id"),
                    "official_state_changed": True,
                },
            }
        ]
        self.graph.update_state(self._config(case_id), update)
        return self._run_result(case_id), deepcopy(extraction)

    def _adopt_extraction_candidates(
        self,
        state: dict[str, Any],
        extraction: dict[str, Any],
        selected: list[dict[str, Any]],
        current_waiting_for: str | None,
    ) -> dict[str, Any]:
        kind = str(extraction.get("document_kind") or "")
        snapshot = self.nodes._runtime_snapshot()
        snapshot["structured_extraction_ref"] = str(
            extraction.get("extraction_id") or ""
        )
        candidate = deepcopy(state)
        candidate["agent_task_results"] = []
        candidate["agent_runs"] = []
        candidate["execution_audits"] = []
        candidate["trace"] = []
        candidate["errors"] = []
        candidate["agent_rerun_context"] = None
        if kind == "credit":
            customer = deepcopy(candidate.get("customer") or {})
            for item in selected:
                customer[str(item["field"])] = item.get("value")
            candidate["customer"] = customer
            plan = build_credit_plan(
                str(state["case_id"]),
                customer,
                list(state.get("source_documents") or []),
                runtime_snapshot=snapshot,
            )
            candidate["active_workflow_plan"] = plan
            for task in plan.get("tasks") or []:
                if task.get("phase") == "analysis":
                    candidate["active_agent_task"] = task
                    self._merge_node_update(
                        candidate, self.nodes.run_credit_analysis(candidate)
                    )
            self._merge_node_update(
                candidate, self.nodes.synthesize_credit_analysis(candidate)
            )
            self._merge_node_update(candidate, self.nodes.score_credit(candidate))
            self._merge_node_update(candidate, self.nodes.verify_credit(candidate))
            waiting_for = (
                "credit_supplement"
                if (candidate.get("credit_assessment") or {}).get(
                    "requires_supplement"
                )
                else "credit_approval"
            )
            if waiting_for != current_waiting_for:
                raise ValueError("采纳候选会改变当前信用审批节点，请走正式补件重审。")
            return {
                "customer": customer,
                "credit_assessment": candidate.get("credit_assessment"),
                "credit_analysis": candidate.get("credit_analysis"),
                "credit_verification": candidate.get("credit_verification"),
                "active_workflow_plan": candidate.get("active_workflow_plan"),
                "workflow_plans": [candidate.get("active_workflow_plan")],
                "agent_task_results": candidate.get("agent_task_results"),
                "agent_runs": candidate.get("agent_runs"),
                "execution_audits": candidate.get("execution_audits"),
                "status": (
                    "credit_supplement_required"
                    if waiting_for == "credit_supplement"
                    else "credit_pending_approval"
                ),
                "credit_status": (
                    "supplement_required"
                    if waiting_for == "credit_supplement"
                    else "pending_approval"
                ),
                "waiting_for": waiting_for,
                "trace": candidate.get("trace"),
            }
        if kind == "contract":
            contracts = [deepcopy(item) for item in candidate.get("contract_facts") or []]
            by_id = {
                str(item.get("document_id") or ""): item for item in contracts
            }
            for item in selected:
                target = by_id.get(str(item.get("document_id") or ""))
                if target is None:
                    raise ValueError("合同字段候选关联的文档已不存在。")
                target[str(item["field"])] = item.get("value")
                target.setdefault("evidence", []).append(
                    {
                        "source": "ai_structured_candidate",
                        "field": item["field"],
                        "value": item.get("value"),
                        "confidence": item.get("confidence"),
                        "excerpt": item.get("evidence_query"),
                        "document_id": item.get("document_id"),
                        "fragment_id": item.get("fragment_id"),
                        "location": dict(item.get("location") or {}),
                    }
                )
            candidate["contract_facts"] = contracts
            plan = build_contract_plan(
                str(state["case_id"]),
                contracts,
                ai_available=bool(
                    self.nodes.contract_ai.enabled
                    and self.nodes.contract_ai.gateway.available
                ),
                runtime_snapshot=snapshot,
            )
            candidate["active_workflow_plan"] = plan
            for task in plan.get("tasks") or []:
                if task.get("phase") == "analysis":
                    candidate["active_agent_task"] = task
                    self._merge_node_update(
                        candidate, self.nodes.run_contract_analysis(candidate)
                    )
            self._merge_node_update(
                candidate, self.nodes.synthesize_contract_reviews(candidate)
            )
            self._merge_node_update(
                candidate, self.nodes.verify_contract_reviews(candidate)
            )
            decision_update = self.nodes.decide(candidate)
            waiting_for = str(decision_update.get("waiting_for") or "") or None
            if waiting_for != current_waiting_for:
                raise ValueError("采纳候选会改变合同审批路由，请提交正式合同重审。")
            return {
                "contract_facts": contracts,
                "contract_reviews": candidate.get("contract_reviews"),
                "contract_verifications": candidate.get("contract_verifications"),
                "decision": decision_update.get("decision"),
                "approval_route": decision_update.get("approval_route"),
                "approval_request": decision_update.get("approval_request"),
                "active_workflow_plan": candidate.get("active_workflow_plan"),
                "workflow_plans": [candidate.get("active_workflow_plan")],
                "agent_task_results": candidate.get("agent_task_results"),
                "agent_runs": candidate.get("agent_runs"),
                "execution_audits": candidate.get("execution_audits"),
                "status": decision_update.get("status"),
                "waiting_for": waiting_for,
                "trace": candidate.get("trace"),
            }
        raise ValueError("未知的结构化提取类型。")

    @staticmethod
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

    @staticmethod
    def _merge_node_update(
        state: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        for key, value in update.items():
            if key in {"agent_task_results", "agent_runs", "execution_audits", "trace", "errors"}:
                state[key] = list(state.get(key) or []) + list(value or [])
            else:
                state[key] = value
        return state

    def _isolated_agent_rerun(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        task: dict[str, Any],
        *,
        incident_id: str,
    ) -> dict[str, Any]:
        """Execute a candidate analysis and re-verify it without changing business state."""
        assert_plan_integrity(plan)
        task_type = str(task.get("task_type") or "")
        if task_type not in RETRYABLE_ANALYSIS_TASKS or task.get("phase") != "analysis":
            raise ValueError("该节点属于决策、核验或非白名单节点，禁止直接重跑。")
        plan_id = str(plan.get("plan_id") or "")
        analysis_ids = {
            str(item.get("task_id") or "")
            for item in plan.get("tasks") or []
            if item.get("phase") == "analysis"
        }
        latest_results = self._latest_plan_rows(
            list(state.get("agent_task_results") or []), plan_id
        )
        latest_runs = self._latest_plan_rows(list(state.get("agent_runs") or []), plan_id)
        candidate = deepcopy(state)
        candidate["active_workflow_plan"] = deepcopy(plan)
        candidate["active_agent_task"] = deepcopy(task)
        candidate["agent_rerun_context"] = {
            "incident_id": incident_id,
            "plan_id": plan_id,
            "task_id": task.get("task_id"),
        }
        candidate["agent_task_results"] = [
            row
            for task_id, row in latest_results.items()
            if task_id in analysis_ids and task_id != task.get("task_id")
        ]
        candidate["agent_runs"] = [
            row
            for task_id, row in latest_runs.items()
            if task_id in analysis_ids and task_id != task.get("task_id")
        ]
        candidate["execution_audits"] = []
        candidate["trace"] = []
        candidate["errors"] = []
        if plan.get("agent") == "credit":
            updates = [
                self.nodes.run_credit_analysis(candidate),
            ]
            for update in updates:
                self._merge_node_update(candidate, update)
            self._merge_node_update(candidate, self.nodes.synthesize_credit_analysis(candidate))
            self._merge_node_update(candidate, self.nodes.score_credit(candidate))
            self._merge_node_update(candidate, self.nodes.verify_credit(candidate))
            assessment = dict(candidate.get("credit_assessment") or {})
            verification = dict(candidate.get("credit_verification") or {})
            summary = {
                "score": assessment.get("score"),
                "risk_level": assessment.get("risk_level"),
                "approved_credit_limit": assessment.get("approved_credit_limit"),
                "recommended_term_days": assessment.get("recommended_term_days"),
                "requires_supplement": bool(assessment.get("requires_supplement")),
                "credit_locked": bool(assessment.get("credit_locked")),
                "verification_status": verification.get("status"),
                "plan_id": plan_id,
                "spec_ref": str(plan.get("spec_hash") or "")[:12],
            }
        elif plan.get("agent") == "contract":
            self._merge_node_update(candidate, self.nodes.run_contract_analysis(candidate))
            self._merge_node_update(candidate, self.nodes.synthesize_contract_reviews(candidate))
            self._merge_node_update(candidate, self.nodes.verify_contract_reviews(candidate))
            reviews = list(candidate.get("contract_reviews") or [])
            contracts = list(candidate.get("contract_facts") or [])
            verifications = {
                str(item.get("document_id") or ""): dict(item)
                for item in candidate.get("contract_verifications") or []
            }
            documents: list[dict[str, Any]] = []
            for index, review in enumerate(reviews):
                document_id = str(
                    review.get("document_id")
                    or (contracts[index] if index < len(contracts) else {}).get(
                        "document_id"
                    )
                    or f"contract-{index + 1}"
                )
                findings = list(review.get("findings") or [])
                assistance = dict(review.get("ai_assistance") or {})
                verification = verifications.get(document_id) or {}
                documents.append(
                    {
                        "document_id": document_id,
                        "decision": review.get("decision"),
                        "risk_level": review.get("risk_level"),
                        "rule_finding_count": len(findings),
                        "ai_finding_count": len(assistance.get("findings") or []),
                        "rule_ids": sorted(
                            {
                                str(item.get("rule_id") or "")
                                for item in findings
                                if item.get("rule_id")
                            }
                        ),
                        "verification_status": verification.get("status"),
                        "evidence_coverage": verification.get("evidence_coverage"),
                    }
                )
            summary = {
                "review_count": len(reviews),
                "decisions": sorted(
                    {str(item.get("decision") or "") for item in reviews if item.get("decision")}
                ),
                "verification_statuses": [
                    str(item.get("status") or "")
                    for item in candidate.get("contract_verifications") or []
                ],
                "documents": documents,
                "plan_id": plan_id,
                "spec_ref": str(plan.get("spec_hash") or "")[:12],
            }
        else:
            raise ValueError("未知的Agent计划类型。")
        rerun_run = next(
            (
                dict(item)
                for item in reversed(candidate.get("agent_runs") or [])
                if item.get("task_id") == task.get("task_id")
            ),
            {},
        )
        audit = dict((candidate.get("execution_audits") or [{}])[-1])
        return {
            "incident_id": incident_id,
            "plan_id": plan_id,
            "task_id": task.get("task_id"),
            "task_type": task_type,
            "status": str(rerun_run.get("status") or "failed"),
            "attempt_count": int(rerun_run.get("attempt_count") or 0),
            "evidence_gate": str(
                rerun_run.get("evidence_gate") or "not_applicable"
            ),
            "execution_audit": str(audit.get("status") or "pending"),
            "candidate_summary": summary,
            "completed_at": utc_now(),
            "official_state_changed": False,
        }

    def manage_agent_incident(
        self,
        case_id: str,
        *,
        plan_id: str,
        action: str,
        actor: ActorContext,
        note: str = "",
        assignee: dict[str, Any] | None = None,
        task_id: str = "",
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        if not {"system_admin", "system"}.intersection(_actor_roles(actor)):
            raise PermissionError("只有管理员可以处置Agent运行异常。")
        if action not in {"acknowledge", "assign", "rerun", "resolve"}:
            raise ValueError("未知的Agent异常处置动作。")
        current = self.get(case_id)
        state = dict(current.state)
        plan = next(
            (
                deepcopy(item)
                for item in reversed(state.get("workflow_plans") or [])
                if str(item.get("plan_id") or "") == str(plan_id)
            ),
            None,
        )
        if plan is None:
            raise KeyError("Agent计划不存在。")
        clean_note = " ".join(str(note or "").split())[:500]
        if action in {"rerun", "resolve"} and len(clean_note) < 2:
            raise ValueError("重跑或关闭异常时必须填写处理说明。")
        incidents = [deepcopy(item) for item in state.get("agent_incidents") or []]
        incident = next(
            (
                item
                for item in reversed(incidents)
                if item.get("plan_id") == plan_id and item.get("status") != "resolved"
            ),
            None,
        )
        if incident is None:
            signal = detect_plan_incident(state, plan)
            if signal is None:
                raise ValueError("当前Agent计划未发现可处置的运行异常。")
            if any(
                item.get("status") == "resolved"
                and item.get("issue_fingerprint") == signal["issue_fingerprint"]
                for item in incidents
            ):
                raise ValueError("当前Agent异常事实已处置，未发现新的异常变化。")
            if action != "acknowledge":
                raise ValueError("请先确认Agent运行异常，再执行分派、重跑或关闭。")
        elif incident.get("status") == "open" and action != "acknowledge":
            raise ValueError("请先确认Agent运行异常，再执行分派、重跑或关闭。")
        now = utc_now()
        if incident is None:
            incident = new_agent_incident(signal, source="manual")
            incidents.append(incident)
        normalized_assignee = dict(assignee or {})
        if action == "acknowledge":
            incident["status"] = "acknowledged"
            incident["responded_at"] = incident.get("responded_at") or now
            if not incident.get("assignee"):
                incident["assignee"] = {
                    "user_id": actor.actor_id,
                    "display_name": actor.display_name or actor.actor_id,
                }
        elif action == "assign":
            if not normalized_assignee.get("user_id") or not normalized_assignee.get(
                "display_name"
            ):
                raise ValueError("请选择有效的异常责任人。")
            incident["status"] = "assigned"
            incident["responded_at"] = incident.get("responded_at") or now
            incident["assignee"] = {
                "user_id": str(normalized_assignee["user_id"]),
                "display_name": str(normalized_assignee["display_name"]),
            }
        elif action == "rerun":
            task = next(
                (
                    deepcopy(item)
                    for item in plan.get("tasks") or []
                    if str(item.get("task_id") or "") == str(task_id)
                ),
                None,
            )
            if task is None:
                raise KeyError("待重跑节点不存在。")
            rerun = self._isolated_agent_rerun(
                state, plan, task, incident_id=str(incident["incident_id"])
            )
            incident.setdefault("rerun_history", []).append(
                {
                    **rerun,
                    "actor_id": actor.actor_id,
                    "actor_name": actor.display_name or actor.actor_id,
                    "note": clean_note,
                }
            )
            incident["status"] = "rerun_completed"
        else:
            incident["status"] = "resolved"
            incident["resolved_at"] = now
        incident["updated_at"] = now
        incident["latest_note"] = clean_note
        incident.setdefault("history", []).append(
            {
                "action": action,
                "actor_id": actor.actor_id,
                "actor_name": actor.display_name or actor.actor_id,
                "note": clean_note,
                "at": now,
            }
        )
        self.graph.update_state(
            self._config(case_id),
            {
                "agent_incidents": incidents,
                "trace": [
                    {
                        "ts": now,
                        "stage": f"agent.incident.{action}",
                        "message": "Agent运行异常已执行受控处置。",
                        "data": {
                            "incident_id": incident["incident_id"],
                            "plan_id": plan_id,
                            "task_id": task_id,
                            "action": action,
                            "actor_id": actor.actor_id,
                            "official_state_changed": False,
                        },
                    }
                ],
            },
        )
        return self._run_result(case_id), deepcopy(incident)

    def manage_agent_candidate(
        self,
        case_id: str,
        *,
        incident_id: str,
        action: str,
        actor: ActorContext,
        reason: str,
        request_id: str = "",
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        allowed_roles = {
            "credit_approver",
            "legal_reviewer",
            "exception_approver",
            "system",
        }
        if not allowed_roles.intersection(_actor_roles(actor)):
            raise PermissionError("当前角色不能提交或拒绝Agent候选结果。")
        if action not in {"request_adoption", "reject_candidate"}:
            raise ValueError("未知的Agent候选处置动作。")
        clean_reason = " ".join(str(reason or "").split())[:500]
        if len(clean_reason) < 2:
            raise ValueError("提交采纳或拒绝候选时必须填写理由。")
        current = self.get(case_id)
        state = dict(current.state)
        incidents = list(state.get("agent_incidents") or [])
        incident = next(
            (
                deepcopy(item)
                for item in incidents
                if str(item.get("incident_id") or "") == incident_id
            ),
            None,
        )
        if incident is None:
            raise KeyError("Agent异常不存在。")
        plan = next(
            (
                item
                for item in state.get("workflow_plans") or []
                if str(item.get("plan_id") or "")
                == str(incident.get("plan_id") or "")
            ),
            None,
        )
        if plan is None:
            raise KeyError("Agent候选关联的计划不存在。")
        assert_plan_integrity(plan)
        if str(plan.get("agent") or "") != str(incident.get("agent") or ""):
            raise ValueError("Agent候选类型与冻结计划不一致。")
        if str(incident.get("status") or "") == "open":
            raise ValueError("请先确认Agent运行异常，再处置候选结果。")
        candidate = latest_candidate(incident)
        if not candidate:
            raise ValueError("该异常尚未形成可处置的候选结果。")
        fingerprint = candidate_fingerprint(candidate)
        reviews = [deepcopy(item) for item in state.get("agent_candidate_reviews") or []]
        existing = next(
            (
                item
                for item in reversed(reviews)
                if item.get("incident_id") == incident_id
                and (
                    (request_id and str(item.get("request_id") or "") == request_id)
                    or (
                    not request_id
                    and item.get("candidate_fingerprint") == fingerprint
                    and item.get("status") == "pending"
                    )
                )
            ),
            None,
        )
        if request_id and existing is None:
            raise KeyError("Agent候选采纳申请不存在或不属于该异常。")
        now = utc_now()
        if action == "request_adoption":
            agent = str(incident.get("agent") or "")
            eligible = CANDIDATE_WAITING_FOR.get(agent, set())
            if current.waiting_for not in eligible:
                raise ValueError("案件当前不在与该候选兼容的正式审批节点。")
            if incident.get("status") == "resolved":
                raise ValueError("已关闭的Agent异常不能再提交候选采纳申请。")
            summary = dict(candidate.get("candidate_summary") or {})
            if str(candidate.get("execution_audit") or "") != "conformant":
                raise ValueError("候选执行审计未通过，不能提交正式采纳。")
            if agent == "credit" and summary.get("verification_status") != "passed":
                raise ValueError("信用候选独立核验未通过，不能提交正式采纳。")
            if agent == "contract" and (
                not summary.get("documents")
                or any(
                    item.get("verification_status") != "passed"
                    for item in summary.get("documents") or []
                )
            ):
                raise ValueError("合同候选独立核验未通过，不能提交正式采纳。")
            if existing:
                raise ValueError("同一候选已有待审批的采纳申请。")
            for item in reviews:
                if (
                    item.get("incident_id") == incident_id
                    and item.get("status") == "pending"
                    and item.get("candidate_fingerprint") != fingerprint
                ):
                    item.update(
                        {
                            "status": "superseded",
                            "decision": {
                                "action": "candidate_replaced",
                                "official_state_changed": False,
                            },
                            "decided_at": now,
                        }
                    )
            review = {
                "request_id": f"ACR-{uuid4().hex[:12].upper()}",
                "incident_id": incident_id,
                "plan_id": incident.get("plan_id"),
                "agent": agent,
                "rerun_completed_at": candidate.get("completed_at"),
                "status": "pending",
                "requested_by": {
                    "user_id": actor.actor_id,
                    "display_name": actor.display_name or actor.actor_id,
                },
                "requested_at": now,
                "reason": clean_reason,
                "eligible_waiting_for": current.waiting_for,
                "candidate_fingerprint": fingerprint,
                "decision": {},
                "decided_by": {},
                "decided_at": None,
            }
            reviews.append(review)
            trace_stage = "agent.candidate.adoption_requested"
        else:
            if existing and existing.get("status") != "pending":
                raise ValueError("该候选申请已完成处置。")
            review = existing or {
                "request_id": f"ACR-{uuid4().hex[:12].upper()}",
                "incident_id": incident_id,
                "plan_id": incident.get("plan_id"),
                "agent": incident.get("agent"),
                "rerun_completed_at": candidate.get("completed_at"),
                "requested_by": {},
                "requested_at": None,
                "eligible_waiting_for": None,
                "candidate_fingerprint": fingerprint,
            }
            if not existing:
                reviews.append(review)
            review.update(
                {
                    "status": "rejected",
                    "reason": clean_reason,
                    "decision": {"action": "reject_candidate", "reason_recorded": True},
                    "decided_by": {
                        "user_id": actor.actor_id,
                        "display_name": actor.display_name or actor.actor_id,
                    },
                    "decided_at": now,
                }
            )
            trace_stage = "agent.candidate.rejected"
        self.graph.update_state(
            self._config(case_id),
            {
                "agent_candidate_reviews": reviews,
                "trace": [
                    {
                        "ts": now,
                        "stage": trace_stage,
                        "message": "Agent候选结果已进入受控审批处置。",
                        "data": {
                            "request_id": review["request_id"],
                            "incident_id": incident_id,
                            "agent": review.get("agent"),
                            "action": action,
                            "actor_id": actor.actor_id,
                            "official_state_changed": False,
                        },
                    }
                ],
            },
        )
        updated = self._run_result(case_id)
        result = deepcopy(review)
        result["comparison"] = build_candidate_comparison(
            dict(updated.state), incident, candidate
        )
        return updated, result

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
        actor_roles = _actor_roles(actor)
        if "system" in actor_roles:
            return
        required = self.ROLE_REQUIREMENTS.get(str(waiting_for or ""), set())
        if required and not required.intersection(actor_roles):
            raise PermissionError(
                f"{waiting_for} 需要角色 {sorted(required)}，当前角色为 {sorted(actor_roles)}。"
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
        candidate_adoption = self._candidate_adoption_payload(current, payload, actor)
        if candidate_adoption:
            payload["_candidate_adoption"] = candidate_adoption
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
        resume_config, recovered = self._resume_config(current, actor)
        self.graph.invoke(
            Command(
                resume=payload,
                update=None if recovered else {"actor": asdict(actor)},
            ),
            config=resume_config,
        )
        return self._run_result(case_id)

    def _resume_config(
        self,
        current: WorkflowRun,
        actor: ActorContext,
    ) -> tuple[dict[str, Any], bool]:
        base_config = self._config(current.case_id)
        snapshot = self.graph.get_state(base_config)
        failed_interrupt = bool(
            current.waiting_for
            and current.interrupt
            and not snapshot.next
            and any(getattr(task, "error", None) for task in snapshot.tasks)
        )
        if not failed_interrupt:
            return base_config, False

        clean = next(
            (
                item
                for item in self.graph.get_state_history(base_config)
                if item.next
                and self._interrupt_payload(item)
                and (item.values or {}).get("waiting_for") == current.waiting_for
            ),
            None,
        )
        if clean is None:
            return base_config, False
        recovered_config = self.graph.update_state(
            clean.config,
            {
                "actor": asdict(actor),
                "trace": [
                    {
                        "ts": utc_now(),
                        "stage": "workflow.interrupt_retry_prepared",
                        "message": "上一轮人工操作校验失败，已从原等待节点重新提交。",
                        "data": {
                            "waiting_for": current.waiting_for,
                            "actor_id": actor.actor_id,
                        },
                    }
                ],
            },
        )
        return recovered_config, True

    def _candidate_adoption_payload(
        self,
        current: WorkflowRun,
        payload: dict[str, Any],
        actor: ActorContext,
    ) -> dict[str, Any]:
        request_id = str(payload.get("candidate_adoption_request_id") or "").strip()
        if not request_id:
            return {}
        action = str(payload.get("action") or "")
        if action != "approve":
            raise ValueError("正式采纳Agent候选时必须执行批准操作。")
        state = dict(current.state)
        reviews = [deepcopy(item) for item in state.get("agent_candidate_reviews") or []]
        review = next(
            (
                item
                for item in reviews
                if str(item.get("request_id") or "") == request_id
            ),
            None,
        )
        if review is None:
            raise KeyError("Agent候选采纳申请不存在。")
        if review.get("status") != "pending":
            raise ValueError("Agent候选采纳申请已完成处置。")
        agent = str(review.get("agent") or "")
        if current.waiting_for not in CANDIDATE_WAITING_FOR.get(agent, set()):
            raise ValueError("当前正式审批节点与Agent候选不兼容。")
        if review.get("eligible_waiting_for") != current.waiting_for:
            raise ValueError("Agent候选申请对应的审批节点已经变化。")
        incident = next(
            (
                item
                for item in state.get("agent_incidents") or []
                if str(item.get("incident_id") or "")
                == str(review.get("incident_id") or "")
            ),
            None,
        )
        if incident is None:
            raise KeyError("Agent候选关联的异常不存在。")
        plan = next(
            (
                item
                for item in state.get("workflow_plans") or []
                if str(item.get("plan_id") or "")
                == str(review.get("plan_id") or "")
            ),
            None,
        )
        if plan is None:
            raise KeyError("Agent候选关联的计划不存在。")
        assert_plan_integrity(plan)
        if str(plan.get("agent") or "") != agent:
            raise ValueError("Agent候选类型与冻结计划不一致。")
        candidate = latest_candidate(incident)
        if not candidate or candidate_fingerprint(candidate) != review.get(
            "candidate_fingerprint"
        ):
            raise ValueError("Agent候选结果已经变化，原采纳申请已失效。")
        summary = dict(candidate.get("candidate_summary") or {})
        now = utc_now()
        review.update(
            {
                "status": "approved",
                "decision": {
                    "action": action,
                    "waiting_for": current.waiting_for,
                    "official_state_changed": True,
                },
                "decided_by": {
                    "user_id": actor.actor_id,
                    "display_name": actor.display_name or actor.actor_id,
                },
                "decided_at": now,
            }
        )
        state_update: dict[str, Any] = {"agent_candidate_reviews": reviews}
        if agent == "credit":
            assessment = deepcopy(state.get("credit_assessment") or {})
            for key in (
                "score",
                "risk_level",
                "approved_credit_limit",
                "recommended_term_days",
                "requires_supplement",
                "credit_locked",
            ):
                if key in summary:
                    assessment[key] = summary[key]
            state_update["credit_assessment"] = assessment
            payload["approved_credit_limit"] = summary.get(
                "approved_credit_limit"
            )
            payload["approved_term_days"] = summary.get(
                "recommended_term_days"
            )
        elif agent == "contract":
            state_update["contract_reviews"] = apply_contract_candidate(
                list(state.get("contract_reviews") or []),
                list(state.get("contract_facts") or []),
                summary,
                request_id,
            )
        return {
            "agent": agent,
            "request_id": request_id,
            "state_update": state_update,
            "trace": {
                "ts": now,
                "stage": "agent.candidate.approved",
                "message": "Agent候选已由当前正式审批节点确认采纳。",
                "data": {
                    "request_id": request_id,
                    "incident_id": review.get("incident_id"),
                    "agent": agent,
                    "waiting_for": current.waiting_for,
                    "actor_id": actor.actor_id,
                    "official_state_changed": True,
                },
            },
        }

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DongjiangWorkflowHarness":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
