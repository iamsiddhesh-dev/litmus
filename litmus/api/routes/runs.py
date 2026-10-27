"""Run control plane: create, stream, report, cancel (PLAN §10)."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from litmus.api.artifacts import artifact_url
from litmus.api.deps import get_settings_dep, get_store
from litmus.api.queue import enqueue_run
from litmus.api.report import build_report
from litmus.api.sse import SSE_MEDIA_TYPE, TERMINAL, event_stream
from litmus.config import Settings
from litmus.db.store import Store
from litmus.profiles import profile_for_package
from litmus.worker.events import RUN_STATUS

router = APIRouter(prefix="/v1/runs", tags=["runs"])

#: Advice for intermediate proxies: this stream must not be buffered.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class RunCreate(BaseModel):
    """Body of ``POST /v1/runs``."""

    target_id: str
    profile: str = ""
    steps: int = Field(default=25, ge=1, le=200)
    verify: bool = True


def new_run_id() -> str:
    return "run_" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@router.post("", status_code=202)
async def create_run(
    body: RunCreate,
    store: Store = Depends(get_store),
) -> dict:
    target = await store.get_target(body.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"target not found: {body.target_id}")
    apk = target.get("storage_path") or ""
    if not apk or not Path(apk).is_file():
        raise HTTPException(status_code=400, detail="target has no readable APK on disk")

    profile = (
        body.profile
        or target.get("profile")
        or profile_for_package(target.get("package"))
        or "generic"
    )
    run_id = new_run_id()
    run = await store.create_run(
        {
            "id": run_id,
            "target_id": body.target_id,
            "status": "queued",
            "step_budget": body.steps,
            "verify_mode": "pm_clear",
        }
    )
    # Persisted before dispatch: a dashboard that opens the moment the response
    # arrives already has the queued frame to replay.
    await store.append_event(
        run_id,
        RUN_STATUS,
        {
            "status": "queued",
            "run_id": run_id,
            "package": target.get("package") or "",
            "profile": profile,
            "step_budget": body.steps,
        },
    )
    dispatch = await enqueue_run(
        run_id=run_id,
        apk=apk,
        profile=profile,
        steps=body.steps,
        verify=body.verify,
    )
    return {"run": run, "dispatch": dispatch, "target": target}


@router.get("")
async def list_runs(
    store: Store = Depends(get_store),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return {"runs": await store.list_runs(limit)}


@router.get("/{run_id}")
async def get_run(run_id: str, store: Store = Depends(get_store)) -> dict:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    target = await store.get_target(run["target_id"])
    findings = await store.run_findings(run_id)
    counts = Counter(finding.get("verdict") or "not_reproduced" for finding in findings)
    return {"run": run, "target": target, "counts": dict(counts)}


@router.get("/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    from_seq: int | None = Query(
        None,
        ge=0,
        description="Replay events with seq > from_seq. Omit to resume from Last-Event-ID.",
    ),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    store: Store = Depends(get_store),
) -> StreamingResponse:
    """SSE stream over the events table - full replay when ``from_seq=0``."""
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")

    cursor = from_seq
    if cursor is None:
        try:
            cursor = int(last_event_id) if last_event_id else 0
        except ValueError:
            cursor = 0

    return StreamingResponse(
        event_stream(store, run_id, cursor),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


def _attach_artifacts(
    report: dict[str, Any], artifacts: list[dict[str, Any]], root: Path
) -> None:
    """Add served evidence URLs to each lane entry."""
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        url = artifact_url(artifact["path"], root)
        if not url:
            continue
        by_finding.setdefault(artifact["finding_id"], []).append(
            {
                "kind": artifact["kind"],
                "bytes": artifact["bytes"],
                "url": url,
            }
        )
    for lane in ("confirmed", "flaky", "filtered"):
        for entry in report[lane]:
            files = by_finding.get(entry["id"], [])
            entry["artifacts"] = files
            entry["screenshot"] = next(
                (item["url"] for item in files if item["kind"] == "screen_at_bug.png"),
                None,
            )
            entry["repro"] = next(
                (item["url"] for item in files if item["kind"] == "repro.py"), None
            )


@router.get("/{run_id}/report")
async def run_report(
    run_id: str,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    target = await store.get_target(run["target_id"])
    findings = await store.run_findings(run_id)
    verdicts = await store.run_verdicts(run_id)
    report = build_report(run, target, findings, verdicts)
    _attach_artifacts(report, await store.run_artifacts(run_id), settings.artifacts_dir)
    return report


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, store: Store = Depends(get_store)) -> dict:
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run["status"] in TERMINAL:
        raise HTTPException(status_code=409, detail=f"run already {run['status']}")
    await store.append_event(run_id, RUN_STATUS, {"status": "cancelled", "run_id": run_id})
    await store.set_run_status(run_id, "cancelled")
    return {"run_id": run_id, "status": "cancelled"}