"""Business nodes used by the LangGraph workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
)
from ..domain.decision import resolve_case_decision
from ..domain.models import (
    ApprovalRoute,
    AuditDecision,
    ContractReview,
    RiskFinding,
    RiskLevel,
    utc_now,
)
from ..ingestion import DocumentExtractor, ExtractedDocument, locate_excerpt
from ..integrations import IntegrationBundle
from ..llm import ContractAIAssistant
from ..persistence import CaseDocumentArchive, CaseRepository, TaskExecutionStore
from ..reporting import AuditReporter
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
    ) -> None:
        self.repository = repository
        self.vault_dir = Path(vault_dir)
        self.inbox_dir = Path(inbox_dir).resolve()
        self.output_dir = Path(output_dir)
        self.archive = CaseDocumentArchive(archive_dir)
        self.executions = TaskExecutionStore(execution_dir)
        self.integrations = integrations or IntegrationBundle.from_environment()
        self.documents = DocumentExtractor()
        self.credit_facts = CreditFactExtractor()
        self.credit_engine = CreditScoringEngine(policy)
        self.contract_facts = ContractFactExtractor()
        self.contract_engine = ContractReviewEngine(self.credit_engine.policy)
        self.contract_ai = ai_assistant or ContractAIAssistant()
        self.reporter = AuditReporter()

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
        }

    @staticmethod
    def _is_contract(document: ExtractedDocument) -> bool:
        name = Path(document.path).name.lower()
        if any(token in name for token in ("合同", "协议", "contract", "agreement")):
            return True
        signals = ("甲方", "乙方", "违约责任", "争议解决", "payment terms", "party a", "party b")
        return sum(signal.lower() in document.text.lower() for signal in signals) >= 2

    def create_case(self, state: WorkflowState) -> dict[str, Any]:
        return {
            "stage": "intake",
            "status": "processing",
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
                    "已规划资料解析、信审、合同审查、风险路由和报告任务。",
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
                document = self.documents.extract(archived["archived_path"])
                is_contract = self._is_contract(document)
                if document_kind == "credit" and is_contract:
                    raise ValueError("信用资料入口不接受合同文件。")
                if document_kind == "contract" and not is_contract:
                    raise ValueError("合同入口只接受合同或协议文件。")
                source_files.append(document.path)
                source_documents[archive_index or 0].update(
                    {
                        "parse_status": "parsed",
                        "media_type": document.media_type,
                        "extractor": document.extractor,
                        "warnings": document.warnings,
                        "fragments": [
                            {
                                "fragment_id": fragment.fragment_id,
                                "text": vault.redact(fragment.text),
                                "location": fragment.location,
                            }
                            for fragment in document.fragments
                        ],
                    }
                )
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
                        sha256=archived["sha256"],
                    )
                )
                if document_kind == "contract":
                    redacted = vault.redact(document.text)
                    facts = self.contract_facts.extract(
                        document.text,
                        contract_name=path.name,
                        customer_name=customer.customer_name,
                        business_type=customer.business_type,
                    )
                    facts.document_id = str(archived["document_id"])
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
                    customer = self.credit_facts.enrich(customer, document.text, path.name)
            except Exception as exc:
                if archive_index is not None:
                    source_documents[archive_index].update(
                        {"parse_status": "failed", "error": str(exc)}
                    )
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
            "trace": traces,
            "errors": errors,
        }

    def ingest_credit_documents(self, state: WorkflowState) -> dict[str, Any]:
        return self._ingest_documents(state, document_kind="credit")

    def ingest_contract_documents(self, state: WorkflowState) -> dict[str, Any]:
        if (
            state.get("credit_status") != "effective"
            or not state.get("effective_credit_assessment")
        ):
            raise PermissionError("正式授信尚未生效，不能解析或评审合同。")
        return self._ingest_documents(state, document_kind="contract")

    def check_credit_cache(self, state: WorkflowState) -> dict[str, Any]:
        customer = profile_from_dict(dict(state["customer"]))
        cached = (
            self.repository.find_valid_credit(customer)
            if state.get("use_cached_credit", True)
            else None
        )
        if cached:
            assessment = checkpoint_dict(cached)
            credit_control = self.credit_engine.credit_control(
                customer,
                float(assessment.get("approved_credit_limit") or 0),
            )
            assessment.update(
                {
                    "occupied_credit_amount": credit_control["occupied_credit_amount"],
                    "available_credit_amount": credit_control["available_credit_amount"],
                    "credit_locked": credit_control["credit_locked"],
                    "credit_lock_reasons": credit_control["credit_lock_reasons"],
                }
            )
            locked = bool(credit_control["credit_locked"])
            return {
                "stage": "credit_ready",
                "status": "credit_control_locked" if locked else "credit_effective",
                "credit_source": "cache",
                "credit_assessment": assessment,
                "effective_credit_assessment": assessment,
                "credit_status": "effective",
                "credit_control": credit_control,
                "waiting_for": "special_release" if locked else None,
                "credit_approval": {
                    "action": "reuse_effective_credit",
                    "source": "cache",
                    "approved_at": cached.assessed_at,
                },
                "trace": [
                    trace(
                        "credit.cache_hit",
                        "读取到180天内有效信审结果。",
                        assessed_at=cached.assessed_at,
                    )
                ],
            }
        return {
            "stage": "credit_required",
            "credit_source": "new_assessment",
            "credit_assessment": None,
            "effective_credit_assessment": None,
            "credit_status": "calculating",
            "trace": [trace("credit.cache_miss", "未找到有效信审结果，进入信审子图。")],
        }

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
        payload: dict[str, Any]
        summary: str
        if task_type == "credit_data_completeness":
            assessment = self.credit_engine.assess(profile)
            payload = {
                "coverage_ratio": assessment.data_coverage_ratio,
                "available_dimensions": assessment.available_dimensions,
                "missing_fields": assessment.missing_fields,
                "requires_supplement": assessment.requires_supplement,
                "supplement_reasons": assessment.supplement_reasons,
                "evidence_count": len(profile.evidence),
            }
            summary = (
                f"资料覆盖率{assessment.data_coverage_ratio:.0%}，"
                f"识别{len(assessment.available_dimensions)}个有效维度"
            )
        elif task_type == "credit_financial_analysis":
            missing: list[str] = []
            score, reasons = self.credit_engine._financial_score(profile, missing)
            payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
            summary = f"财务维度评分{score:.1f}" if score is not None else "财务指标不足"
        elif task_type == "credit_rating_analysis":
            missing = []
            score, reasons, resolution = self.credit_engine._rating_score(profile, missing)
            selected = dict(resolution.get("selected") or {})
            payload = {
                "score": score,
                "agency": selected.get("agency"),
                "rating": selected.get("rating"),
                "conflict": bool(resolution.get("conflict")),
                "requires_manual_review": bool(resolution.get("requires_manual_review")),
                "warning_count": len(reasons),
            }
            summary = (
                f"采用{selected.get('agency') or '未知机构'} {selected.get('rating') or '未评级'}"
            )
        elif task_type == "credit_cooperation_analysis":
            missing = []
            score, reasons = self.credit_engine._cooperation_score(profile, missing)
            payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
            summary = f"历史交易评分{score:.1f}" if score is not None else "历史交易资料不足"
        elif task_type == "credit_enterprise_analysis":
            missing = []
            score, reasons = self.credit_engine._enterprise_score(profile, missing)
            payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
            summary = f"企业基础评分{score:.1f}" if score is not None else "企业基础资料不足"
        elif task_type == "credit_control_analysis":
            limit = max(0.0, float(profile.requested_credit_limit or profile.monthly_order_amount or 0))
            control = self.credit_engine.credit_control(profile, limit)
            payload = {
                "has_occupied_credit": bool(control["occupied_credit_amount"]),
                "credit_locked": bool(control["credit_locked"]),
                "overdue_above_threshold": (
                    int(control["current_overdue_days"])
                    > int(control["max_current_overdue_days"])
                ),
            }
            summary = (
                f"额度占用={'存在' if payload['has_occupied_credit'] else '无'}，"
                f"逾期阈值={'超出' if payload['overdue_above_threshold'] else '未超出'}"
            )
        elif task_type == "credit_tkm_analysis":
            payload = {
                "business_subtype": profile.tkm_business_subtype or "policy_default",
                "purchase_exemption_requested": bool(profile.purchase_exemption_requested),
                "requested_term_days": profile.requested_term_days,
            }
            summary = "已核对TKM子类型、账期和首期采购款豁免申请"
        else:
            raise ValueError(f"未授权的信用分析任务：{task_type}")
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
        return {
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
        return {
            "stage": "awaiting_contract",
            "status": "awaiting_contract",
            "waiting_for": "contract_upload",
            "trace": [trace("workflow.interrupt", "信审已完成，等待销售上传合同。")],
        }

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
        started_at = utc_now()
        started = perf_counter()
        model = "deterministic"
        attempt_history: list[dict[str, Any]] = []
        evidence_gate = "not_applicable"
        if task_type == "contract_policy_review":
            assessment = assessment_from_dict(state.get("effective_credit_assessment"))
            if assessment is None:
                raise ValueError("合同评审前缺少正式信审结果。")
            review = self.contract_engine.review(facts, assessment)
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
                assistance = self.contract_ai.review(
                    facts,
                    redacted_text=ai_text,
                    fragments=fragments,
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
        return {
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
        response = interrupt(
            {
                "type": "finance_legal_review",
                **dict(state.get("approval_request") or {}),
                "message": "请财务/法务补充资料、确认结论或要求修改合同。",
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
            return Command(
                update={
                    **candidate_update,
                    "status": "approved_after_manual_review",
                    "human_decision": response_payload,
                    "waiting_for": None,
                    "trace": ([candidate_trace] if candidate_trace else []) + [
                        trace("manual.approved", "财务/法务完成复核并确认。")
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
        case = case_from_state(final_state)
        self.repository.save(case)
        reports = self.reporter.export(case, self.output_dir)
        return {
            "stage": "archived",
            "status": final_state["status"],
            "waiting_for": None,
            "reports": reports,
            "writeback": writeback,
            "trace": [final_trace],
        }
