from __future__ import annotations

import asyncio
import random
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
