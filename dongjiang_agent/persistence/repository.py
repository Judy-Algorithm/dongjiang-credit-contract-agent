from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.codec import assessment_from_dict
from ..domain.models import AuditCase, CreditAssessment


class CaseRepository:
    """Local case store; external OA/CRM adapters remain replaceable."""

    def __init__(self, root: str | Path = "data/cases") -> None:
        self.root = Path(root)

    def save(self, case: AuditCase) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{case.case_id}.json"
        target.write_text(json.dumps(case.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def list_cases(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("DJ-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return rows

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        if not case_id.startswith("DJ-") or "/" in case_id or "\\" in case_id:
            return None
        target = self.root / f"{case_id}.json"
        if not target.is_file():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return None

    def find_valid_credit(self, customer_name: str, freshness_days: int = 180) -> CreditAssessment | None:
        now = datetime.now(timezone.utc)
        for case in self.list_cases():
            customer = case.get("customer") or {}
            assessment = case.get("credit_assessment") or {}
            if (
                str(customer.get("customer_name") or "").strip() != customer_name.strip()
                or not assessment
                or case.get("credit_status") != "effective"
            ):
                continue
            try:
                assessed_at = datetime.fromisoformat(str(assessment["assessed_at"]))
                if (now - assessed_at).days > freshness_days:
                    continue
                return assessment_from_dict(assessment)
            except Exception:
                continue
        return None
