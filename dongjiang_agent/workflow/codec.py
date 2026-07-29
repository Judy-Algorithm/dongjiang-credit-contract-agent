"""Convert domain objects to checkpoint-safe dictionaries and restore cases."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from ..domain.codec import (
    assessment_from_dict,
    contract_facts_from_dict,
    profile_from_dict,
    review_from_dict,
)
from ..domain.models import AuditCase


def case_from_state(state: dict[str, Any]) -> AuditCase:
    case = AuditCase(
        case_id=str(state["case_id"]),
        customer=profile_from_dict(dict(state["customer"])),
        contracts=[
            contract_facts_from_dict(item)
            for item in state.get("contract_facts") or []
        ],
        source_files=list(state.get("source_files") or []),
        credit_assessment=assessment_from_dict(
            state.get("effective_credit_assessment")
            or state.get("credit_assessment")
        ),
        model_credit_assessment=assessment_from_dict(state.get("credit_assessment")),
        credit_status=str(state.get("credit_status") or "draft"),
        credit_approval=dict(state.get("credit_approval") or {}),
        contract_reviews=[
            review_from_dict(item)
            for item in state.get("contract_reviews") or []
        ],
        status=str(state.get("status") or "processing"),
        trace=list(state.get("trace") or []),
        created_at=str(state.get("created_at") or ""),
    )
    return case


def checkpoint_value(value: Any) -> Any:
    if is_dataclass(value):
        return checkpoint_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): checkpoint_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [checkpoint_value(item) for item in value]
    return value


def checkpoint_dict(value: Any) -> dict[str, Any]:
    payload = checkpoint_value(value)
    if not isinstance(payload, dict):
        raise TypeError("checkpoint_dict 只接受dataclass或dict。")
    return payload
