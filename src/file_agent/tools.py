"""Deterministic file tools operating through the workspace sandbox."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from file_agent.sandbox import Sandbox, SandboxViolation
from file_agent.types import ErrorCode, ToolResult

TOOL_NAMES = frozenset(
    {
        "list_directory",
        "search_files",
        "read_file",
        "make_directory",
        "write_file",
        "move_file",
        "get_workspace_changes",
    }
)
MUTATING_TOOLS = frozenset({"make_directory", "write_file", "move_file"})


class FileTools:
    """Execute the seven file tools against one bounded workspace."""

    def __init__(
        self, workspace: Path, *, max_run_file_content_bytes: int = 262_144
    ) -> None:
        self.sandbox = Sandbox(workspace)
        self.max_run_file_content_bytes = max_run_file_content_bytes
        self._returned_file_content_bytes = 0
        self._initial_manifest = self._snapshot_manifest()

    def execute(self, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Execute a named tool and return the uniform result contract."""

        try:
            if tool == "list_directory":
                return self._list_directory(
                    path=str(arguments.get("path", ".")),
                    recursive=bool(arguments.get("recursive", False)),
                    max_entries=int(arguments.get("max_entries", 200)),
                )
            if tool == "search_files":
                return self._search_files(
                    query=str(arguments["query"]),
                    path=str(arguments.get("path", ".")),
                    glob=str(arguments.get("glob", "*")),
                    case_sensitive=bool(arguments.get("case_sensitive", False)),
                    max_matches=int(arguments.get("max_matches", 50)),
                )
            if tool == "read_file":
                return self._read_file(
                    path=str(arguments["path"]),
                    start_line=int(arguments.get("start_line", 1)),
                    max_lines=int(arguments.get("max_lines", 200)),
                )
            if tool == "make_directory":
                return self._make_directory(path=str(arguments["path"]))
            if tool == "write_file":
                expected_sha256 = arguments.get("expected_sha256")
                return self._write_file(
                    path=str(arguments["path"]),
                    content=str(arguments["content"]),
                    mode=str(arguments["mode"]),
                    expected_sha256=(
                        str(expected_sha256) if expected_sha256 is not None else None
                    ),
                )
            if tool == "move_file":
                expected_sha256 = arguments.get("expected_sha256")
                return self._move_file(
                    source=str(arguments["source"]),
                    destination=str(arguments["destination"]),
                    expected_sha256=(
                        str(expected_sha256) if expected_sha256 is not None else None
                    ),
                )
            if tool == "get_workspace_changes":
                return self._get_workspace_changes()
            return ToolResult.failure(
                code=ErrorCode.DENIED_BY_POLICY,
                message=f"Unknown tool: {tool}",
                summary=f"Tool {tool} is not available.",
            )
        except SandboxViolation as error:
            return ToolResult.failure(
                code=error.code,
                message=str(error),
                summary=f"Unable to access {error.path}.",
                details={"path": error.path},
            )
        except FileNotFoundError as error:
            return ToolResult.failure(
                code=ErrorCode.FILE_NOT_FOUND,
                message="A file changed or disappeared during the operation.",
                summary="The file operation could not be completed.",
                details={"errno": error.errno},
            )
        except OSError as error:
            return ToolResult.failure(
                code=ErrorCode.DENIED_BY_POLICY,
                message="The filesystem rejected the operation.",
                summary="The file operation could not be completed.",
                details={"errno": error.errno},
            )

    def _list_directory(
        self, *, path: str, recursive: bool, max_entries: int
    ) -> ToolResult:
        if max_entries <= 0:
            return _policy_failure("max_entries must be positive.")
        directory = self.sandbox.resolve(path, must_exist=True)
        if not directory.is_dir():
            return _policy_failure(f"Not a directory: {path}", path=path)

        entries: list[dict[str, Any]] = []
        truncated = False
        pending = list(reversed(_sorted_children(directory)))
        while pending:
            if len(entries) == max_entries:
                truncated = True
                break
            child = pending.pop()
            metadata = os.lstat(child)
            relative = self.sandbox.relative(child)
            if stat.S_ISLNK(metadata.st_mode):
                raise SandboxViolation(
                    ErrorCode.SYMLINK_NOT_ALLOWED,
                    f"Symbolic links are not allowed: {relative}",
                    relative,
                )
            is_directory = stat.S_ISDIR(metadata.st_mode)
            entries.append(
                {
                    "path": relative,
                    "type": "directory" if is_directory else "file",
                    "size": 0 if is_directory else metadata.st_size,
                }
            )
            if recursive and is_directory:
                pending.extend(reversed(_sorted_children(child)))

        count = len(entries)
        noun = "entry" if count == 1 else "entries"
        summary = f"Listed {count} {noun}"
        if truncated:
            summary += "; results were truncated"
        return ToolResult.success(
            summary=f"{summary}.",
            data={"entries": entries, "truncated": truncated},
        )

    def _search_files(
        self,
        *,
        query: str,
        path: str,
        glob: str,
        case_sensitive: bool,
        max_matches: int,
    ) -> ToolResult:
        if not query:
            return _policy_failure("query must not be empty.")
        if max_matches <= 0:
            return _policy_failure("max_matches must be positive.")
        start = self.sandbox.resolve(path, must_exist=True)
        matches: list[dict[str, Any]] = []
        skipped_files = 0
        truncated = False

        for file_path in self._iter_regular_files(start):
            relative = self.sandbox.relative(file_path)
            if not Path(relative).match(glob):
                continue
            file_matches, skipped = self._search_one_file(
                file_path,
                relative=relative,
                query=query,
                case_sensitive=case_sensitive,
                limit=max_matches - len(matches) + 1,
            )
            if skipped:
                skipped_files += 1
                continue
            remaining = max_matches - len(matches)
            matches.extend(file_matches[:remaining])
            if len(file_matches) > remaining:
                truncated = True
                break

        matched_file_count = len({match["path"] for match in matches})
        summary = (
            f"Found {len(matches)} matches in {matched_file_count} files"
            if len(matches) != 1
            else "Found 1 match in 1 file"
        )
        if truncated:
            summary += "; results were truncated"
        return ToolResult.success(
            summary=f"{summary}.",
            data={
                "matches": matches,
                "truncated": truncated,
                "skipped_files": skipped_files,
            },
        )

    def _iter_regular_files(self, start: Path) -> list[Path]:
        metadata = os.lstat(start)
        if stat.S_ISLNK(metadata.st_mode):
            raise SandboxViolation(
                ErrorCode.SYMLINK_NOT_ALLOWED,
                f"Symbolic links are not allowed: {self.sandbox.relative(start)}",
                self.sandbox.relative(start),
            )
        if stat.S_ISREG(metadata.st_mode):
            return [start]
        if not stat.S_ISDIR(metadata.st_mode):
            return []

        files: list[Path] = []
        pending = [start]
        while pending:
            directory = pending.pop()
            child_directories: list[Path] = []
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                if child.name == ".DS_Store":
                    continue
                child_metadata = os.lstat(child)
                relative = self.sandbox.relative(child)
                if stat.S_ISLNK(child_metadata.st_mode):
                    raise SandboxViolation(
                        ErrorCode.SYMLINK_NOT_ALLOWED,
                        f"Symbolic links are not allowed: {relative}",
                        relative,
                    )
                if stat.S_ISDIR(child_metadata.st_mode):
                    child_directories.append(child)
                elif stat.S_ISREG(child_metadata.st_mode):
                    files.append(child)
            pending.extend(reversed(child_directories))
        return sorted(files, key=self.sandbox.relative)

    @staticmethod
    def _search_one_file(
        file_path: Path,
        *,
        relative: str,
        query: str,
        case_sensitive: bool,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        matches: list[dict[str, Any]] = []
        needle = query if case_sensitive else query.casefold()
        overlap_size = max(len(needle) - 1, 120)
        line_number = 1
        overlap = ""
        matched_line = False

        try:
            with file_path.open(
                "r", encoding="utf-8", errors="strict", newline=""
            ) as file:
                while fragment := file.readline(65_536):
                    if "\x00" in fragment:
                        return [], True
                    search_fragment = overlap + fragment
                    comparable = (
                        search_fragment
                        if case_sensitive
                        else search_fragment.casefold()
                    )
                    match_index = comparable.find(needle)
                    if not matched_line and match_index >= 0:
                        if len(matches) < limit:
                            snippet_start = max(0, match_index - 120)
                            snippet = search_fragment[
                                snippet_start : snippet_start + 240
                            ].rstrip("\r\n")
                            matches.append(
                                {
                                    "path": relative,
                                    "line": line_number,
                                    "untrusted_snippet": snippet,
                                }
                            )
                        matched_line = True

                    if fragment.endswith(("\n", "\r")):
                        line_number += 1
                        overlap = ""
                        matched_line = False
                    else:
                        overlap = search_fragment[-overlap_size:]
        except UnicodeDecodeError:
            return [], True
        return matches, False

    def _read_file(self, *, path: str, start_line: int, max_lines: int) -> ToolResult:
        if start_line <= 0:
            return _policy_failure("start_line must be positive.", path=path)
        if max_lines <= 0 or max_lines > 500:
            return _policy_failure(
                "max_lines must be between 1 and 500.",
                path=path,
            )
        file_path = self.sandbox.resolve(path, must_exist=True)
        metadata = os.lstat(file_path)
        if not stat.S_ISREG(metadata.st_mode):
            return _policy_failure(f"Not a regular file: {path}", path=path)

        remaining_budget = (
            self.max_run_file_content_bytes - self._returned_file_content_bytes
        )
        if remaining_budget <= 0:
            return ToolResult.failure(
                code=ErrorCode.READ_BUDGET_EXCEEDED,
                message="The per-run file content budget has been exhausted.",
                summary="Unable to read more file content in this run.",
                details={
                    "limit_bytes": self.max_run_file_content_bytes,
                    "path": path,
                },
            )

        byte_limit = min(32_768, remaining_budget)
        file_size = metadata.st_size
        sha256 = _sha256_file(file_path)
        start_offset = _line_start_offset(file_path, start_line)
        with file_path.open("rb") as file:
            file.seek(start_offset)
            candidate = file.read(byte_limit + 1)

        newline_ends = [
            index + 1 for index, value in enumerate(candidate) if value == ord("\n")
        ]
        requested_end = (
            newline_ends[max_lines - 1]
            if len(newline_ends) >= max_lines
            else len(candidate)
        )
        raw_content = candidate[: min(requested_end, byte_limit)]
        truncated_by_bytes = requested_end > byte_limit
        try:
            untrusted_content = raw_content.decode("utf-8")
        except UnicodeDecodeError as error:
            if (
                error.end == len(raw_content)
                and error.reason == "unexpected end of data"
            ):
                raw_content = raw_content[: error.start]
                untrusted_content = raw_content.decode("utf-8")
                truncated_by_bytes = True
            else:
                return _policy_failure(
                    f"File is not valid UTF-8: {path}",
                    path=path,
                )

        returned_size = len(raw_content)
        if returned_size == 0 and start_offset < file_size:
            return ToolResult.failure(
                code=ErrorCode.READ_BUDGET_EXCEEDED,
                message=(
                    "The remaining per-run budget cannot hold the next UTF-8 character."
                ),
                summary="Unable to read more file content in this run.",
                details={
                    "limit_bytes": self.max_run_file_content_bytes,
                    "path": path,
                },
            )
        self._returned_file_content_bytes += returned_size
        content_line_count = untrusted_content.count("\n")
        if untrusted_content and not untrusted_content.endswith("\n"):
            content_line_count += 1
        end_line = (
            start_line + content_line_count - 1 if content_line_count > 0 else None
        )
        has_more = start_offset + returned_size < file_size
        data: dict[str, Any] = {
            "path": self.sandbox.relative(file_path),
            "start_line": start_line,
            "end_line": end_line,
            "has_more": has_more,
            "next_start_line": (
                end_line + 1 if has_more and end_line is not None else None
            ),
            "size": file_size,
            "sha256": sha256,
            "untrusted_content": untrusted_content,
            "truncated_by_bytes": truncated_by_bytes,
        }
        if file_size > 32_768:
            data["guidance"] = (
                "For large files, search first and read only the relevant line range."
            )
        relative_path = self.sandbox.relative(file_path)
        summary = (
            f"Read lines {start_line}-{end_line} from {relative_path}."
            if end_line is not None
            else f"No content found at or after line {start_line} in {relative_path}."
        )
        return ToolResult.success(summary=summary, data=data)

    def _make_directory(self, *, path: str) -> ToolResult:
        directory = self.sandbox.resolve(path)
        if directory.exists():
            if directory.is_dir():
                return ToolResult.success(
                    summary=f"Directory already exists: {path}.",
                    data={"path": self.sandbox.relative(directory), "created": False},
                )
            return ToolResult.failure(
                code=ErrorCode.DESTINATION_EXISTS,
                message=f"A non-directory already exists at {path}.",
                summary=f"Unable to create directory {path}.",
                details={"path": path},
            )
        if not directory.parent.is_dir():
            return ToolResult.failure(
                code=ErrorCode.FILE_NOT_FOUND,
                message=f"Parent directory does not exist: {path}.",
                summary=f"Unable to create directory {path}.",
                details={"path": path},
            )
        directory.mkdir()
        return ToolResult.success(
            summary=f"Created directory {path}.",
            data={"path": self.sandbox.relative(directory), "created": True},
        )

    def _write_file(
        self,
        *,
        path: str,
        content: str,
        mode: str,
        expected_sha256: str | None,
    ) -> ToolResult:
        encoded_content = content.encode("utf-8")
        if len(encoded_content) > 131_072:
            return ToolResult.failure(
                code=ErrorCode.CONTENT_TOO_LARGE,
                message="write_file content exceeds the 128KB limit.",
                summary=f"Unable to write {path}; content is too large.",
                details={"path": path, "limit_bytes": 131_072},
            )
        if mode not in {"create", "overwrite"}:
            return _policy_failure(
                "mode must be either 'create' or 'overwrite'.",
                path=path,
            )

        destination = self.sandbox.resolve(path)
        if not destination.parent.is_dir():
            return ToolResult.failure(
                code=ErrorCode.FILE_NOT_FOUND,
                message=f"Parent directory does not exist: {path}.",
                summary=f"Unable to write {path}.",
                details={"path": path},
            )

        if mode == "create":
            if destination.exists():
                return ToolResult.failure(
                    code=ErrorCode.DESTINATION_EXISTS,
                    message=f"Destination already exists: {path}.",
                    summary=f"Unable to create {path}.",
                    details={"path": path},
                )
            created = _atomic_create(destination, encoded_content)
            if not created:
                return ToolResult.failure(
                    code=ErrorCode.DESTINATION_EXISTS,
                    message=f"Destination already exists: {path}.",
                    summary=f"Unable to create {path}.",
                    details={"path": path},
                )
        else:
            if expected_sha256 is None:
                return ToolResult.failure(
                    code=ErrorCode.HASH_REQUIRED,
                    message="expected_sha256 is required for overwrite.",
                    summary=f"Unable to overwrite {path} without a file version.",
                    details={"path": path},
                )
            if not destination.exists():
                return ToolResult.failure(
                    code=ErrorCode.FILE_NOT_FOUND,
                    message=f"File does not exist: {path}.",
                    summary=f"Unable to overwrite {path}.",
                    details={"path": path},
                )
            metadata = os.lstat(destination)
            if not stat.S_ISREG(metadata.st_mode):
                return _policy_failure(
                    f"Not a regular file: {path}",
                    path=path,
                )
            actual_sha256 = _sha256_file(destination)
            if actual_sha256 != expected_sha256:
                return _hash_mismatch(path, expected_sha256, actual_sha256)
            if not _atomic_overwrite(
                destination,
                encoded_content,
                expected_sha256=expected_sha256,
                mode=stat.S_IMODE(metadata.st_mode),
            ):
                return _hash_mismatch(
                    path,
                    expected_sha256,
                    _sha256_file(destination),
                )

        new_sha256 = hashlib.sha256(encoded_content).hexdigest()
        action = "Created" if mode == "create" else "Overwrote"
        return ToolResult.success(
            summary=f"{action} {path}.",
            data={
                "path": self.sandbox.relative(destination),
                "size": len(encoded_content),
                "sha256": new_sha256,
            },
        )

    def _move_file(
        self,
        *,
        source: str,
        destination: str,
        expected_sha256: str | None,
    ) -> ToolResult:
        if expected_sha256 is None:
            return ToolResult.failure(
                code=ErrorCode.HASH_REQUIRED,
                message="expected_sha256 is required for move_file.",
                summary=f"Unable to move {source} without a file version.",
                details={"source": source, "destination": destination},
            )
        source_path = self.sandbox.resolve(source, must_exist=True)
        destination_path = self.sandbox.resolve(destination)
        source_metadata = os.lstat(source_path)
        if not stat.S_ISREG(source_metadata.st_mode):
            return _policy_failure(
                f"Only regular files can be moved: {source}",
                path=source,
            )
        if not destination_path.parent.is_dir():
            return ToolResult.failure(
                code=ErrorCode.FILE_NOT_FOUND,
                message=f"Destination parent does not exist: {destination}.",
                summary=f"Unable to move {source}.",
                details={"source": source, "destination": destination},
            )
        if destination_path.exists():
            return ToolResult.failure(
                code=ErrorCode.DESTINATION_EXISTS,
                message=f"Destination already exists: {destination}.",
                summary=f"Unable to move {source}.",
                details={"source": source, "destination": destination},
            )

        actual_sha256 = _sha256_file(source_path)
        if actual_sha256 != expected_sha256:
            return _hash_mismatch(source, expected_sha256, actual_sha256)
        try:
            os.link(source_path, destination_path)
        except FileExistsError:
            return ToolResult.failure(
                code=ErrorCode.DESTINATION_EXISTS,
                message=f"Destination already exists: {destination}.",
                summary=f"Unable to move {source}.",
                details={"source": source, "destination": destination},
            )
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            return _policy_failure(
                "Moving files across filesystems is not allowed.",
                path=source,
            )

        moved_sha256 = _sha256_file(destination_path)
        if moved_sha256 != expected_sha256:
            destination_path.unlink()
            return _hash_mismatch(source, expected_sha256, moved_sha256)
        current_source_metadata = os.lstat(source_path)
        destination_metadata = os.lstat(destination_path)
        source_identity = (source_metadata.st_dev, source_metadata.st_ino)
        if (
            current_source_metadata.st_dev,
            current_source_metadata.st_ino,
        ) != source_identity or (
            destination_metadata.st_dev,
            destination_metadata.st_ino,
        ) != source_identity:
            destination_path.unlink()
            return _hash_mismatch(
                source,
                expected_sha256,
                _sha256_file(source_path),
            )
        source_path.unlink()
        return ToolResult.success(
            summary=f"Moved {source} to {destination}.",
            data={
                "source": self.sandbox.relative(source_path),
                "destination": self.sandbox.relative(destination_path),
                "sha256": moved_sha256,
            },
        )

    def _get_workspace_changes(self) -> ToolResult:
        current_manifest = self._snapshot_manifest()
        before_paths = set(self._initial_manifest)
        current_paths = set(current_manifest)
        created = current_paths - before_paths
        deleted = before_paths - current_paths
        modified = sorted(
            path
            for path in before_paths & current_paths
            if self._initial_manifest[path] != current_manifest[path]
        )

        deleted_by_hash: dict[str, list[str]] = {}
        for path in deleted:
            deleted_by_hash.setdefault(self._initial_manifest[path], []).append(path)
        created_by_hash: dict[str, list[str]] = {}
        for path in created:
            created_by_hash.setdefault(current_manifest[path], []).append(path)

        moved: list[dict[str, str]] = []
        for digest in sorted(deleted_by_hash.keys() & created_by_hash.keys()):
            sources = sorted(deleted_by_hash[digest])
            destinations = sorted(created_by_hash[digest])
            for source, destination in zip(sources, destinations, strict=False):
                moved.append({"source": source, "destination": destination})
                deleted.remove(source)
                created.remove(destination)

        created_result = sorted(created)
        deleted_result = sorted(deleted)
        moved_result = sorted(
            moved,
            key=lambda item: (item["source"], item["destination"]),
        )
        data: dict[str, Any] = {
            "created": created_result,
            "modified": modified,
            "deleted": deleted_result,
            "moved": moved_result,
        }
        change_count = (
            len(created_result)
            + len(modified)
            + len(deleted_result)
            + len(moved_result)
        )
        return ToolResult.success(
            summary=f"Found {change_count} workspace changes.",
            data=data,
        )

    def _snapshot_manifest(self) -> dict[str, str]:
        return {
            self.sandbox.relative(path): _sha256_file(path)
            for path in self._iter_regular_files(self.sandbox.root)
        }


def _policy_failure(message: str, *, path: str | None = None) -> ToolResult:
    details = {"path": path} if path is not None else {}
    return ToolResult.failure(
        code=ErrorCode.DENIED_BY_POLICY,
        message=message,
        summary=message,
        details=details,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _sorted_children(directory: Path) -> list[Path]:
    return sorted(
        (child for child in directory.iterdir() if child.name != ".DS_Store"),
        key=lambda child: child.name,
    )


def _line_start_offset(path: Path, line_number: int) -> int:
    if line_number == 1:
        return 0
    offset = 0
    current_line = 1
    with path.open("rb") as file:
        while chunk := file.read(65_536):
            for index, value in enumerate(chunk):
                if value == ord("\n"):
                    current_line += 1
                    if current_line == line_number:
                        return offset + index + 1
            offset += len(chunk)
    return offset


def _write_temp_file(parent: Path, content: bytes, *, mode: int = 0o600) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".file-agent-", dir=parent)
    temp_path = Path(raw_path)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _atomic_create(destination: Path, content: bytes) -> bool:
    temp_path = _write_temp_file(destination.parent, content)
    try:
        os.link(temp_path, destination)
    except FileExistsError:
        return False
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _atomic_overwrite(
    destination: Path,
    content: bytes,
    *,
    expected_sha256: str,
    mode: int,
) -> bool:
    temp_path = _write_temp_file(destination.parent, content, mode=mode)
    try:
        if _sha256_file(destination) != expected_sha256:
            return False
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _hash_mismatch(path: str, expected: str, actual: str) -> ToolResult:
    return ToolResult.failure(
        code=ErrorCode.HASH_MISMATCH,
        message=f"File version changed for {path}.",
        summary=f"Unable to modify {path}; its content no longer matches.",
        details={
            "path": path,
            "expected_sha256": expected,
            "actual_sha256": actual,
        },
    )
