"""Rule-first extraction of contract facts with evidence excerpts."""

from __future__ import annotations

import re

from ..domain.models import ContractFacts, Evidence


def _number(text: str) -> float:
    value = float(text.replace(",", ""))
    return value


def _excerpt(text: str, start: int, end: int, radius: int = 80) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - radius): min(len(text), end + radius)]).strip()


def _detect_language(text: str, fallback: str) -> str:
    content = str(text or "")
    if len(re.findall(r"[\u3040-\u30ff]", content)) >= 4:
        return "ja"
    lowered = content.lower()
    words = re.findall(r"[a-zÀ-ỹ]+", lowered)
    vietnamese_marks = len(re.findall(r"[ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]", lowered))
    vietnamese_words = sum(
        word in {"bên", "hợp", "đồng", "thanh", "toán", "trách", "nhiệm", "điều", "khoản"}
        for word in words
    )
    if vietnamese_marks >= 3 or vietnamese_words >= 3:
        return "vi"
    spanish_marks = len(re.findall(r"[áéíóúüñ¿¡]", lowered))
    spanish_words = sum(
        word in {"contrato", "pago", "comprador", "vendedor", "responsabilidad", "confidencial", "terminación", "jurisdicción"}
        for word in words
    )
    if spanish_marks >= 3 or spanish_words >= 3:
        return "es"
    english_words = len(re.findall(r"\b[A-Za-z]{4,}\b", content))
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content))
    if english_words >= 12 and english_words > chinese_chars:
        return "en"
    return fallback


class ContractFactExtractor:
    _AMOUNT = re.compile(
        r"(?:合同(?:总)?金额|总价|价款|含税金额|contract (?:amount|value)|total price|"
        r"value of (?:this )?contract|giá trị hợp đồng|tổng giá trị|tổng giá)"
        r"(?:\s*\|)?\s*[：:为]?\s*(?:人民币|RMB|CNY|USD|US\$|HKD|HK\$|VND|₫|¥|￥)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>万元|万|元|美元|港币|越南盾|RMB|CNY|USD|HKD|VND|₫)?",
        re.IGNORECASE,
    )
    _CREDIT = re.compile(
        r"(?:授信(?:额度)?|信用额度|赊销额度|credit limit|requested credit|"
        r"credit requested|hạn mức tín dụng|hạn mức công nợ)"
        r"(?:\s*\|)?\s*[：:为]?\s*(?:人民币|RMB|CNY|USD|US\$|HKD|HK\$|VND|₫|¥|￥)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>万元|万|元|美元|港币|越南盾|RMB|CNY|USD|HKD|VND|₫)?",
        re.IGNORECASE,
    )
    _TERM = re.compile(
        r"(?:账期|付款期限|月结|payment term|payment within|net|"
        r"thời hạn thanh toán|thanh toán trong)(?:\s*\|)?\s*[：:为]?\s*"
        r"(?P<value>\d{1,3})\s*(?P<unit>天|日|个月|月|days?|months?|ngày|tháng)",
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
    _DAMAGES_RATIO = re.compile(
        r"(?:损失赔偿(?:金)?|赔偿(?:责任|金额|上限)?|damages|indemn(?:ity|ification))"
        r"[^。\n；;]{0,80}?(?P<value>\d{1,3}(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    )
    _COMBINED_LIABILITY_RATIO = re.compile(
        r"(?:违约金[^。\n；;]{0,40}(?:损失赔偿(?:金)?|赔偿(?:金|责任)?)|"
        r"(?:损失赔偿(?:金)?|赔偿(?:金|责任)?)[^。\n；;]{0,40}违约金)"
        r"[^。\n；;]{0,80}?(?:合计|累计|总计)[^。\n；;]{0,40}?"
        r"(?P<value>\d{1,3}(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    )
    _HKD_LIABILITY_CAP = re.compile(
        r"(?:违约金|损失赔偿(?:金)?|赔偿(?:责任|金额|上限)?|累计责任|责任上限)"
        r"[^。\n；;]{0,80}?(?:港币|HKD|HK\$)\s*"
        r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万元|万|元)?",
        re.IGNORECASE,
    )
    _WARRANTY = re.compile(r"质保期|质量保证期|warranty period", re.IGNORECASE)
    _WARRANTY_SHOTS = re.compile(
        r"\d[\d,]*(?:\.\d+)?\s*(?:万)?\s*(?:啤|啤次|模次|shot(?:s)?|cycle(?:s)?)",
        re.IGNORECASE,
    )
    _WARRANTY_TIME = re.compile(
        r"(?:质保期|质量保证期|warranty period)[^。\n；;]{0,100}?"
        r"\d+(?:\.\d+)?\s*(?:天|日|个月|月|年|days?|months?|years?)",
        re.IGNORECASE,
    )
    _WARRANTY_FIRST = re.compile(
        r"(?:两者|二者|上述条件).{0,12}(?:先到|较早)|"
        r"whichever.{0,12}(?:comes|occurs).{0,8}first|earlier of",
        re.IGNORECASE,
    )
    _REPLACEMENT_WARRANTY_RESET = re.compile(
        r"(?:替代品|替换品|更换品|replacement product|replacement part)"
        r".{0,80}(?:质保期|质量保证期|warranty)"
        r".{0,60}(?:重新(?:起算|计算)|重新开始|终端成品.{0,20}(?:起算|时间为准)|"
        r"restart|recommence|begin anew)",
        re.IGNORECASE | re.DOTALL,
    )
    _SALES_COUNTRY_COMPLIANCE_SHIFT = re.compile(
        r"(?:产品销售国|销售目的国|销售地区|country of sale|destination countr(?:y|ies))"
        r".{0,100}(?:法律法规|法规要求|regulations?|laws?)"
        r".{0,100}(?:仅由|全部由|完全由|solely|exclusively)"
        r".{0,30}(?:我方|乙方|卖方|供应商|东江|supplier|seller)"
        r".{0,40}(?:负责|承担|responsib|liable)|"
        r"(?:我方|乙方|卖方|供应商|东江|supplier|seller)"
        r".{0,40}(?:solely|exclusively|仅|全部|完全)"
        r".{0,80}(?:产品销售国|销售目的国|销售地区|country of sale|destination countr(?:y|ies))"
        r".{0,80}(?:法律法规|法规要求|regulations?|laws?)",
        re.IGNORECASE | re.DOTALL,
    )

    _CLAUSE_MARKERS = {
        "has_parties": ("甲方", "乙方", "买方", "卖方", "客户", "供应商", "purchaser", "buyer", "seller", "supplier", "bên mua", "bên bán", "nhà cung cấp"),
        "has_subject": ("合同标的", "产品", "货物", "服务内容", "模具", "技术要求", "product", "goods", "services", "tooling", "hàng hóa", "sản phẩm", "khuôn"),
        "has_payment": ("付款", "支付", "价款", "账期", "月结", "payment", "price", "invoice", "thanh toán", "giá trị"),
        "has_breach": ("违约", "赔偿", "违约金", "breach", "damages", "penalty", "indemnif", "vi phạm", "bồi thường", "trách nhiệm"),
        "has_ip": ("知识产权", "专利", "著作权", "商标", "技术成果", "intellectual property", "patent", "copyright", "trademark", "sở hữu trí tuệ"),
        "has_confidentiality": ("保密", "商业秘密", "不得披露", "confidential", "non-disclosure", "trade secret", "bảo mật", "bí mật kinh doanh"),
        "has_termination": ("解除", "终止", "合同期限", "termination", "terminate", "term of this agreement", "chấm dứt", "thời hạn hợp đồng"),
        "has_dispute_resolution": ("争议解决", "仲裁", "人民法院", "适用法律", "governing law", "jurisdiction", "arbitration", "dispute", "giải quyết tranh chấp", "trọng tài", "luật áp dụng"),
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
        detected_language = _detect_language(content, language)
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
            if term.group("unit").lower() in {"个月", "月", "month", "months", "tháng"}:
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

        penalty_matches = list(self._PENALTY_RATIO.finditer(content))
        penalty_ratios = [
            float(match.group("value")) / 100 for match in penalty_matches
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

        damages_matches = list(self._DAMAGES_RATIO.finditer(content))
        damages_ratios = [
            float(match.group("value")) / 100 for match in damages_matches
        ]
        if damages_ratios:
            facts.max_damages_ratio = max(damages_ratios)
            match = max(
                self._DAMAGES_RATIO.finditer(content),
                key=lambda item: float(item.group("value")),
            )
            facts.evidence.append(
                Evidence(
                    "contract",
                    "max_damages_ratio",
                    facts.max_damages_ratio,
                    0.82,
                    _excerpt(content, *match.span()),
                )
            )
        explicit_combined = list(self._COMBINED_LIABILITY_RATIO.finditer(content))
        if explicit_combined:
            match = max(
                explicit_combined,
                key=lambda item: float(item.group("value")),
            )
            facts.combined_liability_ratio = float(match.group("value")) / 100
            facts.evidence.append(
                Evidence(
                    "contract",
                    "combined_liability_ratio",
                    facts.combined_liability_ratio,
                    0.9,
                    _excerpt(content, *match.span()),
                )
            )
        elif facts.max_penalty_ratio is not None or facts.max_damages_ratio is not None:
            penalty_value_spans = {
                match.span("value") for match in penalty_matches
            }
            damages_value_spans = {
                match.span("value") for match in damages_matches
            }
            same_percentage_reference = bool(
                penalty_value_spans.intersection(damages_value_spans)
            )
            facts.combined_liability_ratio = round(
                max(
                    float(facts.max_penalty_ratio or 0),
                    float(facts.max_damages_ratio or 0),
                )
                if same_percentage_reference
                else float(facts.max_penalty_ratio or 0)
                + float(facts.max_damages_ratio or 0),
                6,
            )
            facts.evidence.append(Evidence(
                "contract",
                "combined_liability_ratio",
                facts.combined_liability_ratio,
                0.78,
                "；".join(
                    item.excerpt
                    for item in facts.evidence
                    if item.field in {"max_penalty_ratio", "max_damages_ratio"}
                ),
            ))

        hkd_caps = list(self._HKD_LIABILITY_CAP.finditer(content))
        if hkd_caps:
            values = []
            for match in hkd_caps:
                value = _number(match.group("value"))
                if match.group("unit") in {"万元", "万"}:
                    value *= 10_000
                values.append((value, match))
            facts.liability_cap_hkd, match = max(values, key=lambda item: item[0])
            facts.evidence.append(
                Evidence(
                    "contract",
                    "liability_cap_hkd",
                    facts.liability_cap_hkd,
                    0.88,
                    _excerpt(content, *match.span()),
                )
            )

        facts.has_warranty_clause = bool(self._WARRANTY.search(content))
        if facts.has_warranty_clause:
            facts.warranty_has_shot_limit = bool(self._WARRANTY_SHOTS.search(content))
            facts.warranty_has_time_limit = bool(self._WARRANTY_TIME.search(content))
            facts.warranty_first_expiry_applies = bool(self._WARRANTY_FIRST.search(content))
        facts.replacement_warranty_resets = bool(
            self._REPLACEMENT_WARRANTY_RESET.search(content)
        )
        facts.sales_country_compliance_shifted = bool(
            self._SALES_COUNTRY_COMPLIANCE_SHIFT.search(content)
        )

        ip_clauses = re.findall(
            r"(?:知识产权|intellectual property)[^。\n；;]{0,260}",
            content,
            flags=re.IGNORECASE,
        )
        license_clauses = [
            clause
            for clause in ip_clauses
            if re.search(r"许可|授权|licen[cs]e", clause, flags=re.IGNORECASE)
        ]
        if license_clauses:
            facts.ip_license_present = True
            facts.ip_license_purpose_limited = any(re.search(
                    r"(?:仅限|仅许可).{0,20}(?:履约|履行本合同|本合同目的)|solely for.{0,30}(?:perform|purpose)|"
                    r"only for.{0,30}(?:perform|purpose)",
                    clause,
                    flags=re.IGNORECASE,
                ) for clause in license_clauses)
            facts.ip_license_term_limited = any(re.search(
                    r"(?:期限|有效期).{0,25}(?:\d+|本合同)|(?:during|for).{0,25}(?:term|year|month|day)",
                    clause,
                    flags=re.IGNORECASE,
                ) for clause in license_clauses)
            facts.ip_license_royalty_free = any(re.search(
                    r"免费|无偿|royalty[- ]free|free of charge",
                    clause,
                    flags=re.IGNORECASE,
                ) for clause in license_clauses)
            facts.ip_license_non_transferable = any(re.search(
                    r"不可转让|不得转让|non[- ]transferable|not transferable",
                    clause,
                    flags=re.IGNORECASE,
                ) for clause in license_clauses)

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
