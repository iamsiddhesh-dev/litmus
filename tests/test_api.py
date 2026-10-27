"""Control plane API: upload, run creation, SSE replay, report, cancel."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeStore
from httpx import ASGITransport, AsyncClient

from litmus.api.app import create_app
from litmus.api.artifacts import artifact_url, resolve_artifact
from litmus.api.deps import get_settings_dep
from litmus.config import get_settings
from litmus.ingest.apk import ApkInfo

APK_MIME = "application/vnd.android.package-archive"


@pytest.fixture
def settings(tmp_path: Path):
    return get_settings().model_copy(
        update={"artifacts_dir": tmp_path, "redis_url": "", "database_url": ""}
    )


def _target(artifacts_dir: Path, storage: Path | None = None) -> dict:
    apk = storage or (artifacts_dir / "uploads" / "tgt_1" / "app.apk")
    apk.parent.mkdir(parents=True, exist_ok=True)
    apk.write_bytes(b"PK\x03\x04 uploaded apk")
    return {
        "id": "tgt_1",
        "kind": "apk",
        "package": "com.bazaar.demo",
        "version": "1.0.3",
        "min_sdk": 21,
        "abis": "x86_64",
        "source": "upload",
        "storage_path": str(apk),
        "profile": "bazaar",
        "created_at": datetime.now(UTC),
    }


@pytest.fixture
async def client(settings, tmp_path: Path):
    store = FakeStore(targets={"tgt_1": _target(tmp_path)})
    app = create_app(settings)
    app.state.store = store
    app.dependency_overrides[get_settings_dep] = lambda: settings
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://litmus.test"
    ) as http:
        yield http, store


async def test_healthz_reports_database(client) -> None:
    http, _ = client
    response = await http.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["db"] == "up"
    assert body["status"] == "ok"


async def test_upload_registers_a_target(client, monkeypatch) -> None:
    http, store = client
    monkeypatch.setattr(
        "litmus.api.routes.targets.inspect_apk",
        lambda path, settings: ApkInfo(package="com.acme.app", version="2.0.0", min_sdk=24),
    )
    response = await http.post(
        "/v1/targets",
        files={"file": ("app-debug.apk", b"PK\x03\x04 payload", APK_MIME)},
        data={"profile": "generic"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["package"] == "com.acme.app"
    assert body["version"] == "2.0.0"
    assert body["profile"] == "generic"
    assert Path(body["storage_path"]).is_file()
    assert body["id"] in store.targets


async def test_upload_rejects_an_empty_file(client) -> None:
    http, _ = client
    response = await http.post(
        "/v1/targets", files={"file": ("empty.apk", b"", "application/octet-stream")}
    )
    assert response.status_code == 400


async def test_create_run_404_for_unknown_target(client) -> None:
    http, _ = client
    response = await http.post("/v1/runs", json={"target_id": "tgt_missing"})
    assert response.status_code == 404


async def test_create_run_400_when_the_apk_is_gone(client, monkeypatch, tmp_path) -> None:
    http, store = client
    store.targets["tgt_dead"] = _target(tmp_path, storage=tmp_path / "gone.apk")
    Path(store.targets["tgt_dead"]["storage_path"]).unlink(missing_ok=True)
    response = await http.post("/v1/runs", json={"target_id": "tgt_dead"})
    assert response.status_code == 400


async def test_create_run_persists_queued_event_before_dispatch(client, monkeypatch) -> None:
    http, store = client
    calls: list[dict] = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "arq"

    monkeypatch.setattr("litmus.api.routes.runs.enqueue_run", fake_enqueue)
    response = await http.post(
        "/v1/runs", json={"target_id": "tgt_1", "profile": "bazaar", "steps": 12, "verify": False}
    )
    assert response.status_code == 202
    body = response.json()
    run_id = body["run"]["id"]
    assert body["dispatch"] == "arq"
    assert store.runs[run_id]["status"] == "queued"
    assert store.runs[run_id]["step_budget"] == 12
    assert store.events[0]["type"] == "run.status"
    assert store.events[0]["payload_json"]["status"] == "queued"
    assert calls and calls[0]["run_id"] == run_id


async def test_create_run_picks_the_profile_from_the_package(client, monkeypatch) -> None:
    """An APK uploaded from the dashboard has no profile: the package decides."""
    http, store = client
    store.targets["tgt_1"]["profile"] = ""
    calls: list[dict] = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "inline"

    monkeypatch.setattr("litmus.api.routes.runs.enqueue_run", fake_enqueue)
    response = await http.post("/v1/runs", json={"target_id": "tgt_1"})
    assert response.status_code == 202
    assert calls[0]["profile"] == "bazaar"


async def test_create_run_falls_back_to_generic_for_an_unknown_package(client, monkeypatch) -> None:
    http, store = client
    store.targets["tgt_1"]["profile"] = ""
    store.targets["tgt_1"]["package"] = "com.example.nothing"
    calls: list[dict] = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "inline"

    monkeypatch.setattr("litmus.api.routes.runs.enqueue_run", fake_enqueue)
    response = await http.post("/v1/runs", json={"target_id": "tgt_1"})
    assert response.status_code == 202
    assert calls[0]["profile"] == "generic"


async def test_run_detail_reports_counts(client) -> None:
    http, store = client
    store.runs["run_1"] = {
        "id": "run_1",
        "target_id": "tgt_1",
        "status": "done",
        "step_budget": 25,
        "steps_taken": 7,
        "created_at": datetime.now(UTC),
    }
    store.findings["run_1:F1"] = {"id": "run_1:F1", "run_id": "run_1", "verdict": "confirmed"}
    response = await http.get("/v1/runs/run_1")
    assert response.status_code == 200
    body = response.json()
    assert body["target"]["package"] == "com.bazaar.demo"
    assert body["counts"] == {"confirmed": 1}


async def test_events_endpoint_replays_from_zero_and_from_a_cursor(client) -> None:
    http, store = client
    store.runs["run_1"] = {"id": "run_1", "target_id": "tgt_1", "status": "done"}
    await store.append_event("run_1", "run.status", {"status": "queued"})
    await store.append_event("run_1", "step", {"idx": 1, "action": "tap"})
    await store.append_event("run_1", "done", {"status": "done"})

    full = await http.get("/v1/runs/run_1/events?from_seq=0")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    assert full.text.count("event: ") == 3

    missed = await http.get("/v1/runs/run_1/events?from_seq=2")
    assert missed.text.count("event: ") == 1
    assert "event: done" in missed.text


async def test_events_endpoint_honours_last_event_id(client) -> None:
    http, store = client
    store.runs["run_1"] = {"id": "run_1", "target_id": "tgt_1", "status": "done"}
    await store.append_event("run_1", "run.status", {"status": "queued"})
    await store.append_event("run_1", "done", {"status": "done"})

    response = await http.get("/v1/runs/run_1/events", headers={"Last-Event-ID": "1"})
    assert response.text.count("event: ") == 1
    assert "event: done" in response.text


async def test_report_links_evidence_files(client, tmp_path) -> None:
    http, store = client
    store.runs["run_1"] = {
        "id": "run_1",
        "target_id": "tgt_1",
        "status": "done",
        "step_budget": 25,
        "steps_taken": 7,
        "started_at": datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 10, 27, 9, 2, tzinfo=UTC),
        "llm_calls": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    store.findings["run_1:F1"] = {
        "id": "run_1:F1",
        "run_id": "run_1",
        "kind": "crash",
        "signature": "java.lang.NullPointerException:Integer.intValue",
        "fingerprint": "fp",
        "screen": "ProductActivity",
        "first_step_idx": 6,
        "severity": "high",
        "confidence": "hard",
        "observed_once": True,
        "verdict": "confirmed",
    }
    store.verdicts.extend(
        {
            "finding_id": "run_1:F1",
            "attempt": attempt,
            "reproduced": True,
            "valid": True,
            "state_hash": "h",
            "evidence_path": None,
        }
        for attempt in (1, 2, 3)
    )
    shot = tmp_path / "run_1" / "F1" / "screen_at_bug.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG evidence")
    store.artifacts.append(
        {
            "id": "a1",
            "run_id": "run_1",
            "finding_id": "run_1:F1",
            "kind": "screen_at_bug.png",
            "path": str(shot),
            "bytes": 9,
            "sha256": "abc",
        }
    )

    response = await http.get("/v1/runs/run_1/report")
    assert response.status_code == 200
    report = response.json()
    assert report["summary"]["confirmed"] == 1
    entry = report["confirmed"][0]
    assert entry["confidence"] == "hard"
    assert entry["repro_rate"] == "3/3"
    assert entry["screenshot"] == "/artifacts/run_1/F1/screen_at_bug.png"
    assert entry["artifacts"][0]["url"] == "/artifacts/run_1/F1/screen_at_bug.png"


async def test_finding_detail_includes_verdicts(client, tmp_path) -> None:
    http, store = client
    store.findings["run_1:F1"] = {
        "id": "run_1:F1",
        "run_id": "run_1",
        "kind": "crash",
        "signature": "sig",
        "fingerprint": "fp",
        "confidence": "hard",
        "observed_once": True,
        "verdict": "confirmed",
    }
    store.verdicts.append(
        {"finding_id": "run_1:F1", "attempt": 1, "reproduced": True, "valid": True}
    )
    response = await http.get("/v1/findings/run_1:F1")
    assert response.status_code == 200
    body = response.json()
    assert body["repro_rate"] == "1/1"
    assert body["verdicts"][0]["reproduced"] is True


async def test_cancel_a_queued_run(client) -> None:
    http, store = client
    store.runs["run_1"] = {"id": "run_1", "target_id": "tgt_1", "status": "queued"}
    response = await http.post("/v1/runs/run_1/cancel")
    assert response.status_code == 200
    assert store.runs["run_1"]["status"] == "cancelled"
    assert store.events[-1]["type"] == "run.status"
    assert store.events[-1]["payload_json"]["status"] == "cancelled"


async def test_cancel_a_finished_run_is_a_conflict(client) -> None:
    http, store = client
    store.runs["run_1"] = {"id": "run_1", "target_id": "tgt_1", "status": "done"}
    response = await http.post("/v1/runs/run_1/cancel")
    assert response.status_code == 409


async def test_dashboard_renders_runs_server_side(client) -> None:
    http, store = client
    store.runs["run_1"] = {
        "id": "run_1",
        "target_id": "tgt_1",
        "status": "done",
        "step_budget": 25,
        "steps_taken": 7,
        "created_at": datetime.now(UTC),
    }
    response = await http.get("/")
    assert response.status_code == 200
    assert "litmus" in response.text
    assert "run_1" in response.text
    assert "/static/app.js" in response.text


def test_artifact_lookup_cannot_escape_the_root(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("evidence")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")

    assert resolve_artifact("ok.txt", tmp_path) is not None
    assert resolve_artifact("../outside.txt", tmp_path) is None
    assert resolve_artifact("..%2F..%2Fetc%2Fpasswd", tmp_path) is None
    assert resolve_artifact("", tmp_path) is None
    assert artifact_url(outside, tmp_path) is None