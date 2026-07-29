"""Normalize ratings from multiple agencies and resolve conflicts conservatively."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Iterable

from ..domain.models import ExternalRating


AGENCY_ALIASES = {
    "中债资信": ("中债资信", "chinaratings"),
    "中诚信国际": ("中诚信国际", "中诚信", "ccxi"),
    "联合资信": ("联合资信", "联合信用", "lhratings"),
    "标普全球": ("标普全球", "标准普尔", "s&p"),
    "穆迪": ("穆迪", "moody"),
    "惠誉": ("惠誉", "fitch"),
}


def normalize_agency(value: str) -> str:
    raw = re.sub(r"\s+", "", value or "").lower()
    for canonical, aliases in AGENCY_ALIASES.items():
        if any(alias.lower() in raw for alias in aliases):
            return canonical
    return (value or "未识别机构").strip()


def detect_agency(text: str) -> str:
    raw = re.sub(r"\s+", "", text or "").lower()
    for canonical, aliases in AGENCY_ALIASES.items():
        if any(alias.lower() in raw for alias in aliases):
            return canonical
    return "未识别机构"


def normalize_rating(value: str) -> str:
    rating = (value or "").strip().upper()
    rating = rating.replace("（", "(").replace("）", ")").replace("级", "")
    rating = re.sub(r"\s+", "", rating)
    rating = re.sub(r"\((?:PI|预评|暂定)\)$", "", rating)
    match = re.search(r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)", rating)
    return match.group(1) if match else rating


def normalize_outlook(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"positive", "正面", "上调", "列入上调观察"}:
        return "正面"
    if raw in {"negative", "负面", "下调", "列入下调观察"}:
        return "负面"
    if raw in {"stable", "稳定"}:
        return "稳定"
    return (value or "").strip()


def _parse_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"[年/.]", "-", raw).replace("月", "-").replace("日", "")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _adjusted_score(rating: str, outlook: str, policy: dict[str, Any]) -> float | None:
    score_map = policy["external_rating_scores"]
    if rating not in score_map:
        return None
    score = float(score_map[rating])
    adjustment = policy.get("external_rating_policy", {}).get(
        "outlook_adjustments",
        {"正面": 3, "稳定": 0, "负面": -10},
    )
    return max(0.0, min(100.0, score + float(adjustment.get(outlook, 0))))


def resolve_ratings(
    ratings: Iterable[ExternalRating],
    policy: dict[str, Any],
    *,
    fallback_rating: str = "",
    fallback_outlook: str = "",
) -> dict[str, Any]:
    items = list(ratings)
    if fallback_rating and not items:
        items.append(
            ExternalRating(
                agency="手工录入/历史字段",
                rating=fallback_rating,
                outlook=fallback_outlook,
                source="credit_profile",
            )
        )

    today = datetime.now(timezone.utc).date()
    freshness_days = int(policy.get("external_rating_policy", {}).get("freshness_days", 540))
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()

    for item in items:
        agency = normalize_agency(item.agency)
        rating = normalize_rating(item.rating)
        outlook = normalize_outlook(item.outlook)
        rating_date = _parse_date(item.rating_date)
        score = _adjusted_score(rating, outlook, policy)
        if score is None:
            warnings.append(f"{agency}评级“{item.rating}”无法映射，未参与计算")
            continue
        key = (agency, rating, outlook, item.rating_date)
        if key in seen:
            continue
        seen.add(key)
        age_days = (today - rating_date).days if rating_date else None
        stale = age_days is not None and age_days > freshness_days
        if not rating_date:
            warnings.append(f"{agency}的{rating}缺少评级日期")
        elif stale:
            warnings.append(f"{agency}的{rating}已超过{freshness_days}天有效窗口")
        normalized.append(
            {
                **asdict(item),
                "agency": agency,
                "rating": rating,
                "outlook": outlook,
                "rating_date": rating_date.isoformat() if rating_date else "",
                "age_days": age_days,
                "stale": stale,
                "score": score,
            }
        )

    valid = [item for item in normalized if not item["stale"]]
    candidates = valid or normalized
    if not candidates:
        return {
            "selected": None,
            "ratings": normalized,
            "strategy": "conservative_min_score",
            "conflict": False,
            "material_conflict": False,
            "requires_manual_review": False,
            "warnings": warnings,
        }

    selected = sorted(
        candidates,
        key=lambda item: (
            float(item["score"]),
            item["age_days"] if item["age_days"] is not None else 10**9,
            -float(item.get("confidence") or 0),
        ),
    )[0]
    distinct = {item["rating"] for item in candidates}
    scores = [float(item["score"]) for item in candidates]
    conflict = len(distinct) > 1
    spread = max(scores) - min(scores)
    material_threshold = float(
        policy.get("external_rating_policy", {}).get("material_conflict_score_gap", 10)
    )
    material_conflict = conflict and spread >= material_threshold
    if conflict:
        summary = "、".join(f'{item["agency"]}{item["rating"]}' for item in candidates)
        warnings.append(f"多机构评级不一致（{summary}），按保守策略采用{selected['rating']}")
    if not valid and normalized:
        warnings.append("没有处于有效期内的评级，当前结果仅供人工复核")

    return {
        "selected": selected,
        "ratings": normalized,
        "strategy": "conservative_min_score",
        "conflict": conflict,
        "score_spread": round(spread, 2),
        "material_conflict": material_conflict,
        "requires_manual_review": material_conflict or not valid,
        "warnings": warnings,
    }
