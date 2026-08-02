"""Text-only enhancement for locally extracted, redacted document text."""

from __future__ import annotations

from typing import Any

from .structured_extractor import StructuredFieldExtractor


class DocumentTextEnhancer:
    """Recover evidence-bound fields; it never reads images or PDF pixels."""

    prompt_version = "dongjiang-document-text-enhancer-v1"

    def __init__(self, extractor: StructuredFieldExtractor | None = None) -> None:
        self.extractor = extractor or StructuredFieldExtractor()

    @property
    def available(self) -> bool:
        return bool(self.extractor.gateway.available)

    @property
    def model(self) -> str:
        return str(self.extractor.gateway.model or "")

    def enhance(
        self,
        document_kind: str,
        *,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        fragments = list(document.get("fragments") or [])
        text = "\n".join(str(item.get("text") or "") for item in fragments)
        base = {
            "status": "not_configured",
            "mode": "text_only",
            "model": self.model,
            "prompt_version": self.prompt_version,
            "summary": "文本模型未配置，无法进行解析增强。",
            "candidates": [],
            "verification": {"status": "not_run"},
            "limitation": "只能处理本地已提取文字，不能读取图片像素或空白扫描页。",
        }
        if not self.available:
            return base
        if not text.strip():
            return {
                **base,
                "status": "not_applicable",
                "summary": "本地解析与OCR没有形成文字，文本模型无法继续处理。",
            }
        result = self.extractor.extract(
            document_kind,
            redacted_text=text,
            documents=[document],
        )
        candidates = [
            {**item, "source": "text_model_enhancement"}
            for item in result.get("candidates") or []
        ]
        return {
            **base,
            **result,
            "mode": "text_only",
            "prompt_version": self.prompt_version,
            "candidates": candidates,
            "limitation": base["limitation"],
        }
