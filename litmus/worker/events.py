"""Structured run events (PLAN §4, §10).

The SSE endpoint tails the ``events`` table, so an event only exists once it has
been written. The lifecycle runs in a worker *thread* (adb and uiautomator2 are
blocking), while the database connection belongs to the asyncio loop, so
``StoreSink`` hands each event back to the loop and waits for the insert. That
gives two guarantees the dashboard depends on:

* an event is on disk before the lifecycle moves on (persist before publish);
* a late or refreshed client replays the whole history from ``seq > cursor``.

Event type names are frozen here because the SSE stream, the dashboard and the
tests all key off them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from litmus.db.store import Store

logger = logging.getLogger(__name__)

RUN_STATUS = "run.status"
STEP = "step"
FINDING_CANDIDATE = "finding.candidate"
VERDICT = "verdict"
WARNING = "warning"
REPORT_READY = "report.ready"
DONE = "done"

#: Terminal statuses the store records on the ``runs`` row.
TERMINAL = {"done", "failed", "cancelled"}


class EventSink(Protocol):
    """Anything the lifecycle can report through."""

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        ...


class NullSink:
    """Sink for the CLI path, where there is no database and no dashboard."""

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        return None


class StoreSink:
    """Write events to Postgres from the worker thread.

    ``emit`` blocks until the row is committed. The wait is ~1 ms on a local
    database and it is what makes "persist before publish" true rather than
    aspirational. A database failure is logged and swallowed: losing the
    dashboard feed must never abort a run.
    """

    def __init__(self, store: Store, run_id: str, loop: asyncio.AbstractEventLoop,
                 timeout_s: float = 20.0) -> None:
        self.store = store
        self.run_id = run_id
        self.loop = loop
        self.timeout_s = timeout_s

    def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        coro = self._write(type_, payload)
        if running is self.loop:
            # Same thread as the loop: blocking here would deadlock it.
            self.loop.create_task(coro)
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - the feed is best effort
            logger.warning("event %s not persisted: %s: %s", type_, type(exc).__name__, exc)

    async def _write(self, type_: str, payload: dict[str, Any]) -> None:
        await self.store.append_event(self.run_id, type_, payload)
        if type_ == RUN_STATUS:
            status = str(payload.get("status") or "")
            if status:
                await self.store.set_run_status(self.run_id, status)
        elif type_ == DONE:
            status = str(payload.get("status") or "done")
            updates = {
                key: payload[key]
                for key in ("steps_taken", "error")
                if payload.get(key) is not None
            }
            await self.store.set_run_status(self.run_id, status, **updates)