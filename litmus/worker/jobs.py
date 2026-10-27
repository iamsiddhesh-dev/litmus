"""Queue jobs: drive one API-created run through the device lifecycle.

The lifecycle is blocking (adb, uiautomator2, ``subprocess``), so it runs in a
worker thread while the asyncio loop stays free for two jobs: persisting the
events the lifecycle emits, and watching the ``runs`` row for a cancel.

This module is imported lazily by ``litmus.worker.settings`` so the queue can
start even when the Android SDK is missing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from litmus.config import get_settings
from litmus.db.store import Store
from litmus.worker.events import DONE, StoreSink
from litmus.worker.lifecycle import Lifecycle, RunOptions

logger = logging.getLogger(__name__)

CANCEL_POLL_S = 2.0


class CancelWatcher:
    """Polls the run row so a long explore loop is cancellable.

    The lifecycle thread cannot await a database call at every step, so a plain
    boolean is flipped by a small polling task instead.
    """

    def __init__(self, store: Store, run_id: str, interval_s: float = CANCEL_POLL_S) -> None:
        self.store = store
        self.run_id = run_id
        self.interval_s = interval_s
        self.cancelled = False

    async def watch(self) -> None:
        while not self.cancelled:
            await asyncio.sleep(self.interval_s)
            try:
                run = await self.store.get_run(self.run_id)
            except Exception as exc:  # noqa: BLE001 - a hiccup must not cancel a run
                logger.debug("cancel poll failed: %s", exc)
                continue
            if run is None or run["status"] == "cancelled":
                self.cancelled = True


async def execute_run(
    run_id: str,
    apk: str,
    profile: str,
    steps: int,
    verify: bool = True,
) -> dict[str, Any]:
    """Run the lifecycle for ``run_id`` and return a JSON-safe summary."""
    settings = get_settings()
    store = await Store.connect(settings)
    loop = asyncio.get_running_loop()
    sink = StoreSink(store, run_id, loop)
    watcher = CancelWatcher(store, run_id)
    watch_task = asyncio.create_task(watcher.watch())

    try:
        run = await store.get_run(run_id)
        if run is not None and run["status"] == "cancelled":
            return {"run_id": run_id, "status": "cancelled", "skipped": True}

        apk_path = Path(apk)
        if not apk_path.is_file():
            raise FileNotFoundError(f"apk not found: {apk_path}")

        options = RunOptions(
            apk=apk_path,
            profile=profile,
            steps=steps,
            verify=verify,
            # The control plane owns the identity of the run and its target.
            run_id=run_id,
            target_id=run["target_id"] if run is not None else None,
        )
        lifecycle = Lifecycle(
            options,
            settings=settings,
            sink=sink,
            cancel_check=lambda: watcher.cancelled,
        )
        result = await asyncio.to_thread(lifecycle.run)
        return {
            "run_id": result.run_id,
            "status": result.status,
            "steps_taken": len(result.steps),
            "findings": len(result.findings),
            "confirmations": result.confirmations,
            "duration_s": result.duration_s,
            "error": result.error or None,
        }
    except Exception as exc:  # noqa: BLE001 - a failed job still closes the run
        logger.exception("run %s failed", run_id)
        sink.emit(DONE, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        watch_task.cancel()
        await store.close()