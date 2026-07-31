"""Business nodes used by the LangGraph workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from ..ingestion import DocumentExtractor, ExtractedDocument
from ..integrations import IntegrationBundle
from ..persistence import CaseDocumentArchive, CaseRepository
from ..reporting import AuditReporter
from ..security import RedactionVault
from .codec import case_from_state, checkpoint_dict
from .state import WorkflowState


def trace(stage: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ts": utc_now(), "stage": stage, "message": message, "data": data}


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
        integrations: IntegrationBundle | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.repository = repository
        self.vault_dir = Path(vault_dir)
        self.inbox_dir = Path(inbox_dir).resolve()
        self.output_dir = Path(output_dir)
        self.archive = CaseDocumentArchive(archive_dir)
        self.integrations = integrations or IntegrationBundle.from_environment()
        self.documents = DocumentExtractor()
        self.credit_facts = CreditFactExtractor()
        self.credit_engine = CreditScoringEngine(policy)
        self.contract_facts = ContractFactExtractor()
        self.contract_engine = ContractReviewEngine(self.credit_engine.policy)
        self.reporter = AuditReporter()

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

    def score_credit(self, state: WorkflowState) -> dict[str, Any]:
        customer = profile_from_dict(dict(state["customer"]))
        assessment = self.credit_engine.assess(customer)
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
            "trace": traces,
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

    def review_contracts(self, state: WorkflowState) -> dict[str, Any]:
        if state.get("credit_status") != "effective":
            raise PermissionError("正式授信尚未生效，不能执行合同评审。")
        assessment = assessment_from_dict(state.get("effective_credit_assessment"))
        if assessment is None:
            raise ValueError("合同评审前缺少正式信审结果。")
        contract_facts = list(state.get("contract_facts") or [])
        reviews = []
        for item in contract_facts:
            review = self.contract_engine.review(contract_facts_from_dict(item), assessment)
            reviews.append(checkpoint_dict(review))
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
            )
            reviews.append(checkpoint_dict(review))
        return {
            "stage": "contract_reviewed",
            "contract_reviews": reviews,
            "trace": [
                trace(
                    "contract.completed",
                    "合同子图已完成完整性、授信、账期和法律风险检查。",
                    contract_count=len(contract_facts),
                    review_count=len(reviews),
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
        approved = str((response or {}).get("action") or "") == "approve"
        if approved:
            evidence = list((response or {}).get("approval_evidence") or [])
            if not evidence:
                raise ValueError("合同特批必须上传批准邮件、OA截图或其他审批附件。")
            terms = approval_terms(
                dict(response or {}),
                default_scope=f'仅限案件 {state["case_id"]} 的合同例外',
            )
            decision = {**dict(response or {}), **terms}
            return Command(
                update={
                    "status": "approved_by_exception",
                    "human_decision": decision,
                    "approval_evidence": list(state.get("approval_evidence") or []) + evidence,
                    "exception_approval": decision,
                    "waiting_for": None,
                    "trace": [trace(
                        "manager.approved",
                        "管理层已批准本次例外并归档审批证据。",
                        evidence_count=len(evidence),
                    )],
                },
                goto="finalize",
            )
        return Command(
            update={
                "status": "blocked",
                "human_decision": dict(response),
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
        action = str((response or {}).get("action") or "")
        if action == "approve":
            return Command(
                update={
                    "status": "approved_after_manual_review",
                    "human_decision": dict(response),
                    "waiting_for": None,
                    "trace": [trace("manual.approved", "财务/法务完成复核并确认。")],
                },
                goto="finalize",
            )
        files = list((response or {}).get("file_paths") or [])
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
