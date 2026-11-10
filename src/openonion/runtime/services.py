"""Per-session resources (browsers, Tor slot), created lazily and torn down together."""

from __future__ import annotations

from ..browser.service import BrowserService
from ..browser.tor import TorPortPool
from ..config import Settings
from ..policy import SessionPolicy


class SessionServices:
    def __init__(self, session_id: str, settings: Settings, policy: SessionPolicy, tor_pool: TorPortPool) -> None:
        self.session_id = session_id
        self.settings = settings
        self.policy = policy
        self.tor_pool = tor_pool
        self.tor_port: int | None = None
        self._clearnet: BrowserService | None = None
        self._tor: BrowserService | None = None

    async def clearnet_browser(self) -> BrowserService:
        if self._clearnet is None:
            self._clearnet = BrowserService(None, headless=self.settings.browser_headless,
                                            javascript=self.policy.javascript, nav_timeout_ms=45_000)
        return self._clearnet

    async def tor_browser(self) -> BrowserService:
        if self._tor is None:
            self.tor_port = await self.tor_pool.acquire(self.session_id)
            proxy = f"socks5://{self.settings.tor_socks_host}:{self.tor_port}"
            self._tor = BrowserService(proxy, headless=self.settings.browser_headless,
                                       javascript=self.policy.javascript, nav_timeout_ms=90_000)
        return self._tor

    async def reset_tor_browser(self) -> None:
        if self._tor is not None:
            await self._tor.close()
            self._tor = None

    async def close(self) -> None:
        for b in (self._clearnet, self._tor):
            if b is not None:
                try:
                    await b.close()
                except Exception:
                    pass
        self._clearnet = self._tor = None
        await self.tor_pool.release(self.session_id)
        self.tor_port = None
