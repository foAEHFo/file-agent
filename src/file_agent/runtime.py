"""Single-process Web runtime with buffered events and background agent runs."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from file_agent.agent import (
    AgentEvent,
    AgentEventKind,
    AgentRunner,
    ApprovalRequest,
    RunLimits,
)
from file_agent.model import ResponsesClient
from file_agent.tools import FileTools
from file_agent.trace import TraceWriter
from file_agent.workspace import WorkspaceManager, WorkspaceNotFound

TERMINAL_EVENT_KINDS = frozenset(
    {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}
)


class RuntimeGone(LookupError):
    pass


class RuntimeConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    id: int
    kind: str
    data: Mapping[str, Any]


@dataclass(slots=True)
class PendingApproval:
    id: str
    run_id: str
    request: ApprovalRequest
    future: asyncio.Future[bool]
    decision: bool | None = None


@dataclass(slots=True)
class WebRun:
    id: str
    workspace_id: str
    prompt: str
    trace_path: Path
    events: list[RuntimeEvent] = field(default_factory=list)
    status: str = "active"
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


class _WebApprovalHandler:
    def __init__(self, run: WebRun) -> None:
        self._run = run

    async def request(self, request: ApprovalRequest) -> bool:
        try:
            pending = self._run.approvals[request.id]
        except KeyError as error:
            raise RuntimeError("Approval was not registered before waiting") from error
        return await pending.future


class WebRuntime:
    """Coordinate workspaces, background runs, approvals, replay, and cleanup."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        model_factory: Callable[[], ResponsesClient],
        limits: RunLimits | None = None,
        max_run_file_content_bytes: int = 262_144,
    ) -> None:
        self.workspaces = workspaces
        self._model_factory = model_factory
        self._limits = limits or RunLimits()
        self._max_run_file_content_bytes = max_run_file_content_bytes
        self.runs_root = workspaces.runtime_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, WebRun] = {}
        self._approvals: dict[str, PendingApproval] = {}

    async def create_run(self, workspace_id: str, prompt: str) -> WebRun:
        session = self.workspaces.get(workspace_id)
        if session.active_run_id is not None:
            raise RuntimeConflict(f"Workspace {workspace_id} already has an active run")
        run_id = uuid4().hex
        run_directory = self.runs_root / run_id
        run_directory.mkdir(parents=True)
        trace_path = run_directory / "trace.jsonl"
        trace_path.touch()
        run = WebRun(
            id=run_id,
            workspace_id=workspace_id,
            prompt=prompt,
            trace_path=trace_path,
        )
        self._runs[run_id] = run
        session.active_run_id = run_id
        self.workspaces.touch(session)
        await self._append(
            run,
            "run.started",
            {"run_id": run.id, "workspace_id": workspace_id},
        )
        run.task = asyncio.create_task(self._execute(run))
        return run

    def get_run(self, run_id: str) -> WebRun:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise RuntimeGone(run_id) from error

    async def wait_run(self, run_id: str) -> WebRun:
        run = self.get_run(run_id)
        if run.task is not None:
            await run.task
        return run

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
    ) -> PendingApproval:
        try:
            pending = self._approvals[approval_id]
        except KeyError as error:
            raise RuntimeGone(approval_id) from error
        if pending.decision is not None:
            if pending.decision is not approved:
                raise RuntimeConflict(
                    f"Approval {approval_id} was already resolved differently"
                )
            return pending
        pending.decision = approved
        pending.future.set_result(approved)
        run = self.get_run(pending.run_id)
        try:
            session = self.workspaces.get(run.workspace_id, touch=False)
        except WorkspaceNotFound:
            pass
        else:
            self.workspaces.touch(session)
        return pending

    async def events_after(
        self,
        run_id: str,
        *,
        last_event_id: int,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncGenerator[RuntimeEvent | None]:
        run = self.get_run(run_id)
        cursor = last_event_id
        while True:
            available: list[RuntimeEvent] = []
            terminal = False
            heartbeat = False
            async with run.condition:
                available = [event for event in run.events if event.id > cursor]
                terminal = run.status != "active"
                if not available and not terminal:
                    try:
                        await asyncio.wait_for(
                            run.condition.wait(),
                            timeout=heartbeat_seconds,
                        )
                    except TimeoutError:
                        heartbeat = True
                    continue_after_wait = not heartbeat
                else:
                    continue_after_wait = False
            if continue_after_wait:
                continue
            if heartbeat:
                yield None
                continue
            for event in available:
                cursor = event.id
                yield event
            if terminal and not any(event.id > cursor for event in run.events):
                return

    async def _execute(self, run: WebRun) -> None:
        try:
            session = self.workspaces.get(run.workspace_id)
            tools = FileTools(
                session.path,
                max_run_file_content_bytes=self._max_run_file_content_bytes,
            )
            with TraceWriter(run.trace_path) as trace:
                result = await AgentRunner(
                    model=self._model_factory(),
                    tools=tools,
                    approval=_WebApprovalHandler(run),
                    trace=trace,
                    limits=self._limits,
                ).run(
                    task=run.prompt,
                    history=session.history,
                    emit=lambda event: self._agent_event(run, event),
                )
            session.history = [dict(item) for item in result.history]
        except asyncio.CancelledError:
            await self._append(run, "run.cancelled", {})
        except Exception as error:
            if run.status == "active":
                await self._append(run, "run.failed", {"error": str(error)})
        finally:
            self._release_workspace(run)

    async def _agent_event(self, run: WebRun, event: AgentEvent) -> None:
        data = dict(event.data)
        if event.kind is AgentEventKind.APPROVAL_REQUIRED:
            request = ApprovalRequest(
                tool=str(data.get("tool", "")),
                args=(
                    dict(data["args"]) if isinstance(data.get("args"), Mapping) else {}
                ),
                id=str(data.get("approval_id", "")),
            )
            pending = PendingApproval(
                id=request.id,
                run_id=run.id,
                request=request,
                future=asyncio.get_running_loop().create_future(),
            )
            run.approvals[pending.id] = pending
            self._approvals[pending.id] = pending
        await self._append(run, event.kind.value, data)

    async def _append(
        self,
        run: WebRun,
        kind: str,
        data: Mapping[str, Any],
    ) -> RuntimeEvent:
        async with run.condition:
            event = RuntimeEvent(
                id=len(run.events) + 1,
                kind=kind,
                data=dict(data),
            )
            run.events.append(event)
            if kind in TERMINAL_EVENT_KINDS:
                run.status = kind.removeprefix("run.")
            run.condition.notify_all()
        try:
            session = self.workspaces.get(run.workspace_id, touch=False)
        except WorkspaceNotFound:
            return event
        self.workspaces.touch(session)
        return event

    async def cleanup_expired(self) -> list[str]:
        removed: list[str] = []
        for workspace_id in self.workspaces.expired_ids():
            try:
                session = self.workspaces.get(workspace_id, touch=False)
            except WorkspaceNotFound:
                continue
            if session.active_run_id is not None:
                await self.cancel_run(session.active_run_id)
            run_ids = [
                run.id
                for run in self._runs.values()
                if run.workspace_id == workspace_id
            ]
            for run_id in run_ids:
                run = self._runs.pop(run_id)
                for approval_id in run.approvals:
                    self._approvals.pop(approval_id, None)
                shutil.rmtree(run.trace_path.parent, ignore_errors=True)
            self.workspaces.remove(workspace_id)
            removed.append(workspace_id)
        return removed

    async def cancel_run(self, run_id: str) -> WebRun:
        run = self.get_run(run_id)
        if run.task is not None and not run.task.done():
            run.task.cancel()
            with suppress(asyncio.CancelledError):
                await run.task
        if run.status == "active":
            await self._append(run, "run.cancelled", {})
        for pending in run.approvals.values():
            if pending.decision is None:
                self._approvals.pop(pending.id, None)
                if not pending.future.done():
                    pending.future.cancel()
        self._release_workspace(run)
        return run

    async def shutdown(self) -> None:
        active_run_ids = [
            run.id
            for run in self._runs.values()
            if run.task is not None and not run.task.done()
        ]
        for run_id in active_run_ids:
            await self.cancel_run(run_id)

    def _release_workspace(self, run: WebRun) -> None:
        try:
            session = self.workspaces.get(run.workspace_id, touch=False)
        except WorkspaceNotFound:
            return
        if session.active_run_id == run.id:
            session.active_run_id = None
        self.workspaces.touch(session)
