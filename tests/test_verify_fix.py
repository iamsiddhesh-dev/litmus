"""verify-fix: the demo punchline, driven by a stub device (no emulator).

The whole point of the tool is the distinction between "the bug is gone" and
"the replay did not work": only a *valid* replay can report ``fixed``. These
tests pin that rule down, plus run/finding resolution from a saved ``run.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litmus.detectors.network import NetworkDetector
from litmus.detectors.ui import UIScanner
from litmus.worker.verify import ReplayContext
from litmus.worker.verify_fix import (
    ERROR,
    FIXED,
    STILL_REPRODUCES,
    VerifyFixError,
    finding_from_run,
    replay_trace,
    resolve_run_json,
    split_ref,
    steps_from_run,
    verify_fix,
)
from tests.test_verify import StubDriver
from tests.ui_fixtures import BAZAAR, cart_screen, product_screen

UI_SIGNATURE = "overlap:tv_product_price,tv_product_title"
RUN_ID = "bench-20260101-000000-1"


class FixDriver(StubDriver):
    """StubDriver plus the one extra hook verify-fix needs."""

    def resolve_launcher(self, package: str) -> str:
        return f"{package}/.CatalogActivity"


def write_run(
    root: Path,
    *,
    signature: str = UI_SIGNATURE,
    start_hash: str = "",
    step_idx: int | None = None,
    steps: list[dict] | None = None,
) -> Path:
    payload = {
        "run_id": RUN_ID,
        "status": "done",
        "package": BAZAAR,
        "profile": "",
        "device": {"serial": "emulator-5554"},
        "steps": steps or [],
        "findings": [
            {
                "id": "F1",
                "kind": "ui_broken",
                "signature": signature,
                "fingerprint": signature,
                "confidence": "heuristic",
                "step_idx": step_idx,
                "trace_start": 0,
                "screen_hash": start_hash,
                "probe": {"signature": signature, "start_hash": start_hash},
                "verdict": "confirmed",
            }
        ],
    }
    path = root / RUN_ID / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def context() -> ReplayContext:
    return ReplayContext(ui=UIScanner(BAZAAR), network=NetworkDetector(allowed_hosts=[]))


# -- the three verdicts ----------------------------------------------------
def test_a_gone_signature_on_a_valid_replay_is_fixed(tmp_path):
    entry = product_screen()
    run_json = write_run(tmp_path, start_hash=entry.state_hash(BAZAAR))
    # Same screen structure (so the trace still lands there), no overlap any more.
    driver = FixDriver([product_screen(overlap_px=0)])

    result = verify_fix(
        run_json,
        "F1",
        tmp_path / "fixed-bazaar.apk",
        attempts=2,
        driver=driver,
        ctx=context(),
        settle_cap_ms=50,
        launch_wait_ms=50,
    )

    assert result.verdict == FIXED, result.reason
    assert result.exit_code == 0
    assert result.valid_attempts == 2
    assert result.reproductions == 0
    assert result.kind == "ui_broken"
    assert result.run_id == RUN_ID


def test_a_signature_that_comes_back_is_still_reproducing(tmp_path):
    entry = product_screen()
    run_json = write_run(tmp_path, start_hash=entry.state_hash(BAZAAR))
    driver = FixDriver([product_screen()])

    result = verify_fix(
        run_json,
        "F1",
        tmp_path / "fixed-bazaar.apk",
        attempts=3,
        driver=driver,
        ctx=context(),
        settle_cap_ms=50,
        launch_wait_ms=50,
    )

    assert result.verdict == STILL_REPRODUCES, result.reason
    assert result.exit_code == 1
    assert result.reproductions == 1


def test_an_invalid_replay_is_an_error_never_a_fix(tmp_path):
    # The app never reaches the recorded screen, so "the bug is gone" cannot be
    # concluded: the trace is not replayable against this build.
    entry = product_screen()
    run_json = write_run(tmp_path, start_hash=entry.state_hash(BAZAAR))
    driver = FixDriver([cart_screen()])

    result = verify_fix(
        run_json,
        "F1",
        tmp_path / "fixed-bazaar.apk",
        attempts=1,
        driver=driver,
        ctx=context(),
        settle_cap_ms=50,
        launch_wait_ms=50,
    )

    assert result.verdict == ERROR, result.reason
    assert result.exit_code == 2
    assert "not a fix" in result.reason
    assert result.valid_attempts == 0


# -- resolution ------------------------------------------------------------
def test_finding_reference_forms():
    assert split_ref("F2") == ("", "F2")
    assert split_ref("run_123:F2") == ("run_123", "F2")
    assert split_ref("20260101-000000-1:f2") == ("20260101-000000-1", "f2")


def test_run_json_resolves_by_id_and_by_path(tmp_path):
    run_json = write_run(tmp_path)
    assert resolve_run_json(RUN_ID, artifacts_dir=tmp_path) == run_json
    assert resolve_run_json(str(run_json), artifacts_dir=tmp_path) == run_json
    assert resolve_run_json(RUN_ID, artifacts_dir=tmp_path, cwd=tmp_path) == run_json


def test_a_run_that_does_not_exist_says_so(tmp_path):
    with pytest.raises(VerifyFixError, match="no saved run"):
        resolve_run_json("nope", artifacts_dir=tmp_path)


def test_a_finding_that_is_not_in_the_run_says_so(tmp_path):
    run_json = write_run(tmp_path)
    run = json.loads(run_json.read_text(encoding="utf-8"))
    assert finding_from_run(run, "f1").local_id == "F1"
    with pytest.raises(VerifyFixError, match="have: F1"):
        finding_from_run(run, "F9")


def test_the_trace_is_sliced_from_the_findings_own_segment(tmp_path):
    steps = [
        {
            "idx": index,
            "action": {"kind": "tap", "selectors": [], "target": f"n{index}"},
            "state_before": f"h{index}",
            "state_after": f"h{index + 1}",
        }
        for index in range(4)
    ]
    run_json = write_run(tmp_path, start_hash="h2", step_idx=3, steps=steps)
    run = json.loads(run_json.read_text(encoding="utf-8"))
    records = steps_from_run(run)

    assert [record.idx for record in records] == [0, 1, 2, 3]
    assert records[2].action.target == "n2"

    finding = finding_from_run(run, "F1")
    finding.trace_start = 2
    assert [record.idx for record in replay_trace(finding, records)] == [2, 3]
