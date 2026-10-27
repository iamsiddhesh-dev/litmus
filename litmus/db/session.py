"""Async engine and session factory for Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litmus.config import Settings, get_settings
from litmus.db.models import Base

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def build_engine(settings: Settings | None = None):
    """Create the async engine for the configured database URL."""
    settings = settings or get_settings()
    if not settings.has_database:
        raise RuntimeError("DATABASE_URL is not configured")
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        # The Supabase session pooler does not like prepared statements.
        connect_args={"statement_cache_size": 0},
    )


def build_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Yield a session and close it afterwards."""
    engine = build_engine(settings)
    factory = build_sessionmaker(engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def create_all(settings: Settings | None = None) -> None:
    """Create the ORM tables (the canonical DDL lives in ``schema.sql``)."""
    engine = build_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def apply_schema(settings: Settings | None = None) -> None:
    """Apply ``schema.sql`` idempotently (``CREATE TABLE IF NOT EXISTS``)."""
    settings = settings or get_settings()
    if not settings.has_database:
        raise RuntimeError("DATABASE_URL is not configured")
    import asyncpg

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = await asyncpg.connect(settings.postgres_dsn())
    try:
        await conn.execute(sql)
    finally:
        await conn.close()
