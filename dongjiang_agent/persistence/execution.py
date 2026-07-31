"""Atomic case-level cache for idempotent dynamic task execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4


class TaskExecutionStore:
    def __init__(self, root: str | Path = "data/executions") -> None:
        self.root = Path(root)

    @staticmethod
    def _safe(value: object, label: str) -> str:
        normalized = str(value or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", normalized):
            raise ValueError(f"{label}无效。")
        return normalized

    def _target(self, case_id: object, plan_id: object, key: object) -> Path:
        case = self._safe(case_id, "案件号")
        plan = self._safe(plan_id, "计划号")
        digest = self._safe(key, "幂等键")
        if len(digest) != 64:
            raise ValueError("幂等键长度无效。")
        return self.root / case / plan / f"{digest}.json"

    def load(
        self, case_id: object, plan_id: object, key: object
    ) -> dict[str, Any] | None:
        target = self._target(case_id, plan_id, key)
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(payload.get("idempotency_key") or "") != str(key):
            return None
        return payload

    def save(
        self,
        case_id: object,
        plan_id: object,
        key: object,
        *,
        result: dict[str, Any],
        run: dict[str, Any],
    ) -> Path:
        target = self._target(case_id, plan_id, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "idempotency_key": str(key),
            "case_id": str(case_id),
            "plan_id": str(plan_id),
            "result": result,
            "run": run,
        }
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
        return target
