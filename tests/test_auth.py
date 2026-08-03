from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class NeverCalledModel:
    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        raise AssertionError("The authentication test must not start a model run")
        yield


def test_signed_session_cookie_rejects_bad_credentials_tampering_and_expiry() -> None:
    from file_agent.auth import AuthService, AuthSettings

    clock = FakeClock()
    auth = AuthService(
        AuthSettings(
            username="demo",
            password="correct-password",
            session_secret="signing-secret",
        ),
        clock=clock,
    )

    assert auth.verify_credentials("demo", "correct-password") is True
    assert auth.verify_credentials("other", "correct-password") is False
    assert auth.verify_credentials("demo", "wrong") is False

    token = auth.issue_cookie("demo")
    assert "correct-password" not in token
    assert auth.verify_cookie(token) == "demo"
    replacement = "A" if token[-1] != "A" else "B"
    assert auth.verify_cookie(f"{token[:-1]}{replacement}") is None

    clock.value += 8 * 60 * 60
    assert auth.verify_cookie(token) is None


def test_web_login_cookie_logout_and_route_protection(tmp_path: Path) -> None:
    from file_agent.auth import AuthService, AuthSettings
    from file_agent.runtime import WebRuntime
    from file_agent.web import create_app
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        runtime = WebRuntime(
            workspaces=WorkspaceManager(
                seed_path=seed,
                runtime_root=tmp_path / "runtime",
                ttl_seconds=3600,
            ),
            model_factory=NeverCalledModel,
        )
        auth = AuthService(
            AuthSettings(
                username="demo",
                password="correct-password",
                session_secret="signing-secret",
            )
        )
        app = create_app(runtime, auth=auth)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as client:
            protected = await client.post("/api/workspaces")
            assert protected.status_code == 401
            root = await client.get("/")
            assert root.status_code == 307
            assert root.headers["location"] == "/login"
            login_page = await client.get("/login")
            assert login_page.status_code == 200
            assert 'id="login-form"' in login_page.text

            denied = await client.post(
                "/api/auth/login",
                json={"username": "demo", "password": "wrong"},
            )
            assert denied.status_code == 401
            login = await client.post(
                "/api/auth/login",
                json={"username": "demo", "password": "correct-password"},
            )
            assert login.status_code == 200
            cookie = login.headers["set-cookie"].lower()
            assert "httponly" in cookie
            assert "secure" in cookie
            assert "samesite=lax" in cookie
            assert "max-age=28800" in cookie

            me = await client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json() == {"username": "demo"}
            index = await client.get("/")
            assert index.status_code == 200
            assert 'id="task-form"' in index.text
            assert 'id="file-tree"' in index.text
            assert 'id="activity-feed"' in index.text
            conversation = 'id="conversation" class="conversation" aria-live="off"'
            assert conversation in index.text
            assert index.text.index("/static/markdown.js") < index.text.index(
                "/static/app.js"
            )
            assert "default-src 'self'" in index.headers["content-security-policy"]
            script = await client.get("/static/app.js")
            assert script.status_code == 200
            assert "new EventSource" in script.text
            assert "approval.required" in script.text
            assert "renderAnswerNow" in script.text
            assert "window.setTimeout" in script.text
            assert "renderMarkdown" in script.text
            assert "textContent" in script.text
            assert "innerHTML" not in script.text
            assert "refreshTreeWithFeedback" in script.text
            markdown = await client.get("/static/markdown.js")
            assert markdown.status_code == 200
            assert "renderMarkdown" in markdown.text
            assert "document.createElement" in markdown.text
            assert "textContent" in markdown.text
            assert 'protocol === "http:"' in markdown.text
            assert 'protocol === "https:"' in markdown.text
            assert "innerHTML" not in markdown.text
            assert (await client.post("/api/workspaces")).status_code == 201

            logout = await client.post("/api/auth/logout")
            assert logout.status_code == 200
            assert (await client.post("/api/workspaces")).status_code == 401

    asyncio.run(scenario())
