"""Rule-first extraction of contract facts with evidence excerpts."""

from __future__ import annotations

import re

from ..domain.models import ContractFacts, Evidence


def _number(text: str) -> float:
    value = float(text.replace(",", ""))
    return value


def _excerpt(text: str, start: int, end: int, radius: int = 80) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - radius): min(len(text), end + radius)]).strip()


class ContractFactExtractor:
    _AMOUNT = re.compile(
        r"(?:合同(?:总)?金额|总价|价款|含税金额)\s*[：:为]?\s*(?:人民币|RMB|CNY|¥|￥)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万元|万|元|美元|USD)?",
        re.IGNORECASE,
    )
    _CREDIT = re.compile(
        r"(?:授信(?:额度)?|信用额度|赊销额度)\s*[：:为]?\s*(?:人民币|RMB|CNY|¥|￥)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万元|万|元)?",
        re.IGNORECASE,
    )
    _TERM = re.compile(
        r"(?:账期|付款期限|月结)\s*[：:为]?\s*(?P<value>\d{1,3})\s*(?P<unit>天|日|个月|月)",
        re.IGNORECASE,
    )
    _TAIL_RATIO = re.compile(
        r"(?:尾款|余款)[^。\n；;]{0,30}?(?P<value>\d{1,3}(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    )
    _TAIL_TERM = re.compile(
        r"(?:尾款|余款)[^。\n；;]{0,50}?(?P<value>\d{1,3})\s*(?P<unit>天|日|个月|月|年)",
        re.IGNORECASE,
    )

    _CLAUSE_MARKERS = {
        "has_parties": ("甲方", "乙方", "买方", "卖方", "客户", "供应商"),
        "has_subject": ("合同标的", "产品", "货物", "服务内容", "模具", "技术要求"),
        "has_payment": ("付款", "支付", "价款", "账期", "月结"),
        "has_breach": ("违约", "赔偿", "违约金"),
        "has_ip": ("知识产权", "专利", "著作权", "商标", "技术成果"),
        "has_confidentiality": ("保密", "商业秘密", "不得披露"),
        "has_termination": ("解除", "终止", "合同期限"),
        "has_dispute_resolution": ("争议解决", "仲裁", "人民法院", "适用法律"),
    }

    @staticmethod
    def _money(match: re.Match[str]) -> float:
        value = _number(match.group("value"))
        unit = (match.groupdict().get("unit") or "").upper()
        if unit in {"万元", "万"}:
            value *= 10_000
        return value

    def extract(
        self,
        text: str,
        *,
        contract_name: str = "",
        customer_name: str = "",
        business_type: str = "TKP",
        language: str = "zh",
    ) -> ContractFacts:
        content = str(text or "")
        facts = ContractFacts(
            contract_name=contract_name,
            customer_name=customer_name,
            business_type=business_type.upper(),
            language=language,
            raw_text=content,
        )

        for field_name, pattern in (("amount", self._AMOUNT), ("requested_credit", self._CREDIT)):
            match = pattern.search(content)
            if match:
                value = self._money(match)
                setattr(facts, field_name, value)
                facts.evidence.append(Evidence("contract", field_name, value, 0.9, _excerpt(content, *match.span())))

        term = self._TERM.search(content)
        if term:
            value = int(term.group("value"))
            if term.group("unit") in {"个月", "月"}:
                value *= 30
            facts.payment_term_days = value
            facts.evidence.append(Evidence("contract", "payment_term_days", value, 0.9, _excerpt(content, *term.span())))

        tail_ratio = self._TAIL_RATIO.search(content)
        if tail_ratio:
            value = float(tail_ratio.group("value")) / 100
            facts.tail_payment_ratio = value
            facts.evidence.append(Evidence("contract", "tail_payment_ratio", value, 0.85, _excerpt(content, *tail_ratio.span())))

        tail_term = self._TAIL_TERM.search(content)
        if tail_term:
            value = int(tail_term.group("value"))
            if tail_term.group("unit") in {"个月", "月"}:
                value *= 30
            elif tail_term.group("unit") == "年":
                value *= 365
            facts.tail_payment_term_days = value
            facts.evidence.append(Evidence("contract", "tail_payment_term_days", value, 0.85, _excerpt(content, *tail_term.span())))

        for field_name, markers in self._CLAUSE_MARKERS.items():
            setattr(facts, field_name, any(marker in content for marker in markers))
        return facts
