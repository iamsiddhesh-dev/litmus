"""Replay probes per finding kind, driven by a stub device (no emulator).

The crash path is covered by the M1 tests; these cover the M3 additions:
network flows, dead taps and layout signatures, plus one end-to-end
``verify_finding`` for a symptom visible on the entry screen.
"""

from __future__ import annotations

from pathlib import Path

from litmus.detectors.base import Finding
from litmus.detectors.deadtap import node_content_hash, screen_content_hash
from litmus.detectors.network import Flow, NetworkDetector
from litmus.detectors.ui import UIScanner
from litmus.runner.driver import Screen
from litmus.worker.verify import ReplayContext, _poll_deadtap, _poll_network, _poll_ui
from tests.ui_fixtures import BAZAAR, cart_screen, node, product_screen, screen

UI_SIGNATURE = "overlap:tv_product_price,tv_product_title"


class StubDriver:
    """The device primitives the replay loop calls, with scripted answers."""

    package = BAZAAR

    def __init__(self, screens: list[Screen], *, ime: bool = False, toast: bool = False):
        self.screens = screens
        self.index = 0
        self.ime = ime
        self.toast = toast
        self.logs = {"crash": "", "main": ""}
        self.executed: list = []
        self.cleared = 0

    def dump(self) -> Screen:
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, action, screen=None, component="") -> str | None:
        self.executed.append(action)
        self.index = min(self.index + 1, len(self.screens) - 1)
        return None

    def logcat(self, buffer: str = "crash", lines: int | None = None) -> str:
        return self.logs.get(buffer, "")

    def clear_logcat(self, buffer: str = "crash") -> None:
        self.logs[buffer] = ""

    def force_stop(self, package: str) -> None:
        return None

    def hide_ime(self, force: bool = False) -> bool:
        return False

    def clear_app(self, package: str) -> None:
        self.cleared += 1

    def launch(self, component: str) -> None:
        return None

    def keyevent(self, keycode) -> None:
        return None

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n")
        return path

    def ime_shown(self) -> bool:
        return self.ime

    def toast_visible(self) -> bool:
        return self.toast


class StubProxy:
    def __init__(self, batches: list[list[Flow]] | None = None):
        self.batches = batches or []
        self.calls = 0

    def offset(self) -> int:
        return 0

    def flows_since(self, offset: int) -> list[Flow]:
        batch = self.batches[min(self.calls, len(self.batches) - 1)] if self.batches else []
        self.calls += 1
        return batch


# -- ui --------------------------------------------------------------------
def test_ui_probe_reproduces_b4():
    driver = StubDriver([product_screen()])
    scanner = UIScanner(BAZAAR, density_scale=2.0)
    reproduced, detail = _poll_ui(driver, scanner, UI_SIGNATURE, timeout_ms=300)
    assert reproduced
    assert detail == UI_SIGNATURE


def test_ui_probe_reports_absent_signature():
    driver = StubDriver([cart_screen()])
    scanner = UIScanner(BAZAAR, density_scale=2.0)
    reproduced, detail = _poll_ui(driver, scanner, UI_SIGNATURE, timeout_ms=300)
    assert not reproduced
    assert "not present" in detail


def test_verify_finding_confirms_an_entry_screen_layout_bug(tmp_path):
    from litmus.worker.verify import verify_finding

    driver = StubDriver([product_screen()])
    entry_hash = product_screen().state_hash(BAZAAR)
    finding = Finding(
        local_id="F1",
        kind="ui_broken",
        signature=UI_SIGNATURE,
        fingerprint=UI_SIGNATURE,
        confidence="heuristic",
        probe={"signature": UI_SIGNATURE, "start_hash": entry_hash},
    )
    verification = verify_finding(
        finding=finding,
        driver=driver,
        package=BAZAAR,
        component="com.bazaar.demo/.ProductActivity",
        steps=[],
        workdir=tmp_path,
        required=3,
        max_tries=3,
        inter_attempt_delay_ms=0,
        ctx=ReplayContext(ui=UIScanner(BAZAAR, density_scale=2.0)),
    )
    assert verification.verdict == "confirmed"
    assert verification.confirmations == 3
    assert verification.valid_attempts == 3


# -- network ---------------------------------------------------------------
def test_network_probe_reproduces_b2_from_a_new_flow():
    proxy = StubProxy([[Flow(status=500, method="POST", path="/api/checkout",
                             host="10.0.2.2", port=8100)]])
    reproduced, detail = _poll_network(
        proxy, NetworkDetector(), "500 POST /api/checkout", 0, timeout_ms=300
    )
    assert reproduced
    assert "500 POST /api/checkout" in detail


def test_network_probe_ignores_a_200():
    proxy = StubProxy([[Flow(status=200, method="POST", path="/api/checkout",
                             host="10.0.2.2", port=8100)]])
    reproduced, detail = _poll_network(
        proxy, NetworkDetector(), "500 POST /api/checkout", 0, timeout_ms=300
    )
    assert not reproduced
    assert detail == "no matching flow"


# -- dead tap --------------------------------------------------------------
def _dead_probe(screen_before: Screen, screen_after: Screen | None = None):
    target = screen_before.by_resource_id(f"{BAZAAR}:id/btn_apply_coupon")[0]
    return {
        "rid": "btn_apply_coupon",
        "nav_hash": screen_before.state_hash(BAZAAR),
        "screen_hash": screen_content_hash(screen_before, BAZAAR),
        "content_hash": node_content_hash(target),
    }, screen_after or screen_before


def test_deadtap_probe_reproduces_b3():
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    driver = StubDriver([after])
    reproduced, detail = _poll_deadtap(driver, None, probe, 0, timeout_ms=300)
    assert reproduced, detail


def test_deadtap_probe_fails_when_the_screen_changed():
    before = cart_screen(dead=True)
    probe, _ = _dead_probe(before)
    driver = StubDriver([cart_screen(dead=False)])
    reproduced, detail = _poll_deadtap(driver, None, probe, 0, timeout_ms=300)
    assert not reproduced
    assert "screen content changed" in detail


def test_deadtap_probe_fails_when_a_toast_appears():
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    driver = StubDriver([after], toast=True)
    reproduced, detail = _poll_deadtap(driver, None, probe, 0, timeout_ms=300)
    assert not reproduced
    assert "Toast" in detail


def test_deadtap_probe_fails_when_the_ime_appears():
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    driver = StubDriver([after], ime=True)
    reproduced, detail = _poll_deadtap(driver, None, probe, 0, timeout_ms=300)
    assert not reproduced
    assert "IME" in detail


def test_deadtap_probe_fails_when_a_request_leaves_the_app():
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    proxy = StubProxy([[Flow(status=200, method="POST", path="/api/coupon")]])
    driver = StubDriver([after])
    reproduced, detail = _poll_deadtap(driver, proxy, probe, 0, timeout_ms=300)
    assert not reproduced
    assert "network" in detail


def test_deadtap_probe_ignores_a_background_request_from_earlier_in_the_replay():
    # The catalog's GET /api/products can land late; it is not caused by the tap.
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    proxy = StubProxy([[Flow(status=200, method="GET", path="/api/products")]])
    driver = StubDriver([after])
    reproduced, detail = _poll_deadtap(
        driver,
        proxy,
        probe,
        0,
        timeout_ms=300,
        background=frozenset({"GET /api/products"}),
    )
    assert reproduced, detail


def test_deadtap_probe_ignores_system_traffic():
    # The emulator's own connectivity probe lands in the tap window constantly;
    # it is out of the profile's host scope, so it is not an effect of the tap.
    before = cart_screen(dead=True)
    probe, after = _dead_probe(before)
    proxy = StubProxy(
        [
            [
                Flow(
                    status=204,
                    method="GET",
                    path="/generate_204",
                    host="connectivitycheck.gstatic.com",
                    port=80,
                ),
                Flow(host="play.googleapis.com", port=443, tls_error=True, error="CA rejected"),
            ]
        ]
    )
    driver = StubDriver([after])
    reproduced, detail = _poll_deadtap(
        driver,
        proxy,
        probe,
        0,
        timeout_ms=300,
        network=NetworkDetector(allowed_hosts=["10.0.2.2:8100"]),
    )
    assert reproduced, detail


def test_deadtap_probe_fails_when_the_node_vanished():
    before = cart_screen(dead=True)
    probe, _ = _dead_probe(before)
    gone = screen(node("com.bazaar.demo:id/cart_root", bounds="[0,128][720,1280]"))
    driver = StubDriver([gone])
    reproduced, detail = _poll_deadtap(driver, None, probe, 0, timeout_ms=300)
    assert not reproduced
    assert detail
