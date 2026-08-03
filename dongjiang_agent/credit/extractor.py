"""Extract auditable credit facts from financial and rating report text."""

from __future__ import annotations

import re
from dataclasses import replace

from ..domain.models import CreditProfile, Evidence, ExternalRating
from .ratings import detect_agency


class CreditFactExtractor:
    _RATIO_PATTERNS = {
        "asset_liability_ratio": re.compile(r"资产负债率\s*[：:]?\s*(-?\d+(?:\.\d+)?)\s*%"),
        "net_margin": re.compile(r"(?:销售)?净利率\s*[：:]?\s*(-?\d+(?:\.\d+)?)\s*%"),
        "revenue_growth": re.compile(r"(?:营业收入|营收)(?:同比)?增长率?\s*[：:]?\s*(-?\d+(?:\.\d+)?)\s*%"),
        "on_time_payment_rate": re.compile(r"(?:按时付款率|准时付款率)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*%"),
    }
    _NUMBER_PATTERNS = {
        "current_ratio": re.compile(r"流动比率\s*[：:]?\s*(\d+(?:\.\d+)?)"),
        "registered_capital": re.compile(r"注册资本\s*[：:]?\s*(?:人民币|CNY|RMB)?\s*(\d[\d,]*(?:\.\d+)?)\s*(万元|万|元)?", re.IGNORECASE),
        "years_in_business": re.compile(r"(?:成立年限|经营年限)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*年"),
        "monthly_order_amount": re.compile(r"(?:月度订单额|月均订单额)\s*[：:]?\s*(\d[\d,]*(?:\.\d+)?)\s*(万元|万|元)?"),
        "overdue_count_12m": re.compile(r"(?:近12月|过去一年)[^。\n]{0,20}?逾期\s*(\d+)\s*次"),
        "max_overdue_days_12m": re.compile(r"(?:最长|最大)逾期\s*(\d+)\s*天"),
        "outstanding_receivables_amount": re.compile(r"(?:当前未收款金额|应收账款(?:余额)?)\s*[：:]?\s*(\d[\d,]*(?:\.\d+)?)\s*(万元|万|元)?"),
        "open_order_amount": re.compile(r"(?:在手已入单金额|在手订单金额)\s*[：:]?\s*(\d[\d,]*(?:\.\d+)?)\s*(万元|万|元)?"),
        "current_overdue_days": re.compile(r"(?:当前未收款最长逾期|当前最长逾期)\s*[：:]?\s*(\d+)\s*天"),
    }
    _RATING = re.compile(
        r"(?:主体评级|信用等级|主体信用等级)\s*[：:]?\s*"
        r"(?:(?:中债资信|中诚信国际|中诚信|联合资信)\s*)?"
        r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)\b",
        re.IGNORECASE,
    )
    _OUTLOOK = re.compile(r"(?:评级展望|展望)\s*[：:]?\s*(稳定|正面|负面|positive|negative|stable)", re.IGNORECASE)
    _RATING_DATE = re.compile(
        r"(?:评级日期|评级时间|报告日期|发布日期)\s*[：:]?\s*"
        r"(\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?)"
    )

    @staticmethod
    def _excerpt(text: str, match: re.Match[str]) -> str:
        return re.sub(r"\s+", " ", text[max(0, match.start() - 50):match.end() + 70]).strip()

    def enrich(self, profile: CreditProfile, text: str, source: str) -> CreditProfile:
        updates: dict[str, object] = {}
        evidence = list(profile.evidence)
        for field, pattern in self._RATIO_PATTERNS.items():
            match = pattern.search(text)
            if match and getattr(profile, field) is None:
                value = float(match.group(1)) / 100
                updates[field] = value
                evidence.append(Evidence(source, field, value, 0.88, self._excerpt(text, match)))
        for field, pattern in self._NUMBER_PATTERNS.items():
            match = pattern.search(text)
            if not match or getattr(profile, field) is not None:
                continue
            value = float(match.group(1).replace(",", ""))
            unit = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
            if unit in {"万元", "万"}:
                value *= 10_000
            if field in {
                "years_in_business",
                "overdue_count_12m",
                "max_overdue_days_12m",
                "current_overdue_days",
            }:
                value = int(value)
            updates[field] = value
            evidence.append(Evidence(source, field, value, 0.86, self._excerpt(text, match)))
        rating = self._RATING.search(text)
        outlook = self._OUTLOOK.search(text)
        rating_date = self._RATING_DATE.search(text)
        if rating:
            normalized_rating = rating.group(1).upper()
            if not profile.external_rating:
                updates["external_rating"] = normalized_rating
            ratings = list(profile.external_ratings)
            ratings.append(
                ExternalRating(
                    agency=detect_agency(f"{source}\n{text[:3000]}"),
                    rating=normalized_rating,
                    outlook=outlook.group(1) if outlook else "",
                    rating_date=rating_date.group(1) if rating_date else "",
                    source=source,
                    confidence=0.92,
                )
            )
            updates["external_ratings"] = ratings
            evidence.append(Evidence(source, "external_rating", rating.group(1).upper(), 0.92, self._excerpt(text, rating)))
        if outlook and not profile.rating_outlook:
            updates["rating_outlook"] = outlook.group(1)
            evidence.append(Evidence(source, "rating_outlook", outlook.group(1), 0.9, self._excerpt(text, outlook)))
        updates["evidence"] = evidence
        return replace(profile, **updates)
