"""Optional LLM enhancement. Only redacted text may be passed here."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


class OpenAICompatibleGateway:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def analyze_redacted(self, redacted_text: str, instruction: str) -> str:
        if "⟦" not in redacted_text:
            raise ValueError("拒绝发送：输入未经过本地可逆脱敏。")
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "你是制造业合同风控助手。不得猜测缺失数据，结论必须引用原文。"},
                {"role": "user", "content": f"{instruction}\n\n合同（已脱敏）：\n{redacted_text}"},
            ],
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"])
