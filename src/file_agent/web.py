"""FastAPI surface for temporary workspaces and background agent runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from file_agent.agent import RunLimits
from file_agent.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    AuthService,
    AuthSettings,
)
from file_agent.config import Settings, load_settings
from file_agent.model import OpenAIResponsesClient
from file_agent.prompts import SYSTEM_INSTRUCTIONS, TOOL_DEFINITIONS
from file_agent.runtime import RuntimeConflict, RuntimeGone, WebRuntime
from file_agent.types import ErrorCode, ToolResult
from file_agent.workspace import (
    WorkspaceBusy,
    WorkspaceManager,
    WorkspaceNotFound,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(ApiModel):
    workspace_id: str = Field(min_length=1)
    task: str = Field(min_length=1, max_length=20_000)


class ApprovalDecisionRequest(ApiModel):
    approved: bool


class LoginRequest(ApiModel):
    username: str
    password: str


STATIC_ROOT = Path(__file__).parent / "static"


def create_app(
    runtime: WebRuntime | None = None,
    *,
    auth: AuthService | None = None,
) -> FastAPI:
    if (runtime is None) is not (auth is None):
        raise ValueError("runtime and auth must be provided together")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if runtime is None or auth is None:
            settings = load_settings()
            selected_runtime = _runtime_from_settings(settings)
            selected_auth = _auth_from_settings(settings)
        else:
            selected_runtime = runtime
            selected_auth = auth
        application.state.runtime = selected_runtime
        application.state.auth = selected_auth
        cleanup_task = asyncio.create_task(_cleanup_loop(selected_runtime))
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            await selected_runtime.shutdown()

    application = FastAPI(title="文件助理 Agent", lifespan=lifespan)
    if runtime is not None and auth is not None:
        application.state.runtime = runtime
        application.state.auth = auth

    @application.middleware("http")
    async def authenticate(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        public = (
            path == "/health"
            or path == "/login"
            or path == "/api/auth/login"
            or path.startswith("/static/")
        )
        if not public:
            selected_auth = _auth(request)
            username = selected_auth.verify_cookie(
                request.cookies.get(SESSION_COOKIE_NAME)
            )
            if username is None:
                if path.startswith("/api/"):
                    unauthorized_response = JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required"},
                    )
                    return _secure_headers(unauthorized_response)
                return _secure_headers(RedirectResponse("/login", status_code=307))
            request.state.username = username
        response = await call_next(request)
        return _secure_headers(response)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/login")
    async def login_page(request: Request) -> Response:
        selected_auth = _auth(request)
        if (
            selected_auth.verify_cookie(request.cookies.get(SESSION_COOKIE_NAME))
            is not None
        ):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_ROOT / "login.html")

    @application.get("/")
    async def index_page() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @application.post("/api/auth/login")
    async def login(
        body: LoginRequest,
        request: Request,
    ) -> JSONResponse:
        selected_auth = _auth(request)
        if not selected_auth.verify_credentials(body.username, body.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = selected_auth.issue_cookie(body.username)
        response = JSONResponse({"username": body.username})
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=selected_auth.settings.secure_cookie,
            samesite="lax",
            path="/",
        )
        return response

    @application.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        selected_auth = _auth(request)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            httponly=True,
            secure=selected_auth.settings.secure_cookie,
            samesite="lax",
            path="/",
        )
        return response

    @application.get("/api/auth/me")
    async def current_user(request: Request) -> dict[str, str]:
        return {"username": str(request.state.username)}

    @application.post("/api/workspaces", status_code=status.HTTP_201_CREATED)
    async def create_workspace(request: Request) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        session = selected_runtime.workspaces.create()
        return {"id": session.id}

    @application.post("/api/workspaces/{workspace_id}/reset")
    async def reset_workspace(
        workspace_id: str,
        request: Request,
    ) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            session = selected_runtime.workspaces.reset(workspace_id)
        except WorkspaceBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except WorkspaceNotFound as error:
            raise _gone("workspace", workspace_id) from error
        return {"id": session.id, "reset": True}

    @application.get("/api/workspaces/{workspace_id}/tree")
    async def workspace_tree(
        workspace_id: str,
        request: Request,
        max_entries: Annotated[int, Query(ge=1, le=2_000)] = 500,
    ) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            result = selected_runtime.workspaces.tree(
                workspace_id,
                max_entries=max_entries,
            )
        except WorkspaceNotFound as error:
            raise _gone("workspace", workspace_id) from error
        return _tool_data(result)

    @application.get("/api/workspaces/{workspace_id}/files")
    async def workspace_file(
        workspace_id: str,
        request: Request,
        path: Annotated[str, Query(min_length=1)],
        start_line: Annotated[int, Query(ge=1)] = 1,
        max_lines: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            result = selected_runtime.workspaces.file_page(
                workspace_id,
                path=path,
                start_line=start_line,
                max_lines=max_lines,
            )
        except WorkspaceNotFound as error:
            raise _gone("workspace", workspace_id) from error
        return _tool_data(result)

    @application.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        body: CreateRunRequest,
        request: Request,
    ) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            run = await selected_runtime.create_run(body.workspace_id, body.task)
        except WorkspaceNotFound as error:
            raise _gone("workspace", body.workspace_id) from error
        except RuntimeConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"id": run.id, "workspace_id": run.workspace_id}

    @application.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        last_event_id: Annotated[
            int | None,
            Header(alias="Last-Event-ID"),
        ] = None,
    ) -> StreamingResponse:
        selected_runtime = _runtime(request)
        try:
            selected_runtime.get_run(run_id)
        except RuntimeGone as error:
            raise _gone("run", run_id) from error

        async def stream() -> AsyncIterator[str]:
            async for event in selected_runtime.events_after(
                run_id,
                last_event_id=max(last_event_id or 0, 0),
            ):
                if event is None:
                    yield ": ping\n\n"
                    continue
                data = json.dumps(
                    dict(event.data),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {event.id}\nevent: {event.kind}\ndata: {data}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post("/api/approvals/{approval_id}")
    async def decide_approval(
        approval_id: str,
        body: ApprovalDecisionRequest,
        request: Request,
    ) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            pending = await selected_runtime.decide_approval(
                approval_id,
                approved=body.approved,
            )
        except RuntimeGone as error:
            raise _gone("approval", approval_id) from error
        except RuntimeConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "id": pending.id,
            "run_id": pending.run_id,
            "approved": pending.decision,
        }

    @application.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        selected_runtime = _runtime(request)
        try:
            run = await selected_runtime.cancel_run(run_id)
        except RuntimeGone as error:
            raise _gone("run", run_id) from error
        return {"id": run.id, "status": run.status}

    @application.get("/api/runs/{run_id}/trace")
    async def run_trace(run_id: str, request: Request) -> FileResponse:
        selected_runtime = _runtime(request)
        try:
            run = selected_runtime.get_run(run_id)
        except RuntimeGone as error:
            raise _gone("run", run_id) from error
        return FileResponse(
            run.trace_path,
            media_type="application/x-ndjson",
            filename="trace.jsonl",
        )

    application.mount(
        "/static",
        StaticFiles(directory=STATIC_ROOT),
        name="static",
    )
    return application


def _runtime(request: Request) -> WebRuntime:
    selected = getattr(request.app.state, "runtime", None)
    if not isinstance(selected, WebRuntime):
        raise HTTPException(status_code=503, detail="Web runtime is not ready")
    return selected


def _auth(request: Request) -> AuthService:
    selected = getattr(request.app.state, "auth", None)
    if not isinstance(selected, AuthService):
        raise HTTPException(status_code=503, detail="Authentication is not ready")
    return selected


def _secure_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "connect-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _tool_data(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        return dict(result.data)
    error = result.error
    status_code = (
        404 if error is not None and error.code is ErrorCode.FILE_NOT_FOUND else 400
    )
    raise HTTPException(
        status_code=status_code,
        detail=error.to_dict() if error is not None else result.summary,
    )


def _gone(resource: str, identifier: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=f"{resource} {identifier} is expired or unavailable",
    )


def _runtime_from_settings(settings: Settings) -> WebRuntime:
    workspaces = WorkspaceManager(
        seed_path=settings.workspace_seed_path,
        runtime_root=settings.runtime_root,
        ttl_seconds=settings.session_ttl_seconds,
    )

    def model_factory() -> OpenAIResponsesClient:
        return OpenAIResponsesClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            instructions=SYSTEM_INSTRUCTIONS,
            tools=TOOL_DEFINITIONS,
        )

    return WebRuntime(
        workspaces=workspaces,
        model_factory=model_factory,
        limits=RunLimits(
            max_llm_calls=settings.max_llm_calls,
            max_tool_calls=settings.max_tool_calls,
            active_timeout_seconds=float(settings.run_timeout_seconds),
        ),
        max_run_file_content_bytes=settings.max_run_file_content_bytes,
    )


def _auth_from_settings(settings: Settings) -> AuthService:
    return AuthService(
        AuthSettings(
            username=settings.demo_username,
            password=settings.demo_password,
            session_secret=settings.session_secret,
        )
    )


async def _cleanup_loop(runtime: WebRuntime) -> None:
    while True:
        await asyncio.sleep(300)
        await runtime.cleanup_expired()


app = create_app()
