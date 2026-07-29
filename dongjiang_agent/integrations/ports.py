"""CRM/OA ports keep vendor-specific APIs outside domain logic."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.request import Request, urlopen


class CRMPort(Protocol):
    def read_customer(self, customer_id: str) -> dict[str, Any]: ...
    def write_credit_decision(self, customer_id: str, payload: dict[str, Any]) -> None: ...


class OAPort(Protocol):
    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class _WebhookAdapter:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.base_url}/{path.lstrip('/')}", data=data, headers=headers, method=method)
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8") or "{}")


class WebhookCRMAdapter(_WebhookAdapter):
    def read_customer(self, customer_id: str) -> dict[str, Any]:
        return self._request("GET", f"customers/{customer_id}")

    def write_credit_decision(self, customer_id: str, payload: dict[str, Any]) -> None:
        self._request("POST", f"customers/{customer_id}/credit-decisions", payload)


class WebhookOAAdapter(_WebhookAdapter):
    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"contract-reviews/{case_id}", payload)
