"""Benchmark scoring: B1-B5 signatures and the recall / FP rules (PLAN §14).

The runner lives in ``benchmark/run_bench.py`` so ``python`` can run it
directly; these tests import that same file instead of duplicating the scoring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "benchmark" / "run_bench.py"


def _load():
    spec = importlib.util.spec_from_file_location("litmus_bench_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve ``cls.__module__`` through sys.modules while executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load()

GROUND_TRUTH = bench.load_expected()


def finding(kind: str, signature: str, verdict: str, confidence: str = "hard") -> dict:
    return {
        "kind": kind,
        "signature": signature,
        "verdict": verdict,
        "confidence": confidence,
        "screen": "",
        "step_idx": 0,
    }


B1 = finding("crash", "java.lang.NullPointerException:Integer.intValue", "confirmed")
B2 = finding("http_5xx", "500 POST /api/checkout", "confirmed")
B3 = finding("deadtap", "deadtap:btn_apply_coupon", "confirmed", "heuristic")
B4 = finding("ui_broken", "overlap:tv_product_price,tv_product_title", "confirmed", "heuristic")
B5_FLAKY = finding(
    "crash",
    "java.lang.NullPointerException:express checkout: session token missing",
    "flaky",
)


# -- signatures ------------------------------------------------------------
def test_ground_truth_has_the_five_expected_bugs():
    assert [entry["id"] for entry in GROUND_TRUTH["expected"]] == ["B1", "B2", "B3", "B4", "B5"]


def test_b1_signature_matches_the_expected_entry():
    entry = GROUND_TRUTH["expected"][0]
    assert bench.signatures_match("crash", entry["signature"], B1["signature"])


def test_crash_match_tolerates_an_extra_frame_or_message_token():
    assert bench.signatures_match(
        "crash",
        "java.lang.NullPointerException:Integer.intValue",
        "java.lang.NullPointerException:Integer.intValue@com.bazaar.demo.CartStore.add",
    )


def test_crash_match_rejects_a_different_exception_class():
    assert not bench.signatures_match(
        "crash", "java.lang.IllegalStateException:Integer.intValue",
        "java.lang.NullPointerException:Integer.intValue",
    )


def test_b2_http_match_is_exact_and_order_free_for_the_path_query():
    assert bench.signatures_match("http_5xx", "500 POST /api/checkout", "500 POST /api/checkout")
    assert bench.signatures_match(
        "http_5xx", "500 POST /api/checkout", "500 POST /api/checkout?retry=1"
    )
    assert not bench.signatures_match("http_5xx", "500 POST /api/checkout", "500 POST /api/login")


def test_b3_deadtap_matches_on_the_rid_only():
    assert bench.signatures_match("deadtap", "deadtap:btn_apply_coupon", "btn_apply_coupon")


def test_b4_ui_pair_is_sorted_before_comparison():
    assert bench.signatures_match(
        "ui_broken",
        "overlap:tv_product_title,tv_product_price",
        "overlap:tv_product_price,tv_product_title",
    )


def test_b5_signature_matches_the_expected_entry():
    entry = GROUND_TRUTH["expected"][4]
    assert bench.signatures_match("crash", entry["signature"], B5_FLAKY["signature"])


# -- scoring ---------------------------------------------------------------
def test_perfect_run_scores_four_of_four_with_b5_flaky():
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4, B5_FLAKY])
    assert score.recall == 4
    assert score.b5 == "flaky"
    assert score.false_positives == []
    assert score.line == "run 1  confirmed 4/4  B5 flaky  FP 0"


def test_b5_not_observed_is_n_a_and_still_a_full_score():
    score = bench.score_run(2, GROUND_TRUTH, [B1, B2, B3, B4])
    assert score.recall == 4
    assert score.b5 == "N/A"
    assert score.line == "run 2  confirmed 4/4  B5 N/A  FP 0"


def test_confirmed_b5_is_a_false_positive():
    confirmed_b5 = dict(B5_FLAKY, verdict="confirmed")
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4, confirmed_b5])
    assert score.recall == 4
    assert score.b5 == "confirmed"
    assert len(score.false_positives) == 1
    assert score.false_positives[0]["signature"] == confirmed_b5["signature"]


def test_an_extra_confirmed_finding_is_a_false_positive():
    extra = finding("http_5xx", "502 GET /api/products", "confirmed")
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4, extra])
    assert score.recall == 4
    assert [item["signature"] for item in score.false_positives] == ["502 GET /api/products"]


def test_a_missing_bug_lowers_recall():
    score = bench.score_run(1, GROUND_TRUTH, [B1, B3, B4])
    assert score.recall == 3
    assert score.missed == ["B2"]


def test_flaky_b1_does_not_count_as_recall():
    flaky_b1 = dict(B1, verdict="flaky")
    score = bench.score_run(1, GROUND_TRUTH, [flaky_b1, B2, B3, B4])
    assert score.recall == 3
    assert score.missed == ["B1"]


def test_filtered_findings_are_not_false_positives():
    filtered = finding("pinning", "pinning:10.0.2.2:8100", "filtered", "heuristic")
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4, filtered])
    assert score.false_positives == []
    assert [item["kind"] for item in score.filtered] == ["pinning"]


def test_one_finding_cannot_satisfy_two_expected_bugs():
    # B1 and B5 differ only by message; a single confirmed crash is one bug.
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4, dict(B1, verdict="flaky")])
    assert score.recall == 4
    assert score.b5 == "N/A"


# -- result payload --------------------------------------------------------
def test_score_serializes_for_the_results_json():
    score = bench.score_run(1, GROUND_TRUTH, [B1, B2, B3, B4])
    payload = score.to_dict()
    for key in ("index", "recall", "total", "b5", "false_positives", "confirmed", "run_id"):
        assert key in payload
    assert payload["recall"] == 4


def test_results_directory_is_under_benchmark():
    assert bench.RESULTS_DIR == REPO_ROOT / "benchmark" / "results"


def _fixture_apk_on_disk() -> bool:
    """True when the fixture's *local* APK exists. Never touches the network."""
    try:
        local = (bench.load_fixture("bazaar").get("apk") or {}).get("local")
    except FileNotFoundError:
        return False
    if not local:
        return False
    candidate = Path(local)
    if not candidate.is_absolute():
        candidate = (bench.REPO_ROOT / candidate).resolve()
    return candidate.is_file()


@pytest.mark.skipif(
    not _fixture_apk_on_disk(),
    reason="fixture checkout is not on this machine (E:\\bazaar); the APK is a fixture dependency",
)
def test_the_bazaar_fixture_apk_is_available_on_this_machine():
    fixture = bench.load_fixture("bazaar")
    apk = bench.resolve_apk(fixture)
    assert apk.is_file(), apk
    assert apk.name == "app-debug.apk"


def test_a_local_apk_wins_over_the_release_url(tmp_path, monkeypatch):
    """Resolution is local-path-first, and the path is relative to the repo root."""
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"PK\x03\x04 local build")
    monkeypatch.setattr(bench, "REPO_ROOT", tmp_path)
    fixture = {
        "name": "bazaar",
        "apk": {"local": "app-debug.apk", "url": "https://invalid.test/x.apk"},
    }
    assert bench.resolve_apk(fixture) == apk.resolve()


def test_a_fixture_with_neither_path_nor_url_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        bench.resolve_apk({"name": "nowhere", "apk": {}})


def test_the_bazaar_fixture_declares_both_a_path_and_a_url():
    apk = bench.load_fixture("bazaar")["apk"]
    assert apk["local"].endswith("app-debug.apk")
    assert apk["url"].startswith("https://")


@pytest.mark.parametrize("kind", ["crash", "http_5xx", "deadtap", "ui_broken"])
def test_normalized_signatures_are_idempotent(kind):
    signature = {
        "crash": "java.lang.NullPointerException:Integer.intValue",
        "http_5xx": "500 POST /api/checkout",
        "deadtap": "deadtap:btn_apply_coupon",
        "ui_broken": "overlap:tv_product_title,tv_product_price",
    }[kind]
    once = bench.normalize_signature(kind, signature)
    assert bench.normalize_signature(kind, once) == once
