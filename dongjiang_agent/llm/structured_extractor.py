"""Evidence-bound structured field candidates from locally redacted documents."""

from __future__ import annotations

import json
import re
from typing import Any

from ..ingestion import locate_excerpt
from .gateway import OpenAICompatibleGateway


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)

FIELD_SCHEMAS: dict[str, dict[str, tuple[str, float | None, float | None]]] = {
    "credit": {
        "asset_liability_ratio": ("float", 0, 5),
        "net_margin": ("float", -5, 5),
        "current_ratio": ("float", 0, 100),
        "revenue_growth": ("float", -5, 10),
        "years_in_business": ("int", 0, 500),
        "external_rating": ("rating", None, None),
        "rating_outlook": ("outlook", None, None),
        "cooperation_years": ("float", 0, 500),
        "overdue_count_12m": ("int", 0, 100000),
        "max_overdue_days_12m": ("int", 0, 100000),
        "on_time_payment_rate": ("float", 0, 1),
        "current_overdue_days": ("int", 0, 100000),
        "last_order_date": ("date", None, None),
        "major_litigation": ("bool", None, None),
        "tax_or_enforcement_alert": ("bool", None, None),
    },
    "contract": {
        "payment_term_days": ("int", 0, 3650),
        "tail_payment_ratio": ("float", 0, 1),
        "tail_payment_term_days": ("int", 0, 3650),
        "uses_purchase_exemption": ("bool", None, None),
        "contract_term_years": ("float", 0, 100),
        "max_penalty_ratio": ("float", 0, 10),
        "language": ("language", None, None),
        "has_parties": ("bool", None, None),
        "has_subject": ("bool", None, None),
        "has_payment": ("bool", None, None),
        "has_breach": ("bool", None, None),
        "has_ip": ("bool", None, None),
        "has_confidentiality": ("bool", None, None),
        "has_termination": ("bool", None, None),
        "has_dispute_resolution": ("bool", None, None),
    },
}

FIELD_LABELS = {
    "asset_liability_ratio": "资产负债率",
    "net_margin": "净利率",
    "current_ratio": "流动比率",
    "revenue_growth": "营收增长率",
    "years_in_business": "成立年限",
    "external_rating": "外部评级",
    "rating_outlook": "评级展望",
    "cooperation_years": "合作年限",
    "overdue_count_12m": "近12月逾期次数",
    "max_overdue_days_12m": "近12月最长逾期天数",
    "on_time_payment_rate": "按时付款率",
    "current_overdue_days": "当前逾期天数",
    "last_order_date": "最近订单日期",
    "major_litigation": "重大诉讼",
    "tax_or_enforcement_alert": "税务或执行风险",
    "payment_term_days": "合同账期",
    "tail_payment_ratio": "尾款比例",
    "tail_payment_term_days": "尾款账期",
    "uses_purchase_exemption": "首期采购款豁免",
    "contract_term_years": "合同期限",
    "max_penalty_ratio": "违约金上限比例",
    "language": "合同语言",
    "has_parties": "合同主体",
    "has_subject": "合同标的",
    "has_payment": "付款条款",
    "has_breach": "违约责任",
    "has_ip": "知识产权",
    "has_confidentiality": "保密条款",
    "has_termination": "解除终止",
    "has_dispute_resolution": "争议解决",
}


class StructuredFieldExtractor:
    prompt_version = "dongjiang-structured-extraction-v1"

    def __init__(self, gateway: OpenAICompatibleGateway | None = None) -> None:
        self.gateway = gateway or OpenAICompatibleGateway()

    def extract(
        self,
        document_kind: str,
        *,
        redacted_text: str,
        documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if document_kind not in FIELD_SCHEMAS:
            raise ValueError("结构化提取类型必须是credit或contract。")
        if not self.gateway.available:
            raise RuntimeError("文本模型尚未配置。")
        text = redacted_text
        if "⟦" not in text:
            text = "⟦REDACTED_TEXT⟧\n" + text
        raw = self.gateway.analyze_redacted(text, self._instruction(document_kind))
        payload = self._parse(raw)
        candidates = self._normalize(
            document_kind, payload.get("candidates"), documents
        )
        checks = {
            "schema_valid": bool(candidates),
            "all_fields_allowlisted": all(
                item["field"] in FIELD_SCHEMAS[document_kind]
                for item in candidates
            ),
            "all_evidence_located": bool(candidates)
            and all(item.get("document_id") and item.get("fragment_id") for item in candidates),
            "confidence_valid": all(0 <= float(item["confidence"]) <= 1 for item in candidates),
        }
        return {
            "status": "succeeded",
            "model": self.gateway.model,
            "prompt_version": self.prompt_version,
            "summary": str(payload.get("summary") or "结构化字段候选已生成。")[:500],
            "candidates": candidates,
            "verification": {
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
                "candidate_count": len(candidates),
                "located_count": sum(
                    bool(item.get("document_id") and item.get("fragment_id"))
                    for item in candidates
                ),
                "verifier": "independent_extraction_guard",
            },
        }

    @staticmethod
    def _instruction(document_kind: str) -> str:
        fields = sorted(FIELD_SCHEMAS[document_kind])
        return f"""从下面已经脱敏的{'信用资料' if document_kind == 'credit' else '合同'}中提取结构化字段候选。
只输出JSON对象，不要Markdown：
{{"summary":"不超过120字","candidates":[{{"field":"字段名","value":"字段值","evidence_query":"原文中可精确定位的短语","confidence":0.0}}]}}

只允许字段：{', '.join(fields)}。
要求：
1. 只提取原文明确出现的值，不推测、不计算缺失值；最多20项。
2. evidence_query必须逐字引用脱敏原文中的短语，不能引用客户名称、账号、电话、邮箱或金额。
3. 比例统一输出0到1的小数；布尔值输出true或false；日期输出YYYY-MM-DD。
4. 无法提供原文证据时不要返回该候选。
5. 这只是待人工确认的候选，不得输出审批或风险结论。"""

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        match = _JSON_BLOCK.search(text)
        if match:
            text = match.group(1).strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("模型结构化提取输出不是JSON对象。")
        return payload

    def _normalize(
        self,
        document_kind: str,
        raw_candidates: Any,
        documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_candidates, list):
            return []
        schema = FIELD_SCHEMAS[document_kind]
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_candidates[:20]:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("field") or "").strip()
            query = str(raw.get("evidence_query") or "").strip()[:240]
            if field not in schema or not query or field in seen:
                continue
            try:
                value = self._value(field, raw.get("value"), schema[field])
                confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
            except (TypeError, ValueError):
                continue
            located: dict[str, Any] | None = None
            document_id = ""
            for document in documents:
                match = locate_excerpt(query, list(document.get("fragments") or []))
                if match:
                    located = match
                    document_id = str(document.get("document_id") or "")
                    break
            if not located or not document_id:
                continue
            seen.add(field)
            normalized.append(
                {
                    "candidate_id": f"FIELD-{len(normalized) + 1:02d}",
                    "field": field,
                    "label": FIELD_LABELS.get(field, field),
                    "value": value,
                    "confidence": round(confidence, 3),
                    "evidence_query": query,
                    "document_id": document_id,
                    "fragment_id": str(located.get("fragment_id") or ""),
                    "location": dict(located.get("location") or {}),
                }
            )
        return normalized

    @staticmethod
    def _value(
        field: str, value: Any, spec: tuple[str, float | None, float | None]
    ) -> Any:
        kind, minimum, maximum = spec
        if kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"true", "yes", "1", "是", "有"}:
                return True
            if text in {"false", "no", "0", "否", "无"}:
                return False
            raise ValueError(f"{field}不是布尔值")
        if kind in {"float", "int"}:
            number = float(value)
            if minimum is not None and number < minimum:
                raise ValueError(f"{field}低于允许范围")
            if maximum is not None and number > maximum:
                raise ValueError(f"{field}高于允许范围")
            return int(number) if kind == "int" else round(number, 6)
        text = str(value or "").strip()[:80]
        if not text or "⟦" in text:
            raise ValueError(f"{field}包含空值或脱敏令牌")
        if kind == "rating" and not re.fullmatch(
            r"(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)",
            text.upper(),
        ):
            raise ValueError("评级格式无效")
        if kind == "outlook" and text.lower() not in {
            "stable", "positive", "negative", "稳定", "正面", "负面"
        }:
            raise ValueError("评级展望无效")
        if kind == "language" and text.lower() not in {
            "zh", "en", "vi", "ja", "es", "bilingual",
            "中文", "英文", "越南语", "日语", "西班牙语", "中英双语"
        }:
            raise ValueError("合同语言无效")
        if kind == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("日期格式无效")
        return text
