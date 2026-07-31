from __future__ import annotations

import json
from pathlib import Path


def test_trace_writer_flushes_strict_four_field_jsonl(tmp_path: Path) -> None:
    from file_agent.trace import TraceWriter

    trace_path = tmp_path / "runs" / "run-1" / "trace.jsonl"
    with TraceWriter(trace_path) as writer:
        writer.append(
            tool="search_files",
            args={"query": "Phoenix", "path": "."},
            result_summary="Found 10 matching files.",
        )

        visible_during_run = trace_path.read_text(encoding="utf-8")

        writer.append(
            tool="write_file",
            args={"_raw": "{broken"},
            result_summary="Invalid arguments.",
        )

    lines = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert json.loads(visible_during_run) == {
        "step": 1,
        "tool": "search_files",
        "args": {"query": "Phoenix", "path": "."},
        "result_summary": "Found 10 matching files.",
    }
    assert lines[1] == {
        "step": 2,
        "tool": "write_file",
        "args": {"_raw": "{broken"},
        "result_summary": "Invalid arguments.",
    }
    assert all(
        set(line) == {"step", "tool", "args", "result_summary"} for line in lines
    )
