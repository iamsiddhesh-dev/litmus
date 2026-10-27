"""Queue dispatch for ``POST /v1/runs``.

Arq on Redis is the normal path. If Redis is down the API still starts the run
in-process rather than accepting a job nobody will consume; that fallback runs
one lifecycle in a background thread next to the API and is documented in the
README as a single-process dev mode.
"""

from __future__ import annotations

import asyncio
import logging

from litmus.config import Settings, get_settings

logger = logging.getLogger(__name__)

QUEUE = "arq:queue"


async def enqueue_run(
    run_id: str,
    apk: str,
    profile: str,
    steps: int,
    verify: bool,
    settings: Settings | None = None,
) -> str:
    """Start a run. Returns ``"arq"`` or ``"inline"`` for the audit trail."""
    settings = settings or get_settings()
    if settings.has_redis:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            try:
                # ``_job_id=run_id`` makes a retried POST idempotent.
                await pool.enqueue_job(
                    "run_lifecycle_job",
                    run_id,
                    apk,
                    profile,
                    steps,
                    verify,
                    _job_id=run_id,
                )
            finally:
                await pool.close()
            logger.info("run %s queued on arq", run_id)
            return "arq"
        except Exception as exc:  # noqa: BLE001 - fall back, do not lose the run
            logger.warning("redis unavailable (%s); running inline", exc)

    from litmus.worker.jobs import execute_run

    task = asyncio.create_task(execute_run(run_id, apk, profile, steps, verify))

    def _log_failure(finished: asyncio.Task) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            logger.error("inline run %s failed: %s", run_id, error)

    task.add_done_callback(_log_failure)
    logger.info("run %s started inline (no redis)", run_id)
    return "inline"