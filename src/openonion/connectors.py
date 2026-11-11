"""Connectors: external tools from MCP (Model Context Protocol) servers.

Each configured server gets one long-lived connection, owned by its own asyncio task. The MCP SDK uses anyio
cancel scopes, which must be entered and exited by the same task, so the task opens the client, publishes it,
then sleeps until asked to stop. Tool calls from agent runs go through that open client.

Security model, in short:
  * A connector's tools run with the server's own permissions and network access. They do NOT go through Tor.
    Servers marked "reaches the internet" are switched off in Tor-only and Offline conversations.
  * Each tool has a mode: allow, ask (approval required) or off. Read-only tools default to allow, everything
    else to ask.
  * Results are untrusted data, wrapped like every other tool result before the model sees them.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from .settings_store import MCPServerConfig, ToolMode

log = logging.getLogger("openonion.connectors")

CONNECT_TIMEOUT_S = 45
MAX_RESULT_CHARS = 30_000


def slug(value: str, limit: int = 28) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return (s or "x")[:limit]


@dataclass
class ExternalTool:
    """One tool offered by a connector, as the agent sees it."""

    server_id: str
    server_name: str
    name: str                  # the tool's own name on its server
    qualified: str             # unique name exposed to the model: <server>__<tool>
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    mode: ToolMode
    reaches_internet: bool

    def spec(self) -> dict[str, Any]:
        schema = dict(self.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        desc = (self.description or "").strip() or f"{self.name} tool"
        return {"name": self.qualified, "description": f"[{self.server_name} connector] {desc}"[:1024],
                "parameters": schema}

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "qualified": self.qualified, "description": self.description,
                "read_only": self.read_only, "destructive": self.destructive, "mode": self.mode}


@dataclass
class ServerState:
    config: MCPServerConfig
    status: str = "off"        # off | connecting | ready | error
    error: str = ""
    server_label: str = ""
    raw_tools: list[Any] = field(default_factory=list)
    client: Any = None
    task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


def _default_mode(tool: Any) -> ToolMode:
    ann = getattr(tool, "annotations", None)
    return "allow" if (ann is not None and getattr(ann, "read_only_hint", None)) else "ask"


def _transport_for(cfg: MCPServerConfig):
    if cfg.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, get_default_environment

        parts = shlex.split(cfg.command)
        return StdioServerParameters(command=parts[0], args=parts[1:] + list(cfg.args),
                                     env={**get_default_environment(), **cfg.env})
    if cfg.headers:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        return streamable_http_client(cfg.url, http_client=httpx2.AsyncClient(headers=dict(cfg.headers),
                                                                              timeout=httpx2.Timeout(60, read=300)))
    return cfg.url


class ConnectorManager:
    def __init__(self) -> None:
        self.servers: dict[str, ServerState] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def sync(self, configs: list[MCPServerConfig]) -> None:
        """Bring running connections in line with the saved configuration."""
        wanted = {c.id: c for c in configs}
        for sid in list(self.servers):
            if sid not in wanted:
                await self._stop(sid)
                self.servers.pop(sid, None)
        for cfg in configs:
            state = self.servers.get(cfg.id)
            if state is None:
                self.servers[cfg.id] = ServerState(config=cfg)
                if cfg.enabled:
                    await self._start(cfg.id)
                continue
            connection_changed = state.config.model_dump(exclude={"tools", "enabled", "name", "reaches_internet"}) != \
                cfg.model_dump(exclude={"tools", "enabled", "name", "reaches_internet"})
            state.config = cfg
            if not cfg.enabled:
                await self._stop(cfg.id)
            elif connection_changed or state.status in {"off", "error"}:
                await self._stop(cfg.id)
                await self._start(cfg.id)

    async def reconnect(self, server_id: str) -> None:
        if server_id in self.servers:
            await self._stop(server_id)
            if self.servers[server_id].config.enabled:
                await self._start(server_id)

    async def _start(self, sid: str) -> None:
        state = self.servers[sid]
        state.stop = asyncio.Event()
        state.ready = asyncio.Event()
        state.status, state.error, state.raw_tools = "connecting", "", []
        state.task = asyncio.create_task(self._run(state), name=f"connector-{sid}")

    async def _run(self, state: ServerState) -> None:
        from mcp import Client
        from mcp_types import Implementation

        cfg = state.config
        try:
            transport = _transport_for(cfg)
            async with Client(transport, read_timeout_seconds=300,
                              client_info=Implementation(name="openonion", version="0.1")) as client:
                tools: list[Any] = []
                cursor = None
                for _ in range(20):
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = getattr(page, "next_cursor", None)
                    if not cursor:
                        break
                info = client.server_info
                state.server_label = f"{info.name} {info.version}" if info else ""
                state.client, state.raw_tools, state.status = client, tools, "ready"
                state.ready.set()
                await state.stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # ExceptionGroups from anyio land here too
            state.status = "error"
            state.error = _explain(exc, cfg)
            log.warning("connector %s failed: %s", cfg.name, state.error)
        finally:
            state.client = None
            if state.status == "ready":
                state.status = "off"
            state.ready.set()

    async def _stop(self, sid: str) -> None:
        state = self.servers.get(sid)
        if not state or not state.task:
            if state:
                state.status = "off"
            return
        state.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(state.task), 10)
        except (asyncio.TimeoutError, Exception):
            state.task.cancel()
            try:
                await state.task
            except BaseException:
                pass
        state.task, state.client = None, None
        state.status = "off" if state.status != "error" else "error"

    async def wait_ready(self, timeout: float = CONNECT_TIMEOUT_S) -> None:
        pending = [s.ready.wait() for s in self.servers.values() if s.status == "connecting"]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending), timeout)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        for sid in list(self.servers):
            await self._stop(sid)

    # ── tools ────────────────────────────────────────────────────────────────
    def tools(self, include_off: bool = False) -> list[ExternalTool]:
        out: list[ExternalTool] = []
        used: set[str] = set()
        for state in self.servers.values():
            if state.status != "ready":
                continue
            prefix = slug(state.config.name, 20)
            for t in state.raw_tools:
                mode = state.config.tools.get(t.name) or _default_mode(t)
                if mode == "off" and not include_off:
                    continue
                q = f"{prefix}__{slug(t.name, 40)}"[:64]
                n = 2
                while q in used:
                    q = f"{prefix}__{slug(t.name, 36)}_{n}"[:64]
                    n += 1
                used.add(q)
                ann = getattr(t, "annotations", None)
                out.append(ExternalTool(
                    server_id=state.config.id, server_name=state.config.name, name=t.name, qualified=q,
                    description=(t.description or getattr(t, "title", None) or ""),
                    input_schema=t.input_schema or {"type": "object", "properties": {}},
                    read_only=bool(ann and ann.read_only_hint), destructive=bool(ann and ann.destructive_hint),
                    mode=mode, reaches_internet=state.config.reaches_internet,
                ))
        return out

    def find(self, qualified: str) -> ExternalTool | None:
        return next((t for t in self.tools() if t.qualified == qualified), None)

    async def call(self, tool: ExternalTool, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        state = self.servers.get(tool.server_id)
        if not state or state.status != "ready" or state.client is None:
            raise RuntimeError(f"the {tool.server_name} connector isn't connected right now")
        result = await asyncio.wait_for(state.client.call_tool(tool.name, arguments), timeout)
        texts: list[str] = []
        images: list[tuple[bytes, str]] = []
        for block in result.content or []:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "image":
                try:
                    images.append((base64.b64decode(block.data), getattr(block, "mime_type", None) or "image/png"))
                except (ValueError, TypeError):
                    texts.append("[image that could not be decoded]")
            elif kind == "resource_link":
                texts.append(f"[resource] {getattr(block, 'name', '')} {getattr(block, 'uri', '')}".strip())
            elif kind == "resource":
                res = getattr(block, "resource", None)
                texts.append(getattr(res, "text", None) or f"[resource] {getattr(res, 'uri', '')}")
            else:
                texts.append(f"[{kind or 'unknown'} content]")
        if not texts and result.structured_content is not None:
            texts.append(json.dumps(result.structured_content, indent=2, default=str))
        text = "\n".join(texts).strip() or "(the tool returned no content)"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n…[truncated, {len(text)} characters total]"
        return {"ok": not bool(result.is_error), "text": text, "images": images}

    def public(self) -> list[dict[str, Any]]:
        out = []
        tool_lists: dict[str, list[ExternalTool]] = {}
        for t in self.tools(include_off=True):
            tool_lists.setdefault(t.server_id, []).append(t)
        for sid, state in self.servers.items():
            out.append({"id": sid, "status": state.status if state.config.enabled else "off", "error": state.error,
                        "server_label": state.server_label,
                        "tools": [t.public() for t in tool_lists.get(sid, [])]})
        return out


def _explain(exc: BaseException, cfg: MCPServerConfig) -> str:
    """Turn transport errors (often wrapped in ExceptionGroups) into one useful sentence."""
    leaves: list[BaseException] = []

    def walk(e: BaseException) -> None:
        subs = getattr(e, "exceptions", None)
        if subs:
            for s in subs:
                walk(s)
        else:
            leaves.append(e)

    walk(exc)
    first = leaves[0] if leaves else exc
    msg = str(first).strip() or type(first).__name__
    if isinstance(first, FileNotFoundError):
        cmd = shlex.split(cfg.command)[0] if cfg.command else "the command"
        return f"Couldn't start '{cmd}'. Is it installed and on the PATH of the OpenOnion server?"
    if "ConnectError" in type(first).__name__ or "connection" in msg.lower() and "refused" in msg.lower():
        return f"Couldn't reach {cfg.url}. Is the server running?"
    if "401" in msg or "403" in msg:
        return "The server refused the connection. Check the authorization header."
    return f"{type(first).__name__}: {msg}"[:400]
