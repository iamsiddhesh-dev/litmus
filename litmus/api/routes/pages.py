"""Dashboard pages: one HTML shell, hydrated by vanilla JS over REST + SSE."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from litmus.api.deps import get_store
from litmus.db.store import Store

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    run_id: str | None = None,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    return _render(request, await store.list_runs(30), run_id or "")


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def dashboard_run(
    request: Request,
    run_id: str,
    store: Store = Depends(get_store),
) -> HTMLResponse:
    return _render(request, await store.list_runs(30), run_id)


def _render(request: Request, runs: list[dict], selected: str) -> HTMLResponse:
    """Render the shell with first-paint runs; JS takes over from the bootstrap."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "runs": runs,
            "selected_run": selected,
            "bootstrap": {"runs": jsonable_encoder(runs), "selected": selected},
        },
    )