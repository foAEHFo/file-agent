from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient


def build_test_app(runtime: Any) -> Any:
    from file_agent.auth import AuthService, AuthSettings
    from file_agent.web import create_app

    return create_app(
        runtime,
        auth=AuthService(
            AuthSettings(
                username="demo",
                password="password",
                session_secret="test-signing-secret",
                secure_cookie=False,
            )
        ),
    )


async def log_in(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "password"},
    )
    assert response.status_code == 200


class ImmediateAnswerModel:
    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind

        yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="网页运行完成。")
        yield ModelEvent(
            ModelEventKind.OUTPUT_ITEM,
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "网页运行完成。"}],
            },
        )
        yield ModelEvent(ModelEventKind.COMPLETED)


class WebApprovalModel:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind

        self.calls += 1
        if self.calls == 1:
            yield ModelEvent(
                ModelEventKind.OUTPUT_ITEM,
                item={
                    "type": "function_call",
                    "call_id": "create",
                    "name": "write_file",
                    "arguments": (
                        '{"path":"approved.md","content":"允许\\n",'
                        '"mode":"create","expected_sha256":null}'
                    ),
                },
            )
        else:
            yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="已写入。")
            yield ModelEvent(
                ModelEventKind.OUTPUT_ITEM,
                item={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "已写入。"}],
                },
            )
        yield ModelEvent(ModelEventKind.COMPLETED)


class BlockingWebModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind

        self.started.set()
        await self.release.wait()
        yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="不应完成。")
        yield ModelEvent(ModelEventKind.COMPLETED)


async def wait_for_event(run: Any, kind: str) -> Any:
    async with run.condition:
        while True:
            for event in run.events:
                if event.kind == kind:
                    return event
            await run.condition.wait()


def test_web_workspace_run_sse_replay_and_trace_download(tmp_path: Path) -> None:
    from file_agent.runtime import WebRuntime
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        (seed / "note.md").write_text("第一行\n第二行\n", encoding="utf-8")
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=ImmediateAnswerModel,
        )
        app = build_test_app(runtime)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            await log_in(client)
            create_response = await client.post("/api/workspaces")
            assert create_response.status_code == 201
            workspace_id = create_response.json()["id"]

            tree = await client.get(f"/api/workspaces/{workspace_id}/tree")
            assert tree.status_code == 200
            assert tree.json()["entries"][0]["path"] == "note.md"
            page = await client.get(
                f"/api/workspaces/{workspace_id}/files",
                params={"path": "note.md", "start_line": 2, "max_lines": 1},
            )
            assert page.status_code == 200
            assert page.json()["untrusted_content"] == "第二行\n"

            run_response = await client.post(
                "/api/runs",
                json={"workspace_id": workspace_id, "task": "回答"},
            )
            assert run_response.status_code == 202
            run_id = run_response.json()["id"]
            await runtime.wait_run(run_id)

            events = await client.get(
                f"/api/runs/{run_id}/events",
                headers={"Last-Event-ID": "1"},
            )
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            assert "\nid: 1\n" not in f"\n{events.text}"
            assert "event: answer.delta" in events.text
            assert "event: run.completed" in events.text

            trace = await client.get(f"/api/runs/{run_id}/trace")
            assert trace.status_code == 200
            assert trace.headers["content-type"].startswith("application/x-ndjson")
            assert "trace.jsonl" in trace.headers["content-disposition"]

            session = workspaces.get(workspace_id)
            assert session.history
            (session.path / "extra.md").write_text("临时\n", encoding="utf-8")
            reset = await client.post(f"/api/workspaces/{workspace_id}/reset")
            assert reset.status_code == 200
            assert session.history == []
            assert not (session.path / "extra.md").exists()

            missing = await client.get("/api/workspaces/expired/tree")
            assert missing.status_code == 410

    asyncio.run(scenario())


def test_web_active_run_and_idempotent_approval_contract(tmp_path: Path) -> None:
    from file_agent.runtime import WebRuntime
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        model = WebApprovalModel()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=lambda: model,
        )
        app = build_test_app(runtime)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            await log_in(client)
            workspace_id = (await client.post("/api/workspaces")).json()["id"]
            created = await client.post(
                "/api/runs",
                json={"workspace_id": workspace_id, "task": "创建"},
            )
            run_id = created.json()["id"]
            run = runtime.get_run(run_id)
            approval_event = await wait_for_event(run, "approval.required")
            approval_id = approval_event.data["approval_id"]

            second_run = await client.post(
                "/api/runs",
                json={"workspace_id": workspace_id, "task": "冲突"},
            )
            assert second_run.status_code == 409
            reset = await client.post(f"/api/workspaces/{workspace_id}/reset")
            assert reset.status_code == 409
            live_trace = await client.get(f"/api/runs/{run_id}/trace")
            assert live_trace.status_code == 200
            assert live_trace.text == ""

            first = await client.post(
                f"/api/approvals/{approval_id}",
                json={"approved": True},
            )
            repeated = await client.post(
                f"/api/approvals/{approval_id}",
                json={"approved": True},
            )
            conflicting = await client.post(
                f"/api/approvals/{approval_id}",
                json={"approved": False},
            )
            assert first.status_code == repeated.status_code == 200
            assert conflicting.status_code == 409

            await runtime.wait_run(run_id)
            session = workspaces.get(workspace_id)
            assert (session.path / "approved.md").read_text(encoding="utf-8") == (
                "允许\n"
            )
            completed_trace = await client.get(f"/api/runs/{run_id}/trace")
            assert '"tool":"write_file"' in completed_trace.text

    asyncio.run(scenario())


def test_web_can_cancel_a_background_run(tmp_path: Path) -> None:
    from file_agent.runtime import WebRuntime
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        model = BlockingWebModel()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=lambda: model,
        )
        app = build_test_app(runtime)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            await log_in(client)
            workspace_id = (await client.post("/api/workspaces")).json()["id"]
            run_id = (
                await client.post(
                    "/api/runs",
                    json={"workspace_id": workspace_id, "task": "等待"},
                )
            ).json()["id"]
            await model.started.wait()

            cancelled = await client.post(f"/api/runs/{run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            events = await client.get(f"/api/runs/{run_id}/events")
            assert "event: run.cancelled" in events.text
            assert "event: run.completed" not in events.text
            assert workspaces.get(workspace_id).active_run_id is None

    asyncio.run(scenario())
