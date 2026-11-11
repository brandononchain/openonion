"""Settings and connector (MCP) routes used by the settings modal.

  GET  /api/settings                       current settings (secrets masked) + read-only server info
  PUT  /api/settings                       save a partial update; applies live
  POST /api/settings/models                list the models an endpoint offers (uses unsaved form values)
  POST /api/settings/test-model            send a tiny prompt, and check the model can call tools
  POST /api/settings/test-tor              Tor bootstrap progress + exit check
  GET  /api/connectors                     connectors with status and tools
  POST /api/connectors                     add a connector (waits briefly for it to connect)
  PUT  /api/connectors/{id}                edit a connector
  DELETE /api/connectors/{id}              remove a connector
  POST /api/connectors/{id}/reconnect      reconnect
  PUT  /api/connectors/{id}/enabled        turn a connector on or off
  PUT  /api/connectors/{id}/tools/{tool}   set a tool to allow / ask / off
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from ..browser.tor import tor_bootstrap_progress, tor_check
from ..connectors import CONNECT_TIMEOUT_S, ConnectorManager
from ..providers.base import Message
from ..settings_store import MCPServerInput, SettingsPatch, SettingsStore, ToolMode


class Enabled(BaseModel):
    enabled: bool


class Mode(BaseModel):
    mode: ToolMode


def _friendly(exc: Exception, url: str = "") -> str:
    text = str(exc)
    if isinstance(exc, httpx.ConnectError) or "Connection refused" in text or "All connection attempts failed" in text:
        return f"Couldn't reach {url or 'the server'}. Is it running, and is the address right?"
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, asyncio.TimeoutError):
        return "The server didn't answer in time."
    if "401" in text or "403" in text:
        return "The server rejected the API key."
    if "404" in text:
        return "The server answered, but that address isn't an API endpoint. Check the base URL (it usually ends in /v1)."
    return text[:400] or type(exc).__name__


def register(app: FastAPI, auth, *, store: SettingsStore, manager, connectors: ConnectorManager) -> None:
    settings = store.settings

    def draft(patch: SettingsPatch) -> Any:
        """A copy of the live settings with unsaved form values applied (blank secrets keep the saved ones)."""
        values = {k: v for k, v in patch.model_dump(exclude_unset=True).items() if v is not None}
        values.pop("tor_socks_ports", None)
        if "api_key" in values and values["api_key"] == "":
            values["api_key"] = ""
        return dataclasses.replace(settings, **values)

    # ── settings ─────────────────────────────────────────────────────────────
    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings():
        return store.public_view()

    @app.put("/api/settings", dependencies=[Depends(auth)])
    async def put_settings(patch: SettingsPatch):
        if patch.tor_socks_ports is not None and manager.any_running() \
                and patch.tor_socks_ports != store.public_value("tor_socks_ports"):
            raise HTTPException(409, "Finish or stop running conversations before changing Tor ports.")
        changed = store.update(patch)
        manager.reconfigure(changed)
        return {"changed": changed, "settings": store.public_view()}

    @app.post("/api/settings/models", dependencies=[Depends(auth)])
    async def list_models(patch: SettingsPatch):
        s = draft(patch)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if s.provider == "anthropic":
                    key = s.anthropic_api_key or s.api_key
                    if not key:
                        return {"models": [], "error": "Add your Anthropic API key first."}
                    r = await client.get("https://api.anthropic.com/v1/models?limit=100",
                                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
                else:
                    headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
                    r = await client.get(f"{s.base_url.rstrip('/')}/models", headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001 - shown to the user as advice
            return {"models": [], "error": _friendly(exc, s.base_url)}
        items = data.get("data") if isinstance(data, dict) else data
        models = sorted({str(m.get("id")) for m in items or [] if isinstance(m, dict) and m.get("id")})
        if not models and isinstance(data, dict) and isinstance(data.get("models"), list):  # Ollama's native shape
            models = sorted({str(m.get("name") or m.get("model")) for m in data["models"] if isinstance(m, dict)})
        return {"models": models, "error": "" if models else "The server answered but listed no models."}

    @app.post("/api/settings/test-model", dependencies=[Depends(auth)])
    async def test_model(patch: SettingsPatch):
        s = draft(patch)
        if s.provider != "anthropic" and not s.model:
            return {"ok": False, "error": "Pick a model first."}
        s = dataclasses.replace(s, max_output_tokens=64)
        try:
            provider = manager.provider_factory(s)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _friendly(exc, s.base_url)}
        try:
            t0 = time.monotonic()
            reply = await asyncio.wait_for(provider.complete(
                [Message("system", "You are a connection test."), Message("user", "Reply with just the word OK.")], []), 90)
            latency = time.monotonic() - t0
            tool = {"name": "check_connection", "description": "Confirms the connection works. Call it when asked.",
                    "parameters": {"type": "object", "properties": {}}}
            try:
                probe = await asyncio.wait_for(provider.complete(
                    [Message("user", "Call the check_connection tool now.")], [tool]), 90)
                tools_ok = any(c.name == "check_connection" for c in probe.tool_calls)
            except Exception:  # noqa: BLE001 - a model that rejects tools is the answer we're looking for
                tools_ok = False
            return {"ok": True, "latency_s": round(latency, 2), "reply": (reply.text or "").strip()[:200],
                    "tools": tools_ok, "model": getattr(provider, "model", s.model)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _friendly(exc, s.base_url)}
        finally:
            await provider.aclose()

    @app.post("/api/settings/test-tor", dependencies=[Depends(auth)])
    async def test_tor(patch: SettingsPatch):
        s = draft(patch)
        ports = store.settings.tor_socks_ports
        if patch.tor_socks_ports:
            from ..config import parse_port_range
            ports = parse_port_range(patch.tor_socks_ports)
        out: dict[str, Any] = {"bootstrap": None, "is_tor": None, "exit_ip": None, "errors": []}
        if s.tor_control_password:
            try:
                out["bootstrap"] = await tor_bootstrap_progress(s.tor_control_host, s.tor_control_port,
                                                                s.tor_control_password)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"Control port: {_friendly(exc, f'{s.tor_control_host}:{s.tor_control_port}')}")
        else:
            out["errors"].append("No control password set, so bootstrap progress and new identities aren't available.")
        try:
            result = await asyncio.wait_for(tor_check(s.tor_socks_host, ports[0]), 60)
            out["is_tor"], out["exit_ip"] = bool(result.get("IsTor")), result.get("IP")
        except ImportError:
            out["errors"].append("Install httpx[socks] on the server to run the exit check.")
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"SOCKS {s.tor_socks_host}:{ports[0]}: {_friendly(exc, f'{s.tor_socks_host}:{ports[0]}')}")
        return out

    # ── connectors ───────────────────────────────────────────────────────────
    def listing() -> list[dict[str, Any]]:
        live = {item["id"]: item for item in connectors.public()}
        out = []
        for cfg in store.mcp_servers:
            item = store.server_public(cfg)
            state = live.get(cfg.id, {"status": "off", "error": "", "server_label": "", "tools": []})
            item.update(status=state["status"] if cfg.enabled else "off", error=state["error"],
                        server_label=state["server_label"], tool_list=state["tools"])
            out.append(item)
        return out

    async def settle(server_id: str) -> None:
        state = connectors.servers.get(server_id)
        if state and state.status == "connecting":
            try:
                await asyncio.wait_for(state.ready.wait(), CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass

    def one(server_id: str) -> dict[str, Any]:
        item = next((i for i in listing() if i["id"] == server_id), None)
        if item is None:
            raise HTTPException(404, "connector not found")
        return item

    @app.get("/api/connectors", dependencies=[Depends(auth)])
    async def list_connectors():
        return listing()

    @app.post("/api/connectors", dependencies=[Depends(auth)], status_code=201)
    async def add_connector(body: MCPServerInput):
        try:
            cfg = store.upsert_server(body)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False)[0]["msg"])
        await connectors.sync(store.mcp_servers)
        await settle(cfg.id)
        return one(cfg.id)

    @app.put("/api/connectors/{server_id}", dependencies=[Depends(auth)])
    async def edit_connector(server_id: str, body: MCPServerInput):
        try:
            cfg = store.upsert_server(body, server_id)
        except KeyError:
            raise HTTPException(404, "connector not found")
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False)[0]["msg"])
        await connectors.sync(store.mcp_servers)
        await settle(cfg.id)
        return one(cfg.id)

    @app.delete("/api/connectors/{server_id}", dependencies=[Depends(auth)])
    async def delete_connector(server_id: str):
        if not store.delete_server(server_id):
            raise HTTPException(404, "connector not found")
        await connectors.sync(store.mcp_servers)
        return {"deleted": server_id}

    @app.post("/api/connectors/{server_id}/reconnect", dependencies=[Depends(auth)])
    async def reconnect_connector(server_id: str):
        one(server_id)
        await connectors.reconnect(server_id)
        await settle(server_id)
        return one(server_id)

    @app.put("/api/connectors/{server_id}/enabled", dependencies=[Depends(auth)])
    async def enable_connector(server_id: str, body: Enabled):
        try:
            store.set_enabled(server_id, body.enabled)
        except KeyError:
            raise HTTPException(404, "connector not found")
        await connectors.sync(store.mcp_servers)
        await settle(server_id)
        return one(server_id)

    @app.put("/api/connectors/{server_id}/tools/{tool_name}", dependencies=[Depends(auth)])
    async def set_tool(server_id: str, tool_name: str, body: Mode):
        try:
            store.set_tool_mode(server_id, tool_name, body.mode)
        except KeyError:
            raise HTTPException(404, "connector not found")
        state = connectors.servers.get(server_id)
        if state:
            state.config = store.get_server(server_id)
        return one(server_id)
