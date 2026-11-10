"""Clearnet tools: a fast HTTP fetch and a full browser visit.

web_fetch re-checks policy on every redirect hop and refuses hosts that resolve
to private addresses (SSRF protection), so a public page cannot bounce the
agent into your LAN or cloud metadata endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from ..policy import check_url
from ..readable import paginate, simplify_html
from .base import OutputFile, Tool, ToolContext, ToolError, ToolResult

MAX_BYTES = 3_000_000
PAGE_CHARS = 12_000  # one window of page text per tool result; read on with `offset`

OFFSET_DESC = ("Character offset for reading further into a long page. A truncated result ends with the "
               "offset to use next; the page already read is reused, not fetched again.")


def html_to_text(html: str, base_url: str = "") -> tuple[str, str]:
    """(title, readable markdown) for an HTML document. See `openonion.readable`."""
    page = simplify_html(html, base_url, links=False)
    return page.title, page.text


def _page_result(page: dict, offset: int, files: list[OutputFile] | None = None) -> ToolResult:
    body = paginate(page["text"], offset, PAGE_CHARS, page.get("links"))
    return ToolResult(ok=page["ok"], content=page["header"] + body, data=page["data"], files=files or [])


def _remember(ctx: ToolContext, tool: str, url: str, page: dict) -> None:
    if ctx.services is not None:
        ctx.services.remember_page(tool, url, page)


def _recall(ctx: ToolContext, tool: str, url: str, offset: int) -> dict | None:
    if offset and ctx.services is not None:
        return ctx.services.recall_page(tool, url)
    return None


async def _resolves_private(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


class WebFetch(Tool):
    name = "web_fetch"
    description = ("Fetch a public clearnet URL over plain HTTP(S) and return its main content as markdown. "
                   "Fast; no JavaScript. Use `browse` for JS-heavy pages and `tor_browse` for anonymity/.onion.")
    risk = "network"

    class Args(BaseModel):
        url: str = Field(description="Absolute http(s) URL.")
        offset: int = Field(0, ge=0, description=OFFSET_DESC)

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        if cached := _recall(ctx, self.name, args.url, args.offset):
            return _page_result(cached, args.offset)
        url = args.url
        async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                     headers={"User-Agent": "OpenOnion/0.1 (+https://github.com/openonion)"}) as client:
            for _hop in range(6):
                decision = check_url(url, via_tor=False, policy=ctx.policy)
                if decision.action != "allow":
                    raise ToolError(f"blocked by policy: {decision.reason}")
                host = urlsplit(url).hostname or ""
                if await _resolves_private(host):
                    raise ToolError(f"blocked: '{host}' resolves to a private address")
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    ctype = resp.headers.get("content-type", "")
                    status = resp.status_code
                    break
            else:
                raise ToolError("too many redirects")

        raw = body.decode(resp.encoding or "utf-8", errors="replace")
        if "html" in ctype:
            readable = simplify_html(raw, url)
            title, text, links = readable.title, readable.text, readable.links
        else:
            title, text, links = "", raw, []
        page = {"ok": status < 400, "text": text, "links": links,
                "header": f"URL: {url}\nStatus: {status}\nTitle: {title}\n\n",
                "data": {"final_url": url, "status": status, "title": title, "chars": len(text)}}
        _remember(ctx, self.name, args.url, page)
        return _page_result(page, args.offset)


def _snapshot_result(tool: str, snap, via: str, ctx: ToolContext, requested_url: str, offset: int = 0) -> ToolResult:
    files = []
    if snap.screenshot:
        files.append(OutputFile(kind="screenshot", filename=f"{tool}.jpg", data=snap.screenshot,
                                mime="image/jpeg", meta={"url": snap.final_url, "via": via}))
    page = {"ok": (snap.status or 200) < 400, "text": snap.text, "links": snap.links,
            "header": f"URL: {snap.final_url}\nStatus: {snap.status}\nTitle: {snap.title}\nVia: {via}\n\n",
            "data": {"final_url": snap.final_url, "status": snap.status, "title": snap.title, "via": via,
                     "chars": len(snap.text)}}
    _remember(ctx, tool, requested_url, page)
    return _page_result(page, offset, files)


class Browse(Tool):
    name = "browse"
    description = ("Open a public clearnet URL in a headless browser (runs JavaScript) and return its main content "
                   "as markdown, plus a screenshot. Links appear as [Ln] references.")
    risk = "network"

    class Args(BaseModel):
        url: str = Field(description="Absolute http(s) URL.")
        screenshot: bool = True
        wait_ms: int = Field(0, ge=0, le=15000, description="Extra wait after load for JS-rendered content.")
        offset: int = Field(0, ge=0, description=OFFSET_DESC)

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        if cached := _recall(ctx, self.name, args.url, args.offset):
            return _page_result(cached, args.offset)
        browser = await ctx.services.clearnet_browser()
        snap = await browser.visit(args.url, screenshot=args.screenshot, wait_ms=args.wait_ms)
        return _snapshot_result(self.name, snap, "clearnet", ctx, args.url, args.offset)
