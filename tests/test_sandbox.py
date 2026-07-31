from __future__ import annotations

from pathlib import Path

import pytest


def test_sandbox_rejects_parent_traversal(tmp_path: Path) -> None:
    from file_agent.sandbox import Sandbox, SandboxViolation
    from file_agent.types import ErrorCode

    sandbox = Sandbox(tmp_path)

    with pytest.raises(SandboxViolation) as raised:
        sandbox.resolve("../outside.txt")

    assert raised.value.code is ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_sandbox_rejects_absolute_paths(tmp_path: Path) -> None:
    from file_agent.sandbox import Sandbox, SandboxViolation
    from file_agent.types import ErrorCode

    sandbox = Sandbox(tmp_path)

    with pytest.raises(SandboxViolation) as raised:
        sandbox.resolve(str(tmp_path / "inside.txt"))

    assert raised.value.code is ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_sandbox_rejects_symlinked_path_components(tmp_path: Path) -> None:
    from file_agent.sandbox import Sandbox, SandboxViolation
    from file_agent.types import ErrorCode

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    sandbox = Sandbox(tmp_path)

    with pytest.raises(SandboxViolation) as raised:
        sandbox.resolve("linked/secret.txt")

    assert raised.value.code is ErrorCode.SYMLINK_NOT_ALLOWED
