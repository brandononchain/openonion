"""Clearnet tools: a fast HTTP fetch and a full browser visit.

web_fetch re-checks policy on every redirect hop and refuses hosts that resolve
to private addresses (SSRF protection), so a public page cannot bounce the
agent into your LAN or cloud metadata endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from ..policy import check_url
from .base import OutputFile, Tool, ToolContext, ToolError, ToolResult

MAX_BYTES = 3_000_000
MAX_TEXT = 20_000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


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
    description = ("Fetch a public clearnet URL over plain HTTP(S) and return its readable text. "
                   "Fast; no JavaScript. Use `browse` for JS-heavy pages and `tor_browse` for anonymity/.onion.")
    risk = "network"

    class Args(BaseModel):
        url: str = Field(description="Absolute http(s) URL.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
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
            title, text = html_to_text(raw)
        else:
            title, text = "", raw
        header = f"URL: {url}\nStatus: {status}\nTitle: {title}\n\n"
        return ToolResult(ok=status < 400, content=header + text[:MAX_TEXT],
                          data={"final_url": url, "status": status, "title": title})


def _snapshot_result(tool: str, snap, via: str) -> ToolResult:
    files = []
    if snap.screenshot:
        files.append(OutputFile(kind="screenshot", filename=f"{tool}.jpg", data=snap.screenshot,
                                mime="image/jpeg", meta={"url": snap.final_url, "via": via}))
    header = f"URL: {snap.final_url}\nStatus: {snap.status}\nTitle: {snap.title}\nVia: {via}\n\n"
    return ToolResult(ok=(snap.status or 200) < 400, content=header + snap.text,
                      data={"final_url": snap.final_url, "status": snap.status, "title": snap.title, "via": via},
                      files=files)


class Browse(Tool):
    name = "browse"
    description = "Open a public clearnet URL in a headless browser (runs JavaScript) and return its text plus a screenshot."
    risk = "network"

    class Args(BaseModel):
        url: str = Field(description="Absolute http(s) URL.")
        screenshot: bool = True
        wait_ms: int = Field(0, ge=0, le=15000, description="Extra wait after load for JS-rendered content.")

    async def run(self, args: Args, ctx: ToolContext) -> ToolResult:
        browser = await ctx.services.clearnet_browser()
        snap = await browser.visit(args.url, screenshot=args.screenshot, wait_ms=args.wait_ms)
        return _snapshot_result("browse", snap, via="clearnet")
