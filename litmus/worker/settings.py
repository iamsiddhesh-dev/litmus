"""Arq worker entry point (PLAN §4, §16.1).

Run it with::

    arq litmus.worker.WorkerSettings

``WorkerSettings`` lives here but is re-exported from ``litmus.worker`` so the
documented arq path works. The heavy modules (device driver, lifecycle) are
imported lazily inside the coroutine: the queue must come up even when the
Android SDK or the emulator is unavailable, so the dashboard can report an
honest ``failed`` status instead of losing the job.

Windows note (PLAN §18): ``asyncio`` has no ``add_signal_handler`` on Windows.
Arq 0.28 catches that ``NotImplementedError`` and logs at debug level, so the
worker runs normally; Ctrl+C is handled by the keyboard interrupt path instead
of the graceful-drain signal. Verified on Windows in M2 - see
``docs/progress.md`` - so no Postgres-jobs fallback is needed.
"""

from __future__ import annotations

import logging

from arq.connections import RedisSettings

from litmus.config import get_settings

logger = logging.getLogger(__name__)


async def run_lifecycle_job(
    ctx: dict,
    run_id: str,
    apk: str,
    profile: str,
    steps: int,
    verify: bool = True,
) -> dict:
    """Queue job: drive one run end to end and persist its events."""
    from litmus.worker.jobs import execute_run

    return await execute_run(
        run_id=run_id,
        apk=apk,
        profile=profile,
        steps=steps,
        verify=verify,
    )


async def startup(ctx: dict) -> None:
    logging.basicConfig(level=get_settings().log_level.upper())
    logger.info("litmus worker starting")


async def shutdown(ctx: dict) -> None:
    logger.info("litmus worker stopped")


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    if not settings.has_redis:
        return RedisSettings()  # 127.0.0.1:6379, database 0
    return RedisSettings.from_dsn(settings.redis_url)


class WorkerSettings:
    """Arq settings: one job at a time, because there is one emulator."""

    functions = [run_lifecycle_job]
    redis_settings = _redis_settings()
    max_jobs = 1
    job_timeout = 30 * 60
    keep_result = 60 * 60
    on_startup = startup
    on_shutdown = shutdown