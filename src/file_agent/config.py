"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings shared by CLI and Web entry points."""

    openai_api_key: str = field(repr=False)
    openai_base_url: str
    openai_model: str
    demo_username: str
    demo_password: str = field(repr=False)
    session_secret: str = field(repr=False)
    workspace_seed_path: Path
    runtime_root: Path
    session_ttl_seconds: int
    max_llm_calls: int
    max_tool_calls: int
    run_timeout_seconds: int
    max_run_file_content_bytes: int


def _positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            f"{name} must be a positive integer; got {raw_value!r}"
        ) from error
    if value <= 0:
        raise ConfigurationError(
            f"{name} must be a positive integer; got {raw_value!r}"
        )
    return value


def load_settings() -> Settings:
    """Load settings from the current process environment."""

    required_names = (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "DEMO_USERNAME",
        "DEMO_PASSWORD",
        "SESSION_SECRET",
    )
    missing = sorted(name for name in required_names if not os.environ.get(name))
    if missing:
        raise ConfigurationError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return Settings(
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=os.environ["OPENAI_MODEL"],
        demo_username=os.environ["DEMO_USERNAME"],
        demo_password=os.environ["DEMO_PASSWORD"],
        session_secret=os.environ["SESSION_SECRET"],
        workspace_seed_path=Path(os.environ.get("WORKSPACE_SEED_PATH", "./workspace")),
        runtime_root=Path(os.environ.get("RUNTIME_ROOT", "/tmp/file-agent")),
        session_ttl_seconds=_positive_int("SESSION_TTL_SECONDS", 3600),
        max_llm_calls=_positive_int("MAX_LLM_CALLS", 20),
        max_tool_calls=_positive_int("MAX_TOOL_CALLS", 80),
        run_timeout_seconds=_positive_int("RUN_TIMEOUT_SECONDS", 300),
        max_run_file_content_bytes=_positive_int("MAX_RUN_FILE_CONTENT_BYTES", 262_144),
    )
