from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from openonion import events as ev
from openonion.api import create_app
from openonion.connectors import ConnectorManager
from openonion.policy import SessionPolicy
from openonion.providers.base import ModelResponse, ToolCall
from openonion.providers.scripted import ScriptedProvider
from openonion.runtime.manager import SessionManager
from openonion.settings_store import MCPServerConfig, SettingsPatch, SettingsStore
from openonion.tools import default_registry
from openonion.tools.toolbox import Toolbox, rank

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))
NOTES_CMD = f"{sys.executable} {SERVER}"


async def _client(settings, factory):
    app = create_app(settings, provider_factory=factory)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    return app, ctx, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ── settings ─────────────────────────────────────────────────────────────────
def test_settings_store_masks_persists_and_reloads(settings):
    store = SettingsStore(settings)
    changed = store.update(SettingsPatch(model="qwen2.5:14b", base_url="http://localhost:8888/v1/",
                                         api_key="sk-unsloth-abcdef123456", tor_socks_ports="9100-9104"))
    assert set(changed) == {"model", "base_url", "api_key", "tor_socks_ports"}
    view = store.public_view()
    assert view["api_key"] == {"set": True, "hint": "…3456"}
    assert "sk-unsloth" not in json.dumps(view)
    assert view["base_url"] == "http://localhost:8888/v1"
    assert view["tor_socks_ports"] == "9100-9104" and settings.tor_socks_ports == [9100, 9101, 9102, 9103, 9104]
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600

    # A fresh process picks the saved values up over the environment defaults.
    settings.model, settings.api_key = "", ""
    SettingsStore(settings)
    assert settings.model == "qwen2.5:14b" and settings.api_key == "sk-unsloth-abcdef123456"

    # Blank secret = remove; None = keep.
    store.update(SettingsPatch(api_key=None))
    assert settings.api_key
    store.update(SettingsPatch(api_key=""))
    assert settings.api_key == "" and store.public_view()["api_key"]["set"] is False


@pytest.mark.parametrize("bad", [{"base_url": "ftp://x"}, {"tor_socks_ports": "9200-9100"},
                                 {"tor_control_password": "has space"}, {"provider": "gemini"},
                                 {"database_url": "sqlite://"}])
def test_settings_patch_validation(bad):
    with pytest.raises(ValueError):
        SettingsPatch(**bad)


async def test_settings_api_and_model_test(settings):
    calls = []

    def factory(s):
        calls.append(s)
        return ScriptedProvider([ModelResponse("OK", []),
                                 ModelResponse("", [ToolCall("t1", "check_connection", {})])])

    app, ctx, client = await _client(settings, factory)
    try:
        view = (await client.get("/api/settings")).json()
        assert view["server"]["database"] == "SQLite" and view["api_key"]["set"] is False
        r = await client.put("/api/settings", json={"model": "llama3.1", "enable_shell": True, "api_key": "sk-test-999999"})
        assert r.status_code == 200 and set(r.json()["changed"]) == {"model", "enable_shell", "api_key"}
        assert "run_shell" in app.state.manager.registry.names()
        assert r.json()["settings"]["api_key"]["hint"] == "…9999"
        assert (await client.put("/api/settings", json={"base_url": "nope"})).status_code == 422

        # Test with unsaved form values: the draft model is used, the saved key is kept.
        t = (await client.post("/api/settings/test-model", json={"model": "draft-model"})).json()
        assert t["ok"] and t["reply"] == "OK" and t["tools"] is True
        assert calls[-1].model == "draft-model" and calls[-1].api_key == "sk-test-999999"
        assert settings.model == "llama3.1"  # testing doesn't save

        m = (await client.post("/api/settings/models", json={"base_url": "http://127.0.0.1:9/v1"})).json()
        assert m["models"] == [] and "Couldn't reach" in m["error"]
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


# ── relevance ────────────────────────────────────────────────────────────────
async def test_rank_and_toolbox_selection_with_real_connector():
    m = ConnectorManager()
    await m.sync([MCPServerConfig(name="Notes", transport="stdio", command=NOTES_CMD)])
    await m.wait_ready(30)
    try:
        tools = m.tools()
        assert len(tools) == 8
        top = rank("what's the weather like in Chicago tomorrow?", tools)
        assert top[0][1].name == "get_weather"
        assert rank("add an event to my calendar for friday", tools)[0][1].name == "create_calendar_event"
        assert rank("hello there", tools) == []

        box = Toolbox(default_registry(type("S", (), {"enable_shell": False})()), m, SessionPolicy(), max_external=2)
        chosen = box.select("search my notes about tor")
        assert [t.name for t in chosen][0] == "search_notes" and len(chosen) <= 2
        names = [s["name"] for s in box.specs()]
        assert "find_tools" in names and "notes__search_notes" in names and "notes__translate_text" not in names
        box.search("translate text into french")
        assert "notes__translate_text" in [s["name"] for s in box.specs()]

        tor_box = Toolbox(default_registry(type("S", (), {"enable_shell": False})()), m,
                          SessionPolicy(network="tor"), max_external=6)
        assert tor_box.select("weather") == [] and tor_box.usable_external() == []
    finally:
        await m.close()


# ── end to end through the API ───────────────────────────────────────────────
async def test_connector_end_to_end(settings):
    settings.mcp_max_tools = 3
    script = []

    def factory(_s):
        return ScriptedProvider(script)

    app, ctx, client = await _client(settings, factory)
    try:
        r = await client.post("/api/connectors", json={"name": "Notes", "transport": "stdio", "command": NOTES_CMD,
                                                       "env": {"NOTES_TOKEN": "secret-value-123"}})
        assert r.status_code == 201, r.text
        conn = r.json()
        assert conn["status"] == "ready" and len(conn["tool_list"]) == 8
        assert conn["env"]["NOTES_TOKEN"] == {"set": True, "hint": "…-123"}
        modes = {t["name"]: t["mode"] for t in conn["tool_list"]}
        assert modes["search_notes"] == "allow" and modes["add_note"] == "ask"
        cid = conn["id"]

        # A conversation: relevant tool is offered, read-only call runs, write call asks first,
        # and an irrelevant tool is found through find_tools.
        script[:] = [
            ModelResponse("", [ToolCall("a", "notes__search_notes", {"query": "tor"})]),
            ModelResponse("", [ToolCall("b", "notes__add_note", {"title": "x", "body": "y"})]),
            ModelResponse("", [ToolCall("c", "find_tools", {"query": "translate text"})]),
            ModelResponse("", [ToolCall("d", "notes__translate_text", {"text": "hi", "language": "fr"})]),
            ModelResponse("Done.", []),
        ]
        sid = (await client.post("/api/sessions", json={"title": "t", "policy": {}})).json()["id"]
        q = app.state.bus.subscribe(sid)
        await client.post(f"/api/sessions/{sid}/runs", json={"goal": "search my notes about tor and save a note"})
        seen = []
        while True:
            e = await asyncio.wait_for(q.get(), 20)
            seen.append(e)
            if e.type == ev.APPROVAL_REQUESTED:
                assert e.payload["connector"] == "Notes" and e.payload["connector_tool"] == "add_note"
                await client.post(f"/api/sessions/{sid}/approvals/{e.payload['approval_id']}", json={"approved": True})
            if e.type in ev.TERMINAL_EVENTS:
                break
        assert seen[-1].type == ev.RUN_FINISHED, [x.type for x in seen]
        selected = next(x for x in seen if x.type == ev.TOOLS_SELECTED).payload
        assert "notes__search_notes" in selected["connector_tools"] and selected["available"] == 8
        assert "notes__translate_text" not in selected["connector_tools"]
        done = {x.payload["tool"]: x.payload for x in seen if x.type == ev.TOOL_FINISHED}
        assert done["notes__search_notes"]["ok"] and "three relays" in done["notes__search_notes"]["preview"]
        assert done["notes__search_notes"]["connector"] == "Notes"
        assert done["notes__add_note"]["ok"] and done["notes__translate_text"]["ok"]
        assert "[fr] hi" in done["notes__translate_text"]["preview"]

        # Tool turned off disappears; Tor-only conversations can't use internet connectors.
        r = await client.put(f"/api/connectors/{cid}/tools/search_notes", json={"mode": "off"})
        assert {t["name"]: t["mode"] for t in r.json()["tool_list"]}["search_notes"] == "off"
        assert "notes__search_notes" not in [t.qualified for t in app.state.connectors.tools()]

        script[:] = [ModelResponse("", [ToolCall("e", "notes__get_weather", {"city": "Chicago"})]),
                     ModelResponse("ok", [])]
        tor_sid = (await client.post("/api/sessions", json={"title": "t", "policy": {"network": "tor"}})).json()["id"]
        q2 = app.state.bus.subscribe(tor_sid)
        await client.post(f"/api/sessions/{tor_sid}/runs", json={"goal": "weather in Chicago"})
        tor_events = []
        while True:
            e = await asyncio.wait_for(q2.get(), 20)
            tor_events.append(e)
            if e.type in ev.TERMINAL_EVENTS:
                break
        assert not any(x.type == ev.TOOL_FINISHED for x in tor_events)  # never ran
        assert next(x for x in tor_events if x.type == ev.TOOLS_SELECTED).payload["available"] == 0

        # Disable, reconnect, delete.
        r = await client.put(f"/api/connectors/{cid}/enabled", json={"enabled": False})
        assert r.json()["status"] == "off" and app.state.connectors.tools() == []
        r = await client.put(f"/api/connectors/{cid}/enabled", json={"enabled": True})
        assert r.json()["status"] == "ready"
        assert (await client.delete(f"/api/connectors/{cid}")).status_code == 200
        assert (await client.get("/api/connectors")).json() == []

        bad = await client.post("/api/connectors", json={"name": "Nope", "transport": "stdio", "command": "no-such-cmd-xyz"})
        assert bad.json()["status"] == "error" and "Couldn't start" in bad.json()["error"]
        assert (await client.post("/api/connectors", json={"name": "x", "transport": "http", "url": "ftp://x"})).status_code == 422
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_connector_over_http(settings, tmp_path):
    """Streamable HTTP transport against a real server process."""
    port = 18765
    proc = await asyncio.create_subprocess_exec(sys.executable, SERVER, "--http", str(port),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(f"http://127.0.0.1:{port}/mcp")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.2)
        m = ConnectorManager()
        await m.sync([MCPServerConfig(name="Remote notes", transport="http", url=f"http://127.0.0.1:{port}/mcp",
                                      headers={"Authorization": "Bearer test"})])
        await m.wait_ready(30)
        assert m.public()[0]["status"] == "ready", m.public()
        out = await m.call(m.find("remote_notes__get_weather"), {"city": "Oslo"}, 20)
        assert out["ok"] and "Oslo" in out["text"]
        await m.close()
    finally:
        proc.terminate()
        await proc.wait()
