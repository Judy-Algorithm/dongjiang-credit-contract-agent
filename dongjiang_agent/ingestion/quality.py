"""Explainable quality gate for locally extracted document text."""

from __future__ import annotations

import re
from typing import Any

from .extractors import ExtractedDocument


_REPLACEMENT = "\ufffd"
_MOJIBAKE = ("锟", "鏂", "娉", "绾", "鈥", "馃", "Ã", "Â")


class DocumentQualityGate:
    """Classify extraction quality without claiming visual understanding."""

    def evaluate(
        self,
        document: ExtractedDocument,
        *,
        document_kind: str,
    ) -> dict[str, Any]:
        text = str(document.text or "")
        compact = re.sub(r"\s+", "", text)
        fragments = list(document.fragments or [])
        ocr_fragments = [item for item in fragments if item.location.get("ocr")]
        confidences = [
            float(item.location["ocr_confidence"])
            for item in ocr_fragments
            if item.location.get("ocr_confidence") is not None
        ]
        average_ocr_confidence = (
            round(sum(confidences) / len(confidences), 3) if confidences else None
        )
        replacement_count = text.count(_REPLACEMENT)
        mojibake_count = sum(text.count(token) for token in _MOJIBAKE)
        suspicious_ratio = (
            round((replacement_count + mojibake_count) / max(1, len(compact)), 4)
        )
        readable_length = len(re.findall(r"[\w\u3400-\u9fff]", text))
        table_cells = sum(
            item.location.get("kind") in {"word_table_cell", "cell"}
            for item in fragments
        )
        line_count = len([line for line in text.splitlines() if line.strip()])
        warnings = list(document.warnings or [])
        reasons: list[str] = []
        score = 1.0

        if not compact or not fragments:
            score = 0.0
            reasons.append("本地解析与OCR均未形成可用文本")
        elif readable_length < 40:
            score -= 0.45
            reasons.append("可读取文字过少")
        elif readable_length < 120:
            score -= 0.2
            reasons.append("可读取文字偏少")
        if suspicious_ratio >= 0.03:
            score -= 0.4
            reasons.append("存在较多乱码或替换字符")
        elif suspicious_ratio > 0:
            score -= 0.1
            reasons.append("存在少量乱码字符")
        if average_ocr_confidence is not None and average_ocr_confidence < 0.55:
            score -= 0.4
            reasons.append("OCR平均置信度低")
        elif average_ocr_confidence is not None and average_ocr_confidence < 0.75:
            score -= 0.2
            reasons.append("OCR平均置信度一般")
        if warnings:
            score -= min(0.25, 0.08 * len(warnings))
            reasons.append("本地解析器返回警告")
        if document.media_type in {"docx", "xlsx"} and table_cells >= 8:
            if line_count <= 2 or len(fragments) < 4:
                score -= 0.25
                reasons.append("复杂表格结构可能未完整展开")

        score = round(min(1.0, max(0.0, score)), 3)
        if score >= 0.78:
            status = "passed"
        elif score >= 0.35 and compact:
            status = "needs_text_enhancement"
        else:
            status = "manual_required"
        can_use_text_model = bool(compact and fragments and readable_length >= 40)
        if status == "needs_text_enhancement" and not can_use_text_model:
            status = "manual_required"
        return {
            "status": status,
            "score": score,
            "reasons": list(dict.fromkeys(reasons)),
            "metrics": {
                "character_count": len(text),
                "readable_character_count": readable_length,
                "fragment_count": len(fragments),
                "line_count": line_count,
                "table_cell_count": table_cells,
                "ocr_fragment_count": len(ocr_fragments),
                "average_ocr_confidence": average_ocr_confidence,
                "warning_count": len(warnings),
                "suspicious_character_ratio": suspicious_ratio,
            },
            "can_use_text_model": can_use_text_model,
            "fallback": (
                "text_model"
                if status == "needs_text_enhancement" and can_use_text_model
                else "human"
                if status == "manual_required"
                else "none"
            ),
            "document_kind": document_kind,
            "evaluator": "document-quality-gate-v1",
        }
