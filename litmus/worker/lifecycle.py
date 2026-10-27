"""Run lifecycle: install -> explore -> detect -> verify -> evidence (M1).

The loop follows PLAN §5.1: dump, filter to the app package, hash, pick an
action, act, settle by polling, detect, record. Verification follows §5.2:
``pm clear``, launch, replay the trace with selectors only, poll for the
signature, count valid attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from litmus.config import REPO_ROOT, Settings, get_settings
from litmus.db import repo
from litmus.detectors.base import Finding
from litmus.detectors.crash import CrashDetector, CrashEvent
from litmus.runner.driver import Driver, Screen
from litmus.runner.local_avd import LocalAvd
from litmus.worker import evidence
from litmus.worker.events import (
    DONE,
    FINDING_CANDIDATE,
    REPORT_READY,
    RUN_STATUS,
    STEP,
    VERDICT,
    WARNING,
    EventSink,
    NullSink,
)
from litmus.worker.policy import HeuristicPolicy, StepRecord
from litmus.worker.verify import AttemptResult, Verification, verify_finding

DEFAULT_WALL_CLOCK_S = 12 * 60
DEFAULT_SETTLE_CAP_MS = 4000
POLL_INTERVAL_S = 0.2


class RunCancelled(Exception):
    """Raised internally when a run is cancelled from the dashboard."""


def load_profile(name_or_path: str) -> tuple[Path, dict[str, Any]]:
    """Resolve ``bazaar`` or ``profiles/bazaar.yaml`` to a parsed profile."""
    candidate = Path(name_or_path)
    if candidate.suffix.lower() in {".yaml", ".yml"} and candidate.is_file():
        path = candidate
    else:
        path = REPO_ROOT / "profiles" / f"{name_or_path}.yaml"
        if not path.is_file():
            path = REPO_ROOT / "profiles" / name_or_path
    if not path.is_file():
        raise FileNotFoundError(f"profile not found: {name_or_path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return path, data


@dataclass(slots=True)
class RunOptions:
    apk: Path
    profile: str = "bazaar"
    steps: int = 25
    serial: str | None = None
    artifacts_dir: Path | None = None
    wall_clock_s: int = DEFAULT_WALL_CLOCK_S
    verify: bool = True
    #: Supplied by the control plane so the run row, the event stream and the
    #: evidence directory all share one id. Generated when the CLI drives a run.
    run_id: str | None = None
    #: Supplied by the control plane so persistence updates the uploaded target
    #: instead of registering a second one for the same APK.
    target_id: str | None = None


@dataclass(slots=True)
class RunResult:
    run_id: str
    run_dir: Path
    #: Terminal state of the M2 state machine: done | failed | cancelled.
    status: str = "done"
    steps: list[StepRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    verifications: list[Verification] = field(default_factory=list)
    confirmations: int = 0
    duration_s: float = 0.0
    error: str = ""
    device_info: dict[str, Any] = field(default_factory=dict)
    app_info: dict[str, Any] = field(default_factory=dict)
    bundles: list[evidence.EvidenceBundle] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.confirmations else 1


class Lifecycle:
    """Owns one end-to-end run against one APK."""

    def __init__(
        self,
        options: RunOptions,
        settings: Settings | None = None,
        on_event: Callable[[str, str], None] | None = None,
        sink: EventSink | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.options = options
        self.settings = settings or get_settings()
        self.on_event = on_event or (lambda kind, message: None)
        # The CLI leaves the sink empty; the worker passes a StoreSink so every
        # phase, step and verdict lands in the events table for the dashboard.
        self.sink = sink or NullSink()
        # Polled by the worker so ``POST /v1/runs/{id}/cancel`` stops a run that
        # is already exploring the device.
        self.cancel_check = cancel_check or (lambda: False)
        self.profile_path, self.profile = load_profile(options.profile)
        self.package = str(self.profile.get("package") or "").strip()
        if not self.package:
            raise ValueError(f"profile {self.profile_path} has no package")
        self.run_id = options.run_id or (
            time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        self.run_dir = (options.artifacts_dir or self.settings.artifacts_dir) / self.run_id
        self.avd = LocalAvd(self.settings, options.serial)
        self.crash_detector = CrashDetector(self.package)
        self.driver: Driver | None = None
        self.component = ""
        self._seen_findings: list[Finding] = []
        self._started_at = datetime.now(UTC)

    # -- entry point ------------------------------------------------------
    def run(self) -> RunResult:
        started = time.monotonic()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        result = RunResult(run_id=self.run_id, run_dir=self.run_dir)
        self._emit("run", f"{self.run_id}  {self.package}  step budget {self.options.steps}")
        self._status(
            "provisioning",
            package=self.package,
            apk=str(self.options.apk),
            profile=str(self.profile_path),
        )

        try:
            driver = self.avd.ensure_device()
            self.driver = driver
            driver.package = self.package
            self.avd.prepare(driver)
            result.device_info = self.avd.device_info(driver).to_dict()
            self._emit(
                "device",
                f"{result.device_info['serial']}  API {result.device_info['api_level']}  "
                f"{result.device_info['screen_size']}",
            )

            self._status("installing", package=self.package)
            driver.clear_logcat("crash")
            self.avd.install(driver, self.options.apk)
            result.app_info = self.avd.app_info(driver, self.package)
            self._emit(
                "install",
                f"{self.package} {result.app_info.get('version_name', '?')} installed",
            )

            driver.clear_app(self.package)
            driver.clear_logcat("crash")
            self._status("exploring", step_budget=self.options.steps)
            screen = self._launch_and_wait()
            steps, finding = self._explore(screen)
            result.steps = steps
            if finding is not None:
                result.findings.append(finding)

            if result.findings and self.options.verify:
                self._status("verifying", findings=len(result.findings))
                self._verify(result)

            result.confirmations = sum(
                1 for verification in result.verifications if verification.verdict == "confirmed"
            )
            self._status("reporting", steps_taken=len(result.steps))

        except RunCancelled:
            result.status = "cancelled"
            self._warn("run cancelled by user")
        except Exception as exc:  # noqa: BLE001 - a run failure is reported, not raised
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self._warn(result.error, kind="error")
        finally:
            result.duration_s = round(time.monotonic() - started, 2)
            self._write_run_summary(result)
            self._persist(result)
            self._finish(result)

        return result

    def _finish(self, result: RunResult) -> None:
        """Announce that the report is queryable, then close the run."""
        if result.status == "done":
            self.sink.emit(
                REPORT_READY,
                {
                    "report_url": f"/v1/runs/{self.run_id}/report",
                    "steps": len(result.steps),
                    "confirmations": result.confirmations,
                },
            )
        counts = {"confirmed": 0, "flaky": 0, "filtered": 0}
        for verification in result.verifications:
            if verification.verdict in counts:
                counts[verification.verdict] += 1
        self.sink.emit(
            DONE,
            {
                "status": result.status,
                "steps_taken": len(result.steps),
                "duration_s": result.duration_s,
                "error": result.error or None,
                **counts,
            },
        )

    # -- explore ----------------------------------------------------------
    def _explore(self, screen: Screen) -> tuple[list[StepRecord], Finding | None]:
        driver = self._driver
        policy = HeuristicPolicy(self.profile, self.package)
        steps: list[StepRecord] = []
        finding: Finding | None = None
        deadline = time.monotonic() + self.options.wall_clock_s
        activity = ""
        activity_hash = ""

        for idx in range(self.options.steps):
            if self.cancel_check():
                raise RunCancelled("cancelled from the dashboard")
            if time.monotonic() >= deadline:
                self._warn("wall-clock budget exhausted")
                break
            if not screen.is_rendered(self.package):
                recovered = self._wait_for_app(timeout_s=5)
                if recovered is None:
                    self._warn("app is not on screen; stopping explore")
                    break
                screen = recovered
            state = screen.state_hash(self.package)
            if state != activity_hash:
                activity = driver.current_activity() or activity
                activity_hash = state

            ime_shown = driver.ime_shown() if screen.edit_texts(self.package) else False
            action = policy.next_action(screen, state, ime_shown)
            if action is None:
                self._warn("policy found nothing actionable; stopping explore")
                break

            step = StepRecord(idx=idx, action=action, state_before=state, activity=activity)
            line = (
                f"{idx + 1:02d}  {action.kind:<8} {action.target[:28]:<28} "
                f"{action.reason}"
            )
            error = driver.execute(action, screen, self.component)
            if error:
                step.result = f"error: {error}"
                steps.append(step)
                self._event(STEP, self._step_payload(step, state, ""))
                self._warn(f"{line}  -> {error}")
                # A stale selector is not a reason to abandon the run: refresh
                # the screen, stop repeating this target, and try something else.
                policy.abandon_repeat()
                recovered = self._wait_for_app(timeout_s=5)
                if recovered is None:
                    break
                screen = recovered
                continue

            settled, after = self._settle(state)
            step.state_after = after
            event = self._detect_crash(wait_ms=1500 if settled is None else 0)
            if event is not None:
                step.result = "crash"
                steps.append(step)
                self._emit("step", f"{line}  -> CRASH {event.signature}")
                self._event(STEP, self._step_payload(step, state, after))
                finding = self._finding_from_crash(event, idx, activity)
                break

            steps.append(step)
            policy.observe(action, state, after)
            self._emit(
                "step",
                f"{line}  -> {'changed' if after != state else 'unchanged'}",
            )
            self._event(STEP, self._step_payload(step, state, after))

            if settled is None:
                recovered = self._wait_for_app(timeout_s=8)
                if recovered is None:
                    self._warn("app disappeared without a crash signature")
                    break
                screen = recovered
            else:
                screen = settled

        return steps, finding

    def _settle(self, before: str) -> tuple[Screen | None, str]:
        """Poll until the screen changes and settles, or the cap expires.

        A transition needs two things before the next action: the new screen
        must be up (stability across several polls), and - when nothing changes
        - enough grace that a slow effect is not recorded as "unchanged". Both
        loops poll; neither sleeps for a fixed span.
        """
        driver = self._driver
        started = time.monotonic()
        deadline = started + DEFAULT_SETTLE_CAP_MS / 1000
        last_screen: Screen | None = None
        last_hash = before
        stable = 0
        failures = 0
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            screen = driver.dump()
            # During a transition the dump can land on the launcher, a system
            # window, or an app window that has not drawn yet. None of those is
            # a state change worth recording.
            if not screen.is_rendered(self.package):
                failures += 1
                if failures >= 6:
                    return None, before
                continue
            failures = 0
            current = screen.state_hash(self.package)
            last_screen = screen
            if current != last_hash:
                last_hash = current
                stable = 0
            else:
                stable += 1
            if current != before and stable >= 3:
                # The new screen has held for ~1 s: it is safe to act on it.
                return screen, current
        # Nothing changed within the cap. Only now is "unchanged" a fact: a
        # submit that waits on the network can take longer than a screen push.
        return last_screen, last_hash

    def _detect_crash(self, wait_ms: int = 0) -> CrashEvent | None:
        """Read the crash buffer, polling briefly when the app has gone away."""
        driver = self._driver
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            events = self.crash_detector.detect(driver.logcat("crash"))
            if events:
                return events[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_INTERVAL_S)

    def _finding_from_crash(
        self, event: CrashEvent, step_idx: int, activity: str
    ) -> Finding:
        finding = self.crash_detector.to_finding(
            event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=step_idx
        )
        self._seen_findings.append(finding)
        if activity:
            finding.screen = activity.rsplit("/", 1)[-1]
        screenshot = self.run_dir / "explore" / f"step_{step_idx + 1:02d}_at_bug.png"
        screenshot_name: str | None = None
        try:
            self._driver.screenshot(screenshot)
            screenshot_name = screenshot.name
            self._emit("evidence", f"screenshot {screenshot.name}")
        except Exception as exc:  # noqa: BLE001 - a screenshot must not fail the run
            self._warn(f"screenshot failed: {exc}")
        self._event(
            FINDING_CANDIDATE,
            {
                "finding": finding.to_dict(),
                "step_idx": step_idx + 1,
                "screenshot": screenshot_name,
            },
        )
        return finding

    # -- verification -----------------------------------------------------
    def _verify(self, result: RunResult) -> None:
        driver = self._driver
        verify_profile = self.profile.get("verify") or {}
        required = int(verify_profile.get("attempts", 3))
        delay_ms = int(verify_profile.get("inter_attempt_delay_ms", 1500))

        for finding in result.findings:
            if self.cancel_check():
                raise RunCancelled("cancelled from the dashboard")
            workdir = self.run_dir / "verification" / finding.local_id
            self._emit("verify", f"{finding.local_id}  {finding.signature}")
            verification = verify_finding(
                finding=finding,
                driver=driver,
                package=self.package,
                component=self.component,
                steps=result.steps,
                workdir=workdir,
                required=required,
                max_tries=5,
                settle_cap_ms=DEFAULT_SETTLE_CAP_MS,
                inter_attempt_delay_ms=delay_ms,
                on_attempt=lambda attempt, finding=finding: self._on_attempt(finding, attempt),
            )
            result.verifications.append(verification)
            self._emit(
                "verdict",
                f"{verification.summary}  {finding.local_id}  {finding.kind}  "
                f"{finding.signature}",
            )
            self._event(
                VERDICT,
                {
                    "finding_id": finding.local_id,
                    "kind": finding.kind,
                    "signature": finding.signature,
                    "confidence": finding.confidence,
                    "verdict": verification.verdict,
                    "summary": verification.summary,
                    "confirmations": verification.confirmations,
                    "valid_attempts": verification.valid_attempts,
                    "reason": verification.reason,
                },
            )
            if verification.verdict in {"confirmed", "flaky"}:
                bundle = evidence.write_bundle(
                    run_dir=self.run_dir,
                    finding=finding,
                    steps=result.steps,
                    verification=verification,
                    run_meta={
                        "run_id": self.run_id,
                        "apk": str(self.options.apk),
                        "profile": str(self.profile_path),
                        "started_at": self._started_at.isoformat(),
                    },
                    device_info=result.device_info,
                    app_info=result.app_info,
                    package=self.package,
                    component=self.component,
                    settle_cap_ms=DEFAULT_SETTLE_CAP_MS,
                )
                result.bundles.append(bundle)
                self._emit("evidence", f"bundle {bundle.finding_dir}")

    def _on_attempt(self, finding: Finding, attempt: AttemptResult) -> None:
        if attempt.reproduced:
            detail = "reproduced"
        elif attempt.valid:
            detail = "valid, not reproduced"
        else:
            detail = f"invalid: {attempt.reason}"
        self._emit("attempt", f"attempt {attempt.attempt}  {detail}  ({attempt.duration_s:.1f}s)")
        self._event(
            VERDICT,
            {
                "finding_id": finding.local_id,
                "kind": finding.kind,
                "attempt": attempt.attempt,
                "valid": attempt.valid,
                "reproduced": attempt.reproduced,
                "reason": attempt.reason,
                "duration_s": round(attempt.duration_s, 2),
                "state_hash": attempt.state_hash,
            },
        )

    # -- helpers ----------------------------------------------------------
    def _launch_and_wait(self) -> Screen:
        driver = self._driver
        self.component = self.avd.launch(driver, self.package)
        self._emit("launch", self.component)
        screen = self._wait_for_app(timeout_s=20)
        if screen is None:
            # A system overlay (immersive confirmation, "app keeps stopping")
            # can sit on top of the app window. One BACK clears it.
            self._warn("app not on screen after launch; dismissing an overlay")
            driver.keyevent(4)
            screen = self._wait_for_app(timeout_s=10)
        if screen is None:
            raise RuntimeError("app did not render its UI after launch")
        return screen

    def _wait_for_app(self, timeout_s: float = 15) -> Screen | None:
        driver = self._driver
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            screen = driver.dump()
            if screen.is_rendered(self.package):
                return screen
            time.sleep(0.4)
        return None

    @property
    def _driver(self) -> Driver:
        if self.driver is None:
            raise RuntimeError("device is not attached")
        return self.driver

    def _emit(self, kind: str, message: str) -> None:
        self.on_event(kind, message)

    def _event(self, type_: str, payload: dict[str, Any]) -> None:
        """Persist a structured event for the dashboard (SSE) consumers."""
        self.sink.emit(type_, payload)

    def _status(self, phase: str, **extra: Any) -> None:
        """Move the run through one state-machine phase."""
        self._emit("status", phase)
        self._event(RUN_STATUS, {"status": phase, "run_id": self.run_id, **extra})

    def _warn(self, message: str, kind: str = "warn") -> None:
        self._emit(kind, message)
        self._event(WARNING, {"message": message})

    @staticmethod
    def _step_payload(step: StepRecord, before: str, after: str) -> dict[str, Any]:
        return {
            "idx": step.idx + 1,
            "action": step.action.kind,
            "target": step.action.target,
            "reason": step.action.reason,
            "result": step.result,
            "activity": step.activity,
            "state_before": before,
            "state_after": after,
            "changed": bool(after) and after != before,
        }

    # -- persistence ------------------------------------------------------
    def _write_run_summary(self, result: RunResult) -> None:
        bundles = result.bundles
        summary = {
            "run_id": result.run_id,
            "status": result.status,
            "error": result.error,
            "package": self.package,
            "apk": str(self.options.apk),
            "profile": str(self.profile_path),
            "started_at": self._started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_s": result.duration_s,
            "steps_taken": len(result.steps),
            "steps": [step.to_dict() for step in result.steps],
            "device": result.device_info,
            "app": result.app_info,
            "findings": [finding.to_dict() for finding in result.findings],
            "verifications": [item.to_dict() for item in result.verifications],
            "confirmations": result.confirmations,
            "evidence": [bundle.to_dict() for bundle in bundles],
        }
        (self.run_dir / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    def _persist(self, result: RunResult) -> None:
        if not self.settings.has_database:
            self._warn("DATABASE_URL not set - run not persisted")
            return
        bundles = result.bundles
        version = str(result.app_info.get("version_name", ""))
        target_id = self.options.target_id or (
            "tgt_"
            + hashlib.sha1(f"{self.package}|{version}|{self.options.apk}".encode()).hexdigest()[:16]
        )
        started_at = self._started_at
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "target": {
                "id": target_id,
                "package": self.package,
                "version": version,
                "storage_path": str(self.options.apk),
                "profile": str(self.profile_path),
            },
            "run": {
                "status": result.status,
                "step_budget": self.options.steps,
                "verify_mode": str((self.profile.get("verify") or {}).get("mode", "pm_clear")),
                "started_at": started_at,
                "ended_at": datetime.now(UTC),
                "steps_taken": len(result.steps),
                "error": result.error or None,
            },
            "steps": [
                {
                    "idx": step.idx,
                    "state_hash": step.state_after,
                    "activity": step.activity,
                    "action": f"{step.action.kind} {step.action.target}".strip(),
                    "selector_json": json.dumps(step.action.to_dict()),
                    "result": step.result,
                    "ts": step.ts,
                }
                for step in result.steps
            ],
            "findings": [
                {
                    "id": f"{self.run_id}:{finding.local_id}",
                    "kind": finding.kind,
                    "signature": finding.signature,
                    "fingerprint": finding.fingerprint,
                    "screen": finding.screen,
                    "first_step_idx": finding.step_idx,
                    "severity": "high" if finding.confidence == "hard" else "medium",
                    "confidence": finding.confidence,
                    "observed_once": finding.observed_once,
                    "verdict": finding.verdict,
                }
                for finding in result.findings
            ],
            "verdicts": [
                {
                    "finding_id": f"{self.run_id}:{finding.local_id}",
                    "attempt": attempt["attempt"],
                    "reproduced": attempt["reproduced"],
                    "valid": attempt.get("valid", True),
                    "state_hash": attempt.get("state_hash"),
                    "evidence_path": str(finding.evidence_dir) if finding.evidence_dir else None,
                }
                for finding in result.findings
                for attempt in (finding.attempts or [])
            ],
            "artifacts": [
                {
                    "id": f"{self.run_id}:{bundle.finding_dir.name}:{path.name}",
                    "finding_id": f"{self.run_id}:{bundle.finding_dir.name}",
                    "kind": path.name,
                    "path": str(path),
                    "bytes": path.stat().st_size if path.is_file() else 0,
                    "sha256": _sha256(path),
                }
                for bundle in bundles
                for path in bundle.finding_dir.iterdir()
                if path.is_file()
            ],
        }
        try:
            asyncio.run(repo.persist_run(self.settings, record))
            self._emit("db", "run persisted to Postgres")
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            self._warn( f"database persistence skipped: {type(exc).__name__}: {exc}")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()