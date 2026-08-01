"""Optional LLM enhancement. Only redacted text may be passed here."""

from __future__ import annotations

import json
import os
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .health import ModelHealthStore


class OpenAICompatibleGateway:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, *, health_store: ModelHealthStore | None = None) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        self.health_store = health_store or ModelHealthStore()

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
        started = perf_counter()
        try:
            with urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self._record("healthy", "analysis", started)
            return str(payload["choices"][0]["message"]["content"])
        except HTTPError as exc:
            self._record(
                "unhealthy", "analysis", started,
                http_status=exc.code, error_type=type(exc).__name__,
            )
            raise
        except Exception as exc:
            self._record(
                "unhealthy", "analysis", started, error_type=type(exc).__name__
            )
            raise

    def probe(self, *, timeout: int = 20) -> dict[str, object]:
        if not self.available:
            return self.health_store.record(
                status="not_configured",
                operation="probe",
                model=self.model,
                duration_ms=0,
                error_type="MissingApiKey",
            )
        request = Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        started = perf_counter()
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            model_ids = {
                str(item.get("id"))
                for item in payload.get("data", [])
                if item.get("id")
            }
            status = "healthy" if self.model in model_ids else "model_unavailable"
            return self._record(status, "probe", started)
        except HTTPError as exc:
            return self._record(
                "unhealthy", "probe", started,
                http_status=exc.code, error_type=type(exc).__name__,
            )
        except Exception as exc:
            return self._record(
                "unhealthy", "probe", started, error_type=type(exc).__name__
            )

    def _record(
        self,
        status: str,
        operation: str,
        started: float,
        *,
        http_status: int | None = None,
        error_type: str = "",
    ) -> dict[str, object]:
        return self.health_store.record(
            status=status,
            operation=operation,
            model=self.model,
            duration_ms=round((perf_counter() - started) * 1000),
            http_status=http_status,
            error_type=error_type,
        )
