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
    _CONTRACT_TERM = re.compile(
        r"(?:合同(?:有效期|期限)|协议(?:有效期|期限)|term of (?:this )?(?:agreement|contract))"
        r"[^。\n；;]{0,50}?(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>年|个月|月|years?|months?)",
        re.IGNORECASE,
    )
    _PENALTY_RATIO = re.compile(
        r"(?:违约金|penalt(?:y|ies)|liquidated damages)"
        r"[^。\n；;]{0,80}?(?P<value>\d{1,3}(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    )

    _CLAUSE_MARKERS = {
        "has_parties": ("甲方", "乙方", "买方", "卖方", "客户", "供应商", "purchaser", "buyer", "seller", "supplier"),
        "has_subject": ("合同标的", "产品", "货物", "服务内容", "模具", "技术要求", "product", "goods", "services", "tooling"),
        "has_payment": ("付款", "支付", "价款", "账期", "月结", "payment", "price", "invoice"),
        "has_breach": ("违约", "赔偿", "违约金", "breach", "damages", "penalty", "indemnif"),
        "has_ip": ("知识产权", "专利", "著作权", "商标", "技术成果", "intellectual property", "patent", "copyright", "trademark"),
        "has_confidentiality": ("保密", "商业秘密", "不得披露", "confidential", "non-disclosure", "trade secret"),
        "has_termination": ("解除", "终止", "合同期限", "termination", "terminate", "term of this agreement"),
        "has_dispute_resolution": ("争议解决", "仲裁", "人民法院", "适用法律", "governing law", "jurisdiction", "arbitration", "dispute"),
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
        english_words = len(re.findall(r"\b[A-Za-z]{4,}\b", content))
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content))
        detected_language = (
            "en" if english_words >= 12 and english_words > chinese_chars else language
        )
        facts = ContractFacts(
            contract_name=contract_name,
            customer_name=customer_name,
            business_type=business_type.upper(),
            language=detected_language,
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

        contract_term = self._CONTRACT_TERM.search(content)
        if contract_term:
            value = float(contract_term.group("value"))
            if contract_term.group("unit").lower() in {"个月", "月", "month", "months"}:
                value /= 12
            facts.contract_term_years = round(value, 3)
            facts.evidence.append(
                Evidence(
                    "contract",
                    "contract_term_years",
                    facts.contract_term_years,
                    0.82,
                    _excerpt(content, *contract_term.span()),
                )
            )

        penalty_ratios = [
            float(match.group("value")) / 100
            for match in self._PENALTY_RATIO.finditer(content)
        ]
        if penalty_ratios:
            facts.max_penalty_ratio = max(penalty_ratios)
            match = max(
                self._PENALTY_RATIO.finditer(content),
                key=lambda item: float(item.group("value")),
            )
            facts.evidence.append(
                Evidence(
                    "contract",
                    "max_penalty_ratio",
                    facts.max_penalty_ratio,
                    0.82,
                    _excerpt(content, *match.span()),
                )
            )

        lowered = content.lower()
        for field_name, markers in self._CLAUSE_MARKERS.items():
            setattr(facts, field_name, any(marker.lower() in lowered for marker in markers))
        facts.uses_purchase_exemption = bool(re.search(
            r"(?:未收|无需收|不收).{0,20}(?:首期款|首付款|定金).{0,30}(?:采购|购买).{0,10}(?:物料|材料)|"
            r"(?:首期款|首付款|定金).{0,20}(?:采购豁免|不受控)",
            content,
            flags=re.IGNORECASE,
        ))
        return facts
