"""SSE framing and replay (PLAN §4: tail the events table by seq)."""

from __future__ import annotations

from fakes import FakeStore

from litmus.api.sse import event_stream, format_sse


def test_format_sse_is_a_valid_frame() -> None:
    frame = format_sse(7, "step", {"idx": 3, "action": "tap"})
    assert frame.startswith("id: 7\nevent: step\ndata: ")
    assert frame.endswith("\n\n")
    assert '"action":"tap"' in frame


def test_format_sse_handles_empty_payload() -> None:
    assert "data: {}\n\n" in format_sse(1, "done", None)


async def _collect(store: FakeStore, run_id: str, cursor: int) -> list[str]:
    return [chunk async for chunk in event_stream(store, run_id, cursor, poll_s=0.0)]


async def test_stream_replays_every_persisted_event_then_stops() -> None:
    store = FakeStore(runs={"run_1": {"id": "run_1", "status": "done"}})
    await store.append_event("run_1", "run.status", {"status": "queued"})
    await store.append_event("run_1", "step", {"idx": 1, "action": "tap"})
    await store.append_event("run_1", "done", {"status": "done"})

    frames = await _collect(store, "run_1", 0)
    assert len(frames) == 3
    assert frames[0].startswith("id: 1\nevent: run.status")
    assert frames[1].startswith("id: 2\nevent: step")
    assert frames[2].startswith("id: 3\nevent: done")


async def test_from_seq_replays_only_missed_events() -> None:
    store = FakeStore(runs={"run_1": {"id": "run_1", "status": "done"}})
    await store.append_event("run_1", "run.status", {"status": "queued"})
    await store.append_event("run_1", "step", {"idx": 1})
    await store.append_event("run_1", "done", {"status": "done"})

    frames = await _collect(store, "run_1", 2)
    assert len(frames) == 1
    assert frames[0].startswith("id: 3\nevent: done")


async def test_stream_ignores_other_runs() -> None:
    store = FakeStore(
        runs={
            "run_1": {"id": "run_1", "status": "done"},
            "run_2": {"id": "run_2", "status": "done"},
        }
    )
    await store.append_event("run_2", "step", {"idx": 1})
    await store.append_event("run_1", "done", {"status": "done"})

    frames = await _collect(store, "run_1", 0)
    assert len(frames) == 1
    assert "done" in frames[0]
async def test_completed_status_is_terminal_too() -> None:
    # Runs persisted before the state machine settled on "done" still close.
    store = FakeStore(runs={"run_1": {"id": "run_1", "status": "completed"}})
    await store.append_event("run_1", "done", {"status": "completed"})

    frames = await _collect(store, "run_1", 0)
    assert len(frames) == 1
    assert "done" in frames[0]
