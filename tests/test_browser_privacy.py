"""Regression test for the core Tor privacy property: the browser must hand every
hostname (incl. .onion) to the SOCKS proxy unresolved. A local DNS lookup would
leak what the agent is visiting to the host's resolver, outside Tor.

Skipped when Playwright/Chromium is not installed."""

from __future__ import annotations

import asyncio

import pytest

playwright = pytest.importorskip("playwright.async_api")

from openonion.browser.service import BrowserService  # noqa: E402


async def _chromium_available() -> bool:
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch()
            await browser.close()
        return True
    except Exception:
        return False


async def test_hostnames_are_resolved_by_proxy_not_locally():
    if not await _chromium_available():
        pytest.skip("Chromium not installed (playwright install chromium)")

    seen: list[tuple[str, str]] = []

    async def socks5_sniffer(reader, writer):
        try:
            hdr = await reader.readexactly(2)
            await reader.readexactly(hdr[1])
            writer.write(b"\x05\x00")
            await writer.drain()
            _ver, _cmd, _rsv, atyp = await reader.readexactly(4)
            if atyp == 3:
                n = (await reader.readexactly(1))[0]
                seen.append(("domain", (await reader.readexactly(n)).decode()))
            else:
                await reader.readexactly(4 if atyp == 1 else 16)
                seen.append(("ip-literal", "LEAK"))
            await reader.readexactly(2)
            writer.write(b"\x05\x04\x00\x01" + bytes(6))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(socks5_sniffer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    browser = BrowserService(f"socks5://127.0.0.1:{port}", nav_timeout_ms=10_000)
    try:
        for url in ["http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion/",
                    "https://example.com/"]:
            with pytest.raises(RuntimeError):
                await browser.visit(url, attempts=1, screenshot=False)
    finally:
        await browser.close()
        server.close()

    hosts = {h for kind, h in seen if kind == "domain"}
    assert ("ip-literal", "LEAK") not in seen
    assert "example.com" in hosts
    assert any(h.endswith(".onion") for h in hosts)
