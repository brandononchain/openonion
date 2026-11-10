"""Durable store: sessions, an append-only event log, and artifact metadata.

Works with SQLite (default, zero-config) and Postgres (asyncpg) through
SQLAlchemy's async engine.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..events import Event


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON so anything we persist is guaranteed portable."""
    return json.loads(json.dumps(value, default=str))


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(32), default="idle")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_events_session_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1024))
    mime: Mapped[str] = mapped_column(String(128))
    # `metadata` is reserved on SQLAlchemy declarative models, so the Python
    # attribute is `meta` while the column keeps the name `metadata`.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def _session_dict(row: SessionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "status": row.status,
        "settings": row.settings or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _artifact_dict(row: ArtifactRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "kind": row.kind,
        "path": row.path,
        "mime": row.mime,
        "metadata": row.meta or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class Store:
    """Async persistence. Sequence numbers are allocated in-process, so run a
    single API process per database (scale out with a queue-backed runner)."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, future=True)
        self._sm = async_sessionmaker(self.engine, expire_on_commit=False)
        self._seq_locks: dict[str, asyncio.Lock] = {}
        self._next_seq: dict[str, int] = {}

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ── sessions ─────────────────────────────────────────────────────────────
    async def create_session(self, title: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        row = SessionRow(id=str(uuid.uuid4()), title=title[:300], status="idle", settings=_jsonable(settings or {}))
        async with self._sm() as db:
            db.add(row)
            await db.commit()
        return _session_dict(row)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self._sm() as db:
            row = await db.get(SessionRow, session_id)
            return _session_dict(row) if row else None

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._sm() as db:
            rows = (await db.execute(select(SessionRow).order_by(SessionRow.updated_at.desc()).limit(limit))).scalars()
            return [_session_dict(r) for r in rows]

    async def set_status(self, session_id: str, status: str) -> None:
        async with self._sm() as db:
            row = await db.get(SessionRow, session_id)
            if row:
                row.status = status
                row.updated_at = _now()
                await db.commit()

    async def update_settings(self, session_id: str, settings: dict[str, Any]) -> None:
        async with self._sm() as db:
            row = await db.get(SessionRow, session_id)
            if row:
                row.settings = _jsonable(settings)
                row.updated_at = _now()
                await db.commit()

    # ── events ───────────────────────────────────────────────────────────────
    async def append_event(self, session_id: str, type_: str, payload: dict[str, Any]) -> Event:
        lock = self._seq_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            async with self._sm() as db:
                if session_id not in self._next_seq:
                    current = await db.scalar(
                        select(func.max(EventRow.seq)).where(EventRow.session_id == session_id)
                    )
                    self._next_seq[session_id] = (current or 0) + 1
                seq = self._next_seq[session_id]
                clean = _jsonable(payload)
                row = EventRow(session_id=session_id, seq=seq, type=type_, payload=clean)
                db.add(row)
                await db.commit()
                self._next_seq[session_id] = seq + 1
                return Event(session_id=session_id, seq=seq, type=type_, payload=clean,
                             ts=row.created_at.timestamp())

    async def events(self, session_id: str, after: int = 0, limit: int = 5000,
                     types: set[str] | None = None) -> list[Event]:
        stmt = (select(EventRow)
                .where(EventRow.session_id == session_id, EventRow.seq > after)
                .order_by(EventRow.seq)
                .limit(limit))
        if types:
            stmt = stmt.where(EventRow.type.in_(types))
        async with self._sm() as db:
            rows = (await db.execute(stmt)).scalars()
            return [Event(session_id=r.session_id, seq=r.seq, type=r.type, payload=r.payload,
                          ts=r.created_at.timestamp()) for r in rows]

    # ── artifacts ────────────────────────────────────────────────────────────
    async def add_artifact(self, session_id: str, kind: str, path: str, mime: str,
                           meta: dict[str, Any] | None = None) -> dict[str, Any]:
        row = ArtifactRow(id=str(uuid.uuid4()), session_id=session_id, kind=kind, path=path,
                          mime=mime, meta=_jsonable(meta or {}))
        async with self._sm() as db:
            db.add(row)
            await db.commit()
        return _artifact_dict(row)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self._sm() as db:
            row = await db.get(ArtifactRow, artifact_id)
            return _artifact_dict(row) if row else None

    async def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        async with self._sm() as db:
            rows = (await db.execute(
                select(ArtifactRow).where(ArtifactRow.session_id == session_id).order_by(ArtifactRow.created_at)
            )).scalars()
            return [_artifact_dict(r) for r in rows]
