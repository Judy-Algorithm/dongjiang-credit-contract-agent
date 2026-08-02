"""Provider-neutral registries for business agents and reusable tools.

The workflow only depends on these ports.  Today's implementation executes
in-process handlers; a future Agenthub/Toolhub adapter can implement the same
interfaces without changing credit or contract business nodes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any, Callable, Protocol
from uuid import uuid4

from ..domain.models import utc_now


@dataclass(slots=True, frozen=True)
class InvocationContext:
    case_id: str = ""
    caller: str = "case_orchestrator"
    agent_id: str = ""
    plan_id: str = ""
    task_id: str = ""
    input_summary: str = "结构化输入；敏感正文不记录"
    output_summary: str = ""


@dataclass(slots=True, frozen=True)
class AgentDescriptor:
    agent_id: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    version: str = "1.0"
    provider: str = "local"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class ToolDescriptor:
    tool_name: str
    name: str
    description: str
    allowed_agents: tuple[str, ...]
    version: str = "1.0"
    provider: str = "local"
    risk_level: str = "read_only"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentInvocationResult:
    output: dict[str, Any]
    record: dict[str, Any]


@dataclass(slots=True)
class ToolInvocationResult:
    output: Any
    record: dict[str, Any]


class AgentInvocationError(RuntimeError):
    def __init__(self, message: str, *, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class ToolInvocationError(RuntimeError):
    def __init__(self, message: str, *, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class AgentRegistry(Protocol):
    provider: str

    def discover(self, capability: str | None = None) -> list[dict[str, Any]]: ...

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        context: InvocationContext | None = None,
    ) -> AgentInvocationResult: ...


class ToolRegistry(Protocol):
    provider: str

    def discover(
        self, *, agent_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        context: InvocationContext | None = None,
    ) -> ToolInvocationResult: ...


class LocalAgentRegistry:
    """In-process Agenthub-compatible execution boundary."""

    provider = "local"

    def __init__(self) -> None:
        self._descriptors: dict[str, AgentDescriptor] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(
        self,
        descriptor: AgentDescriptor,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        if not descriptor.agent_id.strip():
            raise ValueError("Agent ID不能为空。")
        if descriptor.agent_id in self._handlers:
            raise ValueError(f"Agent已注册：{descriptor.agent_id}")
        self._descriptors[descriptor.agent_id] = descriptor
        self._handlers[descriptor.agent_id] = handler

    def discover(self, capability: str | None = None) -> list[dict[str, Any]]:
        rows = [item.to_dict() for item in self._descriptors.values()]
        if capability:
            rows = [item for item in rows if capability in item["capabilities"]]
        return sorted(rows, key=lambda item: str(item["agent_id"]))

    def invoke(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        context: InvocationContext | None = None,
    ) -> AgentInvocationResult:
        descriptor = self._descriptors.get(agent_id)
        handler = self._handlers.get(agent_id)
        if descriptor is None or handler is None:
            raise KeyError(f"Agent注册中心中不存在：{agent_id}")
        ctx = context or InvocationContext(agent_id=agent_id)
        invocation_id = f"AGENT-{uuid4().hex[:16].upper()}"
        started_at = utc_now()
        started = perf_counter()
        try:
            output = handler(payload)
        except Exception as exc:
            duration_ms = round((perf_counter() - started) * 1000)
            record = {
                "invocation_id": invocation_id,
                "case_id": ctx.case_id,
                "caller": ctx.caller,
                "agent_id": agent_id,
                "agent_name": descriptor.name,
                "provider": descriptor.provider,
                "version": descriptor.version,
                "plan_id": ctx.plan_id,
                "status": "failed",
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": duration_ms,
                "input_summary": ctx.input_summary,
                "output_summary": f"{type(exc).__name__}：Agent执行失败",
                "sensitive_input": "not_logged",
            }
            setattr(exc, "registry_record", record)
            raise
        duration_ms = round((perf_counter() - started) * 1000)
        active_plan = dict(output.get("active_workflow_plan") or {})
        status = str(active_plan.get("status") or output.get("status") or "completed")
        record = {
            "invocation_id": invocation_id,
            "case_id": ctx.case_id,
            "caller": ctx.caller,
            "agent_id": agent_id,
            "agent_name": descriptor.name,
            "provider": descriptor.provider,
            "version": descriptor.version,
            "plan_id": active_plan.get("plan_id") or ctx.plan_id,
            "status": status,
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_ms": duration_ms,
            "input_summary": ctx.input_summary,
            "output_summary": ctx.output_summary or _agent_output_summary(output),
            "sensitive_input": "not_logged",
        }
        return AgentInvocationResult(output=output, record=record)


class LocalToolRegistry:
    """In-process Toolhub-compatible tool catalog and dispatcher."""

    provider = "local"

    def __init__(self) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(
        self,
        descriptor: ToolDescriptor,
        handler: Callable[[dict[str, Any]], Any],
    ) -> None:
        if not descriptor.tool_name.strip():
            raise ValueError("工具名称不能为空。")
        if descriptor.tool_name in self._handlers:
            raise ValueError(f"工具已注册：{descriptor.tool_name}")
        self._descriptors[descriptor.tool_name] = descriptor
        self._handlers[descriptor.tool_name] = handler

    def discover(
        self, *, agent_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [item.to_dict() for item in self._descriptors.values()]
        if agent_id:
            rows = [item for item in rows if agent_id in item["allowed_agents"]]
        return sorted(rows, key=lambda item: str(item["tool_name"]))

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        context: InvocationContext | None = None,
    ) -> ToolInvocationResult:
        descriptor = self._descriptors.get(tool_name)
        handler = self._handlers.get(tool_name)
        if descriptor is None or handler is None:
            raise KeyError(f"工具注册中心中不存在：{tool_name}")
        ctx = context or InvocationContext()
        effective_agent = ctx.agent_id or ctx.caller
        if effective_agent not in descriptor.allowed_agents:
            raise PermissionError(
                f"Agent {effective_agent or 'unknown'} 无权调用工具 {tool_name}。"
            )
        call_id = f"TOOL-{uuid4().hex[:16].upper()}"
        started_at = utc_now()
        started = perf_counter()
        base_record = {
            "tool_call_id": call_id,
            "case_id": ctx.case_id,
            "caller": ctx.caller,
            "agent_id": effective_agent,
            "plan_id": ctx.plan_id,
            "task_id": ctx.task_id,
            "tool_name": tool_name,
            "tool_label": descriptor.name,
            "provider": descriptor.provider,
            "version": descriptor.version,
            "risk_level": descriptor.risk_level,
            "started_at": started_at,
            "input_fields": sorted(str(key) for key in payload),
            "sensitive_input": "not_logged",
        }
        try:
            output = handler(payload)
        except Exception as exc:
            record = {
                **base_record,
                "status": "failed",
                "completed_at": utc_now(),
                "duration_ms": round((perf_counter() - started) * 1000),
                "output_summary": f"{type(exc).__name__}：工具执行失败",
            }
            setattr(exc, "registry_record", record)
            raise
        record = {
            **base_record,
            "status": "completed",
            "completed_at": utc_now(),
            "duration_ms": round((perf_counter() - started) * 1000),
            "output_summary": _tool_output_summary(output),
        }
        return ToolInvocationResult(output=output, record=record)


def _agent_output_summary(output: dict[str, Any]) -> str:
    plan = dict(output.get("active_workflow_plan") or {})
    if plan:
        return f"完成计划{plan.get('plan_id') or 'unknown'}，共{len(plan.get('tasks') or [])}项任务"
    return f"返回阶段{output.get('stage') or 'unknown'}"


def _tool_output_summary(output: Any) -> str:
    if isinstance(output, dict):
        explicit = str(output.get("_tool_summary") or "").strip()
        if explicit:
            return explicit[:240]
        return f"返回{len(output)}个结构化字段"
    if isinstance(output, list):
        return f"返回{len(output)}项结构化结果"
    return f"返回{type(output).__name__}结果"
