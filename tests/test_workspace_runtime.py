from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class BlockingAnswerModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[Any]:
        from file_agent.model import ModelEvent, ModelEventKind

        self.started.set()
        await self.release.wait()
        yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="完成。")
        yield ModelEvent(
            ModelEventKind.OUTPUT_ITEM,
            item={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "完成。"}],
            },
        )
        yield ModelEvent(ModelEventKind.COMPLETED)


class ApprovalWriteModel:
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
                    "call_id": "create-note",
                    "name": "write_file",
                    "arguments": (
                        '{"path":"created.md","content":"已批准\\n",'
                        '"mode":"create","expected_sha256":null}'
                    ),
                },
            )
        else:
            yield ModelEvent(ModelEventKind.ANSWER_DELTA, delta="写入完成。")
            yield ModelEvent(
                ModelEventKind.OUTPUT_ITEM,
                item={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "写入完成。"}],
                },
            )
        yield ModelEvent(ModelEventKind.COMPLETED)


async def next_event_of_kind(runtime: Any, run_id: str, kind: str) -> Any:
    stream = runtime.events_after(
        run_id,
        last_event_id=0,
        heartbeat_seconds=0.01,
    )
    try:
        async for event in stream:
            if event is not None and event.kind == kind:
                return event
    finally:
        await stream.aclose()
    raise AssertionError(f"Run ended without event {kind}")


def test_workspace_copy_reset_context_and_ttl_cleanup(tmp_path: Path) -> None:
    from file_agent.workspace import WorkspaceBusy, WorkspaceManager

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "note.md").write_text("种子内容\n", encoding="utf-8")
    clock = FakeClock()
    manager = WorkspaceManager(
        seed_path=seed,
        runtime_root=tmp_path / "runtime",
        ttl_seconds=60,
        clock=clock,
    )

    session = manager.create()
    assert session.path != seed
    assert (session.path / "note.md").read_text(encoding="utf-8") == "种子内容\n"

    (session.path / "note.md").write_text("已修改\n", encoding="utf-8")
    (session.path / "extra.md").write_text("新增\n", encoding="utf-8")
    session.history.append({"role": "user", "content": "旧上下文"})
    manager.reset(session.id)

    assert (session.path / "note.md").read_text(encoding="utf-8") == "种子内容\n"
    assert not (session.path / "extra.md").exists()
    assert session.history == []

    session.active_run_id = "run-active"
    with pytest.raises(WorkspaceBusy):
        manager.reset(session.id)
    session.active_run_id = None

    clock.value = 61
    assert manager.expired_ids() == [session.id]
    manager.remove(session.id)
    assert not session.path.exists()


def test_runtime_allows_one_run_and_replays_after_sse_disconnect(
    tmp_path: Path,
) -> None:
    from file_agent.runtime import RuntimeConflict, WebRuntime
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        model = BlockingAnswerModel()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=lambda: model,
        )
        workspace = workspaces.create()
        run = await runtime.create_run(workspace.id, "完成任务")
        await model.started.wait()

        with pytest.raises(RuntimeConflict):
            await runtime.create_run(workspace.id, "并发任务")

        stream = runtime.events_after(run.id, last_event_id=0, heartbeat_seconds=0.01)
        first_event = await anext(stream)
        assert first_event is not None
        assert first_event.id == 1
        assert first_event.kind == "run.started"
        await stream.aclose()

        assert run.task is not None
        assert not run.task.done()
        heartbeat_stream = runtime.events_after(
            run.id,
            last_event_id=run.events[-1].id,
            heartbeat_seconds=0.001,
        )
        assert await anext(heartbeat_stream) is None
        await heartbeat_stream.aclose()
        model.release.set()
        await runtime.wait_run(run.id)

        replayed = [
            event
            async for event in runtime.events_after(
                run.id,
                last_event_id=1,
                heartbeat_seconds=0.01,
            )
            if event is not None
        ]
        assert [event.id for event in replayed] == list(range(2, len(replayed) + 2))
        assert "answer.delta" in [event.kind for event in replayed]
        assert replayed[-1].kind == "run.completed"
        assert workspace.history[-1]["role"] == "assistant"
        assert workspace.active_run_id is None

    asyncio.run(scenario())


def test_runtime_pauses_for_idempotent_approval_and_flushes_trace(
    tmp_path: Path,
) -> None:
    from file_agent.runtime import RuntimeConflict, WebRuntime
    from file_agent.workspace import WorkspaceBusy, WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=ApprovalWriteModel,
        )
        workspace = workspaces.create()
        run = await runtime.create_run(workspace.id, "创建文件")
        approval_event = await next_event_of_kind(
            runtime,
            run.id,
            "approval.required",
        )
        approval_id = approval_event.data["approval_id"]

        assert run.task is not None
        assert not run.task.done()
        assert run.trace_path.exists()
        assert run.trace_path.read_text(encoding="utf-8") == ""
        with pytest.raises(WorkspaceBusy):
            workspaces.reset(workspace.id)

        first = await runtime.decide_approval(str(approval_id), approved=True)
        repeated = await runtime.decide_approval(str(approval_id), approved=True)
        assert first is repeated
        with pytest.raises(RuntimeConflict):
            await runtime.decide_approval(str(approval_id), approved=False)

        await runtime.wait_run(run.id)
        assert (workspace.path / "created.md").read_text(encoding="utf-8") == (
            "已批准\n"
        )
        trace_lines = run.trace_path.read_text(encoding="utf-8").splitlines()
        assert len(trace_lines) == 1
        assert '"tool":"write_file"' in trace_lines[0]
        assert run.status == "completed"

    asyncio.run(scenario())


def test_runtime_ttl_cancels_waiting_approval_and_removes_all_state(
    tmp_path: Path,
) -> None:
    from file_agent.runtime import RuntimeGone, WebRuntime
    from file_agent.workspace import WorkspaceManager, WorkspaceNotFound

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        clock = FakeClock()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=60,
            clock=clock,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=ApprovalWriteModel,
        )
        workspace = workspaces.create()
        run = await runtime.create_run(workspace.id, "等待审批")
        approval_event = await next_event_of_kind(
            runtime,
            run.id,
            "approval.required",
        )
        approval_id = str(approval_event.data["approval_id"])
        workspace_path = workspace.path
        trace_directory = run.trace_path.parent

        clock.value = 61
        assert await runtime.cleanup_expired() == [workspace.id]
        assert run.status == "cancelled"
        assert not workspace_path.exists()
        assert not trace_directory.exists()
        with pytest.raises(WorkspaceNotFound):
            workspaces.get(workspace.id)
        with pytest.raises(RuntimeGone):
            runtime.get_run(run.id)
        with pytest.raises(RuntimeGone):
            await runtime.decide_approval(approval_id, approved=True)

    asyncio.run(scenario())


def test_runtime_immediate_cancel_releases_workspace(tmp_path: Path) -> None:
    from file_agent.runtime import WebRuntime
    from file_agent.workspace import WorkspaceManager

    async def scenario() -> None:
        seed = tmp_path / "seed"
        seed.mkdir()
        model = BlockingAnswerModel()
        workspaces = WorkspaceManager(
            seed_path=seed,
            runtime_root=tmp_path / "runtime",
            ttl_seconds=3600,
        )
        runtime = WebRuntime(
            workspaces=workspaces,
            model_factory=lambda: model,
        )
        workspace = workspaces.create()
        run = await runtime.create_run(workspace.id, "立即取消")

        await runtime.cancel_run(run.id)

        assert run.status == "cancelled"
        assert workspace.active_run_id is None
        assert run.events[-1].kind == "run.cancelled"
        workspaces.reset(workspace.id)

    asyncio.run(scenario())
