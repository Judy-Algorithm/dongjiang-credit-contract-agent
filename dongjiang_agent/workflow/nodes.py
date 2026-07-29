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
from ..domain.models import utc_now
from ..ingestion import DocumentExtractor, ExtractedDocument
from ..persistence import CaseRepository
from ..reporting import AuditReporter
from ..security import RedactionVault
from .codec import case_from_state, checkpoint_dict
from .state import WorkflowState


def trace(stage: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ts": utc_now(), "stage": stage, "message": message, "data": data}


class WorkflowNodes:
    """Dependency container: graph nodes stay small and domain engines stay reusable."""

    def __init__(
        self,
        *,
        repository: CaseRepository,
        vault_dir: str | Path,
        inbox_dir: str | Path,
        output_dir: str | Path,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.repository = repository
        self.vault_dir = Path(vault_dir)
        self.inbox_dir = Path(inbox_dir).resolve()
        self.output_dir = Path(output_dir)
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
        credit_source_files = list(state.get("credit_source_files") or [])
        contract_source_files = list(state.get("contract_source_files") or [])
        traces: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        vault = RedactionVault(str(state["case_id"]), self.vault_dir)

        for raw_path in state.get("pending_files") or []:
            path = Path(raw_path)
            try:
                document = self.documents.extract(path)
                is_contract = self._is_contract(document)
                if document_kind == "credit" and is_contract:
                    raise ValueError("信用资料入口不接受合同文件。")
                if document_kind == "contract" and not is_contract:
                    raise ValueError("合同入口只接受合同或协议文件。")
                source_files.append(document.path)
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
            self.repository.find_valid_credit(customer.customer_name)
            if state.get("use_cached_credit", True)
            else None
        )
        if cached:
            assessment = checkpoint_dict(cached)
            return {
                "stage": "credit_ready",
                "credit_source": "cache",
                "credit_assessment": assessment,
                "effective_credit_assessment": assessment,
                "credit_status": "effective",
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
        return {
            "stage": "credit_calculated",
            "status": "credit_calculated",
            "credit_source": "new_assessment",
            "credit_assessment": checkpoint_dict(assessment),
            "effective_credit_assessment": None,
            "credit_status": "calculated",
            "trace": traces,
        }

    def prepare_credit_approval(self, state: WorkflowState) -> dict[str, Any]:
        if not state.get("credit_assessment"):
            raise ValueError("缺少模型信用计算结果，不能提交审批。")
        return {
            "stage": "credit_pending_approval",
            "status": "credit_pending_approval",
            "credit_status": "pending_approval",
            "waiting_for": "credit_approval",
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
            payload["approved_credit_limit"] = approved_limit
            payload["approved_term_days"] = approved_term
            return Command(
                update={
                    "credit_approval_request": payload,
                    "human_decision": payload,
                    "waiting_for": None,
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
        effective["recommended_term_days"] = int(approval["approved_term_days"])
        now = datetime.now(timezone.utc)
        approval.update(
            {
                "approved_at": now.isoformat(timespec="seconds"),
                "effective_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(days=180)).isoformat(timespec="seconds"),
            }
        )
        return {
            "stage": "credit_effective",
            "status": "credit_effective",
            "credit_status": "effective",
            "effective_credit_assessment": effective,
            "credit_approval": approval,
            "credit_approval_request": None,
            "waiting_for": None,
            "trace": [
                trace(
                    "credit.effective",
                    "信用审批完成，正式授信已经生效。",
                    approved_credit_limit=effective["approved_credit_limit"],
                    approved_term_days=effective["recommended_term_days"],
                )
            ],
        }

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
        reviews = []
        for item in state.get("contract_facts") or []:
            review = self.contract_engine.review(contract_facts_from_dict(item), assessment)
            reviews.append(checkpoint_dict(review))
        return {
            "stage": "contract_reviewed",
            "contract_reviews": reviews,
            "trace": [
                trace(
                    "contract.completed",
                    "合同子图已完成完整性、授信、账期和法律风险检查。",
                    contract_count=len(reviews),
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
            return Command(
                update={
                    "status": "approved_by_exception",
                    "human_decision": dict(response),
                    "waiting_for": None,
                    "trace": [trace("manager.approved", "管理层已批准本次例外。")],
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
        case = case_from_state(final_state)
        self.repository.save(case)
        reports = self.reporter.export(case, self.output_dir)
        return {
            "stage": "archived",
            "status": final_state["status"],
            "waiting_for": None,
            "reports": reports,
            "writeback": {
                "crm": "ready",
                "oa": "ready",
                "case_id": state["case_id"],
            },
            "trace": [final_trace],
        }
