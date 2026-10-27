"""FastAPI application: REST + SSE + the Jinja dashboard (PLAN §4, §10).

Bind address is ``127.0.0.1`` by default: single tenant, no auth, no CORS. The
API holds no run state of its own - every answer comes from Postgres, so the
process can be restarted mid-run without losing the stream.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from litmus.api.artifacts import resolve_artifact
from litmus.api.deps import get_settings_dep
from litmus.api.routes import findings, pages, runs, targets
from litmus.config import Settings, get_settings
from litmus.db.session import apply_schema
from litmus.db.store import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.has_database:
        raise RuntimeError("DATABASE_URL is not configured; the control plane needs Postgres")
    # schema.sql is idempotent, so an existing dev database upgrades in place.
    await apply_schema(settings)
    app.state.store = await Store.connect(settings)
    logger.info("litmus api ready (db=%s)", settings.postgres_dsn().split("@")[-1])
    try:
        yield
    finally:
        await app.state.store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Litmus",
        version="0.2.0",
        description="A litmus test for mobile bugs: explore, detect, replay 3x, report survivors.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(targets.router)
    app.include_router(runs.router)
    app.include_router(findings.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> dict:
        report: dict = {"status": "ok", "db": "down", "redis": "down", "worker": "unknown"}

        store: Store | None = getattr(request.app.state, "store", None)
        if store is not None:
            try:
                await store.pool.fetchval("SELECT 1")
                report["db"] = "up"
            except Exception as exc:  # noqa: BLE001 - health reports, never raises
                report["status"] = "degraded"
                report["db_error"] = f"{type(exc).__name__}: {exc}"
        else:
            report["status"] = "degraded"

        if settings.has_redis:
            try:
                from redis.asyncio import from_url

                client = from_url(settings.redis_url)
                try:
                    await client.ping()
                    report["redis"] = "up"
                    # Arq refreshes this key while a worker is alive.
                    alive = await client.exists("arq:queue:health-check")
                    report["worker"] = "up" if alive else "unknown"
                finally:
                    await client.aclose()
            except Exception:  # noqa: BLE001 - redis is optional for the dashboard
                report["redis"] = "down"

        return report

    @app.get("/artifacts/{path:path}", tags=["artifacts"])
    async def serve_artifact(
        path: str, settings: Settings = Depends(get_settings_dep)
    ) -> FileResponse:
        """Serve evidence, guarded to the artifacts directory."""
        resolved = resolve_artifact(path, settings.artifacts_dir)
        if resolved is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(resolved)

    return app


app = create_app()