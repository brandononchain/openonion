from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ProviderError

RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}


async def post_json(client: httpx.AsyncClient, url: str, body: dict[str, Any],
                    headers: dict[str, str], attempts: int = 4) -> dict[str, Any]:
    """POST with exponential backoff on transient failures."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.post(url, json=body, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
        else:
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code not in RETRY_STATUSES:
                raise ProviderError(f"{resp.status_code} from {url}: {resp.text[:500]}")
            last = ProviderError(f"{resp.status_code} from {url}: {resp.text[:200]}")
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                await asyncio.sleep(min(int(retry_after), 30))
                continue
        await asyncio.sleep(min(2 ** attempt + random.random(), 20))
    raise ProviderError(f"request failed after {attempts} attempts: {last}")


class StreamUnsupported(ProviderError):
    """The server rejected a streaming request; callers fall back to a plain one."""


async def stream_sse(client: httpx.AsyncClient, url: str, body: dict[str, Any],
                     headers: dict[str, str], attempts: int = 4) -> AsyncIterator[dict[str, Any]]:
    """POST and yield each JSON `data:` payload of a server-sent event stream.

    Transient failures are retried only before the first event arrives, so a retry
    never replays text the caller has already shown."""
    last: Exception | None = None
    for attempt in range(attempts):
        started = False
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode(errors="replace")
                    if resp.status_code not in RETRY_STATUSES:
                        if resp.status_code in (400, 404, 415, 422) and "stream" in text.lower():
                            raise StreamUnsupported(text[:300])
                        raise ProviderError(f"{resp.status_code} from {url}: {text[:500]}")
                    last = ProviderError(f"{resp.status_code} from {url}: {text[:200]}")
                else:
                    if "text/event-stream" not in resp.headers.get("content-type", ""):
                        # Server ignored stream=true and sent one JSON body.
                        yield {"__whole__": json.loads(await resp.aread())}
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        started = True
                        yield obj
                    return
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if started:
                raise ProviderError(f"stream from {url} broke off: {exc}") from exc
            last = exc
        await asyncio.sleep(min(2 ** attempt + random.random(), 20))
    raise ProviderError(f"request failed after {attempts} attempts: {last}")
