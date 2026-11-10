"""Owns running sessions: starts runs as background tasks, routes approvals and
cancellation, and shuts everything down cleanly."""

from __future__ import annotations

import asyncio
from typing import Callable

from ..browser.tor import TorPortPool
from ..config import Settings
from ..events import EventBus
from ..memory.store import Store
from ..policy import SessionPolicy
from ..providers import build_provider
from ..providers.base import ModelProvider
from ..tools import ToolRegistry, default_registry
from .orchestrator import Orchestrator
from .services import SessionServices

ProviderFactory = Callable[[Settings], ModelProvider]


class SessionBusy(RuntimeError):
    pass


class SessionManager:
    def __init__(self, store: Store, bus: EventBus, settings: Settings,
                 provider_factory: ProviderFactory = build_provider,
                 registry: ToolRegistry | None = None) -> None:
        self.store = store
        self.bus = bus
        self.settings = settings
        self.provider_factory = provider_factory
        self.registry = registry or default_registry(settings)
        self.tor_pool = TorPortPool(settings.tor_socks_ports)
        self._runs: dict[str, tuple[Orchestrator, asyncio.Task]] = {}

    def is_running(self, session_id: str) -> bool:
        entry = self._runs.get(session_id)
        return bool(entry and not entry[1].done())

    async def start_run(self, session_id: str, goal: str) -> asyncio.Task:
        if self.is_running(session_id):
            raise SessionBusy("a run is already in progress for this session")
        session = await self.store.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        policy = SessionPolicy.model_validate(session["settings"].get("policy") or {})
        provider = self.provider_factory(self.settings)
        services = SessionServices(session_id, self.settings, policy, self.tor_pool)
        orch = Orchestrator(session_id, store=self.store, bus=self.bus, provider=provider,
                            registry=self.registry, policy=policy, settings=self.settings, services=services)

        async def runner() -> None:
            try:
                await orch.run(goal)
            finally:
                await provider.aclose()

        task = asyncio.create_task(runner(), name=f"openonion-run-{session_id}")
        self._runs[session_id] = (orch, task)
        return task

    def resolve_approval(self, session_id: str, approval_id: str, approved: bool) -> bool:
        entry = self._runs.get(session_id)
        return bool(entry and entry[0].resolve_approval(approval_id, approved))

    async def cancel(self, session_id: str) -> bool:
        entry = self._runs.get(session_id)
        if not entry or entry[1].done():
            return False
        entry[1].cancel()
        try:
            await entry[1]
        except (asyncio.CancelledError, Exception):
            pass
        return True

    async def shutdown(self) -> None:
        for sid in list(self._runs):
            await self.cancel(sid)
