"""FastAPI dependencies: one store per app, resolved from app state."""

from __future__ import annotations

from fastapi import HTTPException, Request

from litmus.config import Settings, get_settings
from litmus.db.store import Store


def get_store(request: Request) -> Store:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="database is not available")
    return store


def get_settings_dep() -> Settings:
    return get_settings()