"""Headless Chromium via Playwright, optionally routed through a Tor SOCKS listener.

One BrowserService per agent session: cookies and storage persist across calls
within a session (so multi-page flows work) but never leak between sessions.

What this does and does NOT give you over Tor:
  * Your IP is hidden and DNS (incl. .onion) resolves inside Tor — Chromium
    sends hostnames to SOCKS5 proxies rather than resolving locally.
  * WebRTC is forced to proxied-only and QUIC is disabled, closing the two
    classic UDP side channels that bypass a SOCKS proxy.
  * It is NOT Tor Browser. Chromium's fingerprint is distinctive and differs
    from every Tor Browser user. Treat it as "IP-anonymized", not "anonymous".
    See docs/tor-threat-model.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..readable import simplify_html

PRIVACY_ARGS = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
]


class BrowserUnavailable(RuntimeError):
    pass


@dataclass
class PageSnapshot:
    url: str
    final_url: str
    status: int | None
    title: str
    text: str
    screenshot: bytes | None
    links: list[str] = field(default_factory=list)


# Remove elements a human can't see before reading the DOM. Hidden text is a
# common prompt-injection channel, and it is never content the reader meant.
# Runs after the screenshot, so the screenshot still shows the page as rendered.
STRIP_HIDDEN_JS = """() => {
  if (!document.body) return '';
  const doomed = [];
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse' ||
        parseFloat(cs.opacity) === 0 || parseFloat(cs.fontSize) < 1) { doomed.push(el); continue; }
    if (cs.position === 'absolute' || cs.position === 'fixed') {
      const r = el.getBoundingClientRect();
      if (r.right <= 0 || r.bottom <= 0 || (r.width <= 1 && r.height <= 1)) doomed.push(el);
    }
  }
  for (const el of doomed) el.remove();
  return document.documentElement.outerHTML;
}"""


class BrowserService:
    def __init__(self, proxy_server: str | None = None, *, headless: bool = True,
                 javascript: bool = True, nav_timeout_ms: int = 90_000) -> None:
        self.proxy_server = proxy_server
        self.headless = headless
        self.javascript = javascript
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise BrowserUnavailable(
                    "Playwright is not installed. pip install 'openonion[browser]' && playwright install chromium"
                ) from exc

            self._pw = await async_playwright().start()
            launch_kwargs: dict = {"headless": self.headless, "args": PRIVACY_ARGS}
            if self.proxy_server:
                launch_kwargs["proxy"] = {"server": self.proxy_server}
            try:
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                await self._pw.stop()
                self._pw = None
                raise BrowserUnavailable(f"could not launch Chromium: {exc}") from exc

            # Headless Chromium advertises "HeadlessChrome" in its UA, which is
            # an instant bot flag. Use the matching regular-Chrome UA instead of
            # spoofing a different browser (a mismatched UA is itself a fingerprint).
            ua = await self._default_user_agent()
            self._context = await self._browser.new_context(
                user_agent=ua,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="UTC",
                java_script_enabled=self.javascript,
                accept_downloads=False,
                service_workers="block",
            )

    async def _default_user_agent(self) -> str:
        probe = await self._browser.new_context()
        try:
            page = await probe.new_page()
            ua = await page.evaluate("navigator.userAgent")
        finally:
            await probe.close()
        return ua.replace("HeadlessChrome", "Chrome")

    async def visit(self, url: str, *, screenshot: bool = True, wait_ms: int = 0,
                    max_text: int = 400_000, attempts: int = 2) -> PageSnapshot:
        await self.start()
        last_exc: Exception | None = None
        for attempt in range(attempts):
            page = await self._context.new_page()
            page.set_default_timeout(self.nav_timeout_ms)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                if wait_ms:
                    await page.wait_for_timeout(min(wait_ms, 15_000))
                title = await page.title()
                shot = await page.screenshot(type="jpeg", quality=60, full_page=False) if screenshot else None
                html = await page.evaluate(STRIP_HIDDEN_JS)
                page_text = simplify_html(html or "", page.url)
                text, links = page_text.text, page_text.links
                if len(text) < 200:  # canvas apps and odd markup: fall back to what's visibly rendered
                    raw = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    if len((raw or "").strip()) > len(text):
                        text, links = raw.strip(), []
                return PageSnapshot(url=url, final_url=page.url, status=resp.status if resp else None,
                                    title=title or page_text.title, text=text[:max_text], screenshot=shot,
                                    links=links)
            except Exception as exc:  # Tor circuits fail transiently; retry once.
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(2)
            finally:
                await page.close()
        raise RuntimeError(f"navigation failed: {last_exc}")

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
            self._context = self._browser = self._pw = None
