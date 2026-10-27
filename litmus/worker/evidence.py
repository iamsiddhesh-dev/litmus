"""Evidence bundles (PLAN §8).

Every confirmed finding gets a self-contained directory:

    artifacts/{run_id}/{finding_id}/
      trace.json        steps + full selector ladder
      repro.py          standalone adb + stdlib replay, no Litmus imports
      logcat.txt        crash buffer from the confirming attempt
      logcat_full.txt   main buffer around the attempt
      screen_before.png screen immediately before the trigger tap
      screen_at_bug.png screen after the crash was observed
      manifest.json     device, app, timings, verdicts, file hashes

``repro.py`` is generated, not copied, so it can be run on any machine with
``adb`` and the APK installed. It exits 0 when the finding reproduces.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from litmus.detectors.base import Finding
from litmus.worker.policy import StepRecord
from litmus.worker.verify import Verification


@dataclass(slots=True)
class EvidenceBundle:
    finding_dir: Path
    trace_path: Path
    repro_path: Path
    logcat_path: Path
    screenshot_path: Path | None
    manifest_path: Path

    def to_dict(self) -> dict:
        return {
            "dir": str(self.finding_dir),
            "trace": str(self.trace_path),
            "repro": str(self.repro_path),
            "logcat": str(self.logcat_path),
            "screen_at_bug": str(self.screenshot_path) if self.screenshot_path else None,
            "manifest": str(self.manifest_path),
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repro_trace(steps: list[StepRecord], trigger: int | None, start: int = 0) -> list[dict]:
    """The steps a standalone repro must replay.

    ``start`` is the first step of the finding's segment: after a crash the app
    is relaunched, so a cart finding must not replay the earlier crash steps.
    """
    begin = max(0, min(start, len(steps) - 1)) if steps else 0
    limit = len(steps) if trigger is None else min(trigger + 1, len(steps))
    return [step.to_dict() for step in steps[begin:limit]]


def _render_repro(
    finding: Finding,
    package: str,
    component: str,
    trace: list[dict],
    settle_cap_ms: int,
    density_scale: float = 2.0,
) -> str:
    template = (Path(__file__).with_name("repro_template.tmpl")).read_text(encoding="utf-8")
    return (
        template.replace("__FINDING_ID__", json.dumps(finding.local_id))
        .replace("__KIND__", json.dumps(finding.kind))
        .replace("__SIGNATURE__", json.dumps(finding.signature))
        .replace("__PACKAGE__", json.dumps(package))
        .replace("__COMPONENT__", json.dumps(component))
        .replace("__FINGERPRINT__", json.dumps(finding.fingerprint))
        .replace("__SETTLE_MS__", str(int(settle_cap_ms)))
        .replace("__DENSITY__", repr(float(density_scale or 2.0)))
        .replace("__PROBE__", json.dumps(finding.probe, indent=2))
        .replace("__TRACE__", json.dumps(trace, indent=2))
    )


def _pick_attempt(verification: Verification):
    reproduced = [a for a in verification.attempts if a.reproduced]
    if reproduced:
        return reproduced[0]
    valid = [a for a in verification.attempts if a.valid]
    return valid[-1] if valid else (verification.attempts[-1] if verification.attempts else None)


def write_bundle(
    run_dir: Path,
    finding: Finding,
    steps: list[StepRecord],
    verification: Verification,
    run_meta: dict,
    device_info: dict,
    app_info: dict,
    package: str,
    component: str,
    settle_cap_ms: int = 4000,
    density_scale: float = 2.0,
) -> EvidenceBundle:
    """Write the evidence bundle for one finding and return its paths."""
    finding_dir = run_dir / finding.local_id
    finding_dir.mkdir(parents=True, exist_ok=True)
    finding.evidence_dir = finding_dir

    trace = _repro_trace(steps, finding.step_idx, finding.trace_start)
    trace_path = finding_dir / "trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "run_id": run_meta.get("run_id"),
                "package": package,
                "component": component,
                "apk": run_meta.get("apk"),
                "profile": run_meta.get("profile"),
                "device": device_info,
                "app": app_info,
                "finding": finding.to_dict(),
                "trigger_step": finding.step_idx,
                "replay_steps": len(trace),
                "steps": [step.to_dict() for step in steps],
                "verification": verification.to_dict(),
                "recorded_at": _utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    repro_path = finding_dir / "repro.py"
    repro_path.write_text(
        _render_repro(
            finding, package, component, trace, settle_cap_ms, density_scale
        ),
        encoding="utf-8",
    )

    # Kind-specific evidence (the proxy flow behind an http finding, for example).
    extra_paths: list[Path] = []
    for name, payload in (finding.evidence or {}).items():
        path = finding_dir / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        extra_paths.append(path)

    attempt = _pick_attempt(verification)
    logcat_path = finding_dir / "logcat.txt"
    logcat_full_path = finding_dir / "logcat_full.txt"
    screenshot_path: Path | None = None
    if attempt is not None and attempt.logcat_path and attempt.logcat_path.is_file():
        shutil.copyfile(attempt.logcat_path, logcat_path)
    else:
        logcat_path.write_text(finding.log_text or "", encoding="utf-8")
    if attempt is not None and attempt.logcat_full_path and attempt.logcat_full_path.is_file():
        shutil.copyfile(attempt.logcat_full_path, logcat_full_path)

    if attempt is not None and attempt.screen_at_bug and attempt.screen_at_bug.is_file():
        screenshot_path = finding_dir / "screen_at_bug.png"
        shutil.copyfile(attempt.screen_at_bug, screenshot_path)
    if attempt is not None and attempt.screen_before and attempt.screen_before.is_file():
        shutil.copyfile(attempt.screen_before, finding_dir / "screen_before.png")

    files = [
        trace_path,
        repro_path,
        logcat_path,
        finding_dir / "screen_before.png",
        finding_dir / "screen_at_bug.png",
        *extra_paths,
    ]
    manifest = {
        "tool": "litmus",
        "run_id": run_meta.get("run_id"),
        "finding": finding.to_dict(),
        "verdict": verification.verdict,
        "summary": verification.summary,
        "verification": verification.to_dict(),
        "device": device_info,
        "app": app_info,
        "apk": run_meta.get("apk"),
        "profile": run_meta.get("profile"),
        "package": package,
        "component": component,
        "started_at": run_meta.get("started_at"),
        "ended_at": _utc_now(),
        "repro_command": f"python repro.py --serial {device_info.get('serial', 'emulator-5554')}",
        "artifacts": [
            {
                "path": path.name,
                "bytes": path.stat().st_size if path.is_file() else 0,
                "sha256": _sha256(path),
            }
            for path in files
        ],
    }
    manifest_path = finding_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return EvidenceBundle(
        finding_dir=finding_dir,
        trace_path=trace_path,
        repro_path=repro_path,
        logcat_path=logcat_path,
        screenshot_path=screenshot_path,
        manifest_path=manifest_path,
    )