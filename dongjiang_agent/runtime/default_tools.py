"""Registration of the current local capabilities as Toolhub-style tools."""

from __future__ import annotations

from typing import Any

from ..domain.codec import assessment_from_dict, contract_facts_from_dict, profile_from_dict
from ..workflow.codec import checkpoint_dict
from .registry import LocalToolRegistry, ToolDescriptor


def register_default_tools(
    registry: LocalToolRegistry,
    *,
    documents: Any,
    credit_facts: Any,
    credit_engine: Any,
    contract_facts: Any,
    contract_engine: Any,
    contract_ai: Any,
    document_quality: Any | None = None,
    document_enhancer: Any | None = None,
) -> LocalToolRegistry:
    """Expose existing deterministic engines and AI assistance through one port."""

    def register(
        tool_name: str,
        name: str,
        description: str,
        allowed_agents: tuple[str, ...],
        handler: Any,
        *,
        risk_level: str = "read_only",
    ) -> None:
        registry.register(
            ToolDescriptor(
                tool_name=tool_name,
                name=name,
                description=description,
                allowed_agents=allowed_agents,
                risk_level=risk_level,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
            handler,
        )

    register(
        "document.extract",
        "文档结构解析",
        "解析Word、PDF、Excel、图片和文本并保留证据坐标。",
        ("case_orchestrator",),
        lambda payload: document_payload(documents.extract(payload["path"])),
    )
    if document_quality is not None:
        register(
            "document.quality_gate",
            "文档解析质量门禁",
            "评价本地解析、OCR、乱码、表格结构和证据片段质量。",
            ("case_orchestrator",),
            lambda payload: document_quality.evaluate(
                _document_from_payload(payload["document"]),
                document_kind=str(payload.get("document_kind") or "credit"),
            ),
        )
    if document_enhancer is not None:
        register(
            "document.text_enhance",
            "文本模型解析增强",
            "仅对本地已提取并脱敏的文字进行字段补强，不读取图片像素。",
            ("case_orchestrator",),
            lambda payload: document_enhancer.enhance(
                str(payload.get("document_kind") or "credit"),
                document=dict(payload["document"]),
            ),
        )
    register(
        "credit.extract_facts",
        "信用字段提取",
        "从信用资料正文补充客户信用档案。",
        ("case_orchestrator", "credit_review"),
        lambda payload: checkpoint_dict(
            credit_facts.enrich(
                profile_from_dict(payload["profile"]),
                str(payload.get("text") or ""),
                str(payload.get("source_name") or ""),
            )
        ),
    )
    register(
        "contract.extract_facts",
        "合同事实提取",
        "提取合同主体、金额、账期和关键条款。",
        ("case_orchestrator", "contract_review"),
        lambda payload: checkpoint_dict(
            contract_facts.extract(
                str(payload.get("text") or ""),
                contract_name=str(payload.get("contract_name") or ""),
                customer_name=str(payload.get("customer_name") or ""),
                business_type=str(payload.get("business_type") or ""),
            )
        ),
    )
    register(
        "credit.assess",
        "信用评分与额度测算",
        "按照受控信用政策计算评分、额度、账期和补件要求。",
        ("credit_review", "case_orchestrator"),
        lambda payload: checkpoint_dict(
            credit_engine.assess(profile_from_dict(payload["profile"]))
        ),
    )
    register(
        "credit.control",
        "额度占用与逾期控制",
        "计算已占用额度、可用额度和信用锁定原因。",
        ("credit_review", "case_orchestrator"),
        lambda payload: credit_engine.credit_control(
            profile_from_dict(payload["profile"]),
            float(payload.get("approved_limit") or 0),
        ),
    )
    register(
        "credit.analyze_dimension",
        "信用维度分析",
        "按动态任务分析资料完整性、财务、评级、交易和TKM条件。",
        ("credit_review",),
        lambda payload: credit_dimension_result(
            credit_engine,
            str(payload["task_type"]),
            profile_from_dict(payload["profile"]),
        ),
    )
    register(
        "contract.policy_review",
        "合同制度规则审查",
        "使用确定性制度规则审查合同并交叉核对正式授信。",
        ("contract_review",),
        lambda payload: checkpoint_dict(
            contract_engine.review(
                contract_facts_from_dict(payload["facts"]),
                assessment_from_dict(payload["assessment"]),
            )
        ),
    )
    register(
        "contract.ai_review",
        "合同AI辅助审查",
        "调用文本模型识别规则难以覆盖的语义风险，结果仅作辅助。",
        ("contract_review",),
        lambda payload: contract_ai.review(
            contract_facts_from_dict(payload["facts"]),
            redacted_text=str(payload.get("redacted_text") or ""),
            fragments=list(payload.get("fragments") or []),
        ),
    )
    return registry


def document_payload(document: Any) -> dict[str, Any]:
    return {
        "path": document.path,
        "media_type": document.media_type,
        "text": document.text,
        "extractor": document.extractor,
        "warnings": list(document.warnings),
        "fragments": [
            {
                "fragment_id": item.fragment_id,
                "text": item.text,
                "location": dict(item.location),
            }
            for item in document.fragments
        ],
        "_tool_summary": (
            f"{document.media_type}经{document.extractor}解析，"
            f"形成{len(document.fragments)}个证据片段"
        ),
    }


def _document_from_payload(payload: dict[str, Any]) -> Any:
    from ..ingestion import DocumentFragment, ExtractedDocument

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


def credit_dimension_result(
    engine: Any, task_type: str, profile: Any
) -> dict[str, Any]:
    if task_type == "credit_data_completeness":
        assessment = engine.assess(profile)
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
        executor = "credit-policy-engine"
    elif task_type == "credit_financial_analysis":
        missing: list[str] = []
        score, reasons = engine._financial_score(profile, missing)
        payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
        summary = f"财务维度评分{score:.1f}" if score is not None else "财务指标不足"
        executor = "financial-analysis-tool"
    elif task_type == "credit_rating_analysis":
        missing = []
        score, reasons, resolution = engine._rating_score(profile, missing)
        selected = dict(resolution.get("selected") or {})
        payload = {
            "score": score,
            "agency": selected.get("agency"),
            "rating": selected.get("rating"),
            "conflict": bool(resolution.get("conflict")),
            "requires_manual_review": bool(resolution.get("requires_manual_review")),
            "warning_count": len(reasons),
        }
        summary = f"采用{selected.get('agency') or '未知机构'} {selected.get('rating') or '未评级'}"
        executor = "rating-analysis-tool"
    elif task_type == "credit_cooperation_analysis":
        missing = []
        score, reasons = engine._cooperation_score(profile, missing)
        payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
        summary = f"历史交易评分{score:.1f}" if score is not None else "历史交易资料不足"
        executor = "cooperation-analysis-tool"
    elif task_type == "credit_enterprise_analysis":
        missing = []
        score, reasons = engine._enterprise_score(profile, missing)
        payload = {"score": score, "missing_fields": missing, "reason_count": len(reasons)}
        summary = f"企业基础评分{score:.1f}" if score is not None else "企业基础资料不足"
        executor = "enterprise-analysis-tool"
    elif task_type == "credit_control_analysis":
        limit = max(0.0, float(profile.requested_credit_limit or profile.monthly_order_amount or 0))
        control = engine.credit_control(profile, limit)
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
        executor = "credit-control-tool"
    elif task_type == "credit_tkm_analysis":
        payload = {
            "business_subtype": profile.tkm_business_subtype or "policy_default",
            "purchase_exemption_requested": bool(profile.purchase_exemption_requested),
            "requested_term_days": profile.requested_term_days,
        }
        summary = "已核对TKM子类型、账期和首期采购款豁免申请"
        executor = "tkm-policy-tool"
    else:
        raise ValueError(f"未授权的信用分析任务：{task_type}")
    return {
        "payload": payload,
        "summary": summary,
        "executor": executor,
        "_tool_summary": summary,
    }
