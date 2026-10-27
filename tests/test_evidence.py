"""Evidence bundles and the generated standalone repro script (PLAN §8)."""

from __future__ import annotations

import json
import py_compile

from litmus.detectors.base import Finding
from litmus.worker.evidence import write_bundle
from litmus.worker.policy import Action, StepRecord
from litmus.worker.verify import AttemptResult, Verification

RUN_META = {
    "run_id": "20261027-120000-abcd12",
    "apk": "E:/bazaar/app-debug.apk",
    "profile": "profiles/bazaar.yaml",
    "started_at": "2026-10-27T12:00:00+00:00",
}
DEVICE = {"serial": "emulator-5554", "api_level": "33", "screen_size": [720, 1280]}
APP = {"package": "com.bazaar.demo", "version_name": "1.0.3"}


def sample_steps() -> list[StepRecord]:
    email = Action(
        kind="input",
        selectors=[
            {"level": "resource_id", "value": "com.bazaar.demo:id/et_email", "occurrence": 0},
            {"level": "path", "value": "/android.widget.FrameLayout[0]", "occurrence": 0},
        ],
        text="qa@bazaar.dev",
        target="et_email",
    )
    add_cart = Action(
        kind="tap",
        selectors=[
            {"level": "resource_id", "value": "com.bazaar.demo:id/btn_add_cart", "occurrence": 0},
            {"level": "bounds", "value": [32, 1104, 688, 1200], "occurrence": 0},
        ],
        target="btn_add_cart",
    )
    return [
        StepRecord(idx=0, action=email, state_before="hash-login", state_after="hash-login"),
        StepRecord(idx=1, action=add_cart, state_before="hash-product", state_after="hash-product"),
    ]


def sample_finding() -> Finding:
    return Finding(
        local_id="F1",
        kind="crash",
        signature="java.lang.NullPointerException:Integer.intValue",
        fingerprint="java.lang.NullPointerException@com.bazaar.demo.CartStore.add",
        confidence="hard",
        screen=".ProductActivity",
        step_idx=1,
        detail="Attempt to invoke virtual method 'int java.lang.Integer.intValue()'",
        observed_once=True,
        log_text="E/AndroidRuntime: FATAL EXCEPTION: main",
    )


def test_bundle_writes_required_files(tmp_path) -> None:
    finding = sample_finding()
    steps = sample_steps()
    verification = Verification(
        finding=finding, verdict="confirmed", valid_attempts=3, confirmations=3
    )
    verification.attempts = [AttemptResult(attempt=1, valid=True, reproduced=True)]

    bundle = write_bundle(
        run_dir=tmp_path,
        finding=finding,
        steps=steps,
        verification=verification,
        run_meta=RUN_META,
        device_info=DEVICE,
        app_info=APP,
        package="com.bazaar.demo",
        component="com.bazaar.demo/.LoginActivity",
    )

    assert bundle.trace_path.is_file()
    assert bundle.repro_path.is_file()
    assert bundle.logcat_path.is_file()
    assert bundle.manifest_path.is_file()
    assert (bundle.finding_dir / "trace.json").is_file()


def test_trace_contains_the_selector_ladder(tmp_path) -> None:
    finding = sample_finding()
    verification = Verification(
        finding=finding, verdict="confirmed", valid_attempts=3, confirmations=3
    )
    bundle = write_bundle(
        tmp_path, finding, sample_steps(), verification, RUN_META, DEVICE, APP,
        "com.bazaar.demo", "com.bazaar.demo/.LoginActivity",
    )
    trace = json.loads(bundle.trace_path.read_text(encoding="utf-8"))
    assert trace["trigger_step"] == 1
    assert trace["steps"][0]["action"]["kind"] == "input"
    assert trace["steps"][0]["action"]["selectors"][0]["level"] == "resource_id"
    assert trace["steps"][1]["state_before"] == "hash-product"


def test_generated_repro_is_valid_python(tmp_path) -> None:
    finding = sample_finding()
    verification = Verification(
        finding=finding, verdict="confirmed", valid_attempts=3, confirmations=3
    )
    bundle = write_bundle(
        tmp_path, finding, sample_steps(), verification, RUN_META, DEVICE, APP,
        "com.bazaar.demo", "com.bazaar.demo/.LoginActivity",
    )
    source = bundle.repro_path.read_text(encoding="utf-8")
    py_compile.compile(str(bundle.repro_path), doraise=True)
    assert 'PACKAGE = "com.bazaar.demo"' in source
    assert "import litmus" not in source
    assert "FINGERPRINT" in source
    assert "PASS:" in source
    # The embedded trace must be loadable without Python-literal quirks (null).
    namespace: dict = {}
    trace_block = source.split('TRACE = json.loads(r"""', 1)[1].split('""")', 1)[0]
    namespace["json"] = json
    assert json.loads(trace_block)[0]["action"]["kind"] == "input"


def test_manifest_records_verdict_and_hashes(tmp_path) -> None:
    finding = sample_finding()
    finding.verdict = "confirmed"
    verification = Verification(
        finding=finding, verdict="confirmed", valid_attempts=3, confirmations=3
    )
    bundle = write_bundle(
        tmp_path, finding, sample_steps(), verification, RUN_META, DEVICE, APP,
        "com.bazaar.demo", "com.bazaar.demo/.LoginActivity",
    )
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["verdict"] == "confirmed"
    assert manifest["summary"] == "confirmed 3/3"
    assert manifest["device"]["serial"] == "emulator-5554"
    trace_entry = next(item for item in manifest["artifacts"] if item["path"] == "trace.json")
    assert trace_entry["sha256"]


def test_repro_trace_stops_at_the_trigger(tmp_path) -> None:
    finding = sample_finding()
    extra = StepRecord(
        idx=2,
        action=Action(kind="key", keycode="4", target="back"),
        state_before="hash-after",
        state_after="hash-after",
    )
    verification = Verification(
        finding=finding, verdict="confirmed", valid_attempts=3, confirmations=3
    )
    steps = sample_steps() + [extra]
    bundle = write_bundle(
        tmp_path, finding, steps, verification, RUN_META, DEVICE, APP,
        "com.bazaar.demo", "com.bazaar.demo/.LoginActivity",
    )
    source = bundle.repro_path.read_text(encoding="utf-8")
    assert '"idx": 2' not in source
    trace = json.loads(bundle.trace_path.read_text(encoding="utf-8"))
    # trace.json keeps the full explore trace for humans; repro replays up to the trigger.
    assert len(trace["steps"]) == 3