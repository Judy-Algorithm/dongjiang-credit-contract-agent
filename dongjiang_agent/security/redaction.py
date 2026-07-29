"""Local reversible masking. Raw values never need to leave this process."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Pattern


class RedactionVault:
    _PATTERNS: tuple[tuple[str, Pattern[str]], ...] = (
        ("BANK", re.compile(r"\b[1-9]\d{15,18}\b")),
        ("PHONE", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
        ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
        ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
        ("USCC", re.compile(r"\b[0-9A-HJ-NPQRTUWXY]{18}\b")),
        ("MONEY", re.compile(r"(?:人民币|RMB|CNY|¥|￥)\s*[\d,]+(?:\.\d{1,2})?")),
        (
            "PRICE",
            re.compile(
                r"(?:单价|价格|价款|金额|信用额度|授信额度|赊销额度)"
                r"\s*[：:为]?\s*[\d,]+(?:\.\d{1,2})?\s*(?:元|万元|美元)?"
            ),
        ),
        ("TECH", re.compile(r"(?:技术参数|工艺参数|图纸编号)\s*[：:]\s*[^\n；;]{2,80}")),
        ("PARTY", re.compile(r"(?:甲方|乙方|客户|供应商|买方|卖方)\s*[：:]\s*[^，,。\n；;]{2,60}")),
    )

    def __init__(self, case_id: str, vault_dir: str | Path | None = None) -> None:
        self.case_id = str(case_id)
        self.mapping: dict[str, str] = {}
        self.vault_dir = Path(vault_dir) if vault_dir else None

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
        for token, raw in sorted(self.mapping.items(), key=lambda item: len(item[0]), reverse=True):
            restored = restored.replace(token, raw)
        return restored

    def persist_local(self) -> Path | None:
        if self.vault_dir is None:
            return None
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        target = self.vault_dir / f"{self.case_id}.vault.json"
        target.write_text(
            json.dumps({"case_id": self.case_id, "mapping": self.mapping}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target
