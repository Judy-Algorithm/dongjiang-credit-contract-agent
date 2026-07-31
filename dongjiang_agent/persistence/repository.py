from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.codec import assessment_from_dict
from ..domain.models import AuditCase, CreditAssessment, CreditProfile
from ..integrations import IntegrationBundle


class CaseRepository:
    """Local case store; external OA/CRM adapters remain replaceable."""

    def __init__(self, root: str | Path = "data/cases") -> None:
        self.root = Path(root)

    def save(self, case: AuditCase) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{case.case_id}.json"
        target.write_text(json.dumps(case.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def save_dict(self, case: dict[str, Any]) -> Path:
        case_id = str(case.get("case_id") or "")
        if not case_id.startswith("DJ-") or "/" in case_id or "\\" in case_id:
            raise ValueError("案件号无效。")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{case_id}.json"
        target.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def _read_cases(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("DJ-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return rows

    @staticmethod
    def _inactive_due(case: dict[str, Any], now: datetime, inactive_days: int) -> bool:
        customer = case.get("customer") or {}
        if str(customer.get("customer_status") or "Active").lower() == "inactive":
            return False
        raw_date = str(customer.get("last_order_date") or "").strip()
        if not raw_date:
            return False
        try:
            last_order = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if last_order.tzinfo is None:
                last_order = last_order.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        no_unpaid = float(customer.get("outstanding_receivables_amount") or 0) <= 0
        no_open_orders = float(customer.get("open_order_amount") or 0) <= 0
        return no_unpaid and no_open_orders and (now - last_order).days > inactive_days

    def apply_inactivity_policy(
        self,
        *,
        as_of: datetime | None = None,
        inactive_days: int = 365,
        integrations: IntegrationBundle | None = None,
    ) -> list[str]:
        now = as_of or datetime.now(timezone.utc)
        changed: list[str] = []
        seen_customers: set[str] = set()
        for case in self._read_cases():
            customer = case.get("customer") or {}
            if case.get("credit_status") != "effective":
                continue
            identity = (
                self._normalized_identifier(customer.get("unified_social_credit_code"))
                or self._normalized_identifier(customer.get("crm_customer_id"))
                or str(customer.get("customer_name") or "").strip().upper()
            )
            if identity in seen_customers:
                continue
            seen_customers.add(identity)
            if not self._inactive_due(case, now, inactive_days):
                continue
            customer["customer_status"] = "Inactive"
            customer["customer_type"] = "inactive"
            assessment = case.get("credit_assessment") or {}
            previous = {
                "approved_credit_limit": assessment.get("approved_credit_limit"),
                "recommended_term_days": assessment.get("recommended_term_days"),
                "credit_status": case.get("credit_status"),
            }
            if assessment:
                assessment["approved_credit_limit"] = 0.0
                assessment["total_credit_limit"] = 0.0
                assessment["available_credit_amount"] = 0.0
                assessment["credit_locked"] = False
                assessment["credit_lock_reasons"] = []
            model_assessment = case.get("model_credit_assessment") or {}
            if model_assessment:
                model_assessment["approved_credit_limit"] = 0.0
                model_assessment["total_credit_limit"] = 0.0
                model_assessment["available_credit_amount"] = 0.0
            approval = case.setdefault("credit_approval", {})
            approval["inactivated_at"] = now.isoformat(timespec="seconds")
            approval["inactivation_reason"] = "超过一年无新订单且无未付款、无在手订单"
            approval["previous_credit"] = previous
            case["credit_status"] = "inactive"
            case["status"] = "inactive"
            case.setdefault("trace", []).append({
                "ts": now.isoformat(timespec="seconds"),
                "stage": "credit.inactivated",
                "message": "超过一年无新订单且无欠款，授信已清零并转Inactive。",
                "data": previous,
            })
            target = self.root / f'{case["case_id"]}.json'
            target.write_text(
                json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if integrations is not None:
                customer_id = str(
                    customer.get("crm_customer_id")
                    or customer.get("unified_social_credit_code")
                    or ""
                )
                case["writeback"] = integrations.write_back(
                    str(case["case_id"]),
                    customer_id,
                    {
                        "case_id": case["case_id"],
                        "status": "inactive",
                        "customer_status": "Inactive",
                        "approved_total_credit_limit": 0,
                        "approved_term_days": 0,
                        "inactivation_reason": approval["inactivation_reason"],
                        "inactivated_at": approval["inactivated_at"],
                    },
                    phase="inactivation",
                )
                target.write_text(
                    json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            changed.append(str(case["case_id"]))
        return changed

    def list_cases(self) -> list[dict[str, Any]]:
        self.apply_inactivity_policy()
        return self._read_cases()

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        if not case_id.startswith("DJ-") or "/" in case_id or "\\" in case_id:
            return None
        self.apply_inactivity_policy()
        target = self.root / f"{case_id}.json"
        if not target.is_file():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _normalized_identifier(value: object) -> str:
        return "".join(str(value or "").split()).upper()

    def find_valid_credit(
        self,
        customer: CreditProfile | str,
        freshness_days: int = 180,
    ) -> CreditAssessment | None:
        if isinstance(customer, CreditProfile):
            customer_name = customer.customer_name.strip()
            unified_code = self._normalized_identifier(
                customer.unified_social_credit_code
            )
            crm_customer_id = self._normalized_identifier(customer.crm_customer_id)
            if not unified_code and not crm_customer_id:
                return None
        else:
            customer_name = str(customer).strip()
            unified_code = ""
            crm_customer_id = ""
        now = datetime.now(timezone.utc)
        for case in self.list_cases():
            stored_customer = case.get("customer") or {}
            assessment = case.get("credit_assessment") or {}
            if unified_code:
                identity_matches = (
                    self._normalized_identifier(
                        stored_customer.get("unified_social_credit_code")
                    )
                    == unified_code
                )
            elif crm_customer_id:
                identity_matches = (
                    self._normalized_identifier(stored_customer.get("crm_customer_id"))
                    == crm_customer_id
                )
            else:
                identity_matches = (
                    str(stored_customer.get("customer_name") or "").strip()
                    == customer_name
                )
            if (
                not identity_matches
                or not assessment
                or case.get("credit_status") != "effective"
                or str(stored_customer.get("customer_status") or "Active").lower()
                == "inactive"
            ):
                continue
            try:
                expires_at = str((case.get("credit_approval") or {}).get("expires_at") or "")
                if expires_at:
                    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if now > expiry:
                        continue
                assessed_at = datetime.fromisoformat(str(assessment["assessed_at"]))
                if (now - assessed_at).days > freshness_days:
                    continue
                return assessment_from_dict(assessment)
            except Exception:
                continue
        return None
