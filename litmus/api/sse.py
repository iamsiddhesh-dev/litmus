"""Server-sent events: tail the ``events`` table (PLAN §4, §10).

The API never publishes from memory. A client that connects late - or refreshes
mid-run - asks for ``from_seq`` and gets every event the worker committed, in
``seq`` order. That is the whole replay story, and it is why the worker writes
before it acts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from litmus.db.store import Store

SSE_MEDIA_TYPE = "text/event-stream"

#: PLAN §4: poll the table, ~500 ms.
POLL_S = 0.5
#: Comment frame so proxies and browsers do not time out an idle run.
KEEPALIVE_S = 15.0
#: ``completed`` is kept for runs persisted before the state machine settled on
#: ``done``; without it those streams would never close.
TERMINAL = {"done", "completed", "failed", "cancelled"}


def format_sse(seq: int, type_: str, data: dict | None = None) -> str:
    """Render one ``id`` / ``event`` / ``data`` frame."""
    payload = json.dumps(data or {}, separators=(",", ":"), default=str)
    return f"id: {seq}\nevent: {type_}\ndata: {payload}\n\n"


async def event_stream(
    store: Store,
    run_id: str,
    cursor: int = 0,
    poll_s: float = POLL_S,
) -> AsyncIterator[str]:
    """Yield persisted events after ``cursor``; stop once the run is terminal."""
    last_keepalive = time.monotonic()
    while True:
        rows = await store.events_since(run_id, cursor, limit=200)
        if rows:
            for row in rows:
                cursor = int(row["seq"])
                yield format_sse(cursor, row["type"], row["payload_json"])
            last_keepalive = time.monotonic()
            continue

        run = await store.get_run(run_id)
        if run is None:
            return
        if run["status"] in TERMINAL:
            # The worker writes the terminal event before it flips the status,
            # but a cancel can land in either order. Drain once more, then go.
            tail = await store.events_since(run_id, cursor, limit=200)
            if not tail:
                return
            continue

        now = time.monotonic()
        if now - last_keepalive >= KEEPALIVE_S:
            last_keepalive = now
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll_s)