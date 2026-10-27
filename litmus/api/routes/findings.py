"""Finding detail with evidence (PLAN §10)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from litmus.api.artifacts import artifact_url
from litmus.api.deps import get_settings_dep, get_store
from litmus.api.report import repro_rate
from litmus.config import Settings
from litmus.db.store import Store

router = APIRouter(prefix="/v1/findings", tags=["findings"])


@router.get("/{finding_id:path}")
async def get_finding(
    finding_id: str,
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    finding = await store.finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding not found: {finding_id}")

    verdicts = await store.finding_verdicts(finding_id)
    artifacts = await store.finding_artifacts(finding_id)
    rate, confirmations, valid = repro_rate(verdicts)
    return {
        **finding,
        "repro_rate": rate,
        "confirmations": confirmations,
        "valid_attempts": valid,
        "verdicts": verdicts,
        "artifacts": [
            {
                "id": artifact["id"],
                "kind": artifact["kind"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "path": artifact["path"],
                "url": artifact_url(artifact["path"], Path(settings.artifacts_dir)),
            }
            for artifact in artifacts
        ],
    }