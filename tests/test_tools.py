from __future__ import annotations

import hashlib
from pathlib import Path


def test_list_directory_returns_sorted_bounded_metadata(tmp_path: Path) -> None:
    from file_agent.tools import FileTools

    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"ignored")
    tools = FileTools(tmp_path)

    result = tools.execute("list_directory", {"max_entries": 1})

    assert result.to_dict() == {
        "ok": True,
        "summary": "Listed 1 entry; results were truncated.",
        "data": {
            "entries": [{"path": "a", "type": "directory", "size": 0}],
            "truncated": True,
        },
        "error": None,
    }


def test_list_directory_recursive_paths_are_globally_sorted(tmp_path: Path) -> None:
    from file_agent.tools import FileTools

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "nested.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    tools = FileTools(tmp_path)

    result = tools.execute("list_directory", {"recursive": True})

    assert result.ok
    assert [entry["path"] for entry in result.data["entries"]] == [
        "a",
        "a/nested.txt",
        "b.txt",
    ]


def test_search_files_is_literal_streaming_and_skips_non_utf8(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools

    (tmp_path / "notes.txt").write_text(
        "Project Phoenix\nproject falcon\n",
        encoding="utf-8",
    )
    (tmp_path / "literal.txt").write_text("Project.* means literal\n", encoding="utf-8")
    (tmp_path / "binary.txt").write_bytes(b"Project\x00binary")
    (tmp_path / "invalid.txt").write_bytes(b"Project then invalid: \xff")
    (tmp_path / "late-invalid.txt").write_bytes(
        b"Project.* appears first\n" + (b"x" * 70_000) + b"\xff"
    )
    tools = FileTools(tmp_path)

    result = tools.execute(
        "search_files",
        {
            "query": "Project.*",
            "glob": "*.txt",
            "case_sensitive": False,
        },
    )

    assert result.ok
    assert result.data == {
        "matches": [
            {
                "path": "literal.txt",
                "line": 1,
                "untrusted_snippet": "Project.* means literal",
            }
        ],
        "truncated": False,
        "skipped_files": 3,
    }


def test_search_files_handles_long_lines_and_stops_at_match_limit(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools

    long_prefix = "x" * 150_000
    (tmp_path / "large.log").write_text(
        f"{long_prefix}needle after boundary\nneedle second line\n",
        encoding="utf-8",
    )
    tools = FileTools(tmp_path)

    result = tools.execute(
        "search_files",
        {"query": "needle", "max_matches": 1},
    )

    assert result.ok
    assert result.data["truncated"] is True
    assert len(result.data["matches"]) == 1
    assert result.data["matches"][0]["line"] == 1
    assert "needle" in result.data["matches"][0]["untrusted_snippet"]
    assert len(result.data["matches"][0]["untrusted_snippet"]) <= 240


def test_read_file_returns_a_bounded_range_and_whole_file_version(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools

    content = "first\nsecond\nthird\n"
    (tmp_path / "notes.md").write_text(content, encoding="utf-8")
    tools = FileTools(tmp_path)

    result = tools.execute(
        "read_file",
        {"path": "notes.md", "start_line": 2, "max_lines": 1},
    )

    assert result.ok
    assert result.data == {
        "path": "notes.md",
        "start_line": 2,
        "end_line": 2,
        "has_more": True,
        "next_start_line": 3,
        "size": len(content.encode()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "untrusted_content": "second\n",
        "truncated_by_bytes": False,
    }


def test_read_file_enforces_per_call_and_per_run_byte_budgets(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    (tmp_path / "long.txt").write_text("é" * 20_000, encoding="utf-8")
    tools = FileTools(tmp_path, max_run_file_content_bytes=32_768)

    first = tools.execute("read_file", {"path": "long.txt"})
    second = tools.execute("read_file", {"path": "long.txt"})

    assert first.ok
    assert len(first.data["untrusted_content"].encode("utf-8")) <= 32_768
    assert first.data["truncated_by_bytes"] is True
    assert first.data["guidance"]
    assert second.error
    assert second.error.code is ErrorCode.READ_BUDGET_EXCEEDED


def test_read_file_returns_an_explicit_empty_range_past_end(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools

    (tmp_path / "short.txt").write_text("only\n", encoding="utf-8")
    tools = FileTools(tmp_path)

    result = tools.execute(
        "read_file",
        {"path": "short.txt", "start_line": 99},
    )

    assert result.ok
    assert result.data["end_line"] is None
    assert result.data["has_more"] is False
    assert result.data["next_start_line"] is None
    assert result.data["untrusted_content"] == ""


def test_make_directory_requires_explicit_parent_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    tools = FileTools(tmp_path)

    missing_parent = tools.execute("make_directory", {"path": "parent/child"})
    created = tools.execute("make_directory", {"path": "parent"})
    repeated = tools.execute("make_directory", {"path": "parent"})

    assert missing_parent.error
    assert missing_parent.error.code is ErrorCode.FILE_NOT_FOUND
    assert created.ok and created.data == {"path": "parent", "created": True}
    assert repeated.ok and repeated.data == {"path": "parent", "created": False}


def test_write_file_enforces_create_and_versioned_overwrite(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    tools = FileTools(tmp_path)
    created = tools.execute(
        "write_file",
        {"path": "draft.md", "content": "version one", "mode": "create"},
    )
    old_hash = hashlib.sha256(b"version one").hexdigest()
    mismatch = tools.execute(
        "write_file",
        {
            "path": "draft.md",
            "content": "wrong",
            "mode": "overwrite",
            "expected_sha256": "0" * 64,
        },
    )
    overwritten = tools.execute(
        "write_file",
        {
            "path": "draft.md",
            "content": "version two",
            "mode": "overwrite",
            "expected_sha256": old_hash,
        },
    )

    assert created.ok
    assert mismatch.error and mismatch.error.code is ErrorCode.HASH_MISMATCH
    assert overwritten.ok
    assert (tmp_path / "draft.md").read_text(encoding="utf-8") == "version two"
    assert not list(tmp_path.glob(".file-agent-*"))


def test_write_file_rejects_conflicts_missing_hash_and_oversized_content(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    (tmp_path / "existing.md").write_text("keep", encoding="utf-8")
    tools = FileTools(tmp_path)

    create_conflict = tools.execute(
        "write_file",
        {"path": "existing.md", "content": "replace", "mode": "create"},
    )
    missing_hash = tools.execute(
        "write_file",
        {"path": "existing.md", "content": "replace", "mode": "overwrite"},
    )
    too_large = tools.execute(
        "write_file",
        {"path": "large.md", "content": "x" * 131_073, "mode": "create"},
    )

    assert create_conflict.error
    assert create_conflict.error.code is ErrorCode.DESTINATION_EXISTS
    assert missing_hash.error and missing_hash.error.code is ErrorCode.HASH_REQUIRED
    assert too_large.error and too_large.error.code is ErrorCode.CONTENT_TOO_LARGE
    assert (tmp_path / "existing.md").read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "large.md").exists()


def test_move_file_requires_source_version_and_never_overwrites(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    (tmp_path / "source.md").write_text("move me", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    source_hash = hashlib.sha256(b"move me").hexdigest()
    tools = FileTools(tmp_path)

    missing_hash = tools.execute(
        "move_file",
        {"source": "source.md", "destination": "archive/source.md"},
    )
    moved = tools.execute(
        "move_file",
        {
            "source": "source.md",
            "destination": "archive/source.md",
            "expected_sha256": source_hash,
        },
    )

    assert missing_hash.error
    assert missing_hash.error.code is ErrorCode.HASH_REQUIRED
    assert moved.ok
    assert moved.data == {
        "source": "source.md",
        "destination": "archive/source.md",
        "sha256": source_hash,
    }
    assert not (tmp_path / "source.md").exists()
    assert (tmp_path / "archive" / "source.md").read_text(encoding="utf-8") == "move me"


def test_move_file_rejects_stale_source_and_existing_destination(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools
    from file_agent.types import ErrorCode

    (tmp_path / "source.md").write_text("source", encoding="utf-8")
    (tmp_path / "destination.md").write_text("destination", encoding="utf-8")
    source_hash = hashlib.sha256(b"source").hexdigest()
    tools = FileTools(tmp_path)

    stale = tools.execute(
        "move_file",
        {
            "source": "source.md",
            "destination": "new.md",
            "expected_sha256": "0" * 64,
        },
    )
    conflict = tools.execute(
        "move_file",
        {
            "source": "source.md",
            "destination": "destination.md",
            "expected_sha256": source_hash,
        },
    )

    assert stale.error and stale.error.code is ErrorCode.HASH_MISMATCH
    assert conflict.error and conflict.error.code is ErrorCode.DESTINATION_EXISTS
    assert (tmp_path / "source.md").read_text(encoding="utf-8") == "source"
    assert (tmp_path / "destination.md").read_text(encoding="utf-8") == "destination"


def test_tool_registry_exposes_no_delete_or_shell_capability(tmp_path: Path) -> None:
    from file_agent.tools import MUTATING_TOOLS, TOOL_NAMES, FileTools
    from file_agent.types import ErrorCode

    tools = FileTools(tmp_path)

    delete = tools.execute("delete_file", {"path": "anything"})
    shell = tools.execute("shell", {"command": "true"})

    assert {
        "list_directory",
        "search_files",
        "read_file",
        "make_directory",
        "write_file",
        "move_file",
        "get_workspace_changes",
    } == TOOL_NAMES
    assert {"make_directory", "write_file", "move_file"} == MUTATING_TOOLS
    assert delete.error and delete.error.code is ErrorCode.DENIED_BY_POLICY
    assert shell.error and shell.error.code is ErrorCode.DENIED_BY_POLICY


def test_get_workspace_changes_compares_the_run_start_manifest(
    tmp_path: Path,
) -> None:
    from file_agent.tools import FileTools

    (tmp_path / "archive").mkdir()
    (tmp_path / "move.md").write_text("move", encoding="utf-8")
    (tmp_path / "modify.md").write_text("before", encoding="utf-8")
    (tmp_path / "delete.md").write_text("delete", encoding="utf-8")
    tools = FileTools(tmp_path)

    (tmp_path / "move.md").rename(tmp_path / "archive" / "move.md")
    (tmp_path / "modify.md").write_text("after", encoding="utf-8")
    (tmp_path / "delete.md").unlink()
    (tmp_path / "create.md").write_text("create", encoding="utf-8")

    result = tools.execute("get_workspace_changes", {})

    assert result.ok
    assert result.data == {
        "created": ["create.md"],
        "modified": ["modify.md"],
        "deleted": ["delete.md"],
        "moved": [{"source": "move.md", "destination": "archive/move.md"}],
    }
