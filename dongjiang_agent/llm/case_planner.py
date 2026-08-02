"""Optional LLM proposal layer for the governed parent Agent plan."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .gateway import OpenAICompatibleGateway


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class CasePlanningAssistant:
    prompt_version = "dongjiang-case-planner-v1"

    def __init__(
        self,
        gateway: OpenAICompatibleGateway | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.gateway = gateway or OpenAICompatibleGateway()
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("DONGJIANG_ORCHESTRATOR_LLM_PLANNING_ENABLED", "").lower()
            in {"1", "true", "yes", "on"}
        )

    def propose(
        self,
        snapshot: dict[str, Any],
        *,
        allowed_task_types: list[str],
        required_task_types: list[str],
    ) -> dict[str, Any]:
        base = {
            "status": "not_configured",
            "model": self.gateway.model,
            "prompt_version": self.prompt_version,
            "selected_task_types": [],
            "rationale": "",
            "fallback_reason": "主Agent大模型规划未启用，使用确定性案件计划。",
        }
        if not self.enabled or not self.gateway.available:
            return base
        instruction = f"""你是企业信审与合同评审的任务规划助手。
你只能从allowed_task_types选择任务，required_task_types必须全部保留。
不得增加批准、放行、删除、写回或绕过人工审批的权限。
只输出JSON对象：
{{"selected_task_types":["任务类型"],"rationale":"不超过200字"}}

allowed_task_types={json.dumps(allowed_task_types, ensure_ascii=False)}
required_task_types={json.dumps(required_task_types, ensure_ascii=False)}
案件结构化快照={json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}
"""
        try:
            raw = self.gateway.complete_json(
                instruction,
                system_prompt=(
                    "你是受控工作流规划助手。只提出白名单任务，不作任何业务审批决定。"
                ),
                operation="case_planning",
            )
            text = str(raw or "").strip()
            match = _JSON_BLOCK.search(text)
            if match:
                text = match.group(1).strip()
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("模型规划输出不是JSON对象")
            selected = payload.get("selected_task_types")
            if not isinstance(selected, list):
                raise ValueError("模型规划缺少selected_task_types数组")
            return {
                **base,
                "status": "succeeded",
                "selected_task_types": [str(item) for item in selected],
                "rationale": str(payload.get("rationale") or "")[:500],
                "fallback_reason": "",
            }
        except Exception as exc:
            return {
                **base,
                "status": "failed",
                "fallback_reason": (
                    f"模型规划失败或输出不合规，已使用确定性计划：{type(exc).__name__}"
                ),
            }
