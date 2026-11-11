"""Settings you can change from the UI.

Environment variables still set the defaults. Anything saved from the settings modal is written to
``<data dir>/settings.json`` (owner-only permissions) and wins over the environment. Secrets are write-only
from the API's point of view: the UI can set, replace or clear a key, but never read it back.

Not editable here, on purpose:
  * DATABASE_URL and OPENONION_DATA_DIR: the server is already running on them.
  * OPENONION_API_TOKEN: changing auth from inside the app it protects is a good way to lock yourself out.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings, parse_port_range

SECRET_FIELDS = {"api_key", "anthropic_api_key", "tor_control_password"}

# Fields the UI may change, with the Settings attribute each one maps to.
EDITABLE = (
    "provider", "model", "base_url", "api_key", "anthropic_api_key", "max_output_tokens",
    "tor_socks_host", "tor_socks_ports", "tor_control_host", "tor_control_port", "tor_control_password",
    "browser_headless", "enable_shell", "approval_timeout_s", "tool_timeout_s", "mcp_max_tools",
)


def _hint(value: str) -> dict[str, Any]:
    return {"set": bool(value), "hint": ("…" + value[-4:]) if len(value) >= 8 else ("set" if value else "")}


def _check_http_url(v: str) -> str:
    v = v.strip()
    parts = urlsplit(v)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("must be an http:// or https:// URL")
    return v.rstrip("/")


class SettingsPatch(BaseModel):
    """A partial update. For secrets: omit or null = keep, empty string = remove, text = replace."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "anthropic"] | None = None
    model: str | None = Field(None, max_length=300)
    base_url: str | None = None
    api_key: str | None = Field(None, max_length=4096)
    anthropic_api_key: str | None = Field(None, max_length=4096)
    max_output_tokens: int | None = Field(None, ge=64, le=200_000)

    tor_socks_host: str | None = Field(None, min_length=1, max_length=255)
    tor_socks_ports: str | None = None
    tor_control_host: str | None = Field(None, min_length=1, max_length=255)
    tor_control_port: int | None = Field(None, ge=1, le=65535)
    tor_control_password: str | None = Field(None, max_length=256)

    browser_headless: bool | None = None
    enable_shell: bool | None = None
    approval_timeout_s: int | None = Field(None, ge=10, le=86_400)
    tool_timeout_s: int | None = Field(None, ge=5, le=3_600)
    mcp_max_tools: int | None = Field(None, ge=1, le=40)

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else _check_http_url(v)

    @field_validator("tor_socks_ports")
    @classmethod
    def _ports(cls, v: str | None) -> str | None:
        if v is None:
            return None
        ports = parse_port_range(v)
        if len(ports) > 200 or not all(0 < p < 65536 for p in ports):
            raise ValueError("ports must be between 1 and 65535, at most 200 of them")
        return v.replace(" ", "")

    @field_validator("tor_control_password")
    @classmethod
    def _pw(cls, v: str | None) -> str | None:
        if v and not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("use letters, digits, '-' and '_' only (the Tor sidecar requires it)")
        return v


# ── connectors (MCP servers) ─────────────────────────────────────────────────
ToolMode = Literal["allow", "ask", "off"]


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: secrets.token_hex(4))
    name: str = Field(min_length=1, max_length=60)
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    reaches_internet: bool = True
    tools: dict[str, ToolMode] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("a local connector needs a command, for example: npx -y @modelcontextprotocol/server-everything")
        if self.transport == "http":
            self.url = _check_http_url(self.url)
        return self


class MCPServerInput(BaseModel):
    """What the UI sends to create or edit a connector. Env and header values follow the secret rules:
    a value of null keeps the stored one."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str | None] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str | None] = Field(default_factory=dict)
    enabled: bool = True
    reaches_internet: bool = True


class SettingsStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path: Path = settings.data_dir / "settings.json"
        self.overrides: dict[str, Any] = {}
        self.mcp_servers: list[MCPServerConfig] = []
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.overrides = {k: v for k, v in (raw.get("settings") or {}).items() if k in EDITABLE}
        servers = []
        for item in raw.get("mcp_servers") or []:
            try:
                servers.append(MCPServerConfig.model_validate(item))
            except ValueError:
                continue
        self.mcp_servers = servers
        self.apply()

    def _save(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"settings": self.overrides, "mcp_servers": [s.model_dump() for s in self.mcp_servers]}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def apply(self) -> None:
        s = self.settings
        for key, value in self.overrides.items():
            if key == "tor_socks_ports":
                s.tor_socks_ports = parse_port_range(str(value))
            else:
                setattr(s, key, value)

    # ── settings ─────────────────────────────────────────────────────────────
    def update(self, patch: SettingsPatch) -> list[str]:
        """Apply and persist a patch. Returns the names of fields that changed."""
        changed: list[str] = []
        for key, value in patch.model_dump(exclude_unset=True).items():
            if value is None:
                continue
            current = self.public_value(key)
            if key in SECRET_FIELDS:
                if value == getattr(self.settings, key):
                    continue
            elif value == current:
                continue
            self.overrides[key] = value
            changed.append(key)
        if changed:
            self.apply()
            self._save()
        return changed

    def public_value(self, key: str) -> Any:
        s = self.settings
        if key == "tor_socks_ports":
            ports = s.tor_socks_ports
            return f"{ports[0]}-{ports[-1]}" if len(ports) > 1 and ports == list(range(ports[0], ports[-1] + 1)) \
                else ",".join(map(str, ports))
        return getattr(s, key)

    def public_view(self) -> dict[str, Any]:
        s = self.settings
        view: dict[str, Any] = {}
        for key in EDITABLE:
            view[key] = _hint(getattr(s, key)) if key in SECRET_FIELDS else self.public_value(key)
        db = s.database_url.split("://", 1)[0] if "://" in s.database_url else s.database_url
        view["server"] = {
            "data_dir": str(s.data_dir),
            "database": "PostgreSQL" if db.startswith("postgres") else "SQLite" if db.startswith("sqlite") else db,
            "auth_required": bool(s.api_token),
            "in_docker": Path("/.dockerenv").exists(),
            "overrides_file": str(self.path),
        }
        return view

    # ── connectors ───────────────────────────────────────────────────────────
    def get_server(self, server_id: str) -> MCPServerConfig | None:
        return next((c for c in self.mcp_servers if c.id == server_id), None)

    def upsert_server(self, data: MCPServerInput, server_id: str | None = None) -> MCPServerConfig:
        existing = self.get_server(server_id) if server_id else None
        if server_id and existing is None:
            raise KeyError(server_id)

        def merge(new: dict[str, str | None], old: dict[str, str]) -> dict[str, str]:
            out: dict[str, str] = {}
            for k, v in new.items():
                k = k.strip()
                if not k:
                    continue
                if v is None:
                    if k in old:
                        out[k] = old[k]
                else:
                    out[k] = v
            return out

        cfg = MCPServerConfig(
            id=existing.id if existing else secrets.token_hex(4),
            name=data.name.strip(), transport=data.transport, command=data.command.strip(),
            args=[a for a in data.args if a != ""], url=data.url.strip(),
            env=merge(data.env, existing.env if existing else {}),
            headers=merge(data.headers, existing.headers if existing else {}),
            enabled=data.enabled, reaches_internet=data.reaches_internet,
            tools=existing.tools if existing else {},
        )
        if existing:
            self.mcp_servers = [cfg if c.id == cfg.id else c for c in self.mcp_servers]
        else:
            self.mcp_servers.append(cfg)
        self._save()
        return cfg

    def set_tool_mode(self, server_id: str, tool: str, mode: ToolMode) -> MCPServerConfig:
        cfg = self.get_server(server_id)
        if cfg is None:
            raise KeyError(server_id)
        cfg.tools[tool] = mode
        self._save()
        return cfg

    def set_enabled(self, server_id: str, enabled: bool) -> MCPServerConfig:
        cfg = self.get_server(server_id)
        if cfg is None:
            raise KeyError(server_id)
        cfg.enabled = enabled
        self._save()
        return cfg

    def delete_server(self, server_id: str) -> bool:
        before = len(self.mcp_servers)
        self.mcp_servers = [c for c in self.mcp_servers if c.id != server_id]
        if len(self.mcp_servers) != before:
            self._save()
            return True
        return False

    @staticmethod
    def server_public(cfg: MCPServerConfig) -> dict[str, Any]:
        return {
            "id": cfg.id, "name": cfg.name, "transport": cfg.transport, "command": cfg.command, "args": cfg.args,
            "url": cfg.url, "enabled": cfg.enabled, "reaches_internet": cfg.reaches_internet,
            "env": {k: _hint(v) for k, v in cfg.env.items()},
            "headers": {k: _hint(v) for k, v in cfg.headers.items()},
            "tools": cfg.tools,
        }
