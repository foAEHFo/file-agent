from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any


class TerminalInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeCliModel:
    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        self.requests.append([dict(item) for item in input_items])
        for event in self._responses.pop(0):
            yield event


def test_cli_parser_uses_documented_trace_default() -> None:
    from file_agent.cli import parse_args

    options = parse_args(["--workspace", "./custom", "--task", "整理文件"])

    assert options.workspace == Path("./custom")
    assert options.task == "整理文件"
    assert options.trace == Path("trace.jsonl")
    assert options.yes is False


def test_tty_approval_prompts_for_each_mutation() -> None:
    from file_agent.agent import ApprovalRequest
    from file_agent.approval import CliApprovalHandler

    input_stream = TerminalInput("y\nn\n")
    output_stream = io.StringIO()
    approval = CliApprovalHandler(
        auto_approve=False,
        input_stream=input_stream,
        output_stream=output_stream,
    )

    async def scenario() -> list[bool]:
        requests: list[Any] = [
            ApprovalRequest(
                tool="write_file",
                args={"path": "one.md", "content": "一", "mode": "create"},
            ),
            ApprovalRequest(
                tool="move_file",
                args={"source": "one.md", "destination": "two.md"},
            ),
        ]
        return [await approval.request(request) for request in requests]

    assert asyncio.run(scenario()) == [True, False]
    output = output_stream.getvalue()
    assert output.count("是否批准") == 2
    assert "write_file" in output
    assert "move_file" in output


def test_yes_flag_approves_without_a_tty_or_prompt() -> None:
    from file_agent.agent import ApprovalRequest
    from file_agent.approval import CliApprovalHandler

    input_stream = io.StringIO("")
    output_stream = io.StringIO()
    approval = CliApprovalHandler(
        auto_approve=True,
        input_stream=input_stream,
        output_stream=output_stream,
    )

    approved = asyncio.run(
        approval.request(ApprovalRequest(tool="make_directory", args={"path": "new"}))
    )

    assert approved is True
    assert output_stream.getvalue() == ""


def test_non_tty_defaults_to_denial_without_reading_input() -> None:
    from file_agent.agent import ApprovalRequest
    from file_agent.approval import CliApprovalHandler

    input_stream = io.StringIO("yes\n")
    output_stream = io.StringIO()
    approval = CliApprovalHandler(
        auto_approve=False,
        input_stream=input_stream,
        output_stream=output_stream,
    )

    approved = asyncio.run(
        approval.request(
            ApprovalRequest(
                tool="write_file",
                args={"path": "new.md", "content": "内容", "mode": "create"},
            )
        )
    )

    assert approved is False
    assert input_stream.tell() == 0
    assert "默认拒绝" in output_stream.getvalue()


def test_cli_streams_events_and_writes_trace_for_selected_workspace(
    tmp_path: Path,
) -> None:
    from file_agent.cli import CliOptions, run_cli
    from file_agent.model import ModelEvent, ModelEventKind

    workspace = tmp_path / "another-workspace"
    workspace.mkdir()
    (workspace / "status.md").write_text("状态：正常\n", encoding="utf-8")
    trace_path = tmp_path / "runs" / "trace.jsonl"
    function_call = {
        "type": "function_call",
        "call_id": "read-status",
        "name": "read_file",
        "arguments": '{"path":"status.md","start_line":1,"max_lines":20}',
    }
    final_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "当前状态正常。"}],
    }
    model = FakeCliModel(
        [
            [
                ModelEvent(ModelEventKind.REASONING_DELTA, delta="先读取状态文件。"),
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
                ModelEvent(ModelEventKind.ANSWER_DELTA, delta="当前状态正常。"),
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
    output_stream = io.StringIO()

    exit_code = asyncio.run(
        run_cli(
            CliOptions(
                workspace=workspace,
                task="检查当前状态",
                trace=trace_path,
                yes=False,
            ),
            model=model,
            input_stream=io.StringIO(""),
            output_stream=output_stream,
            environment={},
        )
    )

    assert exit_code == 0
    output = output_stream.getvalue()
    assert "[推理] 先读取状态文件。" in output
    assert "[工具]" in output and "read_file" in output
    assert "[结果]" in output and "status.md" in output
    assert "[回答] 当前状态正常。" in output
    assert "[用量]" in output and "39" in output
    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert set(record) == {"step", "tool", "args", "result_summary"}
    assert record["tool"] == "read_file"
    assert record["args"]["path"] == "status.md"


def test_cli_yes_cannot_bypass_hash_or_workspace_sandbox(tmp_path: Path) -> None:
    from file_agent.cli import CliOptions, run_cli
    from file_agent.model import ModelEvent, ModelEventKind
    from file_agent.types import ErrorCode

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected_file = workspace / "protected.md"
    protected_file.write_text("原始内容\n", encoding="utf-8")
    calls = [
        {
            "type": "function_call",
            "call_id": "missing-hash",
            "name": "write_file",
            "arguments": (
                '{"path":"protected.md","content":"被覆盖","mode":"overwrite",'
                '"expected_sha256":null}'
            ),
        },
        {
            "type": "function_call",
            "call_id": "outside-workspace",
            "name": "write_file",
            "arguments": (
                '{"path":"../outside.md","content":"越界","mode":"create",'
                '"expected_sha256":null}'
            ),
        },
    ]
    model = FakeCliModel(
        [
            [
                *[ModelEvent(ModelEventKind.OUTPUT_ITEM, item=call) for call in calls],
                ModelEvent(ModelEventKind.COMPLETED),
            ],
            [
                ModelEvent(ModelEventKind.ANSWER_DELTA, delta="两个操作都被安全拒绝。"),
                ModelEvent(
                    ModelEventKind.OUTPUT_ITEM,
                    item={
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "两个操作都被安全拒绝。",
                            }
                        ],
                    },
                ),
                ModelEvent(ModelEventKind.COMPLETED),
            ],
        ]
    )
    trace_path = tmp_path / "trace.jsonl"

    exit_code = asyncio.run(
        run_cli(
            CliOptions(
                workspace=workspace,
                task="执行不安全写入",
                trace=trace_path,
                yes=True,
            ),
            model=model,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            environment={},
        )
    )

    assert exit_code == 0
    assert protected_file.read_text(encoding="utf-8") == "原始内容\n"
    assert not (tmp_path / "outside.md").exists()
    outputs = [
        json.loads(item["output"])
        for item in model.requests[1]
        if item.get("type") == "function_call_output"
    ]
    assert [output["error"]["code"] for output in outputs] == [
        ErrorCode.HASH_REQUIRED.value,
        ErrorCode.PATH_OUTSIDE_WORKSPACE.value,
    ]
    trace_records = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(trace_records) == 2
    assert all(
        set(record) == {"step", "tool", "args", "result_summary"}
        for record in trace_records
    )
