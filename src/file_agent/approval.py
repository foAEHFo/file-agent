"""Approval policies shared by interactive and automated CLI runs."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from file_agent.agent import ApprovalRequest


class CliApprovalHandler:
    """Approve each mutation according to the CLI's TTY policy."""

    def __init__(
        self,
        *,
        auto_approve: bool,
        input_stream: TextIO = sys.stdin,
        output_stream: TextIO = sys.stdout,
    ) -> None:
        self._auto_approve = auto_approve
        self._input = input_stream
        self._output = output_stream

    async def request(self, request: ApprovalRequest) -> bool:
        if self._auto_approve:
            return True
        if not self._input.isatty():
            self._write("标准输入不是 TTY，本次写操作默认拒绝。\n")
            return False

        arguments = json.dumps(
            dict(request.args),
            ensure_ascii=False,
            sort_keys=True,
        )
        self._write(
            f"待审批写操作：{request.tool} {arguments}\n"
            "是否批准？请输入 y 或 yes：[y/N] "
        )
        answer = self._input.readline().strip().casefold()
        self._write("\n")
        return answer in {"y", "yes"}

    def _write(self, text: str) -> None:
        self._output.write(text)
        self._output.flush()
