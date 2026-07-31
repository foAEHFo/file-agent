"""Temporary workspace copies and their in-memory conversation state."""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from file_agent.model import JsonObject
from file_agent.tools import FileTools
from file_agent.types import ToolResult


class WorkspaceNotFound(LookupError):
    pass


class WorkspaceBusy(RuntimeError):
    pass


@dataclass(slots=True)
class WorkspaceSession:
    id: str
    path: Path
    history: list[JsonObject] = field(default_factory=list)
    active_run_id: str | None = None
    last_activity: float = 0.0


class WorkspaceManager:
    """Own isolated seed copies without persisting session metadata."""

    def __init__(
        self,
        *,
        seed_path: Path,
        runtime_root: Path,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_seed = seed_path.resolve(strict=True)
        if not resolved_seed.is_dir():
            raise ValueError(f"Workspace seed is not a directory: {seed_path}")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.seed_path = resolved_seed
        self.runtime_root = runtime_root.resolve()
        self.workspaces_root = self.runtime_root / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, WorkspaceSession] = {}

    def create(self) -> WorkspaceSession:
        workspace_id = uuid4().hex
        path = self.workspaces_root / workspace_id
        shutil.copytree(self.seed_path, path, symlinks=True)
        session = WorkspaceSession(
            id=workspace_id,
            path=path,
            last_activity=self._clock(),
        )
        self._sessions[workspace_id] = session
        return session

    def get(self, workspace_id: str, *, touch: bool = True) -> WorkspaceSession:
        try:
            session = self._sessions[workspace_id]
        except KeyError as error:
            raise WorkspaceNotFound(workspace_id) from error
        if touch:
            self.touch(session)
        return session

    def touch(self, session: WorkspaceSession) -> None:
        session.last_activity = self._clock()

    def reset(self, workspace_id: str) -> WorkspaceSession:
        session = self.get(workspace_id, touch=False)
        if session.active_run_id is not None:
            raise WorkspaceBusy(f"Workspace {workspace_id} has an active run")

        temporary_parent = Path(
            tempfile.mkdtemp(
                prefix=f".reset-{workspace_id}-",
                dir=self.workspaces_root,
            )
        )
        replacement = temporary_parent / "workspace"
        backup = temporary_parent / "previous"
        try:
            shutil.copytree(self.seed_path, replacement, symlinks=True)
            session.path.rename(backup)
            try:
                replacement.rename(session.path)
            except Exception:
                backup.rename(session.path)
                raise
            shutil.rmtree(backup)
        finally:
            shutil.rmtree(temporary_parent, ignore_errors=True)

        session.history.clear()
        self.touch(session)
        return session

    def tree(
        self,
        workspace_id: str,
        *,
        max_entries: int = 500,
    ) -> ToolResult:
        session = self.get(workspace_id)
        return FileTools(session.path, capture_manifest=False).execute(
            "list_directory",
            {
                "path": ".",
                "recursive": True,
                "max_entries": max_entries,
            },
        )

    def file_page(
        self,
        workspace_id: str,
        *,
        path: str,
        start_line: int,
        max_lines: int,
    ) -> ToolResult:
        session = self.get(workspace_id)
        return FileTools(session.path, capture_manifest=False).execute(
            "read_file",
            {
                "path": path,
                "start_line": start_line,
                "max_lines": max_lines,
            },
        )

    def expired_ids(self) -> list[str]:
        now = self._clock()
        return sorted(
            session.id
            for session in self._sessions.values()
            if now - session.last_activity >= self.ttl_seconds
        )

    def remove(self, workspace_id: str) -> None:
        try:
            session = self._sessions.pop(workspace_id)
        except KeyError as error:
            raise WorkspaceNotFound(workspace_id) from error
        shutil.rmtree(session.path, ignore_errors=True)
