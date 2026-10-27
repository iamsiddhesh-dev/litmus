"""Target ingestion: ``POST /v1/targets`` (PLAN §10)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from litmus.api.deps import get_settings_dep, get_store
from litmus.config import Settings
from litmus.db.store import Store
from litmus.ingest.apk import MAX_APK_BYTES, inspect_apk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/targets", tags=["targets"])

CHUNK = 1 << 20


def _safe_name(filename: str | None) -> str:
    name = Path(filename or "").name.strip()
    if not name:
        return "app.apk"
    if not name.lower().endswith(".apk"):
        name = f"{name}.apk"
    return name


@router.post("", status_code=201)
async def upload_target(
    file: UploadFile = File(...),
    profile: str = Form(""),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """Accept an APK, inspect it, and register the target.

    The file is streamed to disk with a size cap and a sha256, so an identical
    upload resolves to the same target id instead of filling the disk twice.
    """
    upload_dir = settings.artifacts_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    partial = upload_dir / f".upload-{uuid4().hex}.part"

    digest = hashlib.sha256()
    size = 0
    try:
        with partial.open("wb") as handle:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > MAX_APK_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"APK larger than {MAX_APK_BYTES // (1024 * 1024)} MB",
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="empty upload")
        sha256 = digest.hexdigest()

        info = await asyncio.to_thread(inspect_apk, partial, settings)
        target_id = "tgt_" + hashlib.sha1(
            f"{info.package}|{info.version}|{sha256}".encode()
        ).hexdigest()[:16]
        destination = upload_dir / target_id / _safe_name(file.filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    target = await store.create_target(
        {
            "id": target_id,
            "kind": "apk",
            "package": info.package or None,
            "version": info.version or None,
            "min_sdk": info.min_sdk,
            "abis": info.abis or None,
            "source": "upload",
            "storage_path": str(destination),
            "profile": profile or None,
        }
    )
    logger.info("target %s stored (%s bytes, %s)", target_id, size, info.package or "unknown")
    return {**target, "bytes": size, "sha256": sha256}


@router.get("")
async def list_targets(
    store: Store = Depends(get_store), limit: int = 50
) -> dict:
    return {"targets": await store.list_targets(limit)}