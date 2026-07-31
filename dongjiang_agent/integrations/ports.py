"""Vendor-neutral ports and production-capable HTTP adapters for OA/CRM/SAP."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..domain.models import utc_now


class CRMPort(Protocol):
    def read_customer(self, customer_id: str) -> dict[str, Any]: ...
    def write_credit_decision(self, customer_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class OAPort(Protocol):
    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def write_result(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class SAPPort(Protocol):
    def write_credit_control(self, customer_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class IntegrationError(RuntimeError):
    pass


class _HTTPAdapter:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout_seconds: float = 15,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.extra_headers = dict(extra_headers or {})

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                result = json.loads(body or "{}")
                return result if isinstance(result, dict) else {"data": result}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise IntegrationError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise IntegrationError(f"连接失败：{exc}") from exc


class WebhookCRMAdapter(_HTTPAdapter):
    def read_customer(self, customer_id: str) -> dict[str, Any]:
        return self._request("GET", f"customers/{customer_id}")

    def write_credit_decision(self, customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = str(payload.get("case_id") or "")
        phase = str(payload.get("writeback_phase") or "final")
        return self._request(
            "POST",
            f"customers/{customer_id}/credit-decisions",
            payload,
            idempotency_key=f"{case_id}:crm:credit:{phase}",
        )


class WebhookOAAdapter(_HTTPAdapter):
    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"contract-reviews/{case_id}",
            payload,
            idempotency_key=f"{case_id}:oa:submit",
        )

    def write_result(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        phase = str(payload.get("writeback_phase") or "final")
        return self._request(
            "POST",
            f"credit-reviews/{case_id}/result",
            payload,
            idempotency_key=f"{case_id}:oa:result:{phase}",
        )


class WebhookSAPAdapter(_HTTPAdapter):
    def write_credit_control(self, customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = str(payload.get("case_id") or "")
        phase = str(payload.get("writeback_phase") or "final")
        return self._request(
            "POST",
            f"business-partners/{customer_id}/credit-control",
            payload,
            idempotency_key=f"{case_id}:sap:credit:{phase}",
        )


@dataclass(slots=True)
class IntegrationBundle:
    oa: OAPort | None = None
    crm: CRMPort | None = None
    sap: SAPPort | None = None
    audit_root: Path = Path("data/integrations")

    @classmethod
    def from_environment(
        cls, *, audit_root: str | Path = "data/integrations"
    ) -> "IntegrationBundle":
        def headers(prefix: str) -> dict[str, str]:
            raw = os.getenv(f"{prefix}_HEADERS_JSON", "").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"{prefix}_HEADERS_JSON 必须是JSON对象。")
            return {str(key): str(value) for key, value in parsed.items()}

        oa_url = os.getenv("DONGJIANG_OA_BASE_URL", "").strip()
        crm_url = os.getenv("DONGJIANG_CRM_BASE_URL", "").strip()
        sap_url = os.getenv("DONGJIANG_SAP_BASE_URL", "").strip()
        return cls(
            oa=WebhookOAAdapter(
                oa_url,
                os.getenv("DONGJIANG_OA_TOKEN", ""),
                extra_headers=headers("DONGJIANG_OA"),
            ) if oa_url else None,
            crm=WebhookCRMAdapter(
                crm_url,
                os.getenv("DONGJIANG_CRM_TOKEN", ""),
                extra_headers=headers("DONGJIANG_CRM"),
            ) if crm_url else None,
            sap=WebhookSAPAdapter(
                sap_url,
                os.getenv("DONGJIANG_SAP_TOKEN", ""),
                extra_headers=headers("DONGJIANG_SAP"),
            ) if sap_url else None,
            audit_root=Path(audit_root),
        )

    def submit_oa(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(case_id, "oa_submit", self.oa, "submit_review", case_id, payload)

    def write_back(
        self,
        case_id: str,
        customer_id: str,
        payload: dict[str, Any],
        *,
        phase: str = "final",
    ) -> dict[str, Any]:
        payload = {**payload, "writeback_phase": phase}
        results = {
            "phase": phase,
            "oa": self._call(
                case_id, f"oa_result_{phase}", self.oa, "write_result", case_id, payload
            ),
            "crm": self._call(
                case_id,
                f"crm_credit_{phase}",
                self.crm,
                "write_credit_decision",
                customer_id,
                payload,
            ) if customer_id else {"status": "skipped", "reason": "missing_customer_id"},
            "sap": self._call(
                case_id,
                f"sap_credit_{phase}",
                self.sap,
                "write_credit_control",
                customer_id,
                payload,
            ) if customer_id else {"status": "skipped", "reason": "missing_customer_id"},
        }
        return results

    def _call(
        self,
        case_id: str,
        operation: str,
        adapter: object | None,
        method_name: str,
        *args: Any,
    ) -> dict[str, Any]:
        if adapter is None:
            result = {"status": "not_configured"}
        else:
            try:
                response = getattr(adapter, method_name)(*args)
                result = {"status": "succeeded", "response": response}
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
        result["attempted_at"] = utc_now()
        self._audit(case_id, operation, result)
        return result

    def _audit(self, case_id: str, operation: str, result: dict[str, Any]) -> None:
        target_dir = self.audit_root / case_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{operation}.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
