"""Match extracted evidence excerpts to structured document fragments."""

from __future__ import annotations

import re
from typing import Any, Iterable


def _normalize(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def locate_excerpt(
    excerpt: str,
    fragments: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    needle = _normalize(excerpt)
    rows = [dict(item) for item in fragments]
    if not needle or not rows:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for row in rows:
        haystack = _normalize(row.get("text"))
        if not haystack:
            continue
        if needle in haystack:
            score = 1.0
        elif haystack in needle:
            position = needle.find(haystack)
            fragment_center = position + len(haystack) / 2
            excerpt_center = len(needle) / 2
            proximity = max(
                0.0,
                1.0 - abs(fragment_center - excerpt_center) / max(len(needle) / 2, 1),
            )
            coverage = len(haystack) / len(needle)
            score = 0.75 * proximity + 0.25 * coverage
        else:
            tokens = {
                token
                for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", str(excerpt).lower())
                if len(token) >= 2
            }
            if not tokens:
                continue
            score = sum(1 for token in tokens if _normalize(token) in haystack) / len(tokens)
        if best is None or score > best[0]:
            best = (score, row)
    if best is None or best[0] < 0.18:
        return None
    return {
        "fragment_id": str(best[1].get("fragment_id") or ""),
        "location": dict(best[1].get("location") or {}),
        "score": round(best[0], 3),
    }


def location_label(location: dict[str, Any] | None) -> str:
    item = dict(location or {})
    kind = item.get("kind")
    if kind == "page":
        return f"第 {item.get('page')} 页"
    if kind == "paragraph":
        return f"第 {item.get('paragraph')} 段"
    if kind == "cell":
        return f"{item.get('sheet')}!{item.get('cell')}"
    if kind == "line":
        return f"第 {item.get('line')} 行"
    if kind == "image":
        return "图片 OCR"
    return "文档原文"
