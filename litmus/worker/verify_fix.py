"""verify-fix: replay a saved finding against a new build (PLAN §5.6).

The demo loop the platform exists for: Litmus finds a bug, the developer fixes
it from the report plus ``repro.py``, and Litmus proves the fix on the new APK.
Nothing here re-explores or calls a model - the trace and the probe come from
the saved run, so the replay is selector-only and deterministic.

One rule that matters more than the verdict: an **invalid** replay (wrong
screen, selector gone, app did not start) is an *error*, never ``fixed``. The
tool only reports ``fixed`` when every required attempt was valid and none of
them reproduced the recorded signature.

    mqa verify-fix --finding F2 --run bench-20261027-163441-1 --apk fixed.apk
    mqa verify-fix --finding bench-20261027-163441-1:F2 --apk fixed.apk
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litmus.config import Settings, get_settings
from litmus.detectors.anr import AnrDetector
from litmus.detectors.base import Finding
from litmus.detectors.network import NetworkDetector
from litmus.detectors.ui import UIScanner
from litmus.runner.driver import Driver
from litmus.worker.policy import Action, StepRecord
from litmus.worker.verify import AttemptResult, ReplayContext, replay_once

FIXED = "fixed"
STILL_REPRODUCES = "still reproduces"
ERROR = "error"

DEFAULT_ATTEMPTS = 3
DEFAULT_SETTLE_CAP_MS = 4000


class VerifyFixError(RuntimeError):
    """The run or the finding could not be loaded; nothing was replayed."""


@dataclass(slots=True)
class FixResult:
    """Outcome of one verify-fix session."""

    finding_ref: str
    apk: str = ""
    run_id: str = ""
    kind: str = ""
    signature: str = ""
    verdict: str = ""
    reason: str = ""
    attempts: list[AttemptResult] = field(default_factory=list)
    valid_attempts: int = 0
    reproductions: int = 0
    duration_s: float = 0.0

    @property
    def exit_code(self) -> int:
        if self.verdict == FIXED:
            return 0
        if self.verdict == STILL_REPRODUCES:
            return 1
        return 2

    @property
    def summary(self) -> str:
        return (
            f"{self.verdict} after {self.valid_attempts} valid replay(s); "
            f"reproduced {self.reproductions}x"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding_ref,
            "run_id": self.run_id,
            "apk": self.apk,
            "kind": self.kind,
            "signature": self.signature,
            "verdict": self.verdict,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "valid_attempts": self.valid_attempts,
            "reproductions": self.reproductions,
            "duration_s": round(self.duration_s, 2),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


# -- loading --------------------------------------------------------------
def split_ref(ref: str) -> tuple[str, str]:
    """``run_123:F2`` -> ``("run_123", "F2")``; ``F2`` -> ``("", "F2")``."""
    text = (ref or "").strip()
    if ":" in text:
        run_part, _, finding_part = text.rpartition(":")
        return run_part.strip(), finding_part.strip()
    return "", text


def resolve_run_json(
    run_ref: str | None,
    artifacts_dir: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Find the ``run.json`` for a run id, a path, or a directory."""
    base = Path(artifacts_dir) if artifacts_dir else Path(get_settings().artifacts_dir)
    text = (run_ref or "").strip()
    if not text:
        raise VerifyFixError("no run given: pass --run <run_id> or --finding <run_id>:F2")

    candidates: list[Path] = []
    raw = Path(text)
    if raw.suffix == ".json":
        candidates.append(raw)
    candidates.extend([base / text / "run.json", raw, raw / "run.json"])
    if cwd is not None:
        candidates.extend([cwd / text / "run.json", cwd / f"{text}.json"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise VerifyFixError(f"no saved run found for {text!r} (looked under {base})")


def load_run(run_json: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(run_json).read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerifyFixError(f"cannot read {run_json}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VerifyFixError(f"{run_json} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerifyFixError(f"{run_json} does not describe a run")
    return data


def steps_from_run(run: dict[str, Any]) -> list[StepRecord]:
    """Rebuild the recorded trace (actions and navigation hashes)."""
    steps: list[StepRecord] = []
    for raw in run.get("steps") or []:
        action = raw.get("action") or {}
        steps.append(
            StepRecord(
                idx=int(raw.get("idx") or 0),
                action=Action(
                    kind=str(action.get("kind") or ""),
                    selectors=list(action.get("selectors") or []),
                    text=action.get("text"),
                    keycode=action.get("keycode"),
                    target=str(action.get("target") or ""),
                    reason=str(action.get("reason") or ""),
                    force=bool(action.get("force")),
                ),
                state_before=str(raw.get("state_before") or ""),
                state_after=str(raw.get("state_after") or ""),
                activity=str(raw.get("activity") or ""),
                result=str(raw.get("result") or "ok"),
                ts=float(raw.get("ts") or 0.0),
            )
        )
    return steps


def finding_from_run(run: dict[str, Any], finding_ref: str) -> Finding:
    """Pick one finding out of a saved run by its local id (``F2``)."""
    wanted = (finding_ref or "").strip().upper()
    entries = run.get("findings") or []
    for raw in entries:
        if str(raw.get("id") or "").upper() != wanted:
            continue
        return Finding(
            local_id=str(raw.get("id") or ""),
            kind=str(raw.get("kind") or ""),
            signature=str(raw.get("signature") or ""),
            fingerprint=str(raw.get("fingerprint") or raw.get("signature") or ""),
            confidence=str(raw.get("confidence") or "heuristic"),
            screen=str(raw.get("screen") or ""),
            step_idx=raw.get("step_idx"),
            detail=str(raw.get("detail") or ""),
            observed_once=bool(raw.get("observed_once", True)),
            verdict=raw.get("verdict"),
            screen_hash=str(raw.get("screen_hash") or ""),
            trace_start=int(raw.get("trace_start") or 0),
            probe=dict(raw.get("probe") or {}),
        )
    known = ", ".join(str(raw.get("id") or "?") for raw in entries) or "none"
    raise VerifyFixError(f"finding {finding_ref!r} not in this run (have: {known})")


def replay_trace(finding: Finding, steps: list[StepRecord]) -> list[StepRecord]:
    """The slice a replay needs: from the finding's segment to its trigger."""
    if not steps:
        return []
    if finding.step_idx is None:
        return []
    trigger = min(finding.step_idx, len(steps) - 1)
    start = max(0, min(finding.trace_start, len(steps) - 1))
    return steps[start : trigger + 1] if trigger >= start else []


# -- execution ------------------------------------------------------------
def verify_fix(
    run_json: Path,
    finding_ref: str,
    apk: Path,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    serial: str | None = None,
    workdir: Path | None = None,
    settings: Settings | None = None,
    driver: Driver | None = None,
    ctx: ReplayContext | None = None,
    settle_cap_ms: int = DEFAULT_SETTLE_CAP_MS,
    launch_wait_ms: int = 20000,
    on_event: Callable[[str, str], None] | None = None,
) -> FixResult:
    """Install ``apk``, replay the saved finding, and say if it is fixed.

    ``driver`` and ``ctx`` are injectable so the whole flow can be tested
    without a device (the probes never touch adb themselves).
    """
    started = time.monotonic()
    run = load_run(run_json)
    finding = finding_from_run(run, finding_ref)
    steps = replay_trace(finding, steps_from_run(run))
    result = FixResult(
        finding_ref=f"{run.get('run_id') or run_json.parent.name}:{finding.local_id}",
        apk=str(apk),
        run_id=str(run.get("run_id") or run_json.parent.name),
        kind=finding.kind,
        signature=finding.signature,
    )

    def emit(kind: str, message: str) -> None:
        if on_event is not None:
            on_event(kind, message)

    if not steps and finding.step_idx is not None:
        raise VerifyFixError(
            f"{finding.local_id} has no replayable trace (step {finding.step_idx})"
        )

    package = str(run.get("package") or "")
    if not package:
        raise VerifyFixError(f"{run_json} does not record the package under test")
    component = str(run.get("component") or "")
    out_dir = workdir or (run_json.parent / "verify-fix" / finding.local_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    avd = None
    proxy = None
    if driver is None:
        from litmus.runner.local_avd import LocalAvd
        from litmus.runner.proxy import MitmProxy
        from litmus.worker.lifecycle import load_profile

        settings = settings or get_settings()
        avd = LocalAvd(settings, serial or (run.get("device") or {}).get("serial"))
        driver = avd.ensure_device()
        driver.package = package
        avd.prepare(driver)
        emit("device", f"{driver.serial} ready")
        emit("install", f"installing {Path(apk).name}")
        avd.install(driver, Path(apk))
        component = driver.resolve_launcher(package)
        if ctx is None:
            profile: dict[str, Any] = {}
            if run.get("profile"):
                try:
                    _, profile = load_profile(str(run["profile"]))
                except Exception:  # noqa: BLE001 - a missing profile only narrows scope
                    profile = {}
            allowed = [str(host) for host in (profile.get("allowed_hosts") or [])]
            detector = NetworkDetector(allowed_hosts=allowed)
            proxy = MitmProxy(driver, out_dir / "proxy")
            if proxy.start():
                emit("proxy", proxy.status_line())
            ctx = ReplayContext(
                proxy=proxy if proxy.started else None,
                network=detector,
                ui=UIScanner(package),
                anr=AnrDetector(package),
            )
    ctx = ctx or ReplayContext()
    if not component:
        component = driver.resolve_launcher(package)

    emit("finding", f"{finding.local_id}  {finding.kind}  {finding.signature}")
    try:
        for attempt in range(1, max(attempts, 1) + 1):
            outcome = replay_once(
                driver=driver,
                package=package,
                component=component,
                steps=steps,
                finding=finding,
                workdir=out_dir,
                attempt=attempt,
                ctx=ctx,
                settle_cap_ms=settle_cap_ms,
                launch_wait_ms=launch_wait_ms,
            )
            result.attempts.append(outcome)
            if outcome.valid:
                result.valid_attempts += 1
            if outcome.valid and outcome.reproduced:
                result.reproductions += 1
            emit(
                "attempt",
                f"{attempt}/{attempts}  {'valid' if outcome.valid else 'invalid'}  "
                f"{'reproduced' if outcome.reproduced else 'not reproduced'}  "
                f"{(outcome.detail or outcome.reason)[:120]}",
            )
            if result.reproductions:
                break
        if result.reproductions:
            result.verdict = STILL_REPRODUCES
            result.reason = (
                f"the recorded signature reproduced in attempt {result.attempts[-1].attempt}"
            )
        elif result.valid_attempts >= attempts:
            result.verdict = FIXED
            result.reason = (
                f"the signature did not reproduce in {result.valid_attempts}/"
                f"{attempts} clean transitions of the saved trace"
            )
        else:
            result.verdict = ERROR
            last = result.attempts[-1].reason if result.attempts else "no attempt ran"
            result.reason = (
                f"only {result.valid_attempts}/{attempts} replays were valid "
                f"({last}) - an invalid replay is not a fix"
            )
    finally:
        if proxy is not None:
            cleared = proxy.clear_device_proxy()
            proxy.stop()
            emit("proxy", f"stopped; emulator http_proxy={cleared or 'unset'}")
        result.duration_s = round(time.monotonic() - started, 2)

    return result


def run_verify_fix(
    finding: str,
    apk: Path,
    run: str | None = None,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    serial: str | None = None,
    settings: Settings | None = None,
    workdir: Path | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> FixResult:
    """CLI entry: resolve ``<run>:F2`` / ``--run <id>`` and replay ``apk``."""
    run_ref, finding_ref = split_ref(finding)
    if not finding_ref:
        raise VerifyFixError(f"no finding in {finding!r} (expected F2 or <run_id>:F2)")
    run_json = resolve_run_json(run or run_ref)
    return verify_fix(
        run_json,
        finding_ref,
        Path(apk),
        attempts=attempts,
        serial=serial,
        settings=settings,
        workdir=workdir,
        on_event=on_event,
    )
