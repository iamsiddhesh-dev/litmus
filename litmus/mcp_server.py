"""FastMCP server: ``run_test`` for an agent or an IDE (PLAN §15).

The contract the caller sees is tiny and non-blocking::

    run_test(apk_path, profile="bazaar", steps=25, verify=True)
    -> {"run_id": ..., "status": ..., "report_url": ..., "mode": "api"|"local"}

A full run takes minutes, so the tool **never** waits for it: it hands the run
to the control plane (``POST /v1/targets`` then ``POST /v1/runs``) and returns
the run id plus the dashboard URL immediately. When no API is listening - the
common case for a laptop demo - the same run is started in a daemon thread that
writes the usual evidence bundle, and the caller polls ``get_run``.

``fastmcp`` is an optional extra (``pip install -e .[mcp]``). This module keeps
importing without it: :data:`TOOLS` is the source of truth, and
:func:`build_server` wires it into FastMCP only when the extra is installed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from litmus.config import Settings, get_settings

logger = logging.getLogger(__name__)

APK_MIME = "application/vnd.android.package-archive"
PROBE_TIMEOUT_S = 0.6
CALL_TIMEOUT_S = 60.0


class DispatchError(RuntimeError):
    """The control plane could not accept the run."""


@dataclass(slots=True)
class Dispatch:
    """Where a run was handed off, and how to watch it."""

    run_id: str
    status: str
    mode: str
    report_url: str = ""
    target_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "mode": self.mode,
            "report_url": self.report_url,
            "target_id": self.target_id,
            "detail": self.detail,
        }


def new_run_id() -> str:
    return "run_" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def api_base(settings: Settings, override: str | None = None) -> str:
    if override:
        return override.rstrip("/")
    return f"http://{settings.api_host}:{settings.api_port}".rstrip("/")


def report_url(settings: Settings, run_id: str, base: str | None = None) -> str:
    return f"{api_base(settings, base)}/runs/{run_id}"


def _headers(token: str) -> dict[str, str]:
    return {"X-Litmus-Token": token} if token else {}


def api_up(base: str, settings: Settings) -> bool:
    """Cheap liveness probe; a missing API is a normal fallback, not an error."""
    import httpx

    try:
        response = httpx.get(f"{base}/health", timeout=PROBE_TIMEOUT_S)
        return response.status_code < 500
    except Exception:  # noqa: BLE001 - any failure means "use the local path"
        return False


def submit_to_api(
    apk_path: Path,
    profile: str,
    steps: int,
    verify: bool,
    base: str,
    token: str = "",
    timeout_s: float = CALL_TIMEOUT_S,
) -> Dispatch:
    """Upload the APK as a target, then create the run. Returns immediately."""
    import httpx

    headers = _headers(token)
    try:
        with apk_path.open("rb") as handle:
            upload = httpx.post(
                f"{base}/v1/targets",
                files={"file": (apk_path.name, handle, APK_MIME)},
                data={"profile": profile},
                headers=headers,
                timeout=timeout_s,
            )
        if upload.status_code >= 400:
            raise DispatchError(f"target upload failed: {upload.status_code} {upload.text[:200]}")
        target_id = str((upload.json() or {}).get("id") or "")
        if not target_id:
            raise DispatchError("target upload returned no id")

        created = httpx.post(
            f"{base}/v1/runs",
            json={
                "target_id": target_id,
                "profile": profile,
                "steps": steps,
                "verify": verify,
            },
            headers=headers,
            timeout=timeout_s,
        )
        if created.status_code >= 400:
            raise DispatchError(f"run creation failed: {created.status_code} {created.text[:200]}")
        body = created.json() or {}
        run = body.get("run") or {}
        run_id = str(run.get("id") or "")
        if not run_id:
            raise DispatchError("run creation returned no id")
    except DispatchError:
        raise
    except Exception as exc:  # noqa: BLE001 - one error type for the caller
        raise DispatchError(f"control plane unreachable: {exc}") from exc

    return Dispatch(
        run_id=run_id,
        status=str(run.get("status") or "queued"),
        mode="api",
        report_url=report_url(get_settings(), run_id, base),
        target_id=target_id,
        detail=str(body.get("dispatch") or "api"),
    )


def launch_local(
    apk_path: Path,
    profile: str,
    steps: int,
    verify: bool,
    settings: Settings | None = None,
    run_id: str | None = None,
) -> Dispatch:
    """Start the run in a daemon thread next to this process (no API needed)."""
    from litmus.worker.lifecycle import Lifecycle, RunOptions

    settings = settings or get_settings()
    run_id = run_id or new_run_id()
    artifacts = Path(settings.artifacts_dir) / run_id

    def worker() -> None:
        try:
            Lifecycle(
                RunOptions(
                    apk=apk_path,
                    profile=profile,
                    steps=steps,
                    verify=verify,
                    artifacts_dir=Path(settings.artifacts_dir),
                    run_id=run_id,
                )
            ).run()
        except Exception as exc:  # noqa: BLE001 - a background run must not crash the host
            logger.error("local run %s failed: %s", run_id, exc)

    threading.Thread(target=worker, name=f"litmus-run-{run_id}", daemon=True).start()
    return Dispatch(
        run_id=run_id,
        status="queued",
        mode="local",
        report_url=report_url(settings, run_id),
        detail=f"evidence: {artifacts}",
    )


# -- tools -----------------------------------------------------------------
def run_test(
    apk_path: str,
    profile: str = "bazaar",
    steps: int = 25,
    verify: bool = True,
    api_url: str | None = None,
    force_local: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Start a Litmus run and return its id without waiting for the verdict."""
    apk = Path(apk_path).expanduser()
    if not apk.is_file():
        raise FileNotFoundError(f"apk not found: {apk}")
    if steps < 1:
        raise ValueError("steps must be at least 1")
    settings = settings or get_settings()
    base = api_base(settings, api_url)

    if not force_local and api_up(base, settings):
        try:
            dispatch = submit_to_api(
                apk, profile, steps, verify, base, token=settings.api_token
            )
            return {**dispatch.to_dict(), "apk": str(apk)}
        except DispatchError as exc:
            logger.warning("api dispatch failed (%s); running locally", exc)

    dispatch = launch_local(apk, profile, steps, verify, settings=settings)
    return {**dispatch.to_dict(), "apk": str(apk)}


def get_run(run_id: str, api_url: str | None = None, settings: Settings | None = None) -> dict:
    """Status of a run: the API when it is up, else the saved ``run.json``."""
    settings = settings or get_settings()
    base = api_base(settings, api_url)
    if api_up(base, settings):
        import httpx

        try:
            response = httpx.get(
                f"{base}/v1/runs/{run_id}",
                headers=_headers(settings.api_token),
                timeout=CALL_TIMEOUT_S,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001 - fall through to the artifacts
            logger.debug("api lookup failed: %s", exc)

    run_json = Path(settings.artifacts_dir) / run_id / "run.json"
    if not run_json.is_file():
        return {"run": {"id": run_id, "status": "unknown"}, "source": "artifacts"}
    payload = json.loads(run_json.read_text(encoding="utf-8"))
    return {
        "run": {"id": run_id, "status": payload.get("status")},
        "report": payload,
        "source": "artifacts",
    }


def get_report(run_id: str, api_url: str | None = None, settings: Settings | None = None) -> dict:
    """The full report for a finished run (findings, verdicts, evidence paths)."""
    settings = settings or get_settings()
    base = api_base(settings, api_url)
    if api_up(base, settings):
        import httpx

        try:
            response = httpx.get(
                f"{base}/v1/runs/{run_id}/report",
                headers=_headers(settings.api_token),
                timeout=CALL_TIMEOUT_S,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("api report failed: %s", exc)
    return get_run(run_id, api_url=api_url, settings=settings)


#: Single source of truth for what this server exposes to an agent.
TOOLS = {
    "run_test": run_test,
    "get_run": get_run,
    "get_report": get_report,
}


def build_server(name: str = "litmus"):
    """Register :data:`TOOLS` on a FastMCP server (requires the ``mcp`` extra)."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "fastmcp is not installed: pip install -e .[mcp]"
        ) from exc

    server = FastMCP(name)
    for tool in TOOLS.values():
        server.tool()(tool)
    return server


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO)
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
