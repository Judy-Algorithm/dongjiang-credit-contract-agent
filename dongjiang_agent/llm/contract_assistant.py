"""Structured, non-authoritative contract review assistance.

The assistant receives only locally redacted text. Its output is advisory and
is deliberately kept separate from the deterministic policy findings that
drive workflow decisions.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..domain.models import ContractFacts
from ..ingestion import locate_excerpt
from .gateway import OpenAICompatibleGateway


_LEVELS = {"low", "medium", "high", "blocker"}
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class ContractAIAssistant:
    """Call the optional text model and normalize its response safely."""

    def __init__(
        self,
        gateway: OpenAICompatibleGateway | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.gateway = gateway or OpenAICompatibleGateway()
        self.enabled = (
            enabled
            if enabled is not None
            else True
            if gateway is not None
            else os.getenv("DONGJIANG_AI_ASSISTANCE_ENABLED", "").lower()
            in {"1", "true", "yes", "on"}
        )

    def review(
        self,
        facts: ContractFacts,
        *,
        redacted_text: str,
        fragments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        base = {
            "status": "not_configured",
            "model": self.gateway.model,
            "findings": [],
            "summary": "未配置文本模型，当前仅使用制度规则审查。",
        }
        if not self.enabled or not self.gateway.available:
            return base
        if "⟦" not in redacted_text:
            return {
                **base,
                "status": "failed",
                "summary": "输入未检测到本地脱敏标记，已拒绝外发并回退到制度规则审查。",
                "error": "redaction_required",
                "error_type": "ValueError",
            }
        try:
            raw = self.gateway.analyze_redacted(
                redacted_text,
                self._instruction(facts),
            )
            payload = self._parse(raw)
            findings = self._normalize_findings(
                payload.get("findings"),
                fragments or [],
                document_id=facts.document_id,
            )
            return {
                "status": "succeeded",
                "model": self.gateway.model,
                "findings": findings,
                "summary": str(payload.get("summary") or "模型未提供摘要。")[:1000],
            }
        except Exception as exc:  # model assistance must never block policy review
            return {
                "status": "failed",
                "model": self.gateway.model,
                "findings": [],
                "summary": "文本模型调用失败，已自动回退到制度规则审查。",
                "error": str(exc)[:500],
                "error_type": type(exc).__name__,
            }

    @staticmethod
    def _instruction(facts: ContractFacts) -> str:
        return f"""请对下面已经脱敏的{facts.language}合同做辅助审查。
只输出一个 JSON 对象，不要输出 Markdown。JSON 结构必须是：
{{"summary":"不超过200字","findings":[{{"id":"AI-...","title":"风险标题","level":"low|medium|high|blocker","message":"风险解释","suggestion":"建议动作","evidence_query":"原文中可定位的短语","clause_excerpt":"不超过120字的原文片段","confidence":0.0}}]}}

要求：
1. 只报告合同文本中确实出现的内容，不猜测缺失事实；最多返回 8 项。
2. evidence_query 或 clause_excerpt 必须引用合同原文；无法引用时不要输出该项。
3. 这是辅助发现，不得声称批准、拒绝或改变授信决策。
4. 不要复述客户名称、账号、电话或金额等敏感信息；使用原文中的脱敏标记。
5. 优先关注付款、责任、交付、知识产权、保密、终止、争议解决和授信交叉风险。"""

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        match = _JSON_BLOCK.search(text)
        if match:
            text = match.group(1).strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("模型输出不是 JSON 对象")
        return payload

    @staticmethod
    def _normalize_findings(
        raw_findings: Any,
        fragments: list[dict[str, Any]],
        *,
        document_id: str = "",
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_findings, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_findings[:8], start=1):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()[:160]
            message = str(raw.get("message") or "").strip()[:1000]
            suggestion = str(raw.get("suggestion") or "").strip()[:1000]
            query = str(raw.get("evidence_query") or raw.get("clause_excerpt") or "").strip()[:240]
            if not title or not message or not query:
                continue
            key = str(raw.get("id") or f"AI-{index:02d}").strip()[:60]
            if key in seen:
                key = f"{key}-{index}"
            seen.add(key)
            level = str(raw.get("level") or "medium").lower()
            if level not in _LEVELS:
                level = "medium"
            try:
                confidence = float(raw.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = min(1.0, max(0.0, confidence))
            item: dict[str, Any] = {
                "finding_id": key,
                "title": title,
                "level": level,
                "message": message,
                "suggestion": suggestion,
                "evidence_query": query,
                "clause_excerpt": str(raw.get("clause_excerpt") or "")[:500],
                "confidence": round(confidence, 3),
                "source": "text_model",
            }
            matched = locate_excerpt(query, fragments)
            if matched:
                item.update(
                    {
                        "document_id": document_id,
                        "fragment_id": str(matched.get("fragment_id") or ""),
                        "location": dict(matched.get("location") or {}),
                    }
                )
            normalized.append(item)
        return normalized
