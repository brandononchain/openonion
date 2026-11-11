"""HTTP API + live event stream (SSE) + the bundled web UI.

Endpoints (all under /api, bearer-token protected when OPENONION_API_TOKEN is set):
  POST   /sessions                          create a session (title, policy, optional goal to start now)
  GET    /sessions                          list sessions
  GET    /sessions/{id}                     session + whether it is running + pending approvals
  POST   /sessions/{id}/runs                start a run (or a follow-up run) with a goal
  POST   /sessions/{id}/cancel              cancel the active run
  POST   /sessions/{id}/approvals/{aid}     approve / deny a gated tool call
  GET    /sessions/{id}/events?after=N      event log page (JSON)
  GET    /sessions/{id}/stream?after=N      live SSE stream with replay (supports Last-Event-ID)
  GET    /sessions/{id}/artifacts           artifact list
  GET    /sessions/{id}/export              full event log as JSON Lines
  GET    /artifacts/{artifact_id}           artifact bytes
  GET    /health                            liveness + config summary
"""

from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..events import TRANSCRIPT, EventBus
from ..memory.store import Store
from ..policy import SessionPolicy
from ..runtime.manager import ProviderFactory, SessionBusy, SessionManager


class CreateSession(BaseModel):
    title: str = Field("Untitled session", max_length=300)
    policy: SessionPolicy = Field(default_factory=SessionPolicy)
    goal: str | None = Field(None, description="If set, the first run starts immediately.")


class StartRun(BaseModel):
    goal: str = Field(min_length=1, max_length=20_000)
    policy: SessionPolicy | None = Field(
        None, description="Optional new policy for this and later runs (e.g. the chat composer's network mode).")


class ApprovalDecision(BaseModel):
    approved: bool


def _sse(event) -> str:
    return f"id: {event.seq}\ndata: {json.dumps(event.to_dict())}\n\n"


def create_app(settings: Settings | None = None, provider_factory: ProviderFactory | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.database_url)
    bus = EventBus()
    kwargs = {"provider_factory": provider_factory} if provider_factory else {}
    manager = SessionManager(store, bus, settings, **kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.init()
        yield
        await manager.shutdown()
        await store.close()

    app = FastAPI(title="OpenOnion", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.store, app.state.bus, app.state.manager = settings, store, bus, manager

    async def auth(request: Request) -> None:
        if not settings.api_token:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        # EventSource cannot set headers, so the stream also accepts ?token=.
        if not hmac.compare_digest(supplied, settings.api_token):
            raise HTTPException(401, "invalid or missing API token")

    async def get_session_or_404(session_id: str) -> dict[str, Any]:
        session = await store.get_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    # ── sessions ─────────────────────────────────────────────────────────────
    @app.post("/api/sessions", dependencies=[Depends(auth)], status_code=201)
    async def create_session(body: CreateSession):
        session = await store.create_session(body.title, {"policy": body.policy.model_dump()})
        if body.goal:
            await manager.start_run(session["id"], body.goal)
            session["status"] = "running"
        return session

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    async def list_sessions(limit: int = Query(100, ge=1, le=500)):
        return await store.list_sessions(limit)

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(auth)])
    async def get_session(session_id: str):
        session = await get_session_or_404(session_id)
        entry = manager._runs.get(session_id)
        session["running"] = manager.is_running(session_id)
        session["pending_approvals"] = entry[0].pending_approvals() if entry and session["running"] else []
        return session

    @app.post("/api/sessions/{session_id}/runs", dependencies=[Depends(auth)], status_code=202)
    async def start_run(session_id: str, body: StartRun):
        session = await get_session_or_404(session_id)
        if body.policy is not None:
            if manager.is_running(session_id):
                raise HTTPException(409, "a run is already in progress for this session")
            await store.update_settings(session_id, {**session["settings"], "policy": body.policy.model_dump()})
        try:
            await manager.start_run(session_id, body.goal)
        except SessionBusy as exc:
            raise HTTPException(409, str(exc))
        return {"status": "started", "session_id": session_id}

    @app.post("/api/sessions/{session_id}/cancel", dependencies=[Depends(auth)])
    async def cancel_run(session_id: str):
        await get_session_or_404(session_id)
        return {"cancelled": await manager.cancel(session_id)}

    @app.post("/api/sessions/{session_id}/approvals/{approval_id}", dependencies=[Depends(auth)])
    async def resolve_approval(session_id: str, approval_id: str, body: ApprovalDecision):
        if not manager.resolve_approval(session_id, approval_id, body.approved):
            raise HTTPException(404, "no pending approval with that id")
        return {"approval_id": approval_id, "approved": body.approved}

    # ── events ───────────────────────────────────────────────────────────────
    @app.get("/api/sessions/{session_id}/events", dependencies=[Depends(auth)])
    async def list_events(session_id: str, after: int = 0, limit: int = Query(1000, ge=1, le=5000),
                          include_transcript: bool = False):
        await get_session_or_404(session_id)
        events = await store.events(session_id, after=after, limit=limit)
        return [e.to_dict() for e in events if include_transcript or e.type != TRANSCRIPT]

    @app.get("/api/sessions/{session_id}/stream", dependencies=[Depends(auth)])
    async def stream(session_id: str, request: Request, after: int = 0, follow: bool = True):
        """follow=false replays the log and closes (handy for scripts and tests)."""
        await get_session_or_404(session_id)
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isdigit():
            after = max(after, int(last_id))
        # Subscribe BEFORE replaying so nothing published during replay is missed.
        queue = bus.subscribe(session_id)

        async def gen():
            sent = after
            try:
                for event in await store.events(session_id, after=sent):
                    sent = event.seq
                    if event.type != TRANSCRIPT:
                        yield _sse(event)
                if not follow:
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event.seq <= sent:
                        continue
                    if event.seq > sent + 1:
                        # Gap (slow consumer dropped events): backfill from the log.
                        for missed in await store.events(session_id, after=sent):
                            if missed.seq >= event.seq:
                                break
                            sent = missed.seq
                            if missed.type != TRANSCRIPT:
                                yield _sse(missed)
                    sent = event.seq
                    if event.type != TRANSCRIPT:
                        yield _sse(event)
            finally:
                bus.unsubscribe(session_id, queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/sessions/{session_id}/export", dependencies=[Depends(auth)])
    async def export(session_id: str):
        session = await get_session_or_404(session_id)
        events = await store.events(session_id, limit=1_000_000)
        lines = [json.dumps({"session": session})] + [json.dumps(e.to_dict()) for e in events]
        return StreamingResponse(iter(line + "\n" for line in lines), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="openonion-{session_id}.jsonl"'})

    # ── artifacts ────────────────────────────────────────────────────────────
    @app.get("/api/sessions/{session_id}/artifacts", dependencies=[Depends(auth)])
    async def list_artifacts(session_id: str):
        await get_session_or_404(session_id)
        return await store.list_artifacts(session_id)

    @app.get("/api/artifacts/{artifact_id}", dependencies=[Depends(auth)])
    async def get_artifact(artifact_id: str):
        art = await store.get_artifact(artifact_id)
        if art is None:
            raise HTTPException(404, "artifact not found")
        path = Path(art["path"]).resolve()
        if not path.is_relative_to(settings.artifact_root.resolve()) or not path.is_file():
            raise HTTPException(404, "artifact file missing")
        return FileResponse(path, media_type=art["mime"])

    # ── misc ─────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__, "provider": settings.provider, "model": settings.model,
                "tor_slots": len(settings.tor_socks_ports), "shell_enabled": settings.enable_shell,
                "auth_required": bool(settings.api_token)}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return resources.files("openonion.web").joinpath("index.html").read_text(encoding="utf-8")

    _brand_files = {"favicon.svg": "image/svg+xml", "favicon.ico": "image/x-icon",
                    "apple-touch-icon.png": "image/png"}

    @app.get("/fonts/{name}", include_in_schema=False)
    async def font(name: str):
        if name != "manrope-latin-wght.woff2":
            raise HTTPException(404)
        data = resources.files("openonion.web").joinpath("fonts", name).read_bytes()
        return Response(data, media_type="font/woff2", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/{name}", include_in_schema=False)
    async def brand_asset(name: str):
        if name not in _brand_files:
            raise HTTPException(404)
        data = resources.files("openonion.web").joinpath(name).read_bytes()
        return Response(data, media_type=_brand_files[name], headers={"Cache-Control": "public, max-age=86400"})

    return app
