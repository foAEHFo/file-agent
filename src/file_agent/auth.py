"""Shared-account authentication with short-lived signed cookies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SESSION_COOKIE_NAME = "file_agent_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


@dataclass(frozen=True, slots=True)
class AuthSettings:
    username: str
    password: str = field(repr=False)
    session_secret: str = field(repr=False)
    secure_cookie: bool = True


class AuthService:
    """Verify one configured account and authenticate signed cookie payloads."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not settings.username:
            raise ValueError("username must not be empty")
        if not settings.password:
            raise ValueError("password must not be empty")
        if not settings.session_secret:
            raise ValueError("session_secret must not be empty")
        self.settings = settings
        self._clock = clock
        self._secret = settings.session_secret.encode("utf-8")

    def verify_credentials(self, username: str, password: str) -> bool:
        username_matches = hmac.compare_digest(
            username.encode("utf-8"),
            self.settings.username.encode("utf-8"),
        )
        password_matches = hmac.compare_digest(
            password.encode("utf-8"),
            self.settings.password.encode("utf-8"),
        )
        return username_matches and password_matches

    def issue_cookie(self, username: str) -> str:
        if not hmac.compare_digest(
            username.encode("utf-8"),
            self.settings.username.encode("utf-8"),
        ):
            raise ValueError("Cannot issue a cookie for an unknown account")
        payload = {
            "sub": username,
            "exp": int(self._clock()) + SESSION_MAX_AGE_SECONDS,
        }
        encoded_payload = _encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = hmac.new(
            self._secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_encode(signature)}"

    def verify_cookie(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            supplied_signature = _decode(encoded_signature)
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload: Any = json.loads(_decode(encoded_payload))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        username = payload.get("sub")
        expires_at = payload.get("exp")
        if not isinstance(username, str) or not isinstance(expires_at, int):
            return None
        if not hmac.compare_digest(
            username.encode("utf-8"),
            self.settings.username.encode("utf-8"),
        ):
            return None
        if self._clock() >= expires_at:
            return None
        return username


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
    if _encode(decoded) != value:
        raise ValueError("Non-canonical base64 encoding")
    return decoded
