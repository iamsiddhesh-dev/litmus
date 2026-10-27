"""MCP tool ``run_test``: returns a run id without waiting for the verdict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litmus import mcp_server
from litmus.config import get_settings
from litmus.mcp_server import (
    TOOLS,
    Dispatch,
    DispatchError,
    api_base,
    get_report,
    get_run,
    run_test,
)


@pytest.fixture
def settings(tmp_path: Path):
    return get_settings().model_copy(
        update={"artifacts_dir": tmp_path, "api_host": "127.0.0.1", "api_port": 9}
    )


@pytest.fixture
def apk(tmp_path: Path) -> Path:
    path = tmp_path / "app-debug.apk"
    path.write_bytes(b"PK\x03\x04 fake apk")
    return path


# -- dispatch --------------------------------------------------------------
def test_run_test_returns_the_run_id_immediately(monkeypatch, apk, settings):
    calls: list[dict] = []

    monkeypatch.setattr(mcp_server, "api_up", lambda base, s: True)

    def fake_submit(apk_path, profile, steps, verify, base, token="", timeout_s=60.0):
        calls.append(
            {"apk": str(apk_path), "profile": profile, "steps": steps, "verify": verify}
        )
        return Dispatch(
            run_id="run_20260101-000000-abcdef",
            status="queued",
            mode="api",
            report_url="http://127.0.0.1:9/runs/run_20260101-000000-abcdef",
            target_id="tgt_1",
        )

    monkeypatch.setattr(mcp_server, "submit_to_api", fake_submit)

    result = run_test(str(apk), profile="bazaar", steps=15, settings=settings)

    assert result["run_id"] == "run_20260101-000000-abcdef"
    assert result["status"] == "queued"
    assert result["mode"] == "api"
    assert result["report_url"].endswith("/runs/run_20260101-000000-abcdef")
    assert calls == [{"apk": str(apk), "profile": "bazaar", "steps": 15, "verify": True}]


def test_run_test_falls_back_to_a_local_run_when_the_api_is_down(monkeypatch, apk, settings):
    started: list[dict] = []

    monkeypatch.setattr(mcp_server, "api_up", lambda base, s: False)

    def fake_local(apk_path, profile, steps, verify, settings=None, run_id=None):
        started.append({"apk": str(apk_path), "profile": profile, "steps": steps})
        return Dispatch(run_id="run_local_1", status="queued", mode="local")

    monkeypatch.setattr(mcp_server, "launch_local", fake_local)

    result = run_test(str(apk), profile="bazaar", steps=5, settings=settings)

    assert result["run_id"] == "run_local_1"
    assert result["mode"] == "local"
    assert started == [{"apk": str(apk), "profile": "bazaar", "steps": 5}]


def test_a_failed_dispatch_still_starts_the_run_locally(monkeypatch, apk, settings):
    monkeypatch.setattr(mcp_server, "api_up", lambda base, s: True)

    def boom(*args, **kwargs):
        raise DispatchError("target upload failed: 500")

    monkeypatch.setattr(mcp_server, "submit_to_api", boom)
    monkeypatch.setattr(
        mcp_server,
        "launch_local",
        lambda *a, **k: Dispatch(run_id="run_local_2", status="queued", mode="local"),
    )

    result = run_test(str(apk), settings=settings)
    assert result["run_id"] == "run_local_2"
    assert result["mode"] == "local"


def test_a_missing_apk_is_rejected(settings):
    with pytest.raises(FileNotFoundError):
        run_test("no-such.apk", settings=settings)


def test_steps_must_be_positive(apk, settings):
    with pytest.raises(ValueError):
        run_test(str(apk), steps=0, settings=settings)


def test_api_base_prefers_the_explicit_override(settings):
    assert api_base(settings) == "http://127.0.0.1:9"
    assert api_base(settings, "http://host:1234/") == "http://host:1234"


# -- reading a run ---------------------------------------------------------
def test_get_run_reads_the_artifacts_when_the_api_is_down(monkeypatch, settings):
    monkeypatch.setattr(mcp_server, "api_up", lambda base, s: False)
    run_dir = Path(settings.artifacts_dir) / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"status": "done", "findings": []}), "utf-8")

    payload = get_run("run_1", settings=settings)
    assert payload["run"]["status"] == "done"
    assert payload["source"] == "artifacts"


def test_get_report_degrades_to_the_run_when_the_api_is_down(monkeypatch, settings):
    monkeypatch.setattr(mcp_server, "api_up", lambda base, s: False)
    assert get_report("run_missing", settings=settings)["run"]["status"] == "unknown"


# -- registration ----------------------------------------------------------
def test_run_test_is_the_registered_tool():
    assert "run_test" in TOOLS
    assert TOOLS["run_test"] is run_test
    # The documented arguments are part of the contract an agent sees.
    import inspect

    signature = inspect.signature(run_test)
    for name in ("apk_path", "profile", "steps", "verify"):
        assert name in signature.parameters


def test_the_fastmcp_server_registers_every_tool():
    pytest.importorskip("fastmcp")
    import asyncio

    server = mcp_server.build_server()
    listing = server.list_tools()
    if hasattr(listing, "__await__"):
        listing = asyncio.run(listing)
    assert set(TOOLS).issubset({tool.name for tool in listing})
