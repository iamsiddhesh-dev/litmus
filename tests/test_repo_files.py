"""Tests for the repo-file persistence layer (profiles, fixtures, ground truth)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_generic_profile_is_valid_yaml() -> None:
    profile = yaml.safe_load((REPO_ROOT / "profiles" / "generic.yaml").read_text(encoding="utf-8"))
    assert profile["profile_name"] == "generic"
    assert profile["limits"]["steps"] == 25
    assert profile["verify"]["attempts"] == 3


def test_bazaar_fixture_points_at_a_profile_that_exists() -> None:
    fixture = yaml.safe_load((REPO_ROOT / "fixtures" / "bazaar.yaml").read_text(encoding="utf-8"))
    assert fixture["name"] == "bazaar"
    assert fixture["package"] == "com.bazaar.demo"
    assert (REPO_ROOT / fixture["profile"]).is_file()


def test_ground_truth_lists_b1_to_b5() -> None:
    data = json.loads(
        (REPO_ROOT / "benchmark" / "expected_findings.json").read_text(encoding="utf-8")
    )
    ids = [item["id"] for item in data["expected"]]
    assert ids == ["B1", "B2", "B3", "B4", "B5"]
    assert data["expected"][4]["expected_verdict"] == "flaky"


def test_schema_declares_every_plan_table() -> None:
    schema = (REPO_ROOT / "litmus" / "db" / "schema.sql").read_text(encoding="utf-8")
    for table in (
        "runs",
        "targets",
        "steps",
        "findings",
        "verdicts",
        "artifacts",
        "events",
        "workers",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
