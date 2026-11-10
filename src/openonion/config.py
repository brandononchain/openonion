"""Runtime configuration, read from environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def parse_port_range(spec: str) -> list[int]:
    """'9100-9109' -> [9100..9109]; '9050' -> [9050]; '9100,9102' -> [9100, 9102]."""
    ports: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if hi < lo:
                raise ValueError(f"bad port range: {part}")
            ports.extend(range(lo, hi + 1))
        else:
            ports.append(int(part))
    if not ports:
        raise ValueError("empty port spec")
    return ports


@dataclass
class Settings:
    data_dir: Path = Path("./data")
    database_url: str = ""

    # Model provider
    provider: str = "openai"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    anthropic_api_key: str = ""
    max_output_tokens: int = 4096

    # Tor
    tor_socks_host: str = "127.0.0.1"
    tor_socks_ports: list[int] = field(default_factory=lambda: [9050])
    tor_control_host: str = "127.0.0.1"
    tor_control_port: int = 9051
    tor_control_password: str = ""

    # Browser
    browser_headless: bool = True

    # API / safety
    api_token: str = ""
    enable_shell: bool = False
    approval_timeout_s: int = 600
    tool_timeout_s: int = 180

    @property
    def workspace_root(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifact_root(self) -> Path:
        return self.data_dir / "artifacts"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("OPENONION_DATA_DIR", "./data")).resolve()
        db_url = os.getenv("DATABASE_URL") or f"sqlite+aiosqlite:///{data_dir / 'openonion.db'}"
        return cls(
            data_dir=data_dir,
            database_url=db_url,
            provider=os.getenv("OPENONION_PROVIDER", "openai"),
            model=os.getenv("OPENONION_MODEL", ""),
            base_url=os.getenv("OPENONION_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("OPENONION_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            max_output_tokens=_int("OPENONION_MAX_OUTPUT_TOKENS", 4096),
            tor_socks_host=os.getenv("TOR_SOCKS_HOST", "127.0.0.1"),
            tor_socks_ports=parse_port_range(os.getenv("TOR_SOCKS_PORTS", "9050")),
            tor_control_host=os.getenv("TOR_CONTROL_HOST", "127.0.0.1"),
            tor_control_port=_int("TOR_CONTROL_PORT", 9051),
            tor_control_password=os.getenv("TOR_CONTROL_PASSWORD", ""),
            browser_headless=_bool("BROWSER_HEADLESS", True),
            api_token=os.getenv("OPENONION_API_TOKEN", ""),
            enable_shell=_bool("OPENONION_ENABLE_SHELL", False),
            approval_timeout_s=_int("OPENONION_APPROVAL_TIMEOUT", 600),
            tool_timeout_s=_int("OPENONION_TOOL_TIMEOUT", 180),
        )

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.workspace_root, self.artifact_root):
            p.mkdir(parents=True, exist_ok=True)
