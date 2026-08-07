"""Local reversible masking. Raw values never need to leave this process."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Pattern


class RedactionVault:
    _PATTERNS: tuple[tuple[str, Pattern[str]], ...] = (
        ("BANK", re.compile(r"(?<!\d)[1-9]\d{15,18}(?!\d)")),
        (
            "PHONE",
            re.compile(r"(?<!\d)\+\d{1,3}[- ]?\d(?:[ -]?\d){7,11}(?!\d)"),
        ),
        ("PHONE", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
        (
            "EMAIL",
            re.compile(
                r"(?<![A-Za-z0-9._%+-])"
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
                r"(?![A-Za-z0-9.-])"
            ),
        ),
        ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
        (
            "USCC",
            re.compile(
                r"(?<![0-9A-HJ-NPQRTUWXY])"
                r"[0-9A-HJ-NPQRTUWXY]{18}"
                r"(?![0-9A-HJ-NPQRTUWXY])"
            ),
        ),
        (
            "MONEY",
            re.compile(
                r"(?:人民币|RMB|CNY|USD|US\$|HKD|HK\$|VND|₫|¥|￥)\s*"
                r"[\d,]+(?:\.\d{1,2})?|"
                r"[\d,]+(?:\.\d{1,2})?\s*(?:元|万元|美元|港币|越南盾|"
                r"RMB|CNY|USD|HKD|VND|₫)"
            ),
        ),
        (
            "PRICE",
            re.compile(
                r"(?:单价|价格|价款|金额|信用额度|授信额度|赊销额度|"
                r"unit price|price|contract value|contract amount|credit limit|"
                r"đơn giá|giá trị hợp đồng|hạn mức tín dụng)"
                r"\s*[：:为]?\s*(?:RMB|CNY|USD|HKD|VND|US\$|HK\$|₫|¥|￥)?\s*"
                r"[\d,]+(?:\.\d{1,2})?\s*(?:元|万元|美元|港币|越南盾|"
                r"RMB|CNY|USD|HKD|VND|₫)?",
                re.IGNORECASE,
            ),
        ),
        (
            "TECH",
            re.compile(
                r"(?:技术参数|工艺参数|图纸编号|technical parameters?|"
                r"drawing number|thông số kỹ thuật|mã bản vẽ)\s*[：:]\s*"
                r"[^\n；;]{2,100}",
                re.IGNORECASE,
            ),
        ),
        (
            "PARTY",
            re.compile(
                r"(?:甲方|乙方|客户|供应商|买方|卖方|buyer|seller|customer|"
                r"supplier|purchaser|bên mua|bên bán|khách hàng|nhà cung cấp)"
                r"\s*[：:]\s*[^，,。\n；;]{2,80}",
                re.IGNORECASE,
            ),
        ),
        (
            "PARTY",
            re.compile(
                r"(?:[\u4e00-\u9fffA-Za-z0-9（）()·& -]{2,70}"
                r"(?:股份有限公司|有限责任公司|有限公司)|"
                r"(?:Công ty|CÔNG TY)[^\n；;,.]{2,70}|"
                r"[A-Z][A-Za-z0-9&'.,() -]{2,70}(?:Co\.,?\s*Ltd\.?|Ltd\.?|"
                r"Limited|Inc\.?|Corporation))",
            ),
        ),
    )

    def __init__(self, case_id: str, vault_dir: str | Path | None = None) -> None:
        self.case_id = str(case_id)
        self.mapping: dict[str, str] = {}
        self.vault_dir = Path(vault_dir) if vault_dir else None
        self._load_existing()

    def _target(self) -> Path | None:
        return (
            self.vault_dir / f"{self.case_id}.vault.json"
            if self.vault_dir is not None
            else None
        )

    def _load_existing(self) -> None:
        target = self._target()
        if target is None or not target.is_file():
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"脱敏映射文件损坏，拒绝覆盖：{target}") from exc
        if str(payload.get("case_id") or "") != self.case_id:
            raise ValueError(f"脱敏映射案件号不匹配：{target}")
        mapping = payload.get("mapping") or {}
        if not isinstance(mapping, dict):
            raise ValueError(f"脱敏映射格式无效：{target}")
        self.mapping.update({str(key): str(value) for key, value in mapping.items()})

    def _token(self, kind: str, value: str) -> str:
        digest = hashlib.sha256(f"{self.case_id}:{kind}:{value}".encode("utf-8")).hexdigest()[:10]
        return f"⟦{kind}_{digest}⟧"

    def redact(self, text: str) -> str:
        safe = str(text or "")
        for kind, pattern in self._PATTERNS:
            def replace(match: re.Match[str], label: str = kind) -> str:
                raw = match.group(0)
                token = self._token(label, raw)
                self.mapping[token] = raw
                return token
            safe = pattern.sub(replace, safe)
        return safe

    def restore(self, text: str) -> str:
        restored = str(text or "")
        ordered = sorted(
            self.mapping.items(), key=lambda item: len(item[0]), reverse=True
        )
        # A broad party token can contain phone/email tokens created earlier.
        # Expand in bounded passes so nested local tokens are fully restored.
        for _ in range(len(ordered) + 1):
            before = restored
            for token, raw in ordered:
                restored = restored.replace(token, raw)
            if restored == before:
                break
        return restored

    def persist_local(self) -> Path | None:
        if self.vault_dir is None:
            return None
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        target = self._target()
        if target is None:
            return None
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"case_id": self.case_id, "mapping": self.mapping}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target
