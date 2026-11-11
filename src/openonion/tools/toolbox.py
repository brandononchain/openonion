"""Decides which tools the model sees on each turn.

Built-in tools are always offered. Connector (MCP) tools are not all dumped into the prompt: a busy setup can
expose hundreds of them, which wastes context and makes models pick the wrong one. Instead:

  1. At the start of a run, connector tools are ranked against the user's message (and the last few turns) and
     only the top few are offered (``mcp_max_tools``, default 6).
  2. If more tools exist than are offered, the model also gets ``find_tools``. Calling it searches every
     connector tool and makes the matches available from the next step on.

Ranking is BM25 over each tool's name, server and description: fast, deterministic, needs no embedding model,
and good at the thing that matters here (the user said "calendar", the tool says "calendar").
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult

if TYPE_CHECKING:
    from ..connectors import ConnectorManager, ExternalTool
    from ..policy import SessionPolicy

STOP = set("""a an and are as at be by can do for from get give how i in into is it its me my of on or our
please show that the their them then this to up us using use was we what when where which who will with you your
tool tools""".split())


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def rank(query: str, tools: list["ExternalTool"], k1: float = 1.4, b: float = 0.7) -> list[tuple[float, "ExternalTool"]]:
    q = tokens(query)
    if not q or not tools:
        return []
    docs = [tokens(f"{t.name} {t.name} {t.server_name} {t.description}") for t in tools]
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter(term for d in docs for term in set(d))
    scored = []
    for t, d in zip(tools, docs):
        tf = Counter(d)
        score = 0.0
        for term in set(q):
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(d) / avg))
        if score > 0:
            scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    return scored


def network_blocked(tool: "ExternalTool", policy: "SessionPolicy") -> str | None:
    if tool.reaches_internet and policy.network in {"tor", "off"}:
        mode = "Tor-only" if policy.network == "tor" else "Offline"
        return (f"the {tool.server_name} connector reaches the internet directly, not through Tor, "
                f"so it's switched off in {mode} conversations")
    return None


class Toolbox:
    """Per-run view of available tools: built-ins plus the connector tools chosen for this conversation."""

    def __init__(self, registry: Any, connectors: "ConnectorManager | None", policy: "SessionPolicy",
                 max_external: int) -> None:
        self.registry = registry
        self.connectors = connectors
        self.policy = policy
        self.max_external = max(1, max_external)
        self.active: dict[str, "ExternalTool"] = {}

    def usable_external(self) -> list["ExternalTool"]:
        if not self.connectors:
            return []
        allowed = self.policy.allowed_tools
        return [t for t in self.connectors.tools()
                if not network_blocked(t, self.policy) and (allowed is None or t.qualified in allowed)]

    def select(self, context: str) -> list["ExternalTool"]:
        """Choose the connector tools to offer for this run."""
        usable = self.usable_external()
        if len(usable) <= self.max_external:
            chosen = usable
        else:
            chosen = [t for _, t in rank(context, usable)[: self.max_external]]
        self.active = {t.qualified: t for t in chosen}
        return chosen

    def search(self, query: str, limit: int = 5) -> list["ExternalTool"]:
        found = [t for _, t in rank(query, self.usable_external())[:limit]]
        for t in found:
            self.active[t.qualified] = t
        return found

    @property
    def has_more(self) -> bool:
        return len(self.usable_external()) > len(self.active)

    def specs(self) -> list[dict[str, Any]]:
        specs = self.registry.specs(self.policy.allowed_tools)
        specs += [t.spec() for t in self.active.values()]
        if self.has_more:
            specs.append(FindTools().spec())
        return specs

    def get(self, name: str) -> Tool | None:
        tool = self.registry.get(name)
        if tool is not None:
            return tool
        if name == FindTools.name:
            return FindTools() if self.connectors else None
        ext = self.active.get(name)
        return ConnectorTool(ext, self.connectors) if ext else None


class FindTools(Tool):
    name = "find_tools"
    description = ("Search the user's connected apps (connectors) for tools that aren't in your tool list yet. "
                   "Describe what you need in a few words, e.g. 'create calendar event' or 'github issues'. "
                   "Matching tools become available on your next step.")
    risk = "meta"

    class Args(BaseModel):
        query: str = Field(description="A few words describing the capability you need.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        box: Toolbox | None = getattr(ctx, "toolbox", None)
        if box is None:
            raise ToolError("no connectors are configured")
        found = box.search(args.query)
        if not found:
            return ToolResult(ok=True, content="No connector tools match that. Try other words, or do without.",
                              data={"found": []})
        lines = [f"- {t.qualified}: {(t.description or '').strip()[:160]}" for t in found]
        return ToolResult(ok=True, content="These tools are now available to call:\n" + "\n".join(lines),
                          data={"found": [t.qualified for t in found]})


class ConnectorTool(Tool):
    """Adapter that runs one connector tool through the Tool interface."""

    risk = "network"

    class Args(BaseModel):
        model_config = {"extra": "allow"}

    def __init__(self, ext: "ExternalTool", connectors: "ConnectorManager") -> None:
        self.ext = ext
        self.connectors = connectors
        self.name = ext.qualified  # type: ignore[misc]
        self.description = ext.description  # type: ignore[misc]

    def spec(self) -> dict[str, Any]:
        return self.ext.spec()

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        from .base import OutputFile

        arguments = args.model_dump()
        required = (self.ext.input_schema or {}).get("required") or []
        missing = [r for r in required if r not in arguments]
        if missing:
            raise ToolError(f"missing required argument(s): {', '.join(missing)}")
        out = await self.connectors.call(self.ext, arguments, timeout=ctx.settings.tool_timeout_s)
        files = [OutputFile(kind="image", filename=f"{self.ext.name}-{i}.{mime.split('/')[-1]}", data=data, mime=mime,
                            meta={"connector": self.ext.server_name})
                 for i, (data, mime) in enumerate(out["images"])]
        return ToolResult(ok=out["ok"], content=out["text"] if out["ok"] else f"ERROR: {out['text']}",
                          data={"connector": self.ext.server_name, "tool": self.ext.name}, files=files)
