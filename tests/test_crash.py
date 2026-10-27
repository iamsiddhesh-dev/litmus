"""Crash parsing and fingerprinting (PLAN §7, contract §3.2)."""

# ruff: noqa: E501 - logcat fixtures are literal, one-line-bounded device output

from __future__ import annotations

from litmus.detectors.crash import parse_crashes

B1_LOG = """--------- beginning of crash
E/AndroidRuntime( 9503): FATAL EXCEPTION: main
E/AndroidRuntime( 9503): Process: com.bazaar.demo, PID: 9503
E/AndroidRuntime( 9503): java.lang.NullPointerException: Attempt to invoke virtual method \
'int java.lang.Integer.intValue()' on a null object reference
E/AndroidRuntime( 9503): \tat com.bazaar.demo.CartStore.add(CartStore.java:41)
E/AndroidRuntime( 9503): \tat com.bazaar.demo.ProductActivity.lambda$onCreate$0(ProductActivity.java:28)
E/AndroidRuntime( 9503): \tat android.view.View.performClick(View.java:7542)
"""

B5_LOG = """E/AndroidRuntime( 1234): FATAL EXCEPTION: main
E/AndroidRuntime( 1234): Process: com.bazaar.demo, PID: 1234
E/AndroidRuntime( 1234): java.lang.NullPointerException: express checkout: session token missing
E/AndroidRuntime( 1234): \tat com.bazaar.demo.CatalogActivity.lambda$onCreate$1(CatalogActivity.java:33)
"""

THREADTIME_LOG = """10-26 16:00:00.123  9503  9503 E AndroidRuntime: FATAL EXCEPTION: main
10-26 16:00:00.123  9503  9503 E AndroidRuntime: Process: com.bazaar.demo, PID: 9503
10-26 16:00:00.123  9503  9503 E AndroidRuntime: java.lang.IllegalStateException: boom
10-26 16:00:00.123  9503  9503 E AndroidRuntime: \tat com.bazaar.demo.CartActivity.onClick(CartActivity.java:9)
"""

OTHER_APP_LOG = """E/AndroidRuntime( 11): FATAL EXCEPTION: main
E/AndroidRuntime( 11): Process: com.android.systemui, PID: 11
E/AndroidRuntime( 11): java.lang.RuntimeException: system ui exploded
E/AndroidRuntime( 11): \tat com.android.systemui.Foo.bar(Foo.java:1)
"""


def test_b1_signature_matches_ground_truth() -> None:
    events = parse_crashes(B1_LOG, "com.bazaar.demo")
    assert len(events) == 1
    event = events[0]
    assert event.signature == "java.lang.NullPointerException:Integer.intValue"
    assert event.fingerprint == "java.lang.NullPointerException@com.bazaar.demo.CartStore.add"
    assert event.top_app_frame == "com.bazaar.demo.CartStore.add"


def test_b5_signature_keeps_free_form_message() -> None:
    events = parse_crashes(B5_LOG, "com.bazaar.demo")
    assert len(events) == 1
    assert (
        events[0].signature
        == "java.lang.NullPointerException:express checkout: session token missing"
    )


def test_other_packages_are_ignored() -> None:
    assert parse_crashes(OTHER_APP_LOG, "com.bazaar.demo") == []


def test_fingerprint_ignores_pid_and_line_numbers() -> None:
    same_bug = B1_LOG.replace("9503", "4242").replace("CartStore.java:41", "CartStore.java:57")
    first = parse_crashes(B1_LOG, "com.bazaar.demo")[0]
    second = parse_crashes(same_bug, "com.bazaar.demo")[0]
    assert first.fingerprint == second.fingerprint


def test_one_signature_is_one_finding_across_replays() -> None:
    log = B1_LOG + "\n" + B1_LOG.replace("9503", "7777")
    assert len(parse_crashes(log, "com.bazaar.demo")) == 1


def test_threadtime_format_is_parsed() -> None:
    events = parse_crashes(THREADTIME_LOG, "com.bazaar.demo")
    assert len(events) == 1
    assert events[0].exception == "java.lang.IllegalStateException"
    assert events[0].fingerprint.endswith("@com.bazaar.demo.CartActivity.onClick")


def test_empty_or_clean_log_has_no_crashes() -> None:
    assert parse_crashes("", "com.bazaar.demo") == []
    assert parse_crashes("I/BAZAAR_NET: 200 GET /api/products", "com.bazaar.demo") == []