"""Tor plumbing: per-session SOCKS listener allocation and the control protocol.

Isolation model: the Tor sidecar opens N SOCKS listeners. Tor never puts streams
from different listeners on the same circuit, so giving each live session its own
listener means two sessions can never be correlated by sharing an exit.
"""

from __future__ import annotations

import asyncio
import json


class TorPortPool:
    """Sticky allocation of SOCKS ports to sessions."""

    def __init__(self, ports: list[int]) -> None:
        self._free = list(ports)
        self._owned: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> int:
        async with self._lock:
            if session_id in self._owned:
                return self._owned[session_id]
            if not self._free:
                raise RuntimeError(
                    "all Tor isolation slots are in use; finish another session or widen TOR_SOCKS_PORTS"
                )
            port = self._free.pop(0)
            self._owned[session_id] = port
            return port

    async def release(self, session_id: str) -> None:
        async with self._lock:
            port = self._owned.pop(session_id, None)
            if port is not None:
                self._free.append(port)

    def port_for(self, session_id: str) -> int | None:
        return self._owned.get(session_id)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def tor_control(host: str, port: int, password: str, commands: list[str],
                      timeout: float = 10.0) -> list[str]:
    """Run control-port commands after authenticating; returns the reply lines.
    Raises RuntimeError on any non-250 reply."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    replies: list[str] = []
    try:
        async def send(cmd: str) -> list[str]:
            writer.write((cmd + "\r\n").encode())
            await writer.drain()
            lines: list[str] = []
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout)
                if not raw:
                    raise RuntimeError("Tor control connection closed")
                line = raw.decode(errors="replace").rstrip("\r\n")
                lines.append(line)
                # Final reply line has a space after the 3-digit code ("250 OK").
                if len(line) >= 4 and line[3] == " ":
                    break
            if not lines[-1].startswith("250"):
                raise RuntimeError(f"Tor control error: {lines[-1]}")
            return lines

        await send(f"AUTHENTICATE {_quote(password)}")
        for cmd in commands:
            replies.extend(await send(cmd))
        writer.write(b"QUIT\r\n")
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return replies


async def tor_new_identity(host: str, port: int, password: str) -> None:
    """SIGNAL NEWNYM: new circuits for all future streams (rate-limited by Tor to ~1/10s)."""
    await tor_control(host, port, password, ["SIGNAL NEWNYM"])


async def tor_bootstrap_progress(host: str, port: int, password: str) -> int:
    lines = await tor_control(host, port, password, ["GETINFO status/bootstrap-phase"])
    for line in lines:
        if "PROGRESS=" in line:
            try:
                return int(line.split("PROGRESS=", 1)[1].split()[0])
            except (ValueError, IndexError):
                pass
    return 0


async def tor_check(socks_host: str, socks_port: int, timeout: float = 60.0) -> dict:
    """Ask the Tor Project's check service whether traffic on this listener exits via Tor.
    Requires httpx[socks]; returns {"IsTor": bool, "IP": "..."}."""
    import httpx

    proxy = f"socks5://{socks_host}:{socks_port}"
    async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
        resp = await client.get("https://check.torproject.org/api/ip")
        resp.raise_for_status()
        return json.loads(resp.text)
