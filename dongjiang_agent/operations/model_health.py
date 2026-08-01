"""Operational view and active probe for the optional text model."""

from __future__ import annotations

from typing import Any

from ..llm.gateway import OpenAICompatibleGateway


class ModelHealthService:
    def __init__(self, gateway: OpenAICompatibleGateway | None = None) -> None:
        self.gateway = gateway or OpenAICompatibleGateway()

    def summary(self) -> dict[str, Any]:
        summary = self.gateway.health_store.summary()
        summary["configured"] = self.gateway.available
        summary["model"] = self.gateway.model
        return summary

    def probe(self) -> dict[str, Any]:
        event = self.gateway.probe()
        return {"event": event, **self.summary()}
