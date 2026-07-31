"""Path confinement for all workspace file operations."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from file_agent.types import ErrorCode


class SandboxViolation(ValueError):
    """A stable policy failure raised while resolving a workspace path."""

    def __init__(self, code: ErrorCode, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class Sandbox:
    """Resolve untrusted relative paths without following workspace symlinks."""

    def __init__(self, root: Path) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {root}")
        self.root = resolved_root

    def resolve(self, untrusted_path: str, *, must_exist: bool = False) -> Path:
        candidate = Path(untrusted_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SandboxViolation(
                ErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"Path must remain inside the workspace: {untrusted_path}",
                untrusted_path,
            )

        parts = tuple(part for part in candidate.parts if part not in ("", "."))
        target = self.root.joinpath(*parts)
        current = self.root
        missing_component = False
        for part in parts:
            current /= part
            if missing_component:
                continue
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                missing_component = True
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise SandboxViolation(
                    ErrorCode.SYMLINK_NOT_ALLOWED,
                    f"Symbolic links are not allowed: {untrusted_path}",
                    untrusted_path,
                )

        if must_exist and missing_component:
            raise SandboxViolation(
                ErrorCode.FILE_NOT_FOUND,
                f"Path does not exist: {untrusted_path}",
                untrusted_path,
            )
        return target

    def relative(self, path: Path) -> str:
        """Return a normalized POSIX path relative to this workspace."""

        try:
            relative_path = path.relative_to(self.root)
        except ValueError as error:
            raise SandboxViolation(
                ErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"Path is outside the workspace: {path}",
                str(path),
            ) from error
        return relative_path.as_posix() or "."
