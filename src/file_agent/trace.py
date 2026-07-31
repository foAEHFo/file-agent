"""Strict JSONL trace output shared by CLI and Web runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Any, TextIO


class TraceWriter:
    """Append immediately visible four-field tool records."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file: TextIO = path.open("w", encoding="utf-8", newline="\n")
        self._step = 0
        self._lock = Lock()

    def append(
        self,
        *,
        tool: str,
        args: Mapping[str, Any],
        result_summary: str,
    ) -> None:
        with self._lock:
            self._step += 1
            record = {
                "step": self._step,
                "tool": tool,
                "args": dict(args),
                "result_summary": result_summary,
            }
            self._file.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
