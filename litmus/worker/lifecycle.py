"""Run lifecycle: install -> explore -> detect -> verify -> evidence.

The loop follows PLAN §5.1: dump, filter to the app package, hash, pick an
action, act, settle by polling, detect, record. Verification follows §5.2:
``pm clear``, launch, replay the trace with selectors only, poll for the
signature, count valid attempts.

M3 adds three things the M1 slice could skip:

* a mitmproxy instance (``runner/proxy.py``) for the whole run, with the
  emulator proxy cleared in ``finally`` even when the run fails;
* every detector (crash, ANR, http_5xx/4xx, dead tap, broken UI) sharing the
  same step loop and the same evidence bundle;
* a **restart after a crash**. A crawl that stopped at B1 would never reach
  the cart, so B2 and B3 would be unreachable. The app is relaunched and the
  crawl continues in a new trace *segment*; a finding's replay starts at its
  own segment so it never replays the earlier crash.
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
from litmus.detectors.anr import AnrDetector, AnrEvent
from litmus.detectors.base import Finding
from litmus.detectors.crash import CrashDetector, CrashEvent
from litmus.detectors.deadtap import (
    DeadTapDetector,
    TapObservation,
    node_content_hash,
    screen_content_hash,
)
from litmus.detectors.environment import (
    classify_environment,
    filtered_finding,
    find_environment_signal,
)
from litmus.detectors.network import (
    NetworkDetector,
    NetworkEvent,
    app_flows,
    load_flows,
    unreachable_hosts,
)
from litmus.detectors.ui import UIScanner
from litmus.runner.driver import Driver, Node, Screen, resolve_ladder
from litmus.runner.local_avd import LocalAvd
from litmus.runner.proxy import MitmProxy
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
from litmus.worker.verify import AttemptResult, ReplayContext, Verification, verify_finding

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
    #: Network detection needs a proxy; the benchmark turns it off only when
    #: mitmproxy is not installed (the run then lacks http findings, and says so).
    proxy: bool = True
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
    proxy: dict[str, Any] = field(default_factory=dict)

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
        self.anr_detector = AnrDetector(self.package)
        self.deadtap_detector = DeadTapDetector(self.package)
        self.driver: Driver | None = None
        self.component = ""
        self.proxy: MitmProxy | None = None
        self._seen_findings: list[Finding] = []
        self._finding_keys: set[tuple[str, str]] = set()
        self._warned: set[str] = set()
        self._started_at = datetime.now(UTC)
        # Detector configuration lives in the profile, never in this module.
        detectors = self.profile.get("detectors") or {}
        enabled = {str(name) for name in (detectors.get("enable") or [])}
        fourxx = detectors.get("http_4xx") or {}
        self.detectors_enabled = enabled
        self.network_detector = NetworkDetector(
            allowed_hosts=[str(host) for host in (self.profile.get("allowed_hosts") or [])],
            http_4xx=("http_4xx" in enabled),
            http_4xx_paths=[str(path) for path in (fourxx.get("paths") or ["/api/"])],
            http_4xx_statuses=[int(code) for code in (fourxx.get("statuses") or [404])],
        )
        self.ui_scanner = UIScanner(self.package)
        self.degraded_hosts: set[str] = set()
        limits = self.profile.get("limits") or {}
        #: One screen push (4 s). A transition that starts with a network call -
        #: login submits before it navigates - gets the longer recovery window.
        self.settle_ms = int(limits.get("settle_ms") or DEFAULT_SETTLE_CAP_MS)
        self.settle_recover_ms = int(limits.get("settle_timeout_ms") or 12000)

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
            self.ui_scanner.density_scale = driver.density_scale()
            result.device_info = self.avd.device_info(driver).to_dict()
            self._emit(
                "device",
                f"{result.device_info['serial']}  API {result.device_info['api_level']}  "
                f"{result.device_info['screen_size']}",
            )

            self._status("installing", package=self.package)
            driver.clear_logcat("crash")
            try:
                self.avd.install(driver, self.options.apk)
            except Exception as exc:
                # An install that fails on this device (ABI mismatch) is an
                # environment signal: visible in the filtered lane, never a bug.
                kind = classify_environment(str(exc))
                if kind:
                    finding = filtered_finding(
                        kind,
                        f"{kind}:{self.package}",
                        local_id=f"F{len(self._seen_findings) + 1}",
                        detail=str(exc)[:400],
                    )
                    self._seen_findings.append(finding)
                    result.findings.append(finding)
                    self._event(FINDING_CANDIDATE, {"finding": finding.to_dict()})
                raise
            result.app_info = self.avd.app_info(driver, self.package)
            self._emit(
                "install",
                f"{self.package} {result.app_info.get('version_name', '?')} installed",
            )

            self._start_proxy(result)
            driver.clear_app(self.package)
            driver.clear_logcat("crash")
            self._status("exploring", step_budget=self.options.steps)
            screen = self._launch_and_wait()
            steps, findings = self._explore(screen)
            result.steps = steps
            result.findings.extend(findings)

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
            # The global proxy is cleared here, not in the happy path: a device
            # left pointing at a dead proxy breaks every later run.
            self._stop_proxy(result)
            result.duration_s = round(time.monotonic() - started, 2)
            self._write_run_summary(result)
            self._persist(result)
            self._finish(result)

        return result

    # -- proxy ------------------------------------------------------------
    def _start_proxy(self, result: RunResult) -> None:
        driver = self._driver
        allowed = [str(host) for host in (self.profile.get("allowed_hosts") or [])]
        if allowed:
            self.degraded_hosts = unreachable_hosts(allowed)
            for host in sorted(self.degraded_hosts):
                self._warn(
                    f"backend {host} is not reachable - transport errors are warnings, "
                    "not HTTP findings"
                )
        if not self.options.proxy or "http_5xx" not in self.detectors_enabled:
            self._emit("proxy", "disabled by options/profile")
            result.proxy = {"enabled": False}
            return
        proxy = MitmProxy(driver, self.run_dir / "proxy")
        if proxy.start():
            self.proxy = proxy
            self._emit("proxy", proxy.status_line())
            result.proxy = {
                "enabled": True,
                "port": proxy.port,
                "flows": str(proxy.flows_path),
            }
        else:
            result.proxy = {"enabled": False, "error": proxy.error}
            self._emit("proxy", proxy.status_line())
            self._warn(f"network detection disabled: {proxy.error}")

    def _stop_proxy(self, result: RunResult) -> None:
        proxy = self.proxy
        self.proxy = None
        if proxy is None:
            return
        cleared = proxy.clear_device_proxy()
        proxy.stop()
        self._emit("proxy", f"stopped; emulator http_proxy={cleared or 'unset'}")
        result.proxy = {**result.proxy, "cleared": cleared or "unset"}

    def _finish(self, result: RunResult) -> None:
        """Announce that the report is queryable, then close the run."""
        if result.status == "done":
            self.sink.emit(
                REPORT_READY,
                {
                    "report_url": f"/v1/runs/{self.run_id}/report",
                    "steps": len(result.steps),
                    "confirmations": result.confirmations,
                    "kinds": sorted({finding.kind for finding in result.findings}),
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
        self._notify_slack(result, counts)

    def _notify_slack(self, result: RunResult, counts: dict[str, int]) -> None:
        """Fire-and-forget Slack ping (PLAN §15).

        The verdict is already persisted and on the event stream, so this can
        never change the outcome: no webhook means no thread at all, and the
        POST runs on a daemon thread with its own timeout.
        """
        from litmus.services.notify import notify_done_async

        notify_done_async(
            self.settings,
            {
                "run_id": result.run_id,
                "status": result.status,
                "package": self.package,
                "duration_s": result.duration_s,
                **counts,
                "kinds": sorted({finding.kind for finding in result.findings}),
            },
        )
    # -- explore ----------------------------------------------------------
    def _explore(self, screen: Screen) -> tuple[list[StepRecord], list[Finding]]:
        driver = self._driver
        policy = HeuristicPolicy(self.profile, self.package)
        steps: list[StepRecord] = []
        findings: list[Finding] = []
        deadline = time.monotonic() + self.options.wall_clock_s
        activity = ""
        activity_hash = ""
        segment_start = 0
        scanned: set[str] = {screen.state_hash(self.package)}
        #: screen hash -> the resource id that first navigated there. After a
        #: crash this is what must not be tapped again: the crawl would only
        #: re-enter the same crashing screen and waste the step budget.
        entry: dict[str, str] = {}

        # A symptom visible on the entry screen (a broken layout on the first
        # activity) has no step to attach to; it is replayed from the launch.
        self._scan_ui(screen, step_idx=None, trace_start=segment_start, findings=findings)

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
            tapped = (
                resolve_ladder(screen, action.selectors, screen.screen_size)
                if action.kind == "tap"
                else None
            )
            offset = self.proxy.offset() if self.proxy else 0
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

            settled, after, toast = self._settle(state, watch_toast=action.kind == "tap")
            if settled is None:
                # The window was empty for longer than the transition window.
                # Recover before recording the step: a stale ``state_after``
                # would make every replay of this trace invalid.
                recovered = self._wait_for_app(timeout_s=8)
                if recovered is not None:
                    settled = recovered
                    after = recovered.state_hash(self.package)
            step.state_after = after
            event = self._detect_crash(wait_ms=1500 if settled is None else 0)
            if event is not None:
                step.result = "crash"
                steps.append(step)
                self._emit("step", f"{line}  -> CRASH {event.signature}")
                self._event(STEP, self._step_payload(step, state, after))
                finding = self._finding_from_crash(
                    event, idx, activity, segment_start=segment_start
                )
                if finding is not None:
                    findings.append(finding)
                policy.observe(action, state, after)
                # Do not walk back into the crash: the app was relaunched, and
                # the budget is better spent on the rest of the app.
                policy.avoid(_action_rid(action), entry.get(state, ""))
                # The crash is recorded; clear it so the next segment does not
                # attribute this same block to an unrelated tap.
                driver.clear_logcat("crash")
                recovered = self._restart_app()
                if recovered is None:
                    self._warn("app did not come back after the crash; stopping explore")
                    break
                screen = recovered
                segment_start = idx + 1
                activity = ""
                activity_hash = ""
                continue

            self._detect_network(offset, idx, segment_start, state, findings)
            # An ANR looks exactly like a dead tap (nothing changes) yet is a
            # hard signal, so it is classified first; only then is "no effect"
            # allowed to mean "dead tap".
            anr = self._detect_anr(idx, segment_start, activity, findings)
            if not anr:
                self._detect_no_effect(
                    action=action,
                    tapped=tapped,
                    screen=screen,
                    settled=settled,
                    state=state,
                    after=after,
                    toast=toast,
                    offset=offset,
                    idx=idx,
                    trace_start=segment_start,
                    activity=activity,
                    findings=findings,
                )

            steps.append(step)
            policy.observe(action, state, after)
            if after and after != state:
                entry.setdefault(after, _action_rid(action))
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

            new_hash = screen.state_hash(self.package)
            if new_hash not in scanned:
                scanned.add(new_hash)
                self._scan_ui(
                    screen, step_idx=idx, trace_start=segment_start, findings=findings
                )

        return steps, findings

    def _detect_network(
        self,
        offset: int,
        step_idx: int,
        trace_start: int,
        state: str,
        findings: list[Finding],
    ) -> None:
        if self.proxy is None:
            return
        flows = self.proxy.flows_since(offset)
        if not flows:
            return
        for event in self.network_detector.detect(flows, self.degraded_hosts):
            if event.is_finding:
                self._add_network(event, step_idx, trace_start, state, findings)
            else:
                self._network_warning(event)

    def _add_network(
        self,
        event: NetworkEvent,
        step_idx: int,
        trace_start: int,
        state: str,
        findings: list[Finding],
    ) -> None:
        if not self._claim(event.kind, event.fingerprint):
            return
        finding = self.network_detector.to_finding(
            event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=step_idx
        )
        finding.trace_start = trace_start
        finding.screen_hash = state
        self._seen_findings.append(finding)
        findings.append(finding)
        self._emit("detect", f"{finding.local_id}  {event.kind}  {event.signature}")
        self._event(
            FINDING_CANDIDATE,
            {
                "finding": finding.to_dict(),
                "step_idx": step_idx + 1,
                "flow": event.flow.to_dict(),
            },
        )

    def _network_warning(self, event: NetworkEvent) -> None:
        """A transport failure is a harness warning, never a bug (PLAN §7)."""
        key = f"{event.kind}:{event.signature}"
        if key in self._warned:
            return
        self._warned.add(key)
        if event.kind == "network_error":
            self._warn(f"mock API not reachable: {event.detail}")
        else:
            self._warn(event.detail)

    def _detect_no_effect(
        self,
        action,
        tapped: Node | None,
        screen: Screen,
        settled: Screen | None,
        state: str,
        after: str,
        toast: bool,
        offset: int,
        idx: int,
        trace_start: int,
        activity: str,
        findings: list[Finding],
    ) -> None:
        """Dead tap: only when the tap changed nothing at all (PLAN §7)."""
        if action.kind != "tap" or tapped is None or settled is None:
            return
        if after != state:
            return
        # Never judge a tap against a screen that is still transitioning.
        if not settled.is_rendered(self.package):
            return
        node_after = self._node_for_identity(settled, tapped)
        ime_raised = self._driver.ime_shown()
        # Only in-scope (app) flows count: a connectivity probe or another
        # process's TLS failure in the same window is not an effect of the tap.
        network_activity = bool(
            self.proxy
            and app_flows(self.proxy.flows_since(offset), self.network_detector)
        )
        observation = TapObservation(
            rid=tapped.resource_id,
            label=tapped.label(),
            clickable=bool(tapped.clickable or tapped.long_clickable),
            node_present_after=node_after is not None,
            content_before=node_content_hash(tapped),
            content_after=node_content_hash(node_after) if node_after else "",
            nav_before=state,
            nav_after=after,
            screen_before=screen_content_hash(screen, self.package),
            screen_after=screen_content_hash(settled, self.package),
            ime_raised=ime_raised,
            toast_seen=toast,
            network_activity=network_activity,
        )
        event = self.deadtap_detector.evaluate(observation)
        if event is None:
            return
        if not self._claim("deadtap", event.fingerprint):
            return
        finding = self.deadtap_detector.to_finding(
            event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=idx
        )
        finding.trace_start = trace_start
        finding.screen_hash = state
        finding.screen = activity.rsplit("/", 1)[-1] if activity else ""
        self._seen_findings.append(finding)
        findings.append(finding)
        self._emit(
            "detect", f"{finding.local_id}  deadtap  {finding.signature}  ({event.detail})"
        )
        self._event(
            FINDING_CANDIDATE,
            {"finding": finding.to_dict(), "step_idx": idx + 1},
        )

    def _detect_anr(
        self, step_idx: int, trace_start: int, activity: str, findings: list[Finding]
    ) -> bool:
        """Hard logcat signals: ANR, and environment failures (filtered).

        Returns True when a hard ANR was added, which stops the same step from
        also being classified as a dead tap.
        """
        added = False
        log = self._driver.logcat("main", lines=400)
        for event in self.anr_detector.detect(log):
            if not self._claim("anr", event.fingerprint):
                continue
            finding = self._finding_from_anr(event, step_idx, activity, trace_start)
            findings.append(finding)
            added = True
        signal = find_environment_signal(log, self.package)
        if signal is not None:
            kind, line = signal
            if self._claim(kind, f"{kind}:{self.package}"):
                finding = filtered_finding(
                    kind,
                    f"{kind}:{self.package}",
                    local_id=f"F{len(self._seen_findings) + 1}",
                    detail=line[:400],
                    step_idx=step_idx,
                )
                finding.trace_start = trace_start
                self._seen_findings.append(finding)
                findings.append(finding)
                self._emit("detect", f"{finding.local_id}  {kind}  filtered")
                self._event(FINDING_CANDIDATE, {"finding": finding.to_dict(), "filtered": True})
        return added

    def _scan_ui(
        self,
        screen: Screen,
        step_idx: int | None,
        trace_start: int,
        findings: list[Finding],
    ) -> None:
        if "ui_broken" not in self.detectors_enabled:
            return
        for event in self.ui_scanner.scan(screen):
            if not self._claim("ui_broken", event.fingerprint):
                continue
            finding = self.ui_scanner.to_finding(
                event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=step_idx
            )
            finding.trace_start = trace_start
            finding.screen_hash = screen.state_hash(self.package)
            if step_idx is None:
                finding.probe["start_hash"] = finding.screen_hash
            self._seen_findings.append(finding)
            findings.append(finding)
            self._emit("detect", f"{finding.local_id}  ui_broken  {finding.signature}")
            self._event(
                FINDING_CANDIDATE,
                {
                    "finding": finding.to_dict(),
                    "step_idx": None if step_idx is None else step_idx + 1,
                },
            )

    def _claim(self, kind: str, fingerprint: str) -> bool:
        """One signature repeated across steps/replays is one finding."""
        key = (kind, fingerprint)
        if key in self._finding_keys:
            return False
        self._finding_keys.add(key)
        return True

    def _restart_app(self) -> Screen | None:
        """Bring the app back after a crash so the crawl can continue."""
        driver = self._driver
        driver.force_stop(self.package)
        driver.hide_ime(force=True)
        self._emit("recover", "relaunching after the crash")
        driver.launch(self.component)
        screen = self._wait_for_app(timeout_s=20)
        if screen is None:
            driver.keyevent(4)
            screen = self._wait_for_app(timeout_s=10)
        return screen

    @staticmethod
    def _node_for_identity(after: Screen, before: Node) -> Node | None:
        """Find the same node on the next dump (resource-id first, then path)."""
        if before.resource_id:
            suffix = f":id/{before.resource_id.rsplit('/', 1)[-1]}"
            for node in after.app_nodes():
                if node.resource_id.endswith(suffix):
                    return node
        for node in after.nodes:
            if node.path == before.path:
                return node
        return None

    def _settle(self, before: str, watch_toast: bool = False) -> tuple[Screen | None, str, bool]:
        """Poll until the screen changes and settles, or the cap expires.

        Two windows, both polled - never slept:

        * ``settle_ms`` (4 s): if the screen is up and unchanged for this long,
          the action had no visible effect;
        * ``settle_timeout_ms`` (12 s): a transition that begins with a network
          call (the login form submits before it navigates) can leave the app
          window empty for longer than 4 s. Giving up at 4 s would record the
          old hash as ``state_after`` and every later replay would fail on it.

        While a tap is settling the Toast window is sampled: a Toast is the
        visible effect of a successful tap that leaves the hierarchy identical.
        On this AVD the window appears ~1.5-2 s after the tap and lasts ~3 s,
        so it is sampled for the whole settle window, not just the first second.
        """
        driver = self._driver
        started = time.monotonic()
        soft_deadline = started + self.settle_ms / 1000
        hard_deadline = started + self.settle_recover_ms / 1000
        toast_seen = False
        last_screen: Screen | None = None
        last_hash = before
        stable = 0
        while time.monotonic() < hard_deadline:
            if watch_toast:
                toast_seen = driver.toast_visible() or toast_seen
            time.sleep(POLL_INTERVAL_S)
            screen = driver.dump()
            # During a transition the dump can land on the launcher, a system
            # window, or an app window that has not drawn yet. None of those is
            # a state change worth recording - keep polling, the window is empty.
            if not screen.is_rendered(self.package):
                continue
            current = screen.state_hash(self.package)
            last_screen = screen
            if current == before:
                if time.monotonic() >= soft_deadline:
                    # Unchanged for the whole short window: a dead tap.
                    return screen, current, toast_seen
                last_hash = current
                continue
            if current != last_hash:
                last_hash = current
                stable = 1
            else:
                stable += 1
            if stable >= 3:
                # The new screen has held for ~1 s: it is safe to act on it.
                return screen, current, toast_seen
        return last_screen, last_hash, toast_seen

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
        self,
        event: CrashEvent,
        step_idx: int,
        activity: str,
        segment_start: int = 0,
    ) -> Finding | None:
        if not self._claim("crash", event.fingerprint):
            return None
        finding = self.crash_detector.to_finding(
            event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=step_idx
        )
        finding.trace_start = segment_start
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

    def _finding_from_anr(
        self, event: AnrEvent, step_idx: int, activity: str, segment_start: int
    ) -> Finding:
        finding = self.anr_detector.to_finding(
            event, local_id=f"F{len(self._seen_findings) + 1}", step_idx=step_idx
        )
        finding.trace_start = segment_start
        finding.screen = activity.rsplit("/", 1)[-1] if activity else ""
        finding.detail = event.reason or finding.detail
        self._seen_findings.append(finding)
        self._emit("detect", f"{finding.local_id}  anr  {finding.signature}")
        self._event(
            FINDING_CANDIDATE,
            {"finding": finding.to_dict(), "step_idx": step_idx + 1},
        )
        return finding

    # -- verification -----------------------------------------------------
    def _verify(self, result: RunResult) -> None:
        driver = self._driver
        verify_profile = self.profile.get("verify") or {}
        required = int(verify_profile.get("attempts", 3))
        delay_ms = int(verify_profile.get("inter_attempt_delay_ms", 1500))
        ctx = ReplayContext(
            proxy=self.proxy,
            network=self.network_detector,
            ui=self.ui_scanner,
            anr=self.anr_detector,
        )

        for finding in result.findings:
            if self.cancel_check():
                raise RunCancelled("cancelled from the dashboard")
            workdir = self.run_dir / "verification" / finding.local_id
            self._emit("verify", f"{finding.local_id}  {finding.kind}  {finding.signature}")
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
                ctx=ctx,
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
                    density_scale=self.ui_scanner.density_scale,
                )
                result.bundles.append(bundle)
                self._emit("evidence", f"bundle {bundle.finding_dir}")

    def _on_attempt(self, finding: Finding, attempt: AttemptResult) -> None:
        if attempt.reproduced:
            detail = "reproduced"
        elif attempt.valid:
            detail = f"valid, not reproduced: {attempt.detail or attempt.reason}"
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
                "detail": attempt.detail,
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
            "proxy": result.proxy,
            "findings": [finding.to_dict() for finding in result.findings],
            "verifications": [item.to_dict() for item in result.verifications],
            "confirmations": result.confirmations,
            "evidence": [bundle.to_dict() for bundle in bundles],
        }
        (self.run_dir / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        flows = load_flows(self.run_dir / "proxy" / "flows.jsonl")
        if flows:
            (self.run_dir / "proxy" / "flows.json").write_text(
                json.dumps([flow.to_dict() for flow in flows], indent=2), encoding="utf-8"
            )

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
            # Not a failure: the evidence bundle on disk is the deliverable, and
            # a CLI run works without a database. Say what was skipped and why,
            # so the line does not read like a crash.
            self._warn(
                "not persisted to Postgres (evidence on disk is complete): "
                f"{type(exc).__name__}: {exc} - set DATABASE_URL to keep runs in the dashboard"
            )


def _action_rid(action) -> str:
    """The resource id an action targets, if any (empty for keys/swipes)."""
    if action is None:
        return ""
    for selector in getattr(action, "selectors", []) or []:
        if selector.get("level") == "resource_id":
            return str(selector.get("value") or "")
    return ""


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
