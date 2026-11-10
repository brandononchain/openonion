from __future__ import annotations

import asyncio
import json

import httpx

from openonion.api import create_app
from openonion.providers.base import ModelResponse, ToolCall
from openonion.providers.scripted import ScriptedProvider


def scripted(_settings):
    return ScriptedProvider([
        ModelResponse("", [ToolCall("c1", "update_plan", {"steps": [{"id": 1, "description": "answer"}]})]),
        ModelResponse("42", []),
    ])


async def _client(settings):
    app = create_app(settings, provider_factory=scripted)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, ctx, client


async def _wait_finished(client, sid, headers=None):
    for _ in range(100):
        events = (await client.get(f"/api/sessions/{sid}/events", headers=headers)).json()
        if any(e["type"] == "run_finished" for e in events):
            return events
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


async def test_session_lifecycle_and_sse_replay(settings):
    app, ctx, client = await _client(settings)
    try:
        assert (await client.get("/api/health")).json()["ok"]
        r = await client.post("/api/sessions", json={"title": "t", "goal": "meaning of life?"})
        assert r.status_code == 201
        sid = r.json()["id"]
        events = await _wait_finished(client, sid)
        assert "transcript" not in {e["type"] for e in events}
        assert events[-1]["payload"]["answer"] == "42"

        # SSE replay: stream from the start, resume from a Last-Event-ID.
        # (httpx's ASGI transport buffers whole responses, so use follow=false.)
        async def read_sse(headers=None):
            resp = await client.get(f"/api/sessions/{sid}/stream", headers=headers, params={"follow": "false"})
            assert resp.headers["content-type"].startswith("text/event-stream")
            ids = [int(l[4:]) for l in resp.text.splitlines() if l.startswith("id: ")]
            seen = [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]
            assert ids == [e["seq"] for e in seen]
            return seen

        full = await read_sse()
        assert full[0]["type"] == "run_started" and full[-1]["type"] == "run_finished"
        resumed = await read_sse(headers={"Last-Event-ID": str(full[-2]["seq"])})
        assert [e["seq"] for e in resumed] == [full[-1]["seq"]]

        export = await client.get(f"/api/sessions/{sid}/export")
        lines = [json.loads(x) for x in export.text.strip().splitlines()]
        assert lines[0]["session"]["id"] == sid and any(l.get("type") == "transcript" for l in lines[1:])

        busy = await client.get(f"/api/sessions/{sid}")
        assert busy.json()["status"] == "completed" and busy.json()["running"] is False
        assert (await client.post(f"/api/sessions/{sid}/approvals/nope", json={"approved": True})).status_code == 404
        assert (await client.get("/api/sessions/does-not-exist")).status_code == 404
        assert "OpenOnion" in (await client.get("/")).text
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_api_token_required(settings):
    settings.api_token = "tok"
    app, ctx, client = await _client(settings)
    try:
        assert (await client.get("/api/sessions")).status_code == 401
        assert (await client.get("/api/sessions", headers={"Authorization": "Bearer bad"})).status_code == 401
        assert (await client.get("/api/sessions", headers={"Authorization": "Bearer tok"})).status_code == 200
        assert (await client.get("/api/sessions?token=tok")).status_code == 200
        assert (await client.get("/api/health")).status_code == 200
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
