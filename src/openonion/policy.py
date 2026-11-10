"""Policy engine: decides allow / ask (human approval) / deny for every tool call.

Policy is per session, stored with the session, and visible in the event log.
The model never sees or edits it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

NetworkMode = Literal["off", "clearnet", "tor", "both"]


class SessionPolicy(BaseModel):
    """User-controlled limits for one session."""

    network: NetworkMode = Field("both", description="Which network paths tools may use.")
    allowed_tools: list[str] | None = Field(None, description="Whitelist of tool names; None = all registered.")
    approval_required: list[str] = Field(
        default_factory=lambda: ["write_file", "run_shell"],
        description="Tools that pause for human approval before running.",
    )
    clearnet_allowlist: list[str] = Field(
        default_factory=list, description="Domains web_fetch / clearnet browsing may reach. Empty = any public host."
    )
    tor_allowlist: list[str] = Field(
        default_factory=list, description="Domains tor_browse may reach. Empty = any."
    )
    javascript: bool = Field(True, description="Allow JavaScript in browser tools.")
    max_steps: int = Field(25, ge=1, le=500)
    max_tool_calls: int = Field(60, ge=1, le=2000)
    max_total_tokens: int = Field(400_000, ge=1000)


@dataclass
class Decision:
    action: Literal["allow", "ask", "deny"]
    reason: str = ""


CLEARNET_TOOLS = {"web_fetch", "browse"}
TOR_TOOLS = {"tor_browse", "tor_new_identity"}


def host_matches(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower().lstrip("*.").rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def is_private_host(host: str) -> bool:
    """True for localhost / private / link-local literals and internal-looking names.
    (Names that *resolve* to private IPs are handled again at fetch time.)"""
    h = host.lower().rstrip(".")
    if h in {"localhost", "metadata.google.internal"} or h.endswith((".localhost", ".internal", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def check_url(url: str, *, via_tor: bool, policy: SessionPolicy) -> Decision:
    try:
        parts = urlsplit(url)
    except ValueError:
        return Decision("deny", "unparseable URL")
    if parts.scheme not in {"http", "https"}:
        return Decision("deny", f"scheme '{parts.scheme}' is not allowed (http/https only)")
    host = parts.hostname or ""
    if not host:
        return Decision("deny", "URL has no host")
    if is_private_host(host):
        return Decision("deny", f"private/internal host '{host}' is blocked")
    if host.endswith(".onion") and not via_tor:
        return Decision("deny", ".onion addresses are only reachable with tor_browse")
    allow = policy.tor_allowlist if via_tor else policy.clearnet_allowlist
    if allow and not host_matches(host, allow):
        return Decision("deny", f"host '{host}' is not in the session's {'tor' if via_tor else 'clearnet'} allowlist")
    return Decision("allow")


class PolicyEngine:
    def __init__(self, policy: SessionPolicy) -> None:
        self.policy = policy

    def check(self, tool_name: str, args: dict[str, Any]) -> Decision:
        p = self.policy
        if p.allowed_tools is not None and tool_name not in p.allowed_tools and tool_name != "update_plan":
            return Decision("deny", f"tool '{tool_name}' is not enabled for this session")

        if tool_name in CLEARNET_TOOLS:
            if p.network not in {"clearnet", "both"}:
                return Decision("deny", f"clearnet access is disabled (network mode: {p.network})")
            d = check_url(str(args.get("url", "")), via_tor=False, policy=p)
            if d.action != "allow":
                return d
        if tool_name in TOR_TOOLS:
            if p.network not in {"tor", "both"}:
                return Decision("deny", f"Tor access is disabled (network mode: {p.network})")
            if tool_name == "tor_browse":
                d = check_url(str(args.get("url", "")), via_tor=True, policy=p)
                if d.action != "allow":
                    return d

        if tool_name in p.approval_required:
            return Decision("ask", f"'{tool_name}' requires approval for this session")
        return Decision("allow")
