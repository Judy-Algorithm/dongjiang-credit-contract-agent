"""Privacy-safe health history for the optional text model gateway."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ModelHealthStore:
    _lock = threading.Lock()

    def __init__(self, path: str | Path = "data/operations/model-health.json") -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        status: str,
        operation: str,
        model: str,
        duration_ms: int,
        http_status: int | None = None,
        error_type: str = "",
    ) -> dict[str, Any]:
        event = {
            "at": _now(),
            "status": str(status),
            "operation": str(operation),
            "model": str(model),
            "duration_ms": max(0, int(duration_ms)),
            "http_status": http_status,
            "error_type": str(error_type)[:120],
        }
        with self._lock:
            payload = self._read()
            history = list(payload.get("history") or [])[-49:] + [event]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = {"latest": event, "history": history}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)
        return event

    def summary(self) -> dict[str, Any]:
        payload = self._read()
        history = list(payload.get("history") or [])
        recent = history[-20:]
        successes = sum(item.get("status") == "healthy" for item in recent)
        return {
            "configured": bool(payload.get("configured")),
            "latest": dict(payload.get("latest") or {}),
            "recent_call_count": len(recent),
            "recent_success_rate": (
                round(successes / len(recent), 4) if recent else None
            ),
            "recent_failure_count": len(recent) - successes,
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
