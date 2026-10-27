"""Replay verification (PLAN §5.2) for every finding kind.

A trace is replayed with selectors only (zero LLM) on a ``pm clear``ed app.
Each attempt is polled - never fixed-slept - so SwiftShader timing does not
turn a real bug into a flake. An attempt is *valid* only when every selector
resolved and every intermediate navigation hash matched; up to five tries are
allowed to collect three valid attempts.

The trigger step is then probed for the finding's own signature:

===============  ==========================================================
kind             probe (poll cap 4 s)
===============  ==========================================================
crash            ``logcat -b crash`` fingerprint
anr              ``ANR in <pkg>`` in the logcat buffers (5 s grace)
http_5xx/4xx     a new proxy flow with the recorded ``status method path``
deadtap          navigation, node content and screen content all unchanged,
                 with no IME, no Toast and no network request
ui_broken        the same overlap / zero-size / clipped signature on screen
===============  ==========================================================
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from litmus.detectors.anr import AnrDetector, parse_anrs
from litmus.detectors.base import Finding
from litmus.detectors.crash import CrashEvent, parse_crashes
from litmus.detectors.deadtap import node_content_hash, screen_content_hash
from litmus.detectors.network import NetworkDetector, app_flows
from litmus.detectors.ui import UIScanner
from litmus.runner.driver import Driver, Node, Screen, resolve_ladder
from litmus.runner.proxy import MitmProxy
from litmus.worker.policy import StepRecord

#: Poll cap for a step transition or a finding signature.
SETTLE_CAP_MS = 4000
POLL_INTERVAL_S = 0.2
#: An ANR needs the framework's own grace period before it is reported.
ANR_CAP_MS = 5000


@dataclass(slots=True)
class ReplayContext:
    """Side channels a replay probe needs (all optional, all device-free)."""

    proxy: MitmProxy | None = None
    network: NetworkDetector | None = None
    ui: UIScanner | None = None
    anr: AnrDetector | None = None


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
    detail: str = ""

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
            "detail": self.detail,
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
    if not expected:
        return True, None
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
        for event in parse_crashes(log, package):
            if event.fingerprint == fingerprint:
                return event, log
        time.sleep(POLL_INTERVAL_S)
    return None, log


def _poll_anr(
    driver: Driver, package: str, fingerprint: str, timeout_ms: int = ANR_CAP_MS
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_ms / 1000
    log = ""
    while time.monotonic() < deadline:
        log = driver.logcat("main", lines=400)
        for event in parse_anrs(log, package):
            if event.fingerprint == fingerprint:
                return True, log
        time.sleep(POLL_INTERVAL_S)
    return False, log


def _poll_network(
    proxy: MitmProxy | None,
    detector: NetworkDetector | None,
    signature: str,
    offset: int,
    timeout_ms: int = SETTLE_CAP_MS,
) -> tuple[bool, str]:
    """Poll the proxy log for a new flow matching the recorded signature."""
    if proxy is None:
        return False, "no proxy attached"
    deadline = time.monotonic() + timeout_ms / 1000
    seen: list[str] = []
    while time.monotonic() < deadline:
        flows = proxy.flows_since(offset)
        if detector is not None:
            for event in detector.detect(flows):
                if event.is_finding:
                    seen.append(event.signature)
                    if event.signature == signature:
                        return True, event.detail
        elif any(flow.signature == signature for flow in flows):
            return True, signature
        time.sleep(POLL_INTERVAL_S)
    return False, ("; ".join(sorted(set(seen))) or "no matching flow")


def _find_by_short_rid(screen: Screen, rid: str) -> Node | None:
    suffix = f":id/{rid}"
    for node in screen.app_nodes():
        if node.resource_id.endswith(suffix):
            return node
    return None


def _drain_proxy(proxy: MitmProxy, quiet_ms: int = 600, cap_ms: int = 3000) -> None:
    """Wait until the proxy log has been quiet, i.e. no request is in flight.

    The catalog's ``GET /api/products`` is fired on activity creation and can
    land seconds later on a slow proxy. Without draining, that background flow
    appears during a tap's observation window and a dead tap looks alive.
    """
    deadline = time.monotonic() + cap_ms / 1000
    last = proxy.offset()
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        current = proxy.offset()
        if current != last:
            last = current
            quiet_since = time.monotonic()
        elif (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
            return


def _poll_deadtap(
    driver: Driver,
    proxy: MitmProxy | None,
    probe: dict,
    offset: int,
    timeout_ms: int = SETTLE_CAP_MS,
    background: frozenset[str] = frozenset(),
    network: NetworkDetector | None = None,
) -> tuple[bool, str]:
    """Poll for the absence of any effect (nav, content, IME, Toast, network).

    ``background`` holds the ``METHOD path`` pairs already requested earlier in
    this replay. A repeat of one of them (a retry of the products call, say) is
    not evidence that the tap did something. Flows outside the profile's host
    scope are system traffic and are ignored for the same reason.
    """
    nav = str(probe.get("nav_hash") or "")
    screen_hash = str(probe.get("screen_hash") or "")
    content_hash = str(probe.get("content_hash") or "")
    rid = str(probe.get("rid") or "")
    if nav:
        # Judge "nothing happened" only on the recorded screen: a replay that
        # is still transitioning would otherwise look like a dead tap.
        matched, _ = _wait_for_hash(driver, driver.package, nav, timeout_ms)
        if not matched:
            return False, "the recorded screen did not appear"
    deadline = time.monotonic() + timeout_ms / 1000
    toast_seen = False
    while time.monotonic() < deadline:
        if proxy is not None:
            for flow in app_flows(proxy.flows_since(offset), network):
                if f"{flow.method} {flow.clean_path}" in background:
                    continue
                return False, "a network request left the app"
        if driver.toast_visible():
            toast_seen = True
        if driver.ime_shown():
            return False, "the tap raised the IME"
        screen = driver.dump()
        if not screen.is_rendered(driver.package):
            return False, "the app left the screen"
        if nav and screen.state_hash(driver.package) != nav:
            return False, "navigation changed"
        if screen_hash and screen_content_hash(screen, driver.package) != screen_hash:
            return False, "screen content changed"
        node = _find_by_short_rid(screen, rid) if rid else None
        if node is None:
            return False, "the tapped node disappeared"
        if content_hash and node_content_hash(node) != content_hash:
            return False, "the tapped node's content changed"
        time.sleep(POLL_INTERVAL_S)
    if toast_seen:
        return False, "the tap showed a Toast"
    return True, "no navigation, content, IME, Toast or network effect"


def _poll_ui(
    driver: Driver,
    scanner: UIScanner | None,
    signature: str,
    timeout_ms: int = SETTLE_CAP_MS,
    screen_hash: str = "",
) -> tuple[bool, str]:
    if scanner is None:
        return False, "no UI scanner attached"
    if screen_hash:
        # The trigger tap starts a transition: wait for the recorded screen
        # before looking for the layout signature, or a slow draw reads as
        # "signature absent" and a real bug becomes flaky.
        matched, _ = _wait_for_hash(driver, scanner.package, screen_hash, timeout_ms)
        if not matched:
            return False, "the recorded screen did not appear"
    deadline = time.monotonic() + timeout_ms / 1000
    seen: list[str] = []
    while time.monotonic() < deadline:
        screen = driver.dump()
        if screen.is_rendered(scanner.package):
            found = [event.signature for event in scanner.scan(screen)]
            seen.extend(found)
            if signature in found:
                return True, signature
        time.sleep(POLL_INTERVAL_S)
    return False, ("; ".join(sorted(set(seen))) or "layout signature not present")


def _probe(
    finding: Finding,
    driver: Driver,
    ctx: ReplayContext,
    offset: int,
    settle_cap_ms: int,
    background: frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    package = driver.package
    kind = finding.kind
    if kind == "crash":
        crash, _ = _poll_crash(driver, package, finding.fingerprint, settle_cap_ms)
        return crash is not None, "reproduced" if crash else "no matching crash"
    if kind == "anr":
        return _poll_anr(driver, package, finding.fingerprint)
    if kind in {"http_5xx", "http_4xx"}:
        return _poll_network(
            ctx.proxy, ctx.network, str(finding.probe.get("signature") or ""), offset, settle_cap_ms
        )
    if kind == "deadtap":
        return _poll_deadtap(
            driver, ctx.proxy, finding.probe, offset, settle_cap_ms, background, ctx.network
        )
    if kind == "ui_broken":
        return _poll_ui(
            driver,
            ctx.ui,
            str(finding.probe.get("signature") or ""),
            settle_cap_ms,
            screen_hash=finding.screen_hash,
        )
    return False, f"{kind} is not replayable"


def _run_probe(
    finding: Finding,
    driver: Driver,
    ctx: ReplayContext,
    offset: int,
    settle_cap_ms: int,
    background: frozenset[str] = frozenset(),
) -> tuple[CrashEvent | None, bool, str]:
    """Probe the trigger step and, for a crash, return the parsed event too."""
    if finding.kind == "crash":
        crash, _ = _poll_crash(driver, driver.package, finding.fingerprint, settle_cap_ms)
        detail = "reproduced" if crash else "no matching crash"
        return crash, crash is not None, detail
    reproduced, detail = _probe(finding, driver, ctx, offset, settle_cap_ms, background)
    return None, reproduced, detail


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
    finding: Finding,
    workdir: Path,
    attempt: int,
    ctx: ReplayContext | None = None,
    settle_cap_ms: int = SETTLE_CAP_MS,
    launch_wait_ms: int = 20000,
) -> AttemptResult:
    """One clean-state replay of a trace under ``workdir``."""
    started = time.monotonic()
    ctx = ctx or ReplayContext()
    #: Flows written before the trigger are background (login, the product
    #: list); they must not be attributed to the trigger tap.
    attempt_offset = ctx.proxy.offset() if ctx.proxy else 0
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
    first_hash = (
        steps[0].state_before
        if steps
        else (finding.probe.get("start_hash") or finding.screen_hash or "")
    )
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
        # A symptom visible on the entry screen (ui_broken) has no step to replay.
        offset = ctx.proxy.offset() if ctx.proxy else 0
        result.crash, reproduced, detail = _run_probe(finding, driver, ctx, offset, settle_cap_ms)
        result.valid = True
        result.reproduced = reproduced
        result.detail = detail
        result.reason = detail
        result.state_hash = driver.dump().state_hash(package)
        result.screen_at_bug = attempt_dir / "screen_at_bug.png"
        driver.screenshot(result.screen_at_bug)
        logcat_path, full_path, result.log_text = _capture_logs(driver, attempt_dir)
        result.logcat_path = logcat_path
        result.logcat_full_path = full_path
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

        if index == final_index:
            if step.action.kind in {"tap", "input"}:
                if resolve_ladder(screen, step.action.selectors, screen.screen_size) is None:
                    result.reason = f"step {index}: selector not found"
                    _, _, result.log_text = _capture_logs(driver, attempt_dir)
                    result.duration_s = time.monotonic() - started
                    return result
            result.screen_before = attempt_dir / "screen_before.png"
            driver.screenshot(result.screen_before)
            # The probe only looks at what happened after this action.
            background: frozenset[str] = frozenset()
            if ctx.proxy is not None:
                if finding.kind == "deadtap":
                    # Wait for the app to go quiet first; then a request that
                    # appears afterwards is attributable to the tap. System
                    # traffic (connectivity probes, foreign TLS failures) is out
                    # of the profile's host scope and never counts.
                    _drain_proxy(ctx.proxy)
                    background = frozenset(
                        f"{flow.method} {flow.clean_path}"
                        for flow in app_flows(
                            ctx.proxy.flows_since(attempt_offset), ctx.network
                        )
                    )
            offset = ctx.proxy.offset() if ctx.proxy else 0
            error = driver.execute(step.action, screen, component)
            if error:
                result.reason = f"step {index}: {error}"
                _, _, result.log_text = _capture_logs(driver, attempt_dir)
                result.duration_s = time.monotonic() - started
                return result
            if finding.kind == "deadtap":
                current = driver.dump()
                expected = str(finding.probe.get("nav_hash") or "")
                if expected and current.state_hash(package) != expected:
                    result.reason = "replay ended on a different screen than the tap"
                    _, _, result.log_text = _capture_logs(driver, attempt_dir)
                    result.duration_s = time.monotonic() - started
                    return result
            result.crash, reproduced, detail = _run_probe(
                finding, driver, ctx, offset, settle_cap_ms, background
            )
            result.state_hash = driver.dump().state_hash(package)
            result.reproduced = reproduced
            result.detail = detail
            result.valid = True
            result.reason = detail if not reproduced else "reproduced"
            result.screen_at_bug = attempt_dir / "screen_at_bug.png"
            driver.screenshot(result.screen_at_bug)
            logcat_path, full_path, result.log_text = _capture_logs(driver, attempt_dir)
            result.logcat_path = logcat_path
            result.logcat_full_path = full_path
            result.duration_s = time.monotonic() - started
            return result

        node = None
        if step.action.kind in {"tap", "input"}:
            node = resolve_ladder(screen, step.action.selectors, screen.screen_size)
            if node is None:
                result.reason = f"step {index}: selector not found"
                _, _, result.log_text = _capture_logs(driver, attempt_dir)
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
    ctx: ReplayContext | None = None,
    on_attempt: Callable[[AttemptResult], None] | None = None,
) -> Verification:
    """Replay until ``required`` valid attempts are collected (max ``max_tries``)."""
    if finding.verdict == "filtered":
        return Verification(finding=finding, verdict="filtered", reason="environment signal")
    if finding.step_idx is None and not finding.probe.get("start_hash"):
        return Verification(
            finding=finding, verdict="filtered", reason="finding has no trigger step"
        )
    if not steps and finding.step_idx is not None:
        return Verification(finding=finding, verdict="filtered", reason="empty trace")

    trigger = min(finding.step_idx if finding.step_idx is not None else -1, len(steps) - 1)
    start = max(0, min(finding.trace_start, len(steps) - 1))
    replay_steps = steps[start : trigger + 1] if trigger >= start else []
    if not replay_steps and finding.step_idx is not None:
        return Verification(
            finding=finding, verdict="filtered", reason="trigger step precedes its segment"
        )
    verification = Verification(finding=finding)

    for attempt in range(1, max_tries + 1):
        result = replay_once(
            driver=driver,
            package=package,
            component=component,
            steps=replay_steps,
            finding=finding,
            workdir=workdir,
            attempt=attempt,
            ctx=ctx,
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
