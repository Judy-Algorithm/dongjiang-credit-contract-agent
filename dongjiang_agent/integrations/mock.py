"""Persistent, explicit enterprise-system mocks for repeatable demos."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..domain.models import utc_now


class MockEnterpriseStore:
    _lock = threading.Lock()

    def __init__(self, root: str | Path = "data/mock-enterprise") -> None:
        self.root = Path(root)

    def call(
        self,
        system: str,
        operation: str,
        entity_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        target = self.root / system / f"{operation}.json"
        with self._lock:
            rows = self._read(target)
            existing = next(
                (
                    item
                    for item in rows
                    if item.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing:
                return {**existing["response"], "reused": True}
            response = {
                "mock": True,
                "system": system,
                "operation": operation,
                "reference_id": f"MOCK-{system.upper()}-{len(rows) + 1:06d}",
                "entity_id": entity_id,
                "accepted_at": utc_now(),
                "field_summary": self._field_summary(payload),
                "reused": False,
            }
            rows.append(
                {
                    "idempotency_key": idempotency_key,
                    "entity_id": entity_id,
                    "response": response,
                }
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(target)
        return response

    @staticmethod
    def _read(target: Path) -> list[dict[str, Any]]:
        if not target.is_file():
            return []
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
            return [dict(item) for item in value if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _field_summary(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "case_id",
            "status",
            "customer_status",
            "business_type",
            "tkm_business_subtype",
            "approved_total_credit_limit",
            "approved_term_days",
            "purchase_exemption_approved",
            "approval_scope",
            "effective_at",
            "expires_at",
            "oa_evidence_id",
            "writeback_phase",
        }
        return {key: payload.get(key) for key in sorted(allowed) if key in payload}


class MockCRMAdapter:
    def __init__(self, store: MockEnterpriseStore | None = None) -> None:
        self.store = store or MockEnterpriseStore()

    def read_customer(self, customer_id: str) -> dict[str, Any]:
        return {"mock": True, "customer_id": customer_id, "status": "Active"}

    def write_credit_decision(
        self, customer_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        case_id = str(payload.get("case_id") or "")
        phase = str(payload.get("writeback_phase") or "final")
        return self.store.call(
            "crm", "credit-decisions", customer_id, payload,
            idempotency_key=f"{case_id}:crm:credit:{phase}",
        )


class MockOAAdapter:
    def __init__(self, store: MockEnterpriseStore | None = None) -> None:
        self.store = store or MockEnterpriseStore()

    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.call(
            "oa", "submit-review", case_id, payload,
            idempotency_key=f"{case_id}:oa:submit",
        )

    def write_result(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        phase = str(payload.get("writeback_phase") or "final")
        return self.store.call(
            "oa", "write-result", case_id, payload,
            idempotency_key=f"{case_id}:oa:result:{phase}",
        )


class MockSAPAdapter:
    def __init__(self, store: MockEnterpriseStore | None = None) -> None:
        self.store = store or MockEnterpriseStore()

    def write_credit_control(
        self, customer_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        case_id = str(payload.get("case_id") or "")
        phase = str(payload.get("writeback_phase") or "final")
        return self.store.call(
            "sap", "credit-control", customer_id, payload,
            idempotency_key=f"{case_id}:sap:credit:{phase}",
        )
