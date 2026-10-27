"""ANR and environment-signal detectors."""

from __future__ import annotations

from litmus.detectors.anr import AnrDetector, parse_anrs
from litmus.detectors.environment import (
    PINNING,
    UNSUPPORTED_ABI,
    classify_environment,
    filtered_finding,
    find_environment_signal,
)

PKG = "com.bazaar.demo"

ANR_LOG = (
    "10-27 12:00:00.000  1234  1260 E ActivityManager: "
    "ANR in com.bazaar.demo (com.bazaar.demo/.CartActivity)\n"
    "10-27 12:00:00.000  1234  1260 E ActivityManager: "
    "Reason: Input dispatching timed out (Waiting to send key event)\n"
    "10-27 12:00:00.000  1234  1260 I ActivityManager: "
    "Killing 1234:com.bazaar.demo/u0a157\n"
)


# -- ANR -------------------------------------------------------------------
def test_anr_for_the_app_is_parsed_with_its_reason():
    events = parse_anrs(ANR_LOG, PKG)
    assert len(events) == 1
    assert events[0].signature == "ANR in com.bazaar.demo"
    assert "Input dispatching timed out" in events[0].reason


def test_anr_from_another_package_is_ignored():
    log = ANR_LOG.replace("com.bazaar.demo", "com.android.systemui")
    assert parse_anrs(log, PKG) == []


def test_anr_is_deduplicated():
    assert len(parse_anrs(ANR_LOG + ANR_LOG, PKG)) == 1


def test_anr_finding_is_hard():
    event = parse_anrs(ANR_LOG, PKG)[0]
    finding = AnrDetector(PKG).to_finding(event, local_id="F5", step_idx=3)
    assert finding.kind == "anr"
    assert finding.confidence == "hard"
    assert finding.fingerprint.startswith("anr:com.bazaar.demo")


def test_no_anr_in_a_clean_log():
    assert parse_anrs("I/BAZAAR_NET: 200 GET /api/products", PKG) == []


# -- environment -----------------------------------------------------------
def test_abi_install_failure_is_an_environment_signal():
    assert classify_environment("Failure [INSTALL_FAILED_NO_MATCHING_ABIS]") == UNSUPPORTED_ABI
    assert classify_environment("java.lang.UnsatisfiedLinkError: no libfoo.so") == (
        UNSUPPORTED_ABI
    )


def test_tls_failure_is_a_pinning_signal():
    assert classify_environment("javax.net.ssl.SSLPeerUnverifiedException: bad cert") == PINNING
    assert classify_environment("TLS handshake failed") == PINNING


def test_unrelated_text_is_not_an_environment_signal():
    assert classify_environment("I/BAZAAR_NET: 200 GET /api/products") is None


def test_log_scan_is_scoped_to_the_app_package():
    other = "E/Foo: java.lang.UnsatisfiedLinkError in com.other.app"
    assert find_environment_signal(other, PKG) is None
    mine = f"E/Foo: {PKG} java.lang.UnsatisfiedLinkError"
    assert find_environment_signal(mine, PKG)[0] == UNSUPPORTED_ABI


def test_filtered_findings_never_reach_the_confirmed_lane():
    finding = filtered_finding(UNSUPPORTED_ABI, "unsupported_abi:com.bazaar.demo", "F9")
    assert finding.verdict == "filtered"
    assert finding.confidence == "heuristic"
    assert finding.probe["filtered"] is True
