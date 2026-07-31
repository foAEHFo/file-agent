"""Handwritten Responses tool loop with limits, retry, approval, and trace hooks."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from file_agent.model import (
    JsonObject,
    ModelEvent,
    ModelEventKind,
    ModelStreamError,
    ResponsesClient,
)
from file_agent.prompts import ARGUMENT_MODELS
from file_agent.tools import MUTATING_TOOLS, FileTools
from file_agent.trace import TraceWriter
from file_agent.types import ErrorCode, ToolResult


class AgentEventKind(StrEnum):
    LLM_STARTED = "llm.started"
    REASONING_DELTA = "reasoning.delta"
    ANSWER_DELTA = "answer.delta"
    TOOL_STARTED = "tool.started"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    TOOL_COMPLETED = "tool.completed"
    USAGE_UPDATED = "usage.updated"
    LLM_RETRYING = "llm.retrying"
    RUN_COMPLETED = "run.completed"
    RUN_INCOMPLETE = "run.incomplete"
    RUN_FAILED = "run.failed"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: AgentEventKind
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool: str
    args: Mapping[str, Any]
    id: str = field(default_factory=lambda: uuid4().hex)


class ApprovalHandler(Protocol):
    async def request(self, request: ApprovalRequest) -> bool: ...


EventSink = Callable[[AgentEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_llm_calls: int = 30
    max_tool_calls: int = 100
    active_timeout_seconds: float = 1200


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: RunStatus
    answer: str
    history: tuple[JsonObject, ...]
    llm_calls: int
    tool_calls: int
    usage: Mapping[str, int]
    workspace_changes: Mapping[str, Any]
    error_code: ErrorCode | None = None
    error: str | None = None


@dataclass(slots=True)
class _RunState:
    history: list[JsonObject]
    answer_parts: list[str]
    llm_calls: int
    tool_calls: int
    usage: dict[str, int]
    started_at: float
    approval_seconds: float = 0.0


class _ActiveTimeLimit(Exception):
    pass


class AgentRunner:
    """Run one sequential, client-managed Responses conversation."""

    def __init__(
        self,
        *,
        model: ResponsesClient,
        tools: FileTools,
        approval: ApprovalHandler,
        trace: TraceWriter,
        limits: RunLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._tools = tools
        self._approval = approval
        self._trace = trace
        self._limits = limits or RunLimits()
        self._clock = clock

    async def run(
        self,
        *,
        task: str,
        history: Sequence[Mapping[str, Any]],
        emit: EventSink,
    ) -> AgentRunResult:
        state = _RunState(
            history=[dict(item) for item in history]
            + [{"role": "user", "content": task}],
            answer_parts=[],
            llm_calls=0,
            tool_calls=0,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
            },
            started_at=self._clock(),
        )

        while True:
            if self._active_seconds(state) >= self._limits.active_timeout_seconds:
                return await self._incomplete(
                    state, emit, "Active run time limit reached."
                )
            response_retry = 0
            while True:
                if state.llm_calls >= self._limits.max_llm_calls:
                    return await self._incomplete(
                        state, emit, "LLM call limit reached."
                    )
                state.llm_calls += 1
                await emit(
                    AgentEvent(
                        AgentEventKind.LLM_STARTED,
                        {"call": state.llm_calls},
                    )
                )
                try:
                    output_items, response_text = await self._stream_response(
                        state, emit
                    )
                    if response_text:
                        state.answer_parts.append(response_text)
                    break
                except _ActiveTimeLimit:
                    return await self._incomplete(
                        state, emit, "Active run time limit reached."
                    )
                except ModelStreamError as error:
                    if response_retry >= 1:
                        return await self._failed(state, emit, str(error))
                    response_retry += 1
                    await emit(
                        AgentEvent(
                            AgentEventKind.LLM_RETRYING,
                            {"attempt": response_retry, "error": str(error)},
                        )
                    )

            state.history.extend(output_items)
            function_calls = [
                item for item in output_items if item.get("type") == "function_call"
            ]
            if not function_calls:
                final_text = response_text or _text_from_output_items(output_items)
                if not final_text:
                    return await self._incomplete(
                        state,
                        emit,
                        "Model completed without a final answer or tool call.",
                    )
                if not response_text:
                    state.answer_parts.append(final_text)
                return await self._completed(state, emit)

            for function_call in function_calls:
                if state.tool_calls >= self._limits.max_tool_calls:
                    return await self._incomplete(
                        state, emit, "Tool call limit reached."
                    )
                state.tool_calls += 1
                tool_name = str(function_call.get("name", ""))
                raw_arguments = str(function_call.get("arguments", ""))
                execution_args, trace_args, validation_failure = _validate_arguments(
                    tool_name,
                    raw_arguments,
                )
                await emit(
                    AgentEvent(
                        AgentEventKind.TOOL_STARTED,
                        {
                            "step": state.tool_calls,
                            "tool": tool_name,
                            "args": trace_args,
                        },
                    )
                )
                result = validation_failure
                if result is None:
                    if tool_name in MUTATING_TOOLS:
                        approval_request = ApprovalRequest(
                            tool=tool_name,
                            args=execution_args,
                        )
                        await emit(
                            AgentEvent(
                                AgentEventKind.APPROVAL_REQUIRED,
                                {
                                    "approval_id": approval_request.id,
                                    "tool": tool_name,
                                    "args": execution_args,
                                },
                            )
                        )
                        approval_started = self._clock()
                        try:
                            try:
                                approved = await self._approval.request(
                                    approval_request
                                )
                            except Exception as error:
                                return await self._failed(
                                    state,
                                    emit,
                                    f"Approval failed: {error}",
                                )
                        finally:
                            state.approval_seconds += self._clock() - approval_started
                        await emit(
                            AgentEvent(
                                AgentEventKind.APPROVAL_RESOLVED,
                                {
                                    "approval_id": approval_request.id,
                                    "tool": tool_name,
                                    "approved": approved,
                                },
                            )
                        )
                        if not approved:
                            result = ToolResult.failure(
                                code=ErrorCode.DENIED_BY_USER,
                                message="The user denied this write operation.",
                                summary=f"User denied {tool_name}.",
                            )
                    if result is None:
                        result = self._tools.execute(tool_name, execution_args)

                self._trace.append(
                    tool=tool_name,
                    args=trace_args,
                    result_summary=result.summary,
                )
                function_output: JsonObject = {
                    "type": "function_call_output",
                    "call_id": str(function_call.get("call_id", "")),
                    "output": json.dumps(
                        result.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
                state.history.append(function_output)
                await emit(
                    AgentEvent(
                        AgentEventKind.TOOL_COMPLETED,
                        {
                            "step": state.tool_calls,
                            "tool": tool_name,
                            "result": result.to_dict(),
                        },
                    )
                )
                if self._active_seconds(state) >= self._limits.active_timeout_seconds:
                    return await self._incomplete(
                        state, emit, "Active run time limit reached."
                    )

    async def _stream_response(
        self, state: _RunState, emit: EventSink
    ) -> tuple[list[JsonObject], str]:
        output_items: list[JsonObject] = []
        response_text: list[str] = []
        completed = False
        remaining = self._limits.active_timeout_seconds - self._active_seconds(state)
        if remaining <= 0:
            raise _ActiveTimeLimit
        try:
            async with asyncio.timeout(remaining):
                async for event in self._model.stream(state.history):
                    if event.kind is ModelEventKind.COMPLETED:
                        completed = True
                    await self._handle_model_event(
                        event,
                        output_items=output_items,
                        response_text=response_text,
                        state=state,
                        emit=emit,
                    )
        except TimeoutError as error:
            raise _ActiveTimeLimit from error
        if not completed:
            raise ModelStreamError("Model stream ended before completion")
        return output_items, "".join(response_text)

    async def _handle_model_event(
        self,
        event: ModelEvent,
        *,
        output_items: list[JsonObject],
        response_text: list[str],
        state: _RunState,
        emit: EventSink,
    ) -> None:
        if event.kind is ModelEventKind.REASONING_DELTA and event.delta is not None:
            await emit(
                AgentEvent(
                    AgentEventKind.REASONING_DELTA,
                    {"delta": event.delta},
                )
            )
        elif event.kind is ModelEventKind.ANSWER_DELTA and event.delta is not None:
            response_text.append(event.delta)
            await emit(
                AgentEvent(
                    AgentEventKind.ANSWER_DELTA,
                    {"delta": event.delta},
                )
            )
        elif event.kind is ModelEventKind.OUTPUT_ITEM and event.item is not None:
            output_items.append(dict(event.item))
        elif event.kind is ModelEventKind.USAGE and event.usage is not None:
            _merge_usage(state.usage, event.usage)
            await emit(
                AgentEvent(
                    AgentEventKind.USAGE_UPDATED,
                    dict(state.usage),
                )
            )

    def _active_seconds(self, state: _RunState) -> float:
        return self._clock() - state.started_at - state.approval_seconds

    async def _completed(self, state: _RunState, emit: EventSink) -> AgentRunResult:
        changes = self._workspace_changes()
        await emit(
            AgentEvent(
                AgentEventKind.RUN_COMPLETED,
                {"workspace_changes": changes},
            )
        )
        return self._result(state, RunStatus.COMPLETED, changes=changes)

    async def _incomplete(
        self, state: _RunState, emit: EventSink, error: str
    ) -> AgentRunResult:
        changes = self._workspace_changes()
        await emit(
            AgentEvent(
                AgentEventKind.RUN_INCOMPLETE,
                {
                    "code": ErrorCode.INCOMPLETE.value,
                    "error": error,
                    "workspace_changes": changes,
                },
            )
        )
        return self._result(
            state,
            RunStatus.INCOMPLETE,
            changes=changes,
            error_code=ErrorCode.INCOMPLETE,
            error=error,
        )

    async def _failed(
        self, state: _RunState, emit: EventSink, error: str
    ) -> AgentRunResult:
        changes = self._workspace_changes()
        await emit(
            AgentEvent(
                AgentEventKind.RUN_FAILED,
                {"error": error, "workspace_changes": changes},
            )
        )
        return self._result(
            state,
            RunStatus.FAILED,
            changes=changes,
            error=error,
        )

    def _workspace_changes(self) -> Mapping[str, Any]:
        result = self._tools.execute("get_workspace_changes", {})
        return dict(result.data) if result.ok else {}

    @staticmethod
    def _result(
        state: _RunState,
        status: RunStatus,
        *,
        changes: Mapping[str, Any],
        error_code: ErrorCode | None = None,
        error: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            answer="".join(state.answer_parts),
            history=tuple(state.history),
            llm_calls=state.llm_calls,
            tool_calls=state.tool_calls,
            usage=dict(state.usage),
            workspace_changes=dict(changes),
            error_code=error_code,
            error=error,
        )


def _validate_arguments(
    tool_name: str,
    raw_arguments: str,
) -> tuple[dict[str, Any], dict[str, Any], ToolResult | None]:
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        failure = ToolResult.failure(
            code=ErrorCode.DENIED_BY_POLICY,
            message="Tool arguments are not valid JSON.",
            summary=f"Invalid arguments for {tool_name}.",
        )
        return {}, {"_raw": raw_arguments}, failure
    if not isinstance(parsed, dict):
        failure = ToolResult.failure(
            code=ErrorCode.DENIED_BY_POLICY,
            message="Tool arguments must be a JSON object.",
            summary=f"Invalid arguments for {tool_name}.",
        )
        return {}, {"_raw": raw_arguments}, failure

    argument_model = ARGUMENT_MODELS.get(tool_name)
    if argument_model is None:
        failure = ToolResult.failure(
            code=ErrorCode.DENIED_BY_POLICY,
            message=f"Unknown tool: {tool_name}.",
            summary=f"Tool {tool_name} is not available.",
        )
        return {}, parsed, failure
    try:
        validated = argument_model.model_validate(parsed)
    except ValidationError as error:
        issues = [
            {
                "path": ".".join(str(part) for part in issue["loc"]),
                "message": issue["msg"],
            }
            for issue in error.errors(include_url=False, include_input=False)
        ]
        failure = ToolResult.failure(
            code=ErrorCode.DENIED_BY_POLICY,
            message=f"Invalid arguments for {tool_name}.",
            summary=f"Invalid arguments for {tool_name}.",
            details={"issues": issues},
        )
        return {}, parsed, failure
    return validated.model_dump(), parsed, None


def _merge_usage(total: dict[str, int], usage: Mapping[str, Any]) -> None:
    for field_name in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field_name, 0)
        if isinstance(value, int):
            total[field_name] += value
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, Mapping):
        reasoning_tokens = output_details.get("reasoning_tokens", 0)
        if isinstance(reasoning_tokens, int):
            total["reasoning_tokens"] += reasoning_tokens


def _text_from_output_items(items: Sequence[Mapping[str, Any]]) -> str:
    text_parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, Mapping)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                text_parts.append(part["text"])
    return "".join(text_parts)
