"""Report assembly: lanes, confidence, and the repro rate (PLAN §10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from litmus.api.report import build_report, repro_rate

START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def _run(status: str = "done") -> dict:
    return {
        "id": "run_1",
        "status": status,
        "step_budget": 25,
        "steps_taken": 7,
        "llm_calls": 0,
        "cost_usd": 0.0,
        "started_at": START,
        "ended_at": START + timedelta(seconds=135),
        "error": None,
    }


def _target() -> dict:
    return {
        "id": "tgt_1",
        "package": "com.bazaar.demo",
        "version": "1.0.3",
        "profile": "profiles/bazaar.yaml",
        "source": "upload",
    }


def _finding(
    local_id: str,
    verdict: str | None,
    kind: str = "crash",
    confidence: str = "hard",
) -> dict:
    return {
        "id": f"run_1:{local_id}",
        "run_id": "run_1",
        "kind": kind,
        "signature": f"java.lang.NullPointerException:{local_id}",
        "fingerprint": "fp",
        "screen": "ProductActivity",
        "first_step_idx": 6,
        "severity": "high",
        "confidence": confidence,
        "observed_once": True,
        "verdict": verdict,
    }


def _attempts(finding_id: str, reproduced: int, valid: int = 3) -> list[dict]:
    rows = []
    for index in range(valid):
        rows.append(
            {
                "finding_id": finding_id,
                "attempt": index + 1,
                "reproduced": index < reproduced,
                "valid": True,
                "state_hash": "h",
                "evidence_path": None,
            }
        )
    return rows


def test_repro_rate_ignores_invalid_attempts() -> None:
    attempts = [
        {"attempt": 1, "reproduced": True, "valid": True},
        {"attempt": 2, "reproduced": True, "valid": True},
        {"attempt": 3, "reproduced": False, "valid": False},
        {"attempt": 4, "reproduced": True, "valid": True},
    ]
    rate, confirmations, valid = repro_rate(attempts)
    assert rate == "3/3"
    assert confirmations == 3
    assert valid == 3


def test_confirmed_finding_reports_hard_confidence() -> None:
    finding = _finding("F1", "confirmed")
    report = build_report(_run(), _target(), [finding], _attempts(finding["id"], 3))

    assert report["summary"] == {
        "steps": 7,
        "step_budget": 25,
        "confirmed": 1,
        "flaky": 0,
        "filtered": 0,
        "duration_s": 135.0,
        "llm_calls": 0,
        "cost_usd": 0.0,
    }
    entry = report["confirmed"][0]
    assert entry["confidence"] == "hard"
    assert entry["repro_rate"] == "3/3"
    assert entry["local_id"] == "F1"
    assert entry["evidence"] == "/v1/findings/run_1:F1"


def test_flaky_and_filtered_are_separate_lanes() -> None:
    flaky = _finding("F5", "flaky")
    filtered = _finding("F6", "filtered", kind="unsupported_abi", confidence="heuristic")
    report = build_report(
        _run(),
        _target(),
        [flaky, filtered],
        _attempts(flaky["id"], 0) + _attempts(filtered["id"], 0),
    )
    assert report["summary"]["confirmed"] == 0
    assert report["flaky"][0]["id"] == flaky["id"]
    assert report["filtered"][0]["id"] == filtered["id"]


def test_legacy_row_without_verdict_is_derived_from_attempts() -> None:
    finding = _finding("F1", None)
    report = build_report(_run(), _target(), [finding], _attempts(finding["id"], 3))
    assert report["confirmed"], "3/3 reproductions with no stored verdict is confirmed"


def test_not_reproduced_findings_never_reach_a_lane() -> None:
    finding = _finding("F9", "not_reproduced")
    finding["observed_once"] = False
    report = build_report(_run(), _target(), [finding], [])
    assert report["confirmed"] == []
    assert report["flaky"] == []
    assert report["filtered"] == []


def test_heuristic_confidence_is_preserved() -> None:
    finding = _finding("F3", "confirmed", kind="deadtap", confidence="heuristic")
    report = build_report(_run(), _target(), [finding], _attempts(finding["id"], 3))
    assert report["confirmed"][0]["confidence"] == "heuristic"
    assert "hard" in report["confidence_note"]