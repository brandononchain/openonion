"""Tor tools: browse through this session's isolated Tor listener, and rotate circuits."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..browser.tor import tor_new_identity
from .base import Tool, ToolContext, ToolError, ToolResult
from .web import OFFSET_DESC, _page_result, _recall, _snapshot_result


class TorBrowse(Tool):
    name = "tor_browse"
    description = (
        "Open a URL (clearnet or .onion) in a headless browser routed through this session's own Tor circuits. "
        "Your IP is hidden; slower than clearnet. Returns the main content as markdown (links as [Ln]) and a screenshot."
    )
    risk = "network"

    class Args(BaseModel):
        url: str = Field(description="Absolute http(s) URL, including .onion addresses.")
        screenshot: bool = True
        wait_ms: int = Field(0, ge=0, le=15000)
        offset: int = Field(0, ge=0, description=OFFSET_DESC)

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        if cached := _recall(ctx, self.name, args.url, args.offset):
            return _page_result(cached, args.offset)
        browser = await ctx.services.tor_browser()
        snap = await browser.visit(args.url, screenshot=args.screenshot, wait_ms=args.wait_ms)
        return _snapshot_result(self.name, snap, f"tor:{ctx.services.tor_port}", ctx, args.url, args.offset)


class TorNewIdentity(Tool):
    name = "tor_new_identity"
    description = "Ask Tor for fresh circuits (new exit IP) for subsequent requests. Tor rate-limits this to ~1 per 10s."
    risk = "network"

    class Args(BaseModel):
        pass

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        s = ctx.settings
        if not s.tor_control_password:
            raise ToolError("TOR_CONTROL_PASSWORD is not configured")
        await tor_new_identity(s.tor_control_host, s.tor_control_port, s.tor_control_password)
        # Close the browser so keep-alive connections don't pin the old circuit.
        await ctx.services.reset_tor_browser()
        return ToolResult(ok=True, content="Requested new Tor circuits (NEWNYM). The next tor_browse uses fresh circuits.")
