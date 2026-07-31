from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any


class FakeModel:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        self.requests.append([dict(item) for item in input_items])
        response = self.responses.pop(0)
        for event in response:
            yield event


class UnexpectedApproval:
    async def request(self, request: Any) -> bool:
        raise AssertionError(f"Unexpected approval request: {request}")


class DecisionsApproval:
    def __init__(self, decisions: list[bool], clock: Any | None = None) -> None:
        self.decisions = decisions
        self.clock = clock
        self.requests: list[Any] = []

    async def request(self, request: Any) -> bool:
        self.requests.append(request)
        if self.clock is not None:
            self.clock.value += 100
        return self.decisions.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class InterruptOnceModel:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind, ModelStreamError

        self.calls += 1
        if self.calls == 1:
            yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="discarded partial")
            raise ModelStreamError("connection interrupted")
        yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="Final answer.")
        yield ModelEvent(
            ModelEventKind.OUTPUT_ITEM,
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Final answer."}],
            },
        )
        yield ModelEvent(ModelEventKind.COMPLETED)


class AlwaysInterruptModel:
    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind, ModelStreamError

        yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="discarded partial")
        raise ModelStreamError("connection interrupted")


def test_agent_runs_tool_loop_and_replays_complete_context(tmp_path: Path) -> None:
    from file_agent.agent import (
        AgentEventKind,
        AgentRunner,
        RunStatus,
    )
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter

    (tmp_path / "notes.md").write_text("Project Phoenix\n", encoding="utf-8")
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "I should read the note."}],
        "encrypted_content": "encrypted",
    }
    function_call = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"notes.md","max_lines":20}',
    }
    final_message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Found Project Phoenix."}],
    }
    model = FakeModel(
        [
            [
                ModelEvent(ModelEventKind.REASONING_DELTA, delta="Checking"),
                ModelEvent(ModelEventKind.OUTPUT_ITEM, item=reasoning_item),
                ModelEvent(ModelEventKind.OUTPUT_ITEM, item=function_call),
                ModelEvent(
                    ModelEventKind.USAGE,
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                ),
                ModelEvent(ModelEventKind.COMPLETED),
            ],
            [
                ModelEvent(
                    ModelEventKind.ANSWER_DELTA,
                    delta="Found Project Phoenix.",
                ),
                ModelEvent(ModelEventKind.OUTPUT_ITEM, item=final_message),
                ModelEvent(
                    ModelEventKind.USAGE,
                    usage={
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "total_tokens": 25,
                    },
                ),
                ModelEvent(ModelEventKind.COMPLETED),
            ],
        ]
    )
    emitted: list[Any] = []

    async def emit(event: Any) -> None:
        emitted.append(event)

    async def scenario() -> Any:
        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            runner = AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
            )
            return await runner.run(task="Find the project name", history=[], emit=emit)

    result = asyncio.run(scenario())

    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Found Project Phoenix."
    assert result.llm_calls == 2
    assert result.tool_calls == 1
    assert result.usage == {
        "input_tokens": 30,
        "output_tokens": 9,
        "total_tokens": 39,
        "reasoning_tokens": 0,
    }
    assert reasoning_item in result.history
    assert reasoning_item in model.requests[1]
    assert function_call in result.history
    function_output = next(
        item for item in result.history if item.get("type") == "function_call_output"
    )
    assert function_output["call_id"] == "call_1"
    output = json.loads(function_output["output"])
    assert output["ok"] is True
    assert output["data"]["untrusted_content"] == "Project Phoenix\n"
    assert function_output in model.requests[1]
    assert final_message in result.history
    assert [event.kind for event in emitted] == [
        AgentEventKind.LLM_STARTED,
        AgentEventKind.REASONING_DELTA,
        AgentEventKind.USAGE_UPDATED,
        AgentEventKind.TOOL_STARTED,
        AgentEventKind.TOOL_COMPLETED,
        AgentEventKind.LLM_STARTED,
        AgentEventKind.ANSWER_DELTA,
        AgentEventKind.USAGE_UPDATED,
        AgentEventKind.RUN_COMPLETED,
    ]
    trace_record = json.loads((tmp_path / "trace.jsonl").read_text().strip())
    assert trace_record["tool"] == "read_file"
    assert trace_record["args"] == {"path": "notes.md", "max_lines": 20}
    assert set(trace_record) == {"step", "tool", "args", "result_summary"}


def test_agent_refills_bad_arguments_and_tool_failures_in_order(
    tmp_path: Path,
) -> None:
    from file_agent.agent import AgentRunner, RunStatus
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter
    from file_agent.types import ErrorCode

    calls = [
        {
            "type": "function_call",
            "call_id": "bad-json",
            "name": "read_file",
            "arguments": "{broken",
        },
        {
            "type": "function_call",
            "call_id": "missing-file",
            "name": "read_file",
            "arguments": '{"path":"missing.md"}',
        },
    ]
    final_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Both reads failed safely."}],
    }
    model = FakeModel(
        [
            [
                *[ModelEvent(ModelEventKind.OUTPUT_ITEM, item=call) for call in calls],
                ModelEvent(ModelEventKind.COMPLETED),
            ],
            [
                ModelEvent(
                    ModelEventKind.ANSWER_DELTA,
                    delta="Both reads failed safely.",
                ),
                ModelEvent(ModelEventKind.OUTPUT_ITEM, item=final_message),
                ModelEvent(ModelEventKind.COMPLETED),
            ],
        ]
    )

    async def scenario() -> Any:
        async def emit(_: Any) -> None:
            return None

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            return await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
            ).run(task="Read files", history=[], emit=emit)

    result = asyncio.run(scenario())

    assert result.status is RunStatus.COMPLETED
    assert result.tool_calls == 2
    outputs = [
        json.loads(item["output"])
        for item in result.history
        if item.get("type") == "function_call_output"
    ]
    assert [output["error"]["code"] for output in outputs] == [
        ErrorCode.DENIED_BY_POLICY.value,
        ErrorCode.FILE_NOT_FOUND.value,
    ]
    refilled = [
        item for item in model.requests[1] if item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in refilled] == ["bad-json", "missing-file"]
    traces = [
        json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()
    ]
    assert traces[0]["args"] == {"_raw": "{broken"}
    assert traces[1]["args"] == {"path": "missing.md"}


def test_agent_approves_mutations_sequentially_and_excludes_wait_time(
    tmp_path: Path,
) -> None:
    from file_agent.agent import AgentRunner, RunLimits, RunStatus
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter

    calls = [
        {
            "type": "function_call",
            "call_id": "denied",
            "name": "write_file",
            "arguments": (
                '{"path":"denied.md","content":"no","mode":"create",'
                '"expected_sha256":null}'
            ),
        },
        {
            "type": "function_call",
            "call_id": "approved",
            "name": "write_file",
            "arguments": (
                '{"path":"approved.md","content":"yes","mode":"create",'
                '"expected_sha256":null}'
            ),
        },
    ]
    model = FakeModel(
        [
            [
                *[ModelEvent(ModelEventKind.OUTPUT_ITEM, item=call) for call in calls],
                ModelEvent(ModelEventKind.COMPLETED),
            ],
            [
                ModelEvent(ModelEventKind.ANSWER_DELTA, delta="Finished."),
                ModelEvent(
                    ModelEventKind.OUTPUT_ITEM,
                    item={
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Finished."}],
                    },
                ),
                ModelEvent(ModelEventKind.COMPLETED),
            ],
        ]
    )
    clock = FakeClock()
    approval = DecisionsApproval([False, True], clock)

    async def scenario() -> tuple[Any, list[Any]]:
        emitted: list[Any] = []

        async def emit(event: Any) -> None:
            emitted.append(event)

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            result = await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=approval,
                trace=trace,
                limits=RunLimits(active_timeout_seconds=1),
                clock=clock,
            ).run(task="Create two files", history=[], emit=emit)
        return result, emitted

    result, emitted = asyncio.run(scenario())

    assert result.status is RunStatus.COMPLETED
    assert not (tmp_path / "denied.md").exists()
    assert (tmp_path / "approved.md").read_text() == "yes"
    assert [request.tool for request in approval.requests] == [
        "write_file",
        "write_file",
    ]
    approval_events = [
        event.kind for event in emitted if event.kind.value.startswith("approval.")
    ]
    assert [event.value for event in approval_events] == [
        "approval.required",
        "approval.resolved",
        "approval.required",
        "approval.resolved",
    ]


def test_agent_retries_one_interrupted_stream_without_replaying_partial_items(
    tmp_path: Path,
) -> None:
    from file_agent.agent import AgentEventKind, AgentRunner, RunStatus
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter

    model = InterruptOnceModel()

    async def scenario() -> tuple[Any, list[Any]]:
        emitted: list[Any] = []

        async def emit(event: Any) -> None:
            emitted.append(event)

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            result = await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
            ).run(task="Answer", history=[], emit=emit)
        return result, emitted

    result, emitted = asyncio.run(scenario())

    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Final answer."
    assert result.llm_calls == 2
    assert all("discarded partial" not in str(item) for item in result.history)
    assert [event.kind for event in emitted].count(AgentEventKind.LLM_RETRYING) == 1


def test_agent_fails_after_the_second_interrupted_stream(tmp_path: Path) -> None:
    from file_agent.agent import AgentEventKind, AgentRunner, RunStatus
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter

    async def scenario() -> tuple[Any, list[Any]]:
        emitted: list[Any] = []

        async def emit(event: Any) -> None:
            emitted.append(event)

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            result = await AgentRunner(
                model=AlwaysInterruptModel(),
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
            ).run(task="Answer", history=[], emit=emit)
        return result, emitted

    result, emitted = asyncio.run(scenario())

    assert result.status is RunStatus.FAILED
    assert result.answer == ""
    assert result.llm_calls == 2
    assert [event.kind for event in emitted].count(AgentEventKind.LLM_RETRYING) == 1
    assert emitted[-1].kind is AgentEventKind.RUN_FAILED


def test_agent_enforces_llm_call_limit(tmp_path: Path) -> None:
    from file_agent.agent import AgentRunner, RunLimits, RunStatus
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter
    from file_agent.types import ErrorCode

    model = FakeModel(
        [
            [
                ModelEvent(
                    ModelEventKind.OUTPUT_ITEM,
                    item={
                        "type": "function_call",
                        "call_id": "changes",
                        "name": "get_workspace_changes",
                        "arguments": "{}",
                    },
                ),
                ModelEvent(ModelEventKind.COMPLETED),
            ]
        ]
    )

    async def scenario() -> Any:
        async def emit(_: Any) -> None:
            return None

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            return await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
                limits=RunLimits(max_llm_calls=1),
            ).run(task="Inspect", history=[], emit=emit)

    result = asyncio.run(scenario())

    assert result.status is RunStatus.INCOMPLETE
    assert result.error_code is ErrorCode.INCOMPLETE
    assert result.llm_calls == 1
    assert result.tool_calls == 1
    assert result.error == "LLM call limit reached."


def test_agent_enforces_tool_call_limit_before_next_side_effect(
    tmp_path: Path,
) -> None:
    from file_agent.agent import AgentRunner, RunLimits, RunStatus
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter
    from file_agent.types import ErrorCode

    model = FakeModel(
        [
            [
                ModelEvent(
                    ModelEventKind.OUTPUT_ITEM,
                    item={
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "get_workspace_changes",
                        "arguments": "{}",
                    },
                )
                for call_id in ("first", "second")
            ]
            + [ModelEvent(ModelEventKind.COMPLETED)]
        ]
    )

    async def scenario() -> Any:
        async def emit(_: Any) -> None:
            return None

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            return await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
                limits=RunLimits(max_tool_calls=1),
            ).run(task="Inspect twice", history=[], emit=emit)

    result = asyncio.run(scenario())

    assert result.status is RunStatus.INCOMPLETE
    assert result.error_code is ErrorCode.INCOMPLETE
    assert result.tool_calls == 1
    assert result.error == "Tool call limit reached."
    assert len((tmp_path / "trace.jsonl").read_text().splitlines()) == 1


def test_agent_enforces_active_time_limit(tmp_path: Path) -> None:
    from file_agent.agent import AgentEventKind, AgentRunner, RunLimits, RunStatus
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.tools import FileTools
    from file_agent.trace import TraceWriter
    from file_agent.types import ErrorCode

    clock = FakeClock()
    model = FakeModel(
        [
            [
                ModelEvent(ModelEventKind.ANSWER_DELTA, delta="too late"),
                ModelEvent(ModelEventKind.COMPLETED),
            ]
        ]
    )

    async def scenario() -> Any:
        async def emit(event: Any) -> None:
            if event.kind is AgentEventKind.LLM_STARTED:
                clock.value = 2

        with TraceWriter(tmp_path / "trace.jsonl") as trace:
            return await AgentRunner(
                model=model,
                tools=FileTools(tmp_path),
                approval=UnexpectedApproval(),
                trace=trace,
                limits=RunLimits(active_timeout_seconds=1),
                clock=clock,
            ).run(task="Wait", history=[], emit=emit)

    result = asyncio.run(scenario())

    assert result.status is RunStatus.INCOMPLETE
    assert result.error_code is ErrorCode.INCOMPLETE
    assert result.error == "Active run time limit reached."
    assert result.answer == ""
    assert RunLimits() == RunLimits(
        max_llm_calls=20,
        max_tool_calls=80,
        active_timeout_seconds=300,
    )
