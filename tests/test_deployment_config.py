"""Deployment artifact acceptance tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_package_readme_exists() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    readme_path = PROJECT_ROOT / pyproject["project"]["readme"]

    assert readme_path.is_file()


def test_dockerfile_runs_single_python_312_web_worker_on_railway_port() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    command_line = next(
        line.removeprefix("CMD ")
        for line in dockerfile.splitlines()
        if line.startswith("CMD ")
    )
    command = json.loads(command_line)

    assert dockerfile.startswith("FROM python:3.12-slim\n")
    assert "RUN python -m pip install --no-cache-dir ." in dockerfile
    assert "COPY workspace ./workspace" in dockerfile
    assert "USER appuser" in dockerfile
    assert command[:2] == ["sh", "-c"]
    assert "exec uvicorn file_agent.web:app" in command[2]
    assert "--host 0.0.0.0" in command[2]
    assert '--port "${PORT:-8000}"' in command[2]
    assert "--workers 1" in command[2]


def test_docker_context_excludes_secrets_and_local_runtime_files() -> None:
    patterns = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".env.*",
        ".venv/",
        ".git/",
        "**/.DS_Store",
        "__pycache__/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        "runtime/",
        "trace*.jsonl",
    } <= patterns
