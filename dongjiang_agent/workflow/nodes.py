"""Business nodes used by the LangGraph workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from langgraph.types import Command, interrupt

from ..contract import ContractFactExtractor, ContractReviewEngine
from ..credit.extractor import CreditFactExtractor
from ..credit.model import CreditScoringEngine
from ..domain.codec import (
    assessment_from_dict,
    contract_facts_from_dict,
    profile_from_dict,
    review_from_dict,
)
from ..domain.decision import resolve_case_decision
from ..domain.models import (
    ApprovalRoute,
    AuditDecision,
    ContractReview,
    Evidence,
    RiskFinding,
    RiskLevel,
    utc_now,
)
from ..ingestion import (
    DocumentExtractor,
    DocumentFragment,
    DocumentQualityGate,
    ExtractedDocument,
    locate_excerpt,
)
from ..integrations import IntegrationBundle
from ..llm import CasePlanningAssistant, ContractAIAssistant, DocumentTextEnhancer
from ..persistence import CaseDocumentArchive, CaseRepository, TaskExecutionStore
from ..reporting import AuditReporter
from ..runtime import (
    InvocationContext,
    LocalToolRegistry,
    ToolRegistry,
    credit_dimension_result,
    document_payload,
    register_default_tools,
)
from ..security import RedactionVault
from .codec import case_from_state, checkpoint_dict
from .dynamic import (
    assert_plan_integrity,
    audit_plan_execution,
    build_contract_plan,
    build_credit_plan,
    canonical_hash,
    plan_task,
    task_idempotency_key,
)
from .orchestration import (
    allowed_case_task_types,
    build_case_plan,
    case_plan_has_task,
    case_plan_snapshot,
    orchestration_run,
    orchestration_waiting_run,
    required_case_task_types,
)
from .state import WorkflowState


def trace(stage: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ts": utc_now(), "stage": stage, "message": message, "data": data}


def agent_run(
    plan: dict[str, Any],
    task: dict[str, Any],
    *,
    started_at: str,
    duration_ms: int,
    input_summary: str,
    output_summary: str,
    status: str = "completed",
    model: str = "deterministic",
    attempt_history: list[dict[str, Any]] | None = None,
    idempotency_key: str = "",
    evidence_gate: str = "not_applicable",
) -> dict[str, Any]:
    attempts = list(attempt_history or [])
    if not attempts:
        attempts = [
            {
                "attempt": 1,
                "status": status,
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": max(0, int(duration_ms)),
            }
        ]
    return {
        "run_id": f"{plan.get('plan_id')}:{task.get('task_id')}",
        "plan_id": plan.get("plan_id"),
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type"),
        "label": task.get("label"),
        "agent": task.get("agent"),
        "phase": task.get("phase"),
        "status": status,
        "started_at": started_at,
        "completed_at": utc_now(),
        "duration_ms": max(0, int(duration_ms)),
        "input_summary": input_summary[:240],
        "output_summary": output_summary[:500],
        "model": model,
        "idempotency_key": idempotency_key or task_idempotency_key(plan, task),
        "attempt_count": len(attempts) or 1,
        "attempt_history": attempts,
        "execution_policy": dict(task.get("execution_policy") or {}),
        "evidence_gate": evidence_gate,
        "sensitive_input": "redacted_or_structured",
    }


def task_result(
    plan: dict[str, Any],
    task: dict[str, Any],
    payload: dict[str, Any],
    *,
    evidence_gate: str = "not_applicable",
) -> dict[str, Any]:
    return {
        "plan_id": plan.get("plan_id"),
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type"),
        "input_refs": list(task.get("input_refs") or []),
        "idempotency_key": task_idempotency_key(plan, task),
        "evidence_gate": evidence_gate,
        "payload": payload,
    }


def existing_task_result(
    state: WorkflowState, plan: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any] | None:
    key = task_idempotency_key(plan, task)
    return next(
        (
            dict(item)
            for item in state.get("agent_task_results") or []
            if item.get("idempotency_key") == key
        ),
        None,
    )


def cached_task_update(
    state: WorkflowState,
    plan: dict[str, Any],
    task: dict[str, Any],
    executions: TaskExecutionStore,
) -> dict[str, Any] | None:
    rerun = dict(state.get("agent_rerun_context") or {})
    if (
        rerun.get("plan_id") == plan.get("plan_id")
        and rerun.get("task_id") == task.get("task_id")
    ):
        return None
    cached = existing_task_result(state, plan, task)
    source = "checkpoint"
    if cached is None:
        stored = executions.load(
            state.get("case_id"), plan.get("plan_id"), task_idempotency_key(plan, task)
        )
        cached = dict((stored or {}).get("result") or {}) or None
        source = "execution_store"
    if cached is None:
        return None
    return {
        "agent_task_results": [] if source == "checkpoint" else [cached],
        "agent_runs": [
            agent_run(
                plan,
                task,
                started_at=utc_now(),
                duration_ms=0,
                input_summary=f"命中{source}稳定幂等键",
                output_summary="复用已完成任务结果，未重复执行",
                status="reused",
                model="idempotency-cache",
                evidence_gate=str(cached.get("evidence_gate") or "not_applicable"),
            )
        ],
        "trace": [
            trace(
                f"agent.{task.get('agent')}.task_reused",
                f"{task.get('label')}已按幂等键复用。",
                plan_id=plan.get("plan_id"),
                task_id=task.get("task_id"),
                source=source,
            )
        ],
    }


def approval_record(
    stage: str,
    status: str,
    *,
    actor: dict[str, Any] | None = None,
    comment: str = "",
    oa_evidence_id: str = "",
) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": status,
        "actor": dict(actor or {}),
        "comment": comment,
        "oa_evidence_id": oa_evidence_id,
        "acted_at": utc_now(),
    }


def upsert_approval_record(
    chain: list[dict[str, Any]], record: dict[str, Any]
) -> list[dict[str, Any]]:
    stage = str(record.get("stage") or "")
    result: list[dict[str, Any]] = []
    replaced = False
    for item in chain:
        if str(item.get("stage") or "") == stage:
            if not replaced:
                result.append(record)
                replaced = True
            continue
        result.append(item)
    if not replaced:
        result.append(record)
    return result


def approval_terms(
    payload: dict[str, Any],
    *,
    default_scope: str,
    default_validity_days: int = 30,
) -> dict[str, Any]:
    scope = str(payload.get("approval_scope") or default_scope).strip()
    validity_days = int(payload.get("validity_days") or default_validity_days)
    if not scope:
        raise ValueError("批准范围不能为空。")
    if validity_days <= 0 or validity_days > 3650:
        raise ValueError("批准有效期必须在1至3650天之间。")
    now = datetime.now(timezone.utc)
    return {
        "approval_scope": scope,
        "validity_days": validity_days,
        "effective_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=validity_days)).isoformat(
            timespec="seconds"
        ),
        "oa_evidence_id": str(payload.get("oa_evidence_id") or ""),
    }


class WorkflowNodes:
    """Dependency container: graph nodes stay small and domain engines stay reusable."""

    def __init__(
        self,
        *,
        repository: CaseRepository,
        vault_dir: str | Path,
        inbox_dir: str | Path,
        output_dir: str | Path,
        archive_dir: str | Path = "data/archive",
        execution_dir: str | Path = "data/executions",
        integrations: IntegrationBundle | None = None,
        policy: dict[str, Any] | None = None,
        ai_assistant: ContractAIAssistant | None = None,
        case_planner: CasePlanningAssistant | None = None,
        document_enhancer: DocumentTextEnhancer | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.vault_dir = Path(vault_dir)
        self.inbox_dir = Path(inbox_dir).resolve()
        self.output_dir = Path(output_dir)
        self.archive = CaseDocumentArchive(archive_dir)
        self.executions = TaskExecutionStore(execution_dir)
        self.integrations = integrations or IntegrationBundle.from_environment()
        self.documents = DocumentExtractor()
        self.document_quality = DocumentQualityGate()
        self.credit_facts = CreditFactExtractor()
        self.credit_engine = CreditScoringEngine(policy)
        self.contract_facts = ContractFactExtractor()
        self.contract_engine = ContractReviewEngine(self.credit_engine.policy)
        self.contract_ai = ai_assistant or ContractAIAssistant()
        self.case_planner = case_planner or CasePlanningAssistant()
        self.document_enhancer = document_enhancer or DocumentTextEnhancer()
        self.tool_registry = tool_registry or register_default_tools(
            LocalToolRegistry(),
            documents=self.documents,
            credit_facts=self.credit_facts,
            credit_engine=self.credit_engine,
            contract_facts=self.contract_facts,
            contract_engine=self.contract_engine,
            contract_ai=self.contract_ai,
            document_quality=self.document_quality,
            document_enhancer=self.document_enhancer,
        )
        self.reporter = AuditReporter()

    def _call_tool(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        context: InvocationContext,
        records: list[dict[str, Any]],
        fallback: Any,
    ) -> Any:
        registry = getattr(self, "tool_registry", None)
        if registry is None:
            return fallback()
        try:
            invocation = registry.invoke(tool_name, payload, context=context)
        except Exception as exc:
            record = getattr(exc, "registry_record", None)
            if isinstance(record, dict):
                records.append(record)
            raise
        records.append(invocation.record)
        return invocation.output

    @staticmethod
    def _document_from_tool(payload: dict[str, Any]) -> ExtractedDocument:
        return ExtractedDocument(
            path=str(payload.get("path") or ""),
            media_type=str(payload.get("media_type") or ""),
            text=str(payload.get("text") or ""),
            extractor=str(payload.get("extractor") or ""),
            warnings=list(payload.get("warnings") or []),
            fragments=[
                DocumentFragment(
                    fragment_id=str(item.get("fragment_id") or ""),
                    text=str(item.get("text") or ""),
                    location=dict(item.get("location") or {}),
                )
                for item in payload.get("fragments") or []
            ],
        )

    @staticmethod
    def _text_model_enhancement_enabled() -> bool:
        return str(
            os.getenv("DONGJIANG_DOCUMENT_TEXT_ENHANCEMENT_ENABLED", "false")
        ).lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _apply_credit_text_enhancement(
        profile: Any,
        enhancement: dict[str, Any],
        *,
        source_name: str,
    ) -> Any:
        if enhancement.get("status") != "succeeded":
            return profile
        if (enhancement.get("verification") or {}).get("status") != "passed":
            return profile
        allowed = {
            "asset_liability_ratio",
            "net_margin",
            "current_ratio",
            "revenue_growth",
            "years_in_business",
            "external_rating",
            "rating_outlook",
            "cooperation_years",
            "overdue_count_12m",
            "max_overdue_days_12m",
            "on_time_payment_rate",
            "current_overdue_days",
            "last_order_date",
            "major_litigation",
            "tax_or_enforcement_alert",
        }
        updates: dict[str, Any] = {}
        evidence = list(profile.evidence)
        for candidate in enhancement.get("candidates") or []:
            field = str(candidate.get("field") or "")
            confidence = float(candidate.get("confidence") or 0)
            if field not in allowed or confidence < 0.8:
                continue
            current = getattr(profile, field, None)
            if current not in {None, ""} and not (
                isinstance(current, bool) and current is False
            ):
                continue
            updates[field] = candidate.get("value")
            evidence.append(
                Evidence(
                    source=f"text_model_enhancement:{source_name}",
                    field=field,
                    value=candidate.get("value"),
                    confidence=confidence,
                    excerpt=str(candidate.get("evidence_query") or ""),
                    document_id=str(candidate.get("document_id") or ""),
                    fragment_id=str(candidate.get("fragment_id") or ""),
                    location=dict(candidate.get("location") or {}),
                )
            )
        if not updates:
            return profile
        from dataclasses import replace

        return replace(profile, **updates, evidence=evidence)

    @staticmethod
    def _apply_contract_text_enhancement(
        facts: Any,
        enhancement: dict[str, Any],
    ) -> None:
        if enhancement.get("status") != "succeeded":
            return
        if (enhancement.get("verification") or {}).get("status") != "passed":
            return
        allowed = {
            "payment_term_days",
            "tail_payment_ratio",
            "tail_payment_term_days",
            "uses_purchase_exemption",
            "contract_term_years",
            "max_penalty_ratio",
            "language",
            "has_parties",
            "has_subject",
            "has_payment",
            "has_breach",
            "has_ip",
            "has_confidentiality",
            "has_termination",
            "has_dispute_resolution",
        }
        for candidate in enhancement.get("candidates") or []:
            field = str(candidate.get("field") or "")
            confidence = float(candidate.get("confidence") or 0)
            if field not in allowed or confidence < 0.8:
                continue
            current = getattr(facts, field, None)
            if current not in {None, "", False}:
                continue
            setattr(facts, field, candidate.get("value"))
            facts.evidence.append(
                Evidence(
                    source="text_model_enhancement",
                    field=field,
                    value=candidate.get("value"),
                    confidence=confidence,
                    excerpt=str(candidate.get("evidence_query") or ""),
                    document_id=str(candidate.get("document_id") or facts.document_id),
                    fragment_id=str(candidate.get("fragment_id") or ""),
                    location=dict(candidate.get("location") or {}),
                )
            )

    def _runtime_snapshot(self) -> dict[str, Any]:
        contract_policy = dict(self.contract_engine.contract_policy or {})
        return {
            "credit_policy_version": str(self.credit_engine.policy.get("version") or ""),
            "credit_policy_hash": canonical_hash(self.credit_engine.policy),
            "contract_policy_version": str(contract_policy.get("version") or ""),
            "contract_policy_hash": canonical_hash(contract_policy),
            "model": str(self.contract_ai.gateway.model or ""),
            "ai_enabled": bool(self.contract_ai.enabled),
            "prompt_version": str(self.contract_ai.prompt_version),
            "prompt_hash": canonical_hash(
                self.contract_ai._instruction(
                    contract_facts_from_dict({"contract_name": "snapshot"})
                )
            ),
            "tool_registry_provider": str(
                getattr(self.tool_registry, "provider", "unknown")
            ),
            "available_tools": [
                item.get("tool_name")
                for item in self.tool_registry.discover()
            ],
        }

    @staticmethod
    def orchestration_agent_dispatch_run(
        plan: dict[str, Any],
        task_type: str,
        invocation: dict[str, Any],
    ) -> dict[str, Any]:
        return orchestration_run(
            plan,
            task_type,
            status=(
                "completed"
                if invocation.get("status") not in {"failed", "running"}
                else str(invocation.get("status"))
            ),
            output_summary=(
                f"通过{invocation.get('provider') or 'local'}注册中心调用"
                f"{invocation.get('agent_name') or invocation.get('agent_id')}"
            ),
            duration_ms=int(invocation.get("duration_ms") or 0),
        )

    @staticmethod
    def _is_contract(document: ExtractedDocument) -> bool:
        name = Path(document.path).name.lower()
        if any(token in name for token in (
            "合同", "协议", "订单", "采购单", "contract", "agreement",
            "purchase order", "sales order", "po",
        )):
            return True
        signals = (
            "甲方", "乙方", "买方", "卖方", "采购方", "供应商", "订单",
            "付款", "违约责任", "争议解决", "payment terms", "buyer",
            "seller", "supplier", "purchase order", "party a", "party b",
        )
        return sum(signal.lower() in document.text.lower() for signal in signals) >= 2

    def create_case(self, state: WorkflowState) -> dict[str, Any]:
        runtime_snapshot = self._runtime_snapshot()
        snapshot = case_plan_snapshot(
            customer=dict(state.get("customer") or {}),
            credit_file_count=len(state.get("pending_files") or []),
            use_cached_credit=bool(state.get("use_cached_credit", True)),
            runtime_snapshot=runtime_snapshot,
        )
        proposal = self.case_planner.propose(
            snapshot,
            allowed_task_types=allowed_case_task_types(snapshot),
            required_task_types=required_case_task_types(snapshot),
        )
        plan = build_case_plan(
            str(state["case_id"]),
            snapshot,
            proposal=proposal,
            runtime_snapshot=runtime_snapshot,
        )
        return {
            "stage": "intake",
            "status": "processing",
            "orchestration_plan": plan,
            "orchestration_runs": [
                orchestration_run(
                    plan,
                    "case_intake",
                    status="completed",
                    output_summary="案件身份、申请人和基础目标已登记",
                )
            ],
            "approval_chain": [
                approval_record(
                    "applicant",
                    "submitted",
                    actor=dict(state.get("actor") or {}),
                )
            ],
            "trace": [
                trace("workflow.started", "Harness 已创建案件并启动 LangGraph。"),
                trace(
                    "agent.plan",
                    "主 Agent 已生成并冻结案件级动态任务计划。",
                    plan_id=plan["plan_id"],
                    task_count=len(plan["tasks"]),
                    planner=plan["planner"],
                    planner_assistance=plan["planner_assistance"],
                ),
            ],
        }

    def _ingest_documents(
        self,
        state: WorkflowState,
        *,
        document_kind: Literal["credit", "contract"],
    ) -> dict[str, Any]:
        customer = profile_from_dict(dict(state["customer"]))
        existing_contracts = [
            contract_facts_from_dict(item)
            for item in state.get("contract_facts") or []
        ]
        source_files = list(state.get("source_files") or [])
        source_documents = list(state.get("source_documents") or [])
        credit_source_files = list(state.get("credit_source_files") or [])
        contract_source_files = list(state.get("contract_source_files") or [])
        traces: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        vault = RedactionVault(str(state["case_id"]), self.vault_dir)
        actor = dict(state.get("actor") or {})

        for raw_path in state.get("pending_files") or []:
            path = Path(raw_path)
            archive_index: int | None = None
            try:
                archived = self.archive.archive(
                    str(state["case_id"]),
                    path,
                    document_kind=document_kind,
                    actor_id=str(actor.get("actor_id") or ""),
                    source_system=str(actor.get("source_system") or state.get("source_system") or ""),
                )
                source_documents.append({**archived, "parse_status": "pending"})
                archive_index = len(source_documents) - 1
                extracted = self._call_tool(
                    "document.extract",
                    {"path": archived["archived_path"]},
                    context=InvocationContext(
                        case_id=str(state["case_id"]),
                        caller="case_orchestrator",
                        agent_id="case_orchestrator",
                        input_summary=f"案件原件{archived['document_id']}的归档路径",
                    ),
                    records=tool_calls,
                    fallback=lambda: document_payload(
                        self.documents.extract(archived["archived_path"])
                    ),
                )
                document = self._document_from_tool(dict(extracted))
                source_files.append(document.path)
                redacted_fragments = [
                    {
                        "fragment_id": fragment.fragment_id,
                        "text": vault.redact(fragment.text),
                        "location": fragment.location,
                    }
                    for fragment in document.fragments
                ]
                quality_payload = self._call_tool(
                    "document.quality_gate",
                    {
                        "document_kind": document_kind,
                        "document": document_payload(document),
                    },
                    context=InvocationContext(
                        case_id=str(state["case_id"]),
                        caller="case_orchestrator",
                        agent_id="case_orchestrator",
                        input_summary=f"文档{archived['document_id']}的本地解析指标",
                    ),
                    records=tool_calls,
                    fallback=lambda: self.document_quality.evaluate(
                        document, document_kind=document_kind
                    ),
                )
                document_record = {
                        "parse_status": "parsed",
                        "media_type": document.media_type,
                        "extractor": document.extractor,
                        "warnings": document.warnings,
                        "quality": dict(quality_payload),
                        "features": {
                            "ocr_page_count": sum(
                                bool(fragment.location.get("ocr"))
                                and fragment.location.get("kind") == "page"
                                for fragment in document.fragments
                            ),
                            "ocr_image_count": sum(
                                fragment.location.get("kind") == "image"
                                and bool(fragment.location.get("ocr"))
                                for fragment in document.fragments
                            ),
                            "word_table_cell_count": sum(
                                fragment.location.get("kind") == "word_table_cell"
                                for fragment in document.fragments
                            ),
                        },
                        "fragments": redacted_fragments,
                }
                enhancement: dict[str, Any] = {
                    "status": "not_needed",
                    "mode": "text_only",
                    "candidates": [],
                    "summary": "本地解析质量达到门槛，无需文本模型增强。",
                    "limitation": "文本模型不能读取图片像素或空白扫描页。",
                }
                quality_status = str(quality_payload.get("status") or "")
                if quality_status == "needs_text_enhancement":
                    if self._text_model_enhancement_enabled():
                        enhancement = dict(
                            self._call_tool(
                                "document.text_enhance",
                                {
                                    "document_kind": document_kind,
                                    "document": {
                                        "document_id": archived["document_id"],
                                        "path": document.path,
                                        "media_type": document.media_type,
                                        "fragments": redacted_fragments,
                                    },
                                },
                                context=InvocationContext(
                                    case_id=str(state["case_id"]),
                                    caller="case_orchestrator",
                                    agent_id="case_orchestrator",
                                    input_summary=(
                                        f"文档{archived['document_id']}已脱敏的本地文字片段"
                                    ),
                                ),
                                records=tool_calls,
                                fallback=lambda: self.document_enhancer.enhance(
                                    document_kind,
                                    document={
                                        "document_id": archived["document_id"],
                                        "fragments": redacted_fragments,
                                    },
                                ),
                            )
                        )
                    else:
                        enhancement = {
                            "status": "disabled",
                            "mode": "text_only",
                            "model": self.document_enhancer.model,
                            "candidates": [],
                            "summary": "文档质量需要增强，但自动文本模型增强未启用。",
                            "limitation": "文本模型不能读取图片像素或空白扫描页。",
                        }
                elif quality_status == "manual_required":
                    enhancement = {
                        "status": "not_applicable",
                        "mode": "text_only",
                        "model": self.document_enhancer.model,
                        "candidates": [],
                        "summary": "本地解析与OCR未形成足够文字，文本模型无法继续处理，必须人工检查原件。",
                        "limitation": "文本模型不能读取图片像素或空白扫描页。",
                    }
                document_record["text_enhancement"] = enhancement
                document_record["parse_status"] = (
                    "manual_required" if quality_status == "manual_required" else "parsed"
                )
                source_documents[archive_index or 0].update(document_record)
                if quality_status == "manual_required":
                    raise ValueError(
                        "文档未形成可用文字；项目仅提供文本模型，无法读取图片像素，请人工检查或重新上传清晰可复制版本。"
                    )
                is_contract = self._is_contract(document)
                if document_kind == "credit" and is_contract:
                    raise ValueError("信用资料入口不接受合同文件。")
                if document_kind == "contract" and not is_contract:
                    raise ValueError("合同入口只接受合同或协议文件。")
                if document_kind == "credit":
                    credit_source_files.append(document.path)
                else:
                    contract_source_files.append(document.path)
                traces.append(
                    trace(
                        "document.extracted",
                        f"已解析 {path.name}",
                        media_type=document.media_type,
                        extractor=document.extractor,
                        warnings=document.warnings,
                        quality=quality_status,
                        text_enhancement=enhancement.get("status"),
                        sha256=archived["sha256"],
                    )
                )
                if document_kind == "contract":
                    redacted = vault.redact(document.text)
                    facts_payload = self._call_tool(
                        "contract.extract_facts",
                        {
                            "text": document.text,
                            "contract_name": path.name,
                            "customer_name": customer.customer_name,
                            "business_type": customer.business_type,
                        },
                        context=InvocationContext(
                            case_id=str(state["case_id"]),
                            caller="case_orchestrator",
                            agent_id="case_orchestrator",
                            input_summary=f"合同{archived['document_id']}的本地解析正文",
                        ),
                        records=tool_calls,
                        fallback=lambda: checkpoint_dict(
                            self.contract_facts.extract(
                                document.text,
                                contract_name=path.name,
                                customer_name=customer.customer_name,
                                business_type=customer.business_type,
                            )
                        ),
                    )
                    facts = contract_facts_from_dict(dict(facts_payload))
                    facts.document_id = str(archived["document_id"])
                    self._apply_contract_text_enhancement(facts, enhancement)
                    raw_fragments = [
                        {
                            "fragment_id": fragment.fragment_id,
                            "text": fragment.text,
                            "location": fragment.location,
                        }
                        for fragment in document.fragments
                    ]
                    for evidence in facts.evidence:
                        matched = locate_excerpt(evidence.excerpt, raw_fragments)
                        evidence.document_id = facts.document_id
                        if matched:
                            evidence.fragment_id = str(matched["fragment_id"])
                            evidence.location = dict(matched["location"])
                    # Checkpoints只保留脱敏后的条款文本；金额等事实已在本地脱敏前提取。
                    facts.raw_text = redacted
                    for evidence in facts.evidence:
                        evidence.excerpt = vault.redact(evidence.excerpt)
                    existing_contracts.append(facts)
                    traces.append(
                        trace(
                            "privacy.redacted",
                            f"{path.name} 已脱敏后进入工作流状态。",
                            token_count=len(vault.mapping),
                        )
                    )
                else:
                    profile_payload = self._call_tool(
                        "credit.extract_facts",
                        {
                            "profile": checkpoint_dict(customer),
                            "text": document.text,
                            "source_name": path.name,
                        },
                        context=InvocationContext(
                            case_id=str(state["case_id"]),
                            caller="case_orchestrator",
                            agent_id="case_orchestrator",
                            input_summary=f"信用资料{archived['document_id']}的本地解析正文",
                        ),
                        records=tool_calls,
                        fallback=lambda: checkpoint_dict(
                            self.credit_facts.enrich(customer, document.text, path.name)
                        ),
                    )
                    customer = profile_from_dict(dict(profile_payload))
                    customer = self._apply_credit_text_enhancement(
                        customer, enhancement, source_name=path.name
                    )
            except Exception as exc:
                if archive_index is not None:
                    current_status = str(
                        source_documents[archive_index].get("parse_status") or ""
                    )
                    source_documents[archive_index].update({"error": str(exc)})
                    if current_status != "manual_required":
                        source_documents[archive_index]["parse_status"] = "failed"
                errors.append(
                    trace("document.failed", f"{path.name} 解析失败。", error=str(exc))
                )
            finally:
                try:
                    resolved = path.resolve()
                    if self.inbox_dir in resolved.parents and resolved.is_file():
                        resolved.unlink()
                except OSError:
                    pass

        vault_path = vault.persist_local()
        if vault_path:
            traces.append(
                trace(
                    "privacy.vault_saved",
                    "可逆脱敏映射仅保存于本地隔离目录。",
                    path=str(vault_path),
                )
            )
        return {
            "stage": f"{document_kind}_documents_ingested",
            "customer": checkpoint_dict(customer),
            "contract_facts": [checkpoint_dict(item) for item in existing_contracts],
            "source_files": source_files,
            "source_documents": source_documents,
            "credit_source_files": credit_source_files,
            "contract_source_files": contract_source_files,
            "pending_files": [],
            "pending_document_kind": None,
            "tool_calls": tool_calls,
            "trace": traces,
            "errors": errors,
        }

    def ingest_credit_documents(self, state: WorkflowState) -> dict[str, Any]:
        update = self._ingest_documents(state, document_kind="credit")
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            parsed = sum(
                item.get("document_kind") == "credit"
                and item.get("parse_status") == "parsed"
                for item in update.get("source_documents") or []
            )
            runs = [
                orchestration_run(
                    plan,
                    "credit_document_processing",
                    status="completed",
                    output_summary=f"信用资料处理完成，成功解析{parsed}份",
                )
            ]
            if case_plan_has_task(plan, "document_quality_supervision"):
                documents = [
                    item
                    for item in update.get("source_documents") or []
                    if item.get("document_kind") == "credit"
                ]
                runs.append(
                    orchestration_run(
                        plan,
                        "document_quality_supervision",
                        status="completed",
                        output_summary=(
                            f"质量监督完成：{len(documents)}份资料，"
                            f"{sum(item.get('parse_status') == 'failed' for item in documents)}份失败，"
                            f"{sum(bool(item.get('warnings')) for item in documents)}份有警告"
                        ),
                    )
                )
            update["orchestration_runs"] = runs
        return update

    def ingest_contract_documents(self, state: WorkflowState) -> dict[str, Any]:
        if (
            state.get("credit_status") != "effective"
            or not state.get("effective_credit_assessment")
        ):
            raise PermissionError("正式授信尚未生效，不能解析或评审合同。")
        update = self._ingest_documents(state, document_kind="contract")
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            parsed = sum(
                item.get("document_kind") == "contract"
                and item.get("parse_status") == "parsed"
                for item in update.get("source_documents") or []
            )
            update["orchestration_runs"] = [
                orchestration_run(
                    plan,
                    "contract_document_processing",
                    status="completed",
                    output_summary=f"合同资料处理完成，成功解析{parsed}份",
                )
            ]
        return update

    def check_credit_cache(self, state: WorkflowState) -> dict[str, Any]:
        customer = profile_from_dict(dict(state["customer"]))
        update = {
            "stage": "credit_required",
            "credit_source": "new_assessment",
            "credit_assessment": None,
            "effective_credit_assessment": None,
            "credit_status": "calculating",
            "credit_approval": {},
            "waiting_for": None,
            "trace": [
                trace(
                    "credit.fresh_approval_required",
                    "新案件必须重新完成信用评估并由信用审批人批准，历史授信仅作参考。",
                )
            ],
        }
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            runs = [
                orchestration_run(
                    plan,
                    "credit_cache_check",
                    status="completed",
                    output_summary="本案强制重新评估并进入信用人工审批",
                )
            ]
            if case_plan_has_task(plan, "enterprise_identity_supervision"):
                runs.append(
                    orchestration_run(
                        plan,
                        "enterprise_identity_supervision",
                        status="completed",
                        output_summary=(
                            "已识别唯一客户标识，历史授信仅作为参考"
                            if customer.crm_customer_id
                            or customer.unified_social_credit_code
                            else "缺少唯一客户标识，本案仍独立完成信用审批"
                        ),
                    )
                )
            update["orchestration_runs"] = runs
        return update

    def plan_credit_workflow(self, state: WorkflowState) -> dict[str, Any]:
        plan = build_credit_plan(
            str(state["case_id"]),
            dict(state.get("customer") or {}),
            list(state.get("source_documents") or []),
            runtime_snapshot=self._runtime_snapshot(),
        )
        return {
            "stage": "credit_planned",
            "active_workflow_plan": plan,
            "workflow_plans": [plan],
            "trace": [
                trace(
                    "agent.credit.plan_created",
                    "信用信审子 Agent 已按资料情况生成受控任务计划。",
                    plan_id=plan["plan_id"],
                    task_count=len(plan["tasks"]),
                    task_types=[item["task_type"] for item in plan["tasks"]],
                    spec_hash=plan["spec_hash"],
                    runtime_snapshot=plan["runtime_snapshot"],
                )
            ],
        }

    def run_credit_analysis(self, state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("active_workflow_plan") or {})
        task = dict(state.get("active_agent_task") or {})
        assert_plan_integrity(plan)
        reused = cached_task_update(state, plan, task, self.executions)
        if reused:
            return reused
        task_type = str(task.get("task_type") or "")
        profile = profile_from_dict(dict(state["customer"]))
        started_at = utc_now()
        started = perf_counter()
        tool_calls: list[dict[str, Any]] = []
        tool_output = self._call_tool(
            "credit.analyze_dimension",
            {"task_type": task_type, "profile": checkpoint_dict(profile)},
            context=InvocationContext(
                case_id=str(state["case_id"]),
                caller="credit_review",
                agent_id="credit_review",
                plan_id=str(plan.get("plan_id") or ""),
                task_id=str(task.get("task_id") or ""),
                input_summary=f"信用动态任务{task_type}的结构化客户档案",
            ),
            records=tool_calls,
            fallback=lambda: credit_dimension_result(
                self.credit_engine, task_type, profile
            ),
        )
        payload = dict(tool_output.get("payload") or {})
        summary = str(tool_output.get("summary") or "信用维度分析已完成")
        executor = str(tool_output.get("executor") or "credit-analysis-tool")
        duration_ms = round((perf_counter() - started) * 1000)
        result = task_result(plan, task, payload)
        run = agent_run(
                    plan,
                    task,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    input_summary=(
                        f"{len(task.get('input_refs') or [])}个资料引用；敏感内容未写入日志"
                    ),
                    output_summary=summary,
                    model=executor,
                )
        if not state.get("agent_rerun_context"):
            self.executions.save(
                state["case_id"],
                plan["plan_id"],
                result["idempotency_key"],
                result=result,
                run=run,
            )
        return {
            "agent_task_results": [result],
            "agent_runs": [run],
            "tool_calls": tool_calls,
            "trace": [
                trace(
                    "agent.credit.task_completed",
                    f"{task.get('label')}已完成。",
                    plan_id=plan.get("plan_id"),
                    task_id=task.get("task_id"),
                    duration_ms=duration_ms,
                )
            ],
        }

    def synthesize_credit_analysis(self, state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("active_workflow_plan") or {})
        task = plan_task(plan, "credit_synthesis")
        results = [
            item
            for item in state.get("agent_task_results") or []
            if item.get("plan_id") == plan.get("plan_id")
        ]
        started_at = utc_now()
        started = perf_counter()
        analysis = {
            str(item.get("task_type")): dict(item.get("payload") or {})
            for item in results
        }
        completed = sorted(analysis)
        duration_ms = round((perf_counter() - started) * 1000)
        summary = f"已汇总{len(completed)}项动态信用分析"
        return {
            "credit_analysis": analysis,
            "agent_task_results": [task_result(plan, task, {"completed_tasks": completed})],
            "agent_runs": [
                agent_run(
                    plan,
                    task,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    input_summary=f"{len(results)}项并行分析结果",
                    output_summary=summary,
                )
            ],
            "trace": [
                trace(
                    "agent.credit.synthesized",
                    "信用信审子 Agent 已汇总并行分析结果。",
                    plan_id=plan.get("plan_id"),
                    completed_tasks=completed,
                )
            ],
        }

    def score_credit(self, state: WorkflowState) -> dict[str, Any]:
        customer = profile_from_dict(dict(state["customer"]))
        plan = dict(state.get("active_workflow_plan") or {})
        task = plan_task(plan, "credit_scoring")
        started_at = utc_now()
        started = perf_counter()
        assessment = self.credit_engine.assess(customer)
        duration_ms = round((perf_counter() - started) * 1000)
        traces = [
            trace(
                "credit.completed",
                "信审子图完成评分、额度和账期测算。",
                score=assessment.score,
                risk_level=assessment.risk_level.value,
                approved_credit_limit=assessment.approved_credit_limit,
                recommended_term_days=assessment.recommended_term_days,
            )
        ]
        if assessment.rating_resolution.get("conflict"):
            traces.append(
                trace(
                    "credit.rating_conflict",
                    "多机构评级不一致，已采用保守结果。",
                    selected=assessment.rating_resolution.get("selected"),
                    requires_manual_review=assessment.rating_resolution.get("requires_manual_review"),
                )
            )
        if assessment.requires_supplement:
            traces.append(
                trace(
                    "credit.insufficient_data",
                    "信用资料覆盖不足，必须补件后重新评估。",
                    data_coverage_ratio=assessment.data_coverage_ratio,
                    supplement_reasons=assessment.supplement_reasons,
                )
            )
        return {
            "stage": "credit_calculated",
            "status": (
                "credit_supplement_required"
                if assessment.requires_supplement
                else "credit_calculated"
            ),
            "credit_source": "new_assessment",
            "credit_assessment": checkpoint_dict(assessment),
            "effective_credit_assessment": None,
            "credit_status": (
                "supplement_required"
                if assessment.requires_supplement
                else "calculated"
            ),
            "waiting_for": (
                "credit_supplement" if assessment.requires_supplement else None
            ),
            "agent_task_results": [
                task_result(
                    plan,
                    task,
                    {
                        "score": assessment.score,
                        "risk_level": assessment.risk_level.value,
                        "requires_supplement": assessment.requires_supplement,
                        "policy_version": assessment.policy_version,
                    },
                )
            ],
            "agent_runs": [
                agent_run(
                    plan,
                    task,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    input_summary=f"{len(state.get('credit_analysis') or {})}项分析摘要",
                    output_summary=(
                        f"评分{assessment.score:.1f}，风险{assessment.risk_level.value}，"
                        f"资料补充={'是' if assessment.requires_supplement else '否'}"
                    ),
                    model="credit-policy-engine",
                )
            ],
            "trace": traces,
        }

    def verify_credit(self, state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("active_workflow_plan") or {})
        task = plan_task(plan, "credit_verification")
        assessment = dict(state.get("credit_assessment") or {})
        analysis = dict(state.get("credit_analysis") or {})
        started_at = utc_now()
        started = perf_counter()
        checks = {
            "policy_version_present": bool(assessment.get("policy_version")),
            "score_in_range": 0 <= float(assessment.get("score") or 0) <= 100,
            "risk_level_valid": assessment.get("risk_level") in {"low", "medium", "high"},
            "analysis_present": bool(analysis),
            "supplement_consistent": bool(assessment.get("requires_supplement"))
            == bool(assessment.get("supplement_reasons")),
        }
        verification = {
            "status": "passed" if all(checks.values()) else "failed",
            "checks": checks,
            "verified_at": utc_now(),
            "verifier": "independent_credit_guard",
        }
        duration_ms = round((perf_counter() - started) * 1000)
        verification_result = task_result(plan, task, verification)
        verification_run = agent_run(
            plan,
            task,
            started_at=started_at,
            duration_ms=duration_ms,
            input_summary="信用评分与动态分析摘要",
            output_summary=f"独立核验{verification['status']}，{sum(checks.values())}/{len(checks)}项通过",
            status="completed" if verification["status"] == "passed" else "failed",
            model="independent-credit-guard",
        )
        execution_audit = audit_plan_execution(
            plan,
            list(state.get("agent_runs") or []) + [verification_run],
            list(state.get("agent_task_results") or []) + [verification_result],
        )
        if execution_audit["status"] != "conformant":
            verification["status"] = "failed"
            verification["execution_audit"] = "non_conformant"
        if verification["status"] == "failed":
            assessment["requires_supplement"] = True
            reasons = list(assessment.get("supplement_reasons") or [])
            reasons.append("信用结论一致性核验未通过，必须补充资料或人工排查。")
            assessment["supplement_reasons"] = list(dict.fromkeys(reasons))
            assessment["missing_fields"] = list(
                dict.fromkeys(list(assessment.get("missing_fields") or []) + ["verification"])
            )
        plan["status"] = "completed" if verification["status"] == "passed" else "failed"
        plan["completed_at"] = utc_now()
        return {
            "stage": "credit_calculated",
            "active_workflow_plan": plan,
            "workflow_plans": [plan],
            "credit_assessment": assessment,
            "status": (
                state.get("status")
                if verification["status"] == "passed"
                else "credit_supplement_required"
            ),
            "credit_status": (
                state.get("credit_status")
                if verification["status"] == "passed"
                else "supplement_required"
            ),
            "waiting_for": (
                state.get("waiting_for")
                if verification["status"] == "passed"
                else "credit_supplement"
            ),
            "credit_verification": verification,
            "agent_task_results": [verification_result],
            "agent_runs": [verification_run],
            "execution_audits": [execution_audit],
            "trace": [
                trace(
                    "agent.credit.verified",
                    "信用结论已完成独立一致性核验。",
                    plan_id=plan.get("plan_id"),
                    status=verification["status"],
                    checks=checks,
                    execution_audit=execution_audit["status"],
                )
            ],
        }

    def prepare_credit_approval(self, state: WorkflowState) -> dict[str, Any]:
        if not state.get("credit_assessment"):
            raise ValueError("缺少模型信用计算结果，不能提交审批。")
        chain = list(state.get("approval_chain") or [])
        existing = {str(item.get("stage") or "") for item in chain}
        for stage in (
            "marketing_director",
            "credit_control",
            "senior_finance_manager",
            "group_finance_director",
        ):
            if stage not in existing:
                chain.append({"stage": stage, "status": "pending"})
        oa_submission = self.integrations.submit_oa(
            str(state["case_id"]),
            {
                "case_id": state["case_id"],
                "customer": dict(state.get("customer") or {}),
                "model_credit_assessment": dict(state.get("credit_assessment") or {}),
                "approval_chain": chain,
            },
        )
        update = {
            "stage": "credit_pending_approval",
            "status": "credit_pending_approval",
            "credit_status": "pending_approval",
            "waiting_for": "credit_approval",
            "approval_chain": chain,
            "oa_submission": oa_submission,
            "trace": [
                trace("credit.approval_requested", "模型信用结果已提交人工审批。")
            ],
        }
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            update["orchestration_runs"] = [
                orchestration_waiting_run(
                    plan,
                    "credit_human_approval",
                    output_summary="信用子 Agent 结果已提交，等待正式授信人工审批",
                )
            ]
        return update

    def await_credit_approval(
        self,
        state: WorkflowState,
    ) -> Command[
        Literal["activate_credit", "await_credit_supplement", "finalize"]
    ]:
        model_result = dict(state.get("credit_assessment") or {})
        response = interrupt(
            {
                "type": "credit_approval",
                "case_id": state["case_id"],
                "message": "请审核模型建议额度、账期和风险等级。",
                "model_result": model_result,
                "allowed_actions": [
                    "approve",
                    "adjust_and_approve",
                    "request_supplement",
                    "reject",
                ],
            }
        )
        payload = dict(response or {})
        candidate_adoption = dict(payload.pop("_candidate_adoption", {}) or {})
        candidate_update = dict(candidate_adoption.get("state_update") or {})
        candidate_trace = dict(candidate_adoption.get("trace") or {})
        if candidate_adoption:
            if candidate_adoption.get("agent") != "credit":
                raise ValueError("正式信用审批不能采纳非信用Agent候选。")
            model_result = dict(candidate_update.get("credit_assessment") or {})
            if not model_result:
                raise ValueError("信用候选缺少可采纳的结构化结果。")
        action = str(payload.get("action") or "")
        if action in {"approve", "adjust_and_approve"}:
            model_limit = float(model_result.get("approved_credit_limit") or 0)
            model_term = int(model_result.get("recommended_term_days") or 0)
            approved_limit = (
                float(payload.get("approved_credit_limit"))
                if action == "adjust_and_approve"
                and payload.get("approved_credit_limit") is not None
                else model_limit
            )
            approved_term = (
                int(payload.get("approved_term_days"))
                if action == "adjust_and_approve"
                and payload.get("approved_term_days") is not None
                else model_term
            )
            hard_term = int(model_result.get("hard_term_limit_days") or approved_term)
            if approved_limit < 0:
                raise ValueError("正式授信额度不能为负数。")
            if approved_term <= 0 or approved_term > hard_term:
                raise ValueError(f"正式账期必须在1至{hard_term}天之间。")
            if action == "adjust_and_approve" and not str(payload.get("comment") or "").strip():
                raise ValueError("调整模型建议时必须填写调整原因。")
            customer = dict(state.get("customer") or {})
            scope = str(payload.get("approval_scope") or "").strip()
            if not scope:
                scope = " / ".join(filter(None, (
                    str(customer.get("customer_name") or ""),
                    str(customer.get("business_type") or ""),
                    str(customer.get("project_name") or ""),
                )))
            validity_days = int(
                payload.get("validity_days")
                or (self.credit_engine.policy.get("credit_approval") or {}).get(
                    "validity_days", 180
                )
            )
            if validity_days <= 0 or validity_days > 3650:
                raise ValueError("信用批准有效期必须在1至3650天之间。")
            payload["approved_credit_limit"] = approved_limit
            payload["approved_term_days"] = approved_term
            payload["approval_scope"] = scope
            payload["validity_days"] = validity_days
            payload["oa_evidence_id"] = str(payload.get("oa_evidence_id") or "")
            payload["purchase_exemption_approved"] = bool(
                payload.get("purchase_exemption_approved")
            )
            supplied_chain = list(payload.get("approval_chain") or [])
            if supplied_chain:
                required = {
                    "marketing_director",
                    "credit_control",
                    "senior_finance_manager",
                    "group_finance_director",
                }
                approved_stages = {
                    str(item.get("stage") or "")
                    for item in supplied_chain
                    if str(item.get("status") or "") == "approved"
                }
                missing_stages = sorted(required - approved_stages)
                if missing_stages:
                    raise ValueError(
                        "OA审批链缺少已批准节点：" + "、".join(missing_stages)
                    )
            elif str((payload.get("actor") or {}).get("source_system") or "") == "web":
                now_text = utc_now()
                supplied_chain = [
                    item
                    if str(item.get("stage") or "") == "applicant"
                    else {
                        **item,
                        "status": "approved",
                        "acted_at": now_text,
                        "actor": dict(payload.get("actor") or {}),
                        "comment": "Web比赛演示合并审批",
                        "oa_evidence_id": payload["oa_evidence_id"],
                    }
                    for item in state.get("approval_chain") or []
                ]
            return Command(
                update={
                    **candidate_update,
                    "credit_approval_request": payload,
                    "human_decision": payload,
                    "waiting_for": None,
                    "approval_chain": (
                        supplied_chain
                        if supplied_chain
                        else upsert_approval_record(
                            list(state.get("approval_chain") or []),
                            approval_record(
                            "credit_control",
                            "approved",
                            actor=dict(payload.get("actor") or {}),
                            comment=str(payload.get("comment") or ""),
                            oa_evidence_id=payload["oa_evidence_id"],
                            ),
                        )
                    ),
                    "orchestration_runs": [
                        orchestration_run(
                            dict(state.get("orchestration_plan") or {}),
                            "credit_human_approval",
                            status="completed",
                            output_summary="信用审批人已完成正式授信审批",
                        )
                    ] if state.get("orchestration_plan") else [],
                    "trace": [candidate_trace] if candidate_trace else [],
                },
                goto="activate_credit",
            )
        if action == "request_supplement":
            return Command(
                update={
                    "status": "credit_supplement_required",
                    "credit_status": "supplement_required",
                    "human_decision": payload,
                    "waiting_for": "credit_supplement",
                    "trace": [
                        trace("credit.supplement_requested", "信审人员要求补充信用资料。")
                    ],
                },
                goto="await_credit_supplement",
            )
        if action == "reject":
            return Command(
                update={
                    "status": "credit_rejected",
                    "credit_status": "rejected",
                    "credit_approval": payload,
                    "human_decision": payload,
                    "waiting_for": None,
                    "trace": [trace("credit.rejected", "信用申请已被拒绝。")],
                },
                goto="finalize",
            )
        raise ValueError("不支持的信用审批操作。")

    def await_credit_supplement(
        self,
        state: WorkflowState,
    ) -> Command[Literal["ingest_credit_documents", "finalize"]]:
        response = interrupt(
            {
                "type": "credit_supplement",
                "case_id": state["case_id"],
                "message": "请补充财报、评级报告或历史合作资料。",
                "allowed_actions": ["submit_supplement", "close_case"],
            }
        )
        payload = dict(response or {})
        if str(payload.get("action") or "") == "close_case":
            return Command(
                update={
                    "status": "credit_rejected",
                    "credit_status": "rejected",
                    "human_decision": payload,
                    "waiting_for": None,
                    "trace": [trace("credit.closed", "申请人关闭了信用申请。")],
                },
                goto="finalize",
            )
        files = list(payload.get("file_paths") or [])
        if not files:
            raise ValueError("补充信用资料时必须上传文件。")
        return Command(
            update={
                "pending_files": files,
                "pending_document_kind": "credit",
                "use_cached_credit": False,
                "credit_assessment": None,
                "effective_credit_assessment": None,
                "credit_approval": {},
                "credit_approval_request": None,
                "credit_status": "collecting",
                "status": "processing",
                "waiting_for": None,
                "trace": [trace("credit.supplemented", "补充信用资料已提交。")],
            },
            goto="ingest_credit_documents",
        )

    def activate_credit(self, state: WorkflowState) -> dict[str, Any]:
        model_result = dict(state.get("credit_assessment") or {})
        approval = dict(state.get("credit_approval_request") or {})
        if not model_result or not approval:
            raise ValueError("缺少模型结果或审批决定，不能激活授信。")
        effective = dict(model_result)
        effective["approved_credit_limit"] = float(approval["approved_credit_limit"])
        effective["total_credit_limit"] = effective["approved_credit_limit"]
        effective["recommended_term_days"] = int(approval["approved_term_days"])
        effective["purchase_exemption_approved"] = bool(
            approval.get("purchase_exemption_approved")
        )
        customer = profile_from_dict(dict(state["customer"]))
        credit_control = self.credit_engine.credit_control(
            customer,
            effective["approved_credit_limit"],
        )
        effective.update(
            {
                "occupied_credit_amount": credit_control["occupied_credit_amount"],
                "available_credit_amount": credit_control["available_credit_amount"],
                "credit_locked": credit_control["credit_locked"],
                "credit_lock_reasons": credit_control["credit_lock_reasons"],
            }
        )
        now = datetime.now(timezone.utc)
        approval.update(
            {
                "approved_at": now.isoformat(timespec="seconds"),
                "effective_at": now.isoformat(timespec="seconds"),
                "expires_at": (
                    now + timedelta(days=int(approval.get("validity_days") or 180))
                ).isoformat(timespec="seconds"),
            }
        )
        locked = bool(credit_control["credit_locked"])
        customer_payload = dict(state.get("customer") or {})
        customer_id = str(
            customer_payload.get("crm_customer_id")
            or customer_payload.get("unified_social_credit_code")
            or ""
        )
        credit_writeback = self.integrations.write_back(
            str(state["case_id"]),
            customer_id,
            {
                "case_id": state["case_id"],
                "status": "credit_effective",
                "customer_status": customer_payload.get("customer_status") or "Active",
                "business_type": customer_payload.get("business_type"),
                "tkm_business_subtype": customer_payload.get("tkm_business_subtype"),
                "approved_total_credit_limit": effective.get("approved_credit_limit"),
                "approved_term_days": effective.get("recommended_term_days"),
                "purchase_exemption_approved": effective.get(
                    "purchase_exemption_approved", False
                ),
                "max_tail_payment_ratio": effective.get("max_tail_payment_ratio"),
                "max_tail_term_days": effective.get("max_tail_term_days"),
                "approval_scope": approval.get("approval_scope"),
                "effective_at": approval.get("effective_at"),
                "expires_at": approval.get("expires_at"),
                "oa_evidence_id": approval.get("oa_evidence_id"),
                "approval_chain": list(state.get("approval_chain") or []),
            },
            phase="credit_activation",
        )
        return {
            "stage": "credit_effective",
            "status": "credit_control_locked" if locked else "credit_effective",
            "credit_status": "effective",
            "effective_credit_assessment": effective,
            "credit_approval": approval,
            "credit_approval_request": None,
            "credit_control": credit_control,
            "writeback": {"credit_activation": credit_writeback},
            "waiting_for": "special_release" if locked else None,
            "orchestration_runs": (
                [
                    orchestration_run(
                        dict(state.get("orchestration_plan") or {}),
                        "credit_control_gate",
                        status="completed",
                        output_summary=(
                            "信用控制检查完成，需例外授权"
                            if locked
                            else "信用控制检查通过，可进入合同阶段"
                        ),
                    )
                ]
                + ([
                    orchestration_waiting_run(
                        dict(state.get("orchestration_plan") or {}),
                        "exception_authorization",
                        output_summary="额度占用或逾期触发门禁，等待授权审批人处理",
                    )
                ] if locked else [
                    orchestration_run(
                        dict(state.get("orchestration_plan") or {}),
                        "exception_authorization",
                        status="skipped",
                        output_summary="未触发信用例外授权",
                    )
                ])
            ) if state.get("orchestration_plan") else [],
            "trace": [
                trace(
                    "credit.effective",
                    "信用审批完成，正式授信已经生效。",
                    approved_credit_limit=effective["approved_credit_limit"],
                    approved_term_days=effective["recommended_term_days"],
                    occupied_credit_amount=credit_control["occupied_credit_amount"],
                    available_credit_amount=credit_control["available_credit_amount"],
                    credit_locked=credit_control["credit_locked"],
                    approval_scope=approval.get("approval_scope"),
                    purchase_exemption_approved=effective.get(
                        "purchase_exemption_approved"
                    ),
                )
            ],
        }

    def await_special_release(
        self,
        state: WorkflowState,
    ) -> Command[Literal["contract_gate", "finalize"]]:
        assessment = dict(state.get("effective_credit_assessment") or {})
        response = dict(interrupt(
            {
                "type": "special_release",
                "case_id": state["case_id"],
                "message": "客户存在超额占用或逾期超过30天，必须取得特别放行后继续。",
                "credit_control": dict(state.get("credit_control") or {}),
                "lock_reasons": list(assessment.get("credit_lock_reasons") or []),
                "allowed_actions": ["approve", "reject"],
            }
        ) or {})
        action = str(response.get("action") or "")
        if action == "approve":
            evidence = list(response.get("approval_evidence") or [])
            if not evidence:
                raise ValueError("特别放行必须上传市场总监批准邮件或OA附件。")
            terms = approval_terms(
                response,
                default_scope=f'仅限案件 {state["case_id"]} 的项目或订单',
            )
            release = {
                "action": "approve",
                "comment": str(response.get("comment") or ""),
                "actor": dict(response.get("actor") or {}),
                "approved_at": utc_now(),
                "evidence": evidence,
                "lock_reasons": list(assessment.get("credit_lock_reasons") or []),
                **terms,
            }
            return Command(
                update={
                    "status": "credit_effective",
                    "special_release": release,
                    "approval_evidence": list(state.get("approval_evidence") or []) + evidence,
                    "exception_approval": release,
                    "human_decision": response,
                    "waiting_for": None,
                    "orchestration_runs": [
                        orchestration_run(
                            dict(state.get("orchestration_plan") or {}),
                            "exception_authorization",
                            status="completed",
                            output_summary="授权审批人已批准特别放行并归档证据",
                        )
                    ] if state.get("orchestration_plan") else [],
                    "trace": [trace(
                        "credit.special_release_approved",
                        "已取得特别放行证据，允许本案件继续进入合同阶段。",
                        evidence_count=len(evidence),
                    )],
                },
                goto="contract_gate",
            )
        if action == "reject":
            return Command(
                update={
                    "status": "credit_control_rejected",
                    "human_decision": response,
                    "waiting_for": None,
                    "trace": [trace(
                        "credit.special_release_rejected",
                        "特别放行被拒绝，案件停止继续入单、出货或走模。",
                    )],
                },
                goto="finalize",
            )
        raise ValueError("不支持的特别放行操作。")

    def contract_gate(self, state: WorkflowState) -> dict[str, Any]:
        if (
            state.get("credit_status") != "effective"
            or not state.get("effective_credit_assessment")
        ):
            raise PermissionError("信审结果尚未审批生效，不能进入合同阶段。")
        if state.get("contract_facts"):
            return {"stage": "contract_ready", "waiting_for": None}
        update = {
            "stage": "awaiting_contract",
            "status": "awaiting_contract",
            "waiting_for": "contract_upload",
            "trace": [trace("workflow.interrupt", "信审已完成，等待销售上传合同。")],
        }
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            update["orchestration_runs"] = [
                orchestration_waiting_run(
                    plan,
                    "contract_collection",
                    output_summary="正式授信已生效，等待业务经办人提交合同",
                )
            ]
        return update

    def await_contract(
        self, state: WorkflowState
    ) -> Command[Literal["ingest_contract_documents", "finalize"]]:
        response = interrupt(
            {
                "type": "contract_upload",
                "case_id": state["case_id"],
                "message": "信审已完成，请销售上传或更新合同后继续。",
                "credit_assessment": state.get("effective_credit_assessment"),
                "allowed_actions": ["submit_contract", "close_case"],
            }
        )
        action = str((response or {}).get("action") or "")
        if action == "close_case":
            return Command(
                update={
                    "status": "closed_without_contract",
                    "human_decision": dict(response),
                    "waiting_for": None,
                    "trace": [trace("workflow.closed", "销售选择不再提交合同。")],
                },
                goto="finalize",
            )
        files = list((response or {}).get("file_paths") or [])
        if not files:
            raise ValueError("恢复合同流程时必须提供 file_paths。")
        return Command(
            update={
                "pending_files": files,
                "pending_document_kind": "contract",
                "waiting_for": None,
                "status": "processing",
                "orchestration_runs": [
                    orchestration_run(
                        dict(state.get("orchestration_plan") or {}),
                        "contract_collection",
                        status="completed",
                        output_summary=f"业务经办人已提交{len(files)}份合同资料",
                    )
                ] if state.get("orchestration_plan") else [],
                "trace": [trace("workflow.resumed", "收到合同资料，恢复工作流。")],
            },
            goto="ingest_contract_documents",
        )

    def plan_contract_workflow(self, state: WorkflowState) -> dict[str, Any]:
        if state.get("credit_status") != "effective":
            raise PermissionError("正式授信尚未生效，不能执行合同评审。")
        contracts = list(state.get("contract_facts") or [])
        plan = build_contract_plan(
            str(state["case_id"]),
            contracts,
            ai_available=bool(
                self.contract_ai.enabled and self.contract_ai.gateway.available
            ),
            runtime_snapshot=self._runtime_snapshot(),
        )
        return {
            "stage": "contract_planned",
            "active_workflow_plan": plan,
            "workflow_plans": [plan],
            "trace": [
                trace(
                    "agent.contract.plan_created",
                    "合同审查子 Agent 已按合同数量和可用工具生成受控任务计划。",
                    plan_id=plan["plan_id"],
                    task_count=len(plan["tasks"]),
                    contract_count=len(contracts),
                    ai_assistance=any(
                        item["task_type"] == "contract_ai_review"
                        for item in plan["tasks"]
                    ),
                    spec_hash=plan["spec_hash"],
                    runtime_snapshot=plan["runtime_snapshot"],
                )
            ],
        }

    @staticmethod
    def _contract_by_ref(
        state: WorkflowState, document_ref: str
    ) -> dict[str, Any]:
        return next(
            (
                dict(item)
                for item in state.get("contract_facts") or []
                if str(item.get("document_id") or "") == document_ref
            ),
            {},
        )

    @staticmethod
    def _document_by_ref(
        state: WorkflowState, document_ref: str
    ) -> dict[str, Any]:
        return next(
            (
                dict(item)
                for item in state.get("source_documents") or []
                if str(item.get("document_id") or "") == document_ref
            ),
            {},
        )

    def run_contract_analysis(self, state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("active_workflow_plan") or {})
        task = dict(state.get("active_agent_task") or {})
        assert_plan_integrity(plan)
        reused = cached_task_update(state, plan, task, self.executions)
        if reused:
            return reused
        task_type = str(task.get("task_type") or "")
        document_ref = str((task.get("input_refs") or [""])[0])
        raw_contract = self._contract_by_ref(state, document_ref)
        if not raw_contract:
            if document_ref != "unparsed-contract" or task_type != "contract_policy_review":
                raise ValueError(f"动态合同任务找不到输入引用：{document_ref}")
            started_at = utc_now()
            payload = {
                "document_id": document_ref,
                "status": "unreviewable",
                "reason": "no_reviewable_contract_facts",
            }
            return {
                "agent_task_results": [task_result(plan, task, payload)],
                "agent_runs": [
                    agent_run(
                        plan,
                        task,
                        started_at=started_at,
                        duration_ms=0,
                        input_summary="合同文件未形成可审查结构",
                        output_summary="触发保守阻断降级，不允许自动放行",
                        status="failed",
                        model="contract-policy-guard",
                    )
                ],
                "trace": [
                    trace(
                        "agent.contract.task_failed",
                        "合同未形成可审查结构，动态任务已转入保守阻断。",
                        plan_id=plan.get("plan_id"),
                        task_id=task.get("task_id"),
                        reason="no_reviewable_contract_facts",
                    )
                ],
            }
        facts = contract_facts_from_dict(raw_contract)
        document = self._document_by_ref(state, document_ref)
        fragments = list(document.get("fragments") or [])
        tool_calls: list[dict[str, Any]] = []
        started_at = utc_now()
        started = perf_counter()
        model = "deterministic"
        attempt_history: list[dict[str, Any]] = []
        evidence_gate = "not_applicable"
        if task_type == "contract_policy_review":
            assessment = assessment_from_dict(state.get("effective_credit_assessment"))
            if assessment is None:
                raise ValueError("合同评审前缺少正式信审结果。")
            review_payload = self._call_tool(
                "contract.policy_review",
                {
                    "facts": checkpoint_dict(facts),
                    "assessment": checkpoint_dict(assessment),
                },
                context=InvocationContext(
                    case_id=str(state["case_id"]),
                    caller="contract_review",
                    agent_id="contract_review",
                    plan_id=str(plan.get("plan_id") or ""),
                    task_id=str(task.get("task_id") or ""),
                    input_summary=f"合同{document_ref}事实与正式授信条件",
                ),
                records=tool_calls,
                fallback=lambda: checkpoint_dict(
                    self.contract_engine.review(facts, assessment)
                ),
            )
            review = review_from_dict(dict(review_payload))
            for finding in review.findings:
                if finding.fragment_id or not finding.clause_excerpt:
                    continue
                matched = locate_excerpt(
                    finding.evidence_query or finding.clause_excerpt,
                    fragments,
                )
                if matched:
                    finding.document_id = facts.document_id
                    finding.fragment_id = str(matched["fragment_id"])
                    finding.location = dict(matched["location"])
            payload = {"document_id": document_ref, "review": checkpoint_dict(review)}
            summary = (
                f"制度结论{review.decision.value}，发现{len(review.findings)}项风险"
            )
            model = "contract-policy-engine"
        elif task_type == "contract_ai_review":
            ai_text = facts.raw_text or ""
            if "⟦" not in ai_text:
                ai_text = "⟦REDACTED_TEXT⟧\n" + ai_text
            policy = dict(task.get("execution_policy") or {})
            max_attempts = int(policy.get("max_attempts") or 1)
            assistance: dict[str, Any] = {}
            for attempt in range(1, max_attempts + 1):
                attempt_started_at = utc_now()
                attempt_started = perf_counter()
                assistance = self._call_tool(
                    "contract.ai_review",
                    {
                        "facts": checkpoint_dict(facts),
                        "redacted_text": ai_text,
                        "fragments": fragments,
                    },
                    context=InvocationContext(
                        case_id=str(state["case_id"]),
                        caller="contract_review",
                        agent_id="contract_review",
                        plan_id=str(plan.get("plan_id") or ""),
                        task_id=str(task.get("task_id") or ""),
                        input_summary=f"合同{document_ref}的脱敏正文与证据片段",
                    ),
                    records=tool_calls,
                    fallback=lambda: self.contract_ai.review(
                        facts,
                        redacted_text=ai_text,
                        fragments=fragments,
                    ),
                )
                attempt_duration = round((perf_counter() - attempt_started) * 1000)
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "status": str(assistance.get("status") or "failed"),
                        "started_at": attempt_started_at,
                        "completed_at": utc_now(),
                        "duration_ms": attempt_duration,
                        "error_type": str(assistance.get("error_type") or ""),
                    }
                )
                if assistance.get("status") != "failed":
                    break
            findings = list(assistance.get("findings") or [])
            located = [
                item
                for item in findings
                if item.get("document_id") and item.get("fragment_id")
            ]
            evidence_coverage = len(located) / len(findings) if findings else 1.0
            minimum_coverage = float(policy.get("minimum_evidence_coverage") or 0)
            if findings and evidence_coverage < minimum_coverage:
                assistance = {
                    **assistance,
                    "findings": located,
                    "summary": (
                        f"证据定位率{evidence_coverage:.0%}低于门槛"
                        f"{minimum_coverage:.0%}，未定位发现已降级剔除。"
                    ),
                    "evidence_degraded": True,
                    "original_finding_count": len(findings),
                }
                evidence_gate = "degraded"
            else:
                evidence_gate = "passed"
            payload = {"document_id": document_ref, "ai_assistance": assistance}
            summary = (
                f"AI辅助状态{assistance.get('status')}，"
                f"发现{len(assistance.get('findings') or [])}项"
            )
            model = str(assistance.get("model") or "text-model")
        else:
            raise ValueError(f"未授权的合同分析任务：{task_type}")
        duration_ms = round((perf_counter() - started) * 1000)
        node_status = (
            "degraded"
            if task_type == "contract_ai_review"
            and (
                payload["ai_assistance"].get("status") == "failed"
                or evidence_gate == "degraded"
            )
            else "completed"
        )
        result = task_result(
            plan,
            task,
            payload,
            evidence_gate=evidence_gate,
        )
        run = agent_run(
            plan,
            task,
            started_at=started_at,
            duration_ms=duration_ms,
            input_summary=f"合同引用{document_ref}；内容已脱敏",
            output_summary=summary,
            model=model,
            status=node_status,
            attempt_history=attempt_history,
            evidence_gate=evidence_gate,
        )
        if not state.get("agent_rerun_context"):
            self.executions.save(
                state["case_id"],
                plan["plan_id"],
                result["idempotency_key"],
                result=result,
                run=run,
            )
        return {
            "agent_task_results": [result],
            "agent_runs": [run],
            "tool_calls": tool_calls,
            "trace": [
                trace(
                    "agent.contract.task_completed",
                    f"{task.get('label')}已完成。",
                    plan_id=plan.get("plan_id"),
                    task_id=task.get("task_id"),
                    document_id=document_ref,
                    duration_ms=duration_ms,
                    attempt_count=len(attempt_history) or 1,
                    evidence_gate=evidence_gate,
                )
            ],
        }

    def synthesize_contract_reviews(self, state: WorkflowState) -> dict[str, Any]:
        if state.get("credit_status") != "effective":
            raise PermissionError("正式授信尚未生效，不能执行合同评审。")
        assessment = assessment_from_dict(state.get("effective_credit_assessment"))
        if assessment is None:
            raise ValueError("合同评审前缺少正式信审结果。")
        plan = dict(state.get("active_workflow_plan") or {})
        contract_facts = list(state.get("contract_facts") or [])
        results = [
            item
            for item in state.get("agent_task_results") or []
            if item.get("plan_id") == plan.get("plan_id")
        ]
        by_document: dict[str, dict[str, Any]] = {}
        for result in results:
            payload = dict(result.get("payload") or {})
            document_id = str(payload.get("document_id") or "")
            if document_id:
                by_document.setdefault(document_id, {}).update(payload)
        reviews: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        synthesized_results: list[dict[str, Any]] = []
        for item in contract_facts:
            facts = contract_facts_from_dict(item)
            group = by_document.get(facts.document_id) or {}
            review = dict(group.get("review") or {})
            if not review:
                continue
            review["ai_assistance"] = dict(
                group.get("ai_assistance")
                or {
                    "status": "not_configured",
                    "model": self.contract_ai.gateway.model,
                    "findings": [],
                    "summary": "当前动态计划未启用文本模型，使用制度规则审查。",
                }
            )
            reviews.append(review)
            synthesis_task = plan_task(
                plan, "contract_synthesis", input_ref=facts.document_id
            )
            started_at = utc_now()
            summary = (
                f"已合并制度风险{len(review.get('findings') or [])}项、"
                f"AI辅助发现{len(review['ai_assistance'].get('findings') or [])}项"
            )
            runs.append(
                agent_run(
                    plan,
                    synthesis_task,
                    started_at=started_at,
                    duration_ms=0,
                    input_summary=f"合同引用{facts.document_id}的分析结果",
                    output_summary=summary,
                )
            )
            synthesized_results.append(
                task_result(
                    plan,
                    synthesis_task,
                    {
                        "document_id": facts.document_id,
                        "decision": review.get("decision"),
                        "rule_finding_count": len(review.get("findings") or []),
                        "ai_finding_count": len(
                            review["ai_assistance"].get("findings") or []
                        ),
                    },
                )
            )
        if not reviews:
            review = ContractReview(
                decision=AuditDecision.BLOCK,
                approval_route=ApprovalRoute.RETURN_TO_OWNER,
                risk_level=RiskLevel.BLOCKER,
                findings=[
                    RiskFinding(
                        rule_id="DOCUMENT-NO-REVIEWABLE-CONTRACT",
                        title="未形成可审查的合同结果",
                        level=RiskLevel.BLOCKER,
                        message="提交的文件均未成功解析为合同，系统拒绝自动放行。",
                        suggestion="检查文件格式和内容后重新上传合同；必要时转人工确认文件完整性。",
                        hard_stop=True,
                    )
                ],
                summary="合同解析或分类失败，必须重新提交有效合同后再审查。",
                credit_cross_check={
                    "credit_score": assessment.score,
                    "policy_version": assessment.policy_version,
                },
                ai_assistance={
                    "status": "not_applicable",
                    "model": self.contract_ai.gateway.model,
                    "findings": [],
                    "summary": "没有可供模型辅助审查的合同文本。",
                },
            )
            reviews.append(checkpoint_dict(review))
            fallback_task = plan_task(
                plan, "contract_synthesis", input_ref="unparsed-contract"
            )
            runs.append(
                agent_run(
                    plan,
                    fallback_task,
                    started_at=utc_now(),
                    duration_ms=0,
                    input_summary="不可解析合同的降级结果",
                    output_summary="形成保守阻断结论并要求重新提交合同",
                    status="completed",
                    model="contract-policy-guard",
                )
            )
            synthesized_results.append(
                task_result(
                    plan,
                    fallback_task,
                    {
                        "document_id": "unparsed-contract",
                        "decision": "block",
                        "rule_finding_count": 1,
                        "ai_finding_count": 0,
                    },
                )
            )
        return {
            "stage": "contract_reviewed",
            "contract_reviews": reviews,
            "agent_runs": runs,
            "agent_task_results": synthesized_results,
            "trace": [
                trace(
                    "contract.completed",
                    "合同子图已完成完整性、授信、账期和法律风险检查。",
                    contract_count=len(contract_facts),
                    review_count=len(reviews),
                ),
            ],
        }

    def verify_contract_reviews(self, state: WorkflowState) -> dict[str, Any]:
        plan = dict(state.get("active_workflow_plan") or {})
        reviews = list(state.get("contract_reviews") or [])
        contracts = list(state.get("contract_facts") or [])
        runs: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        verifications: list[dict[str, Any]] = []
        valid_decisions = {"pass", "manual_review", "special_approval", "block"}
        for index, review in enumerate(reviews):
            document_id = str(
                (contracts[index] if index < len(contracts) else {}).get("document_id")
                or "unparsed-contract"
            )
            task = plan_task(plan, "contract_verification", input_ref=document_id)
            started_at = utc_now()
            started = perf_counter()
            findings = list(review.get("findings") or [])
            ai_findings = list(
                (review.get("ai_assistance") or {}).get("findings") or []
            )
            checks = {
                "decision_valid": review.get("decision") in valid_decisions,
                "summary_present": bool(str(review.get("summary") or "").strip()),
                "policy_cross_check_present": bool(review.get("credit_cross_check")),
                "rule_findings_structured": all(
                    item.get("rule_id") and item.get("title") and item.get("level")
                    for item in findings
                ),
                "ai_findings_advisory": all(
                    item.get("source") == "text_model" for item in ai_findings
                ),
            }
            verification = {
                "document_id": document_id,
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
                "evidence_coverage": (
                    round(
                        sum(
                            bool(item.get("document_id") and item.get("fragment_id"))
                            for item in findings + ai_findings
                        )
                        / len(findings + ai_findings),
                        3,
                    )
                    if findings or ai_findings
                    else 1.0
                ),
                "verifier": "independent_contract_guard",
                "verified_at": utc_now(),
            }
            duration_ms = round((perf_counter() - started) * 1000)
            verifications.append(verification)
            results.append(task_result(plan, task, verification))
            runs.append(
                agent_run(
                    plan,
                    task,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    input_summary=f"合同引用{document_id}的制度和AI分析摘要",
                    output_summary=(
                        f"独立核验{verification['status']}，证据覆盖率"
                        f"{verification['evidence_coverage']:.0%}"
                    ),
                    status=(
                        "completed" if verification["status"] == "passed" else "failed"
                    ),
                    model="independent-contract-guard",
                )
            )
        plan["status"] = (
            "completed"
            if verifications and all(item["status"] == "passed" for item in verifications)
            else "failed"
        )
        plan["completed_at"] = utc_now()
        execution_audit = audit_plan_execution(
            plan,
            list(state.get("agent_runs") or []) + runs,
            list(state.get("agent_task_results") or []) + results,
        )
        if execution_audit["status"] != "conformant":
            plan["status"] = "failed"
        verified_reviews = [dict(item) for item in reviews]
        if plan["status"] == "failed":
            for review in verified_reviews:
                decision = str(review.get("decision") or "")
                if decision not in {"block", "special_approval"}:
                    review["decision"] = "manual_review"
                    review["approval_route"] = "finance_legal"
                    review["risk_level"] = "medium"
                review["summary"] = (
                    "独立核验未通过，禁止自动放行；转财务/法务人工复核。"
                    + str(review.get("summary") or "")
                )
        return {
            "stage": "contract_reviewed",
            "active_workflow_plan": plan,
            "workflow_plans": [plan],
            "contract_reviews": verified_reviews,
            "contract_verifications": verifications,
            "agent_runs": runs,
            "agent_task_results": results,
            "execution_audits": [execution_audit],
            "trace": [
                trace(
                    "agent.contract.verified",
                    "合同结论已完成独立结构与证据核验。",
                    plan_id=plan.get("plan_id"),
                    status=plan["status"],
                    contract_count=len(verifications),
                    execution_audit=execution_audit["status"],
                )
            ],
        }

    def decide(self, state: WorkflowState) -> dict[str, Any]:
        reviews = state.get("contract_reviews") or []
        assessment = state.get("effective_credit_assessment") or {}
        result = resolve_case_decision(
            (str(item.get("decision") or "") for item in reviews),
            rating_requires_review=bool(
                (assessment.get("rating_resolution") or {}).get("requires_manual_review")
            ),
        )
        request = {
            "case_id": state["case_id"],
            "decision": result.decision.value,
            "approval_route": result.approval_route.value,
            "credit_assessment": assessment,
            "contract_reviews": reviews,
        }
        update = {
            "stage": "decision_ready",
            "decision": result.decision.value,
            "approval_route": result.approval_route.value,
            "approval_request": request,
            "waiting_for": result.waiting_for,
            "status": result.status,
            "trace": [
                trace(
                    "decision.routed",
                    "确定性状态机已完成风险路由。",
                    decision=result.decision.value,
                    approval_route=result.approval_route.value,
                )
            ],
        }
        plan = dict(state.get("orchestration_plan") or {})
        if plan:
            runs = [
                orchestration_run(
                    plan,
                    "result_synthesis",
                    status="completed",
                    output_summary=(
                        f"主 Agent 汇总信用与合同结果，路由为{result.decision.value}"
                    ),
                )
            ]
            if result.decision.value in {
                "block",
                "manual_review",
                "special_approval",
                "pass",
            }:
                runs.append(
                    orchestration_waiting_run(
                        plan,
                        "contract_human_review",
                        output_summary="合同Agent已完成审查，等待人工审批、修改或授权",
                    )
                )
            else:
                runs.append(
                    orchestration_run(
                        plan,
                        "contract_human_review",
                        status="skipped",
                        output_summary="合同风险未触发额外人工复核",
                    )
                )
            update["orchestration_runs"] = runs
        return update

    def await_sales_revision(
        self, state: WorkflowState
    ) -> Command[Literal["ingest_contract_documents", "finalize"]]:
        response = interrupt(
            {
                "type": "sales_revision",
                **dict(state.get("approval_request") or {}),
                "message": "合同命中硬底线，请销售修改交易条件或关闭案件。",
                "allowed_actions": ["submit_revision", "close_case"],
            }
        )
        if str((response or {}).get("action") or "") == "close_case":
            return Command(
                update={
                    "status": "rejected",
                    "human_decision": dict(response),
                    "waiting_for": None,
                    "trace": [trace("sales.closed", "销售确认无法修改，案件关闭。")],
                },
                goto="finalize",
            )
        files = list((response or {}).get("file_paths") or [])
        if not files:
            raise ValueError("提交修订合同时必须提供 file_paths。")
        return Command(
            update={
                "pending_files": files,
                "pending_document_kind": "contract",
                "contract_facts": [],
                "contract_reviews": [],
                "decision": None,
                "approval_route": None,
                "waiting_for": None,
                "status": "processing",
                "human_decision": dict(response),
                "orchestration_runs": [
                    orchestration_run(
                        dict(state.get("orchestration_plan") or {}),
                        "contract_human_review",
                        status="completed",
                        output_summary="业务经办人已按风险意见修改并重新提交合同",
                    )
                ] if state.get("orchestration_plan") else [],
                "trace": [trace("sales.revised", "销售已提交修订合同，重新审查。")],
            },
            goto="ingest_contract_documents",
        )

    def await_manager_approval(
        self, state: WorkflowState
    ) -> Command[Literal["finalize", "await_sales_revision"]]:
        response = interrupt(
            {
                "type": "manager_approval",
                **dict(state.get("approval_request") or {}),
                "message": "合同需要总监或CEO特别审批。",
                "allowed_actions": ["approve", "reject"],
            }
        )
        response_payload = dict(response or {})
        candidate_adoption = dict(
            response_payload.pop("_candidate_adoption", {}) or {}
        )
        candidate_update = dict(candidate_adoption.get("state_update") or {})
        candidate_trace = dict(candidate_adoption.get("trace") or {})
        if candidate_adoption and candidate_adoption.get("agent") != "contract":
            raise ValueError("合同管理审批不能采纳非合同Agent候选。")
        approved = str(response_payload.get("action") or "") == "approve"
        if approved:
            evidence = list(response_payload.get("approval_evidence") or [])
            if not evidence:
                raise ValueError("合同特批必须上传批准邮件、OA截图或其他审批附件。")
            terms = approval_terms(
                response_payload,
                default_scope=f'仅限案件 {state["case_id"]} 的合同例外',
            )
            decision = {**response_payload, **terms}
            return Command(
                update={
                    **candidate_update,
                    "status": "approved_by_exception",
                    "human_decision": decision,
                    "approval_evidence": list(state.get("approval_evidence") or []) + evidence,
                    "exception_approval": decision,
                    "waiting_for": None,
                    "orchestration_runs": [
                        orchestration_run(
                            dict(state.get("orchestration_plan") or {}),
                            "contract_human_review",
                            status="completed",
                            output_summary="授权审批人已批准合同例外并归档证据",
                        )
                    ] if state.get("orchestration_plan") else [],
                    "trace": ([candidate_trace] if candidate_trace else []) + [
                        trace(
                            "manager.approved",
                            "管理层已批准本次例外并归档审批证据。",
                            evidence_count=len(evidence),
                        )
                    ],
                },
                goto="finalize",
            )
        return Command(
            update={
                "status": "blocked",
                "human_decision": response_payload,
                "waiting_for": "sales_revision",
                "trace": [trace("manager.rejected", "管理层拒绝特批，退回销售修改。")],
            },
            goto="await_sales_revision",
        )

    def await_finance_legal(
        self, state: WorkflowState
    ) -> Command[
        Literal["finalize", "ingest_credit_documents", "await_sales_revision"]
    ]:
        normal_contract_approval = state.get("decision") == "pass"
        response = interrupt(
            {
                "type": "finance_legal_review",
                **dict(state.get("approval_request") or {}),
                "message": (
                    "合同Agent审查已完成，请合同法务进行最终人工审批。"
                    if normal_contract_approval
                    else "请财务/法务补充资料、确认结论或要求修改合同。"
                ),
                "allowed_actions": ["approve", "supplement", "revise_contract"],
            }
        )
        response_payload = dict(response or {})
        candidate_adoption = dict(
            response_payload.pop("_candidate_adoption", {}) or {}
        )
        candidate_update = dict(candidate_adoption.get("state_update") or {})
        candidate_trace = dict(candidate_adoption.get("trace") or {})
        if candidate_adoption and candidate_adoption.get("agent") != "contract":
            raise ValueError("财务法务审批不能采纳非合同Agent候选。")
        action = str(response_payload.get("action") or "")
        if action == "approve":
            approved_status = (
                "approved" if normal_contract_approval else "approved_after_manual_review"
            )
            approved_stage = (
                "contract.approved" if normal_contract_approval else "manual.approved"
            )
            approved_message = (
                "合同法务已完成最终审批并确认通过。"
                if normal_contract_approval
                else "财务/法务完成复核并确认。"
            )
            return Command(
                update={
                    **candidate_update,
                    "status": approved_status,
                    "human_decision": response_payload,
                    "waiting_for": None,
                    "orchestration_runs": [
                        orchestration_run(
                            dict(state.get("orchestration_plan") or {}),
                            "contract_human_review",
                            status="completed",
                            output_summary="合同法务已完成人工复核并确认通过",
                        )
                    ] if state.get("orchestration_plan") else [],
                    "trace": ([candidate_trace] if candidate_trace else []) + [
                        trace(approved_stage, approved_message)
                    ],
                },
                goto="finalize",
            )
        files = list(response_payload.get("file_paths") or [])
        if action == "supplement" and files:
            return Command(
                update={
                    "pending_files": files,
                    "pending_document_kind": "credit",
                    "use_cached_credit": False,
                    "credit_assessment": None,
                    "effective_credit_assessment": None,
                    "credit_status": "collecting",
                    "credit_approval": {},
                    "contract_reviews": [],
                    "waiting_for": None,
                    "status": "processing",
                    "human_decision": dict(response),
                    "trace": [trace("manual.supplemented", "已补充信用资料，重新信审。")],
                },
                goto="ingest_credit_documents",
            )
        return Command(
            update={
                "waiting_for": "sales_revision",
                "human_decision": dict(response),
                "trace": [trace("manual.revision_requested", "财务/法务要求销售修改合同。")],
            },
            goto="await_sales_revision",
        )

    def finalize(self, state: WorkflowState) -> dict[str, Any]:
        final_state = dict(state)
        final_state["stage"] = "archived"
        if final_state.get("status") == "processing":
            final_state["status"] = "completed"
        final_trace = trace(
            "workflow.finalized",
            "报告已生成，案件结果准备回写CRM和OA。",
            status=final_state.get("status"),
        )
        final_state["trace"] = list(state.get("trace") or []) + [final_trace]
        customer = dict(final_state.get("customer") or {})
        credit = dict(final_state.get("effective_credit_assessment") or {})
        approval = dict(final_state.get("credit_approval") or {})
        writeback_payload = {
            "case_id": state["case_id"],
            "status": final_state.get("status"),
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
            "approval_chain": list(final_state.get("approval_chain") or []),
        }
        customer_id = str(
            customer.get("crm_customer_id")
            or customer.get("unified_social_credit_code")
            or ""
        )
        final_writeback = self.integrations.write_back(
            str(state["case_id"]), customer_id, writeback_payload, phase="final"
        )
        writeback = dict(final_state.get("writeback") or {})
        writeback["final"] = final_writeback
        final_state["writeback"] = writeback
        plan = dict(state.get("orchestration_plan") or {})
        orchestration_runs = []
        if plan:
            latest_status = {
                str(item.get("task_type") or ""): str(item.get("status") or "")
                for item in state.get("orchestration_runs") or []
            }
            terminal_skips = [
                orchestration_run(
                    plan,
                    str(task.get("task_type") or ""),
                    status="skipped",
                    output_summary="案件已结束，该条件任务无需继续执行",
                )
                for task in plan.get("tasks") or []
                if task.get("task_type") not in {"enterprise_writeback", "case_archive"}
                and latest_status.get(str(task.get("task_type") or ""))
                in {"", "pending", "waiting", "running"}
            ]
            writeback_statuses = {
                str(item.get("status") or "")
                for phase in writeback.values()
                for item in (phase.values() if isinstance(phase, dict) else [])
                if isinstance(item, dict)
            }
            orchestration_runs = terminal_skips + [
                orchestration_run(
                    plan,
                    "enterprise_writeback",
                    status=(
                        "completed"
                        if not writeback_statuses.intersection({"failed"})
                        else "failed"
                    ),
                    output_summary="OA、CRM与SAP回写已执行并保留审计结果",
                ),
                orchestration_run(
                    plan,
                    "case_archive",
                    status="completed",
                    output_summary="案件报告、原件索引和运行记录已归档",
                ),
            ]
        if orchestration_runs:
            final_state["orchestration_runs"] = (
                list(state.get("orchestration_runs") or []) + orchestration_runs
            )
            completed_plan = dict(plan)
            completed_plan["status"] = (
                "failed"
                if any(item.get("status") == "failed" for item in orchestration_runs)
                else "completed"
            )
            completed_plan["completed_at"] = utc_now()
            final_state["orchestration_plan"] = completed_plan
        case = case_from_state(final_state)
        self.repository.save(case)
        reports = self.reporter.export(case, self.output_dir)
        return {
            "stage": "archived",
            "status": final_state["status"],
            "waiting_for": None,
            "reports": reports,
            "writeback": writeback,
            "orchestration_plan": final_state.get("orchestration_plan"),
            "orchestration_runs": orchestration_runs,
            "trace": [final_trace],
        }
