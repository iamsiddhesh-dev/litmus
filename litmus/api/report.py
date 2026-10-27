"""Report assembly (PLAN §10).

Pure functions over rows: given a run, its target, findings and verdicts, build
the JSON the dashboard and ``GET /v1/runs/{id}/report`` return. Keeping this
free of I/O is what makes the report shape testable without a database.
"""

from __future__ import annotations

from typing import Any

#: Lanes in the report. ``not_reproduced`` is deliberately absent: PLAN §5.2
#: treats it as internal bookkeeping, never a finding.
CONFIRMED = "confirmed"
FLAKY = "flaky"
FILTERED = "filtered"

CONFIDENCE_NOTE = (
    "Every finding here survived replay on a clean state. `hard` findings "
    "(crash, ANR, HTTP 5xx) are objective signals; `heuristic` findings "
    "(dead tap, broken UI, 4xx) survive replay but are not proof of a defect."
)


def _duration_s(run: dict[str, Any]) -> float:
    started, ended = run.get("started_at"), run.get("ended_at")
    if not started or not ended:
        return 0.0
    return round((ended - started).total_seconds(), 2)


def group_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        grouped.setdefault(verdict["finding_id"], []).append(verdict)
    return grouped


def repro_rate(attempts: list[dict[str, Any]]) -> tuple[str, int, int]:
    """Return ``("3/3", confirmations, valid_attempts)`` for one finding.

    Only *valid* attempts count: an attempt that could not resolve a selector on
    the recorded screen is environment noise, not a verdict about the bug.
    """
    valid = [attempt for attempt in attempts if attempt.get("valid", True)]
    confirmations = sum(1 for attempt in valid if attempt.get("reproduced"))
    return f"{confirmations}/{len(valid)}", confirmations, len(valid)


def _lane_entry(finding: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    rate, confirmations, valid = repro_rate(attempts)
    entry: dict[str, Any] = {
        "id": finding["id"],
        "local_id": str(finding["id"]).rsplit(":", 1)[-1],
        "kind": finding["kind"],
        "confidence": finding.get("confidence", "heuristic"),
        "severity": finding.get("severity", "medium"),
        "signature": finding["signature"],
        "screen": finding.get("screen") or "",
        "step_idx": finding.get("first_step_idx"),
        "repro_rate": rate,
        "valid_attempts": valid,
        "confirmations": confirmations,
        "observed_once": bool(finding.get("observed_once")),
        "evidence": f"/v1/findings/{finding['id']}",
    }
    evidence_dir = next(
        (attempt.get("evidence_path") for attempt in attempts if attempt.get("evidence_path")),
        None,
    )
    if evidence_dir:
        entry["evidence_dir"] = evidence_dir
    return entry


def _derive_verdict(finding: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    """Fall back to the replay numbers when the verifier did not store a verdict.

    Runs persisted before M2 added ``findings.verdict`` still report correctly.
    """
    stored = (finding.get("verdict") or "").strip()
    if stored:
        return stored
    _, confirmations, valid = repro_rate(attempts)
    if valid and confirmations == valid:
        return CONFIRMED
    if confirmations:
        return FLAKY
    if finding.get("observed_once"):
        return FLAKY
    return "not_reproduced"


def build_report(
    run: dict[str, Any],
    target: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    steps_taken: int | None = None,
) -> dict[str, Any]:
    """Assemble the report JSON for one run."""
    grouped = group_verdicts(verdicts)
    lanes: dict[str, list[dict[str, Any]]] = {CONFIRMED: [], FLAKY: [], FILTERED: []}
    for finding in findings:
        attempts = grouped.get(finding["id"], [])
        verdict = _derive_verdict(finding, attempts)
        if verdict in lanes:
            lanes[verdict].append(_lane_entry(finding, attempts))

    for lane in lanes.values():
        lane.sort(key=lambda entry: (-entry["confirmations"], entry["local_id"]))

    target = target or {}
    return {
        "run_id": run["id"],
        "status": run["status"],
        "target": {
            "id": target.get("id"),
            "package": target.get("package") or "",
            "version": target.get("version") or "",
            "profile": target.get("profile") or "",
            "source": target.get("source") or "",
        },
        "summary": {
            "steps": steps_taken if steps_taken is not None else run.get("steps_taken", 0),
            "step_budget": run.get("step_budget"),
            "confirmed": len(lanes[CONFIRMED]),
            "flaky": len(lanes[FLAKY]),
            "filtered": len(lanes[FILTERED]),
            "duration_s": _duration_s(run),
            "llm_calls": run.get("llm_calls", 0),
            "cost_usd": run.get("cost_usd", 0.0),
        },
        "confidence_note": CONFIDENCE_NOTE,
        "confirmed": lanes[CONFIRMED],
        "flaky": lanes[FLAKY],
        "filtered": lanes[FILTERED],
        "error": run.get("error"),
    }