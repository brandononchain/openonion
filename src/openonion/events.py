"""Events are the single source of truth.

Every thought, plan change, tool call, approval and result is an append-only
event with a per-session sequence number. The database is the durable log; the
EventBus fans live events out to any number of subscribers (UI tabs, CLI,
webhooks). A subscriber that falls behind or reconnects simply replays from the
store by sequence number, so nothing is ever lost and nothing is consumed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

# Event types emitted by the runtime (kept in one place for UI/clients).
RUN_STARTED = "run_started"
ASSISTANT_MESSAGE = "assistant_message"
PLAN_UPDATED = "plan_updated"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_RESOLVED = "approval_resolved"
ARTIFACT_CREATED = "artifact_created"
POLICY_DENIED = "policy_denied"
TOOLS_SELECTED = "tools_selected"
# Live-only: streamed model text. Published on the bus with seq=0 and never stored;
# the full text always follows in assistant_message.
TEXT_DELTA = "text_delta"
USAGE = "usage"
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"
RUN_CANCELLED = "run_cancelled"
RUN_STOPPED = "run_stopped"

# Internal transcript record used to rebuild model context for follow-up runs.
TRANSCRIPT = "transcript"

TERMINAL_EVENTS = {RUN_FINISHED, RUN_FAILED, RUN_CANCELLED, RUN_STOPPED}


@dataclass
class Event:
    session_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "payload": self.payload,
            "ts": self.ts,
        }


class EventBus:
    """In-process fan-out of live events. Durability lives in the store."""

    def __init__(self, queue_size: int = 1000) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = {}
        self._queue_size = queue_size

    def subscribe(self, session_id: str) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subs.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[Event]) -> None:
        subs = self._subs.get(session_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(session_id, None)

    def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.session_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop. It will detect the seq gap and backfill
                # from the store, so dropping here never loses data.
                pass
