"""Replay verification (PLAN §5.2) - M1 scope: crash findings.

A trace is replayed with selectors only (zero LLM) on a ``pm clear``ed app.
Each attempt is polled - never fixed-slept - so SwiftShader timing does not
turn a real bug into a flake. An attempt is *valid* only when every selector
resolved and every intermediate navigation hash matched; up to five tries are
allowed to collect three valid attempts.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from litmus.detectors.base import Finding
from litmus.detectors.crash import CrashEvent, parse_crashes
from litmus.runner.driver import Driver, Screen, resolve_ladder
from litmus.worker.policy import StepRecord

#: Poll cap for a step transition or a crash signature.
SETTLE_CAP_MS = 4000
POLL_INTERVAL_S = 0.2


@dataclass(slots=True)
class AttemptResult:
    attempt: int
    valid: bool
    reproduced: bool
    reason: str = ""
    state_hash: str = ""
    crash: CrashEvent | None = None
    log_text: str = ""
    logcat_path: Path | None = None
    logcat_full_path: Path | None = None
    screen_before: Path | None = None
    screen_at_bug: Path | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "valid": self.valid,
            "reproduced": self.reproduced,
            "reason": self.reason,
            "state_hash": self.state_hash,
            "crash": self.crash.to_dict() if self.crash else None,
            "logcat": str(self.logcat_path) if self.logcat_path else None,
            "screen_before": str(self.screen_before) if self.screen_before else None,
            "screen_at_bug": str(self.screen_at_bug) if self.screen_at_bug else None,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass(slots=True)
class Verification:
    finding: Finding
    attempts: list[AttemptResult] = field(default_factory=list)
    valid_attempts: int = 0
    confirmations: int = 0
    verdict: str = "not_reproduced"
    reason: str = ""

    @property
    def summary(self) -> str:
        return f"{self.verdict} {self.confirmations}/{self.valid_attempts}"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "valid_attempts": self.valid_attempts,
            "confirmations": self.confirmations,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def _wait_for_hash(
    driver: Driver, package: str, expected: str, timeout_ms: int = SETTLE_CAP_MS
) -> tuple[bool, Screen | None]:
    """Poll the hierarchy until the navigation hash matches ``expected``."""
    deadline = time.monotonic() + timeout_ms / 1000
    screen: Screen | None = None
    while time.monotonic() < deadline:
        candidate = driver.dump()
        if candidate.is_rendered(package):
            screen = candidate
            if candidate.state_hash(package) == expected:
                return True, candidate
        time.sleep(POLL_INTERVAL_S)
    return False, screen


def _poll_crash(
    driver: Driver,
    package: str,
    fingerprint: str,
    timeout_ms: int = SETTLE_CAP_MS,
) -> tuple[CrashEvent | None, str]:
    """Poll the crash buffer for the finding's fingerprint."""
    deadline = time.monotonic() + timeout_ms / 1000
    log = ""
    while time.monotonic() < deadline:
        log = driver.logcat("crash")
        events = parse_crashes(log, package)
        for event in events:
            if event.fingerprint == fingerprint:
                return event, log
        time.sleep(POLL_INTERVAL_S)
    return None, log


def _capture_logs(driver: Driver, attempt_dir: Path) -> tuple[Path, Path, str]:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    crash_log = driver.logcat("crash")
    main_log = driver.logcat("main", lines=400)
    logcat_path = attempt_dir / "logcat.txt"
    logcat_full_path = attempt_dir / "logcat_full.txt"
    logcat_path.write_text(crash_log, encoding="utf-8")
    logcat_full_path.write_text(main_log, encoding="utf-8")
    return logcat_path, logcat_full_path, crash_log


def replay_once(
    driver: Driver,
    package: str,
    component: str,
    steps: list[StepRecord],
    fingerprint: str,
    workdir: Path,
    attempt: int,
    settle_cap_ms: int = SETTLE_CAP_MS,
    launch_wait_ms: int = 20000,
) -> AttemptResult:
    """One clean-state replay of a trace under ``workdir``."""
    started = time.monotonic()
    attempt_dir = workdir / f"attempt_{attempt}"
    result = AttemptResult(attempt=attempt, valid=False, reproduced=False)

    driver.clear_logcat("crash")
    driver.force_stop(package)
    # A previous attempt can leave the keyboard (or a crash dialog) up; that
    # changes the first screen and would make a good replay look invalid.
    driver.hide_ime(force=True)
    driver.clear_app(package)
    driver.launch(component)

    # Wait until the app's first recorded screen is actually on display.
    first_hash = steps[0].state_before if steps else ""
    ready, screen = _wait_for_hash(driver, package, first_hash, launch_wait_ms)
    if not ready:
        # A system overlay (immersive confirmation, "app keeps stopping") can
        # sit on top of the app window. One BACK clears it.
        driver.keyevent(4)
        ready, screen = _wait_for_hash(driver, package, first_hash, launch_wait_ms)
    if not ready:
        # One relaunch covers a slow cold start.
        driver.force_stop(package)
        driver.launch(component)
        ready, screen = _wait_for_hash(driver, package, first_hash, launch_wait_ms)
    if not ready:
        result.reason = "app did not reach the recorded start screen"
        _, _, result.log_text = _capture_logs(driver, attempt_dir)
        result.duration_s = time.monotonic() - started
        return result

    if not steps:
        result.reason = "empty trace"
        result.duration_s = time.monotonic() - started
        return result

    final_index = len(steps) - 1
    for index, step in enumerate(steps):
        screen = driver.dump()
        current_hash = screen.state_hash(package)
        if current_hash != step.state_before:
            matched, screen = _wait_for_hash(driver, package, step.state_before, settle_cap_ms)
            if not matched or screen is None:
                result.reason = f"step {index}: wrong screen before action"
                _, _, result.log_text = _capture_logs(driver, attempt_dir)
                result.duration_s = time.monotonic() - started
                return result
            current_hash = step.state_before

        node = None
        if step.action.kind in {"tap", "input"}:
            node = resolve_ladder(screen, step.action.selectors, screen.screen_size)
            if node is None:
                result.reason = f"step {index}: selector not found"
                _, _, result.log_text = _capture_logs(driver, attempt_dir)
                result.duration_s = time.monotonic() - started
                return result

        if index == final_index:
            result.screen_before = attempt_dir / "screen_before.png"
            driver.screenshot(result.screen_before)
            error = driver.execute(step.action, screen, component)
            if error:
                result.reason = f"step {index}: {error}"
                _, _, result.log_text = _capture_logs(driver, attempt_dir)
                result.duration_s = time.monotonic() - started
                return result
            crash, log = _poll_crash(driver, package, fingerprint, settle_cap_ms)
            result.state_hash = driver.dump().state_hash(package)
            result.crash = crash
            result.reproduced = crash is not None
            result.valid = True
            result.reason = "reproduced" if crash else "valid attempt, not reproduced"
            result.screen_at_bug = attempt_dir / "screen_at_bug.png"
            driver.screenshot(result.screen_at_bug)
            logcat_path, full_path, result.log_text = _capture_logs(driver, attempt_dir)
            result.logcat_path = logcat_path
            result.logcat_full_path = full_path
            result.duration_s = time.monotonic() - started
            return result

        error = driver.execute(step.action, screen, component)
        if error:
            result.reason = f"step {index}: {error}"
            _, _, result.log_text = _capture_logs(driver, attempt_dir)
            result.duration_s = time.monotonic() - started
            return result

        matched, _ = _wait_for_hash(driver, package, step.state_after, settle_cap_ms)
        if not matched and step.action.kind == "tap":
            # A tap can land while the layout is still settling. Retry it once
            # on a fresh dump before calling the attempt invalid.
            retry_screen = driver.dump()
            if retry_screen.is_rendered(package):
                retry_error = driver.execute(step.action, retry_screen, component)
                if retry_error is None:
                    matched, _ = _wait_for_hash(
                        driver, package, step.state_after, settle_cap_ms
                    )
        if not matched:
            result.reason = f"step {index}: navigation hash mismatch after action"
            _, _, result.log_text = _capture_logs(driver, attempt_dir)
            result.duration_s = time.monotonic() - started
            return result

    result.reason = "trace ended without a trigger step"
    result.duration_s = time.monotonic() - started
    return result


def verify_finding(
    finding: Finding,
    driver: Driver,
    package: str,
    component: str,
    steps: list[StepRecord],
    workdir: Path,
    required: int = 3,
    max_tries: int = 5,
    settle_cap_ms: int = SETTLE_CAP_MS,
    inter_attempt_delay_ms: int = 1500,
    on_attempt: Callable[[AttemptResult], None] | None = None,
) -> Verification:
    """Replay until ``required`` valid attempts are collected (max ``max_tries``)."""
    if finding.step_idx is None:
        return Verification(
            finding=finding, verdict="filtered", reason="finding has no trigger step"
        )

    trigger = min(finding.step_idx, len(steps) - 1)
    replay_steps = steps[: trigger + 1]
    verification = Verification(finding=finding)

    for attempt in range(1, max_tries + 1):
        result = replay_once(
            driver=driver,
            package=package,
            component=component,
            steps=replay_steps,
            fingerprint=finding.fingerprint,
            workdir=workdir,
            attempt=attempt,
            settle_cap_ms=settle_cap_ms,
        )
        verification.attempts.append(result)
        if on_attempt is not None:
            on_attempt(result)
        if result.valid:
            verification.valid_attempts += 1
            if result.reproduced:
                verification.confirmations += 1
        if verification.valid_attempts >= required:
            break
        time.sleep(inter_attempt_delay_ms / 1000)

    if verification.valid_attempts < required:
        verification.verdict = "filtered"
        verification.reason = (
            f"only {verification.valid_attempts}/{required} valid attempts "
            f"in {max_tries} tries - environment issue, not a bug"
        )
    elif verification.confirmations == verification.valid_attempts:
        verification.verdict = "confirmed"
        verification.reason = "the signature survived every valid replay"
    elif verification.confirmations > 0:
        verification.verdict = "flaky"
        verification.reason = (
            f"reproduced {verification.confirmations}/{verification.valid_attempts} valid attempts"
        )
    elif finding.observed_once:
        verification.verdict = "flaky"
        verification.reason = "observed during explore, not reproducible on replay"
    else:
        verification.verdict = "not_reproduced"
        verification.reason = "never observed and never reproduced"

    finding.verdict = verification.verdict
    finding.attempts = [attempt.to_dict() for attempt in verification.attempts]
    return verification