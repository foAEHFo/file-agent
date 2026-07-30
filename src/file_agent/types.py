"""Shared value objects and stable error codes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable machine-readable failures defined by the technical design."""

    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    SYMLINK_NOT_ALLOWED = "SYMLINK_NOT_ALLOWED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    HASH_REQUIRED = "HASH_REQUIRED"
    HASH_MISMATCH = "HASH_MISMATCH"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    READ_BUDGET_EXCEEDED = "READ_BUDGET_EXCEEDED"
    DENIED_BY_USER = "DENIED_BY_USER"
    DENIED_BY_POLICY = "DENIED_BY_POLICY"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """Serializable error information returned across application seams."""

    code: ErrorCode
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Uniform result contract for every file tool."""

    ok: bool
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error: ErrorDetail | None = None

    @classmethod
    def success(
        cls, *, summary: str, data: Mapping[str, Any] | None = None
    ) -> ToolResult:
        return cls(ok=True, summary=summary, data=data or {})

    @classmethod
    def failure(
        cls,
        *,
        code: ErrorCode,
        message: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            ok=False,
            summary=summary,
            error=ErrorDetail(
                code=code,
                message=message,
                details=details or {},
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": dict(self.data),
            "error": self.error.to_dict() if self.error else None,
        }
