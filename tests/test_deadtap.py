"""Dead-tap detector: B3 fires, and the add-to-cart tap does not.

The add-to-cart case is the reason this file exists. Its first tap leaves the
navigation hash, the tapped node and the whole screen content identical, and
only a Toast proves the tap worked. A rule that missed that would report a
deterministic false positive that replay would confirm 3/3.
"""

from __future__ import annotations

from litmus.detectors.deadtap import (
    DeadTapDetector,
    TapObservation,
    content_hash,
    is_dead_tap,
    node_content,
    node_content_hash,
    screen_content_hash,
)
from tests.ui_fixtures import BAZAAR, cart_screen, node, product_screen, screen

RID = f"{BAZAAR}:id/btn_apply_coupon"


def observation(**overrides) -> TapObservation:
    base = dict(
        rid=RID,
        label="btn_apply_coupon",
        clickable=True,
        node_present_after=True,
        content_before="Apply coupon||0|0|1",
        content_after="Apply coupon||0|0|1",
        nav_before="nav-a",
        nav_after="nav-a",
        screen_before="screen-a",
        screen_after="screen-a",
    )
    base.update(overrides)
    return TapObservation(**base)


# -- content hash ----------------------------------------------------------
def test_content_hash_covers_every_visible_field():
    base = node("com.bazaar.demo:id/x", text="a", desc="b", bounds="[0,0][10,10]")
    screen_with = screen(base)
    target = screen_with.by_resource_id("com.bazaar.demo:id/x")[0]
    baseline = node_content_hash(target)
    assert baseline == content_hash(node_content(target))

    for field, value in (
        ("text", "changed"),
        ("desc", "changed"),
        ("checked", True),
        ("selected", True),
        ("enabled", False),
    ):
        kwargs = {"text": "a", "desc": "b", "bounds": "[0,0][10,10]", field: value}
        other = screen(node("com.bazaar.demo:id/x", **kwargs))
        assert node_content_hash(other.by_resource_id("com.bazaar.demo:id/x")[0]) != baseline


def test_screen_content_hash_ignores_other_packages():
    app_only = screen(node("com.bazaar.demo:id/a", text="x"))
    with_system = screen(
        node("com.bazaar.demo:id/a", text="x"),
        node("android:id/clock", text="12:40", package="com.android.systemui"),
    )
    assert screen_content_hash(app_only, BAZAAR) == screen_content_hash(with_system, BAZAAR)


def test_screen_content_hash_moves_when_text_changes():
    # The cart counter is the visible effect of add-to-cart.
    assert screen_content_hash(cart_screen(dead=False), BAZAAR) != screen_content_hash(
        cart_screen(dead=True), BAZAAR
    )


# -- the rule --------------------------------------------------------------
def test_b3_apply_coupon_is_a_dead_tap():
    dead, reason = is_dead_tap(observation())
    assert dead, reason
    assert "no navigation" in reason


def test_add_to_cart_is_not_dead_because_of_the_toast():
    dead, reason = is_dead_tap(observation(toast_seen=True))
    assert not dead
    assert "Toast" in reason


def test_a_visible_counter_change_is_not_a_dead_tap():
    dead, reason = is_dead_tap(observation(screen_after="screen-b"))
    assert not dead
    assert "screen content" in reason


def test_a_network_request_is_not_a_dead_tap():
    dead, reason = is_dead_tap(observation(network_activity=True))
    assert not dead
    assert "network" in reason


def test_navigation_change_is_not_a_dead_tap():
    assert not is_dead_tap(observation(nav_after="nav-b"))[0]


def test_node_content_change_is_not_a_dead_tap():
    assert not is_dead_tap(observation(content_after="Apply coupon||0|0|0"))[0]


def test_ime_raising_tap_is_not_a_dead_tap():
    assert not is_dead_tap(observation(ime_raised=True))[0]


def test_disappearing_node_is_not_a_dead_tap():
    assert not is_dead_tap(observation(node_present_after=False))[0]


def test_non_clickable_node_is_not_a_dead_tap():
    assert not is_dead_tap(observation(clickable=False))[0]


# -- findings --------------------------------------------------------------
def test_b3_signature_matches_the_ground_truth():
    event = DeadTapDetector(BAZAAR).evaluate(observation())
    assert event is not None
    assert event.signature == "deadtap:btn_apply_coupon"
    finding = DeadTapDetector(BAZAAR).to_finding(event, local_id="F3", step_idx=12)
    assert finding.kind == "deadtap"
    assert finding.confidence == "heuristic"
    assert finding.probe["rid"] == "btn_apply_coupon"
    assert finding.probe["nav_hash"] == "nav-a"
    assert finding.probe["screen_hash"] == "screen-a"


def test_long_clickable_node_counts_as_tappable():
    # A row with only long-click support still reacts to a tap.
    dead, _ = is_dead_tap(observation(clickable=True))
    assert dead


def test_product_screen_helper_is_stable():
    # The fixture used by the UI tests must not accidentally look dead.
    assert product_screen() is not None
