"""Command-line entry point for one end-to-end file-agent run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from file_agent.agent import (
    AgentEvent,
    AgentEventKind,
    AgentRunner,
    RunLimits,
    RunStatus,
)
from file_agent.approval import CliApprovalHandler
from file_agent.model import OpenAIResponsesClient, ResponsesClient
from file_agent.prompts import SYSTEM_INSTRUCTIONS, TOOL_DEFINITIONS
from file_agent.tools import FileTools
from file_agent.trace import TraceWriter


@dataclass(frozen=True, slots=True)
class CliOptions:
    workspace: Path
    task: str
    trace: Path
    yes: bool


class CliEventRenderer:
    """Render internal events immediately as stable, human-readable lines."""

    def __init__(self, output_stream: TextIO) -> None:
        self._output = output_stream
        self.answer_emitted = False

    async def emit(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.REASONING_DELTA:
            self._line("推理", str(data.get("delta", "")))
        elif event.kind is AgentEventKind.ANSWER_DELTA:
            self.answer_emitted = True
            self._line("回答", str(data.get("delta", "")))
        elif event.kind is AgentEventKind.TOOL_STARTED:
            arguments = json.dumps(
                data.get("args", {}),
                ensure_ascii=False,
                sort_keys=True,
            )
            self._line(
                "工具",
                f"第 {data.get('step')} 步 {data.get('tool')} {arguments}",
            )
        elif event.kind is AgentEventKind.APPROVAL_REQUIRED:
            self._line("审批", f"等待确认 {data.get('tool')}")
        elif event.kind is AgentEventKind.APPROVAL_RESOLVED:
            decision = "已批准" if data.get("approved") else "已拒绝"
            self._line("审批", f"{decision} {data.get('tool')}")
        elif event.kind is AgentEventKind.TOOL_COMPLETED:
            result = data.get("result")
            summary = result.get("summary", "") if isinstance(result, Mapping) else ""
            self._line("结果", str(summary))
        elif event.kind is AgentEventKind.USAGE_UPDATED:
            self._line(
                "用量",
                (
                    f"输入 {data.get('input_tokens', 0)}，"
                    f"输出 {data.get('output_tokens', 0)}，"
                    f"推理 {data.get('reasoning_tokens', 0)}，"
                    f"总计 {data.get('total_tokens', 0)} tokens"
                ),
            )
        elif event.kind is AgentEventKind.LLM_RETRYING:
            self._line("重试", str(data.get("error", "")))
        elif event.kind is AgentEventKind.RUN_INCOMPLETE:
            self._line("未完成", str(data.get("error", "")))
        elif event.kind is AgentEventKind.RUN_FAILED:
            self._line("失败", str(data.get("error", "")))

    def write_answer(self, answer: str) -> None:
        self.answer_emitted = True
        self._line("回答", answer)

    def _line(self, label: str, text: str) -> None:
        self._output.write(f"[{label}] {text}\n")
        self._output.flush()


def parse_args(argv: Sequence[str] | None = None) -> CliOptions:
    parser = argparse.ArgumentParser(description="在指定工作区运行文件助理 Agent")
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Agent 可以操作的工作区目录",
    )
    parser.add_argument("--task", required=True, help="要完成的自然语言任务")
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path("trace.jsonl"),
        help="工具调用 trace 文件路径（默认：./trace.jsonl）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="自动批准每个写操作；沙箱和哈希校验仍然生效",
    )
    parsed = parser.parse_args(argv)
    return CliOptions(
        workspace=parsed.workspace,
        task=parsed.task,
        trace=parsed.trace,
        yes=parsed.yes,
    )


async def run_cli(
    options: CliOptions,
    *,
    model: ResponsesClient | None = None,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    environment: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environment is None else environment
    try:
        limits = RunLimits(
            max_llm_calls=_positive_environment(environment, "MAX_LLM_CALLS", 30),
            max_tool_calls=_positive_environment(environment, "MAX_TOOL_CALLS", 100),
            active_timeout_seconds=float(
                _positive_environment(environment, "RUN_TIMEOUT_SECONDS", 1200)
            ),
        )
        tools = FileTools(
            options.workspace,
            max_run_file_content_bytes=_positive_environment(
                environment,
                "MAX_RUN_FILE_CONTENT_BYTES",
                262_144,
            ),
        )
        selected_model = model or _model_from_environment(environment)
        renderer = CliEventRenderer(output_stream)
        approval = CliApprovalHandler(
            auto_approve=options.yes,
            input_stream=input_stream,
            output_stream=output_stream,
        )
        with TraceWriter(options.trace) as trace:
            result = await AgentRunner(
                model=selected_model,
                tools=tools,
                approval=approval,
                trace=trace,
                limits=limits,
            ).run(
                task=options.task,
                history=[],
                emit=renderer.emit,
            )
        if result.answer and not renderer.answer_emitted:
            renderer.write_answer(result.answer)
        output_stream.write(f"[追踪] {options.trace}\n")
        output_stream.flush()
        return 0 if result.status is RunStatus.COMPLETED else 1
    except (KeyError, OSError, ValueError) as error:
        output_stream.write(f"[错误] {error}\n")
        output_stream.flush()
        return 2


def _model_from_environment(
    environment: Mapping[str, str],
) -> OpenAIResponsesClient:
    required = ("OPENAI_API_KEY", "OPENAI_MODEL")
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise ValueError(f"缺少必要环境变量：{', '.join(missing)}")
    return OpenAIResponsesClient(
        api_key=environment["OPENAI_API_KEY"],
        base_url=environment.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=environment["OPENAI_MODEL"],
        instructions=SYSTEM_INSTRUCTIONS,
        tools=TOOL_DEFINITIONS,
    )


def _positive_environment(
    environment: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw_value = environment.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是正整数，当前值为 {raw_value!r}") from error
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数，当前值为 {raw_value!r}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        return asyncio.run(run_cli(options))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
