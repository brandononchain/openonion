from __future__ import annotations

from pathlib import Path

import pytest

from openonion.config import Settings
from openonion.events import EventBus
from openonion.memory.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
                 approval_timeout_s=5, tool_timeout_s=10, tor_socks_ports=[9100, 9101])
    s.ensure_dirs()
    return s


@pytest.fixture
async def store(settings: Settings):
    st = Store(settings.database_url)
    await st.init()
    yield st
    await st.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()
