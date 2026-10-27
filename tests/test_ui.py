"""UI detector: B4 fires once, and normal screens stay quiet.

The thresholds were checked against the real AVD hierarchy: B4 is
``tv_product_title`` (two lines, 22 sp) overlapped by ``tv_product_price``
(a -16 dp margin) for 656x32 px = 5248 dp² at 2x. Login, Catalog and Cart
produce zero events.
"""

from __future__ import annotations

from litmus.detectors.ui import UIScanner
from tests.ui_fixtures import BAZAAR, node, product_screen, screen

TITLE = "com.bazaar.demo:id/tv_product_title"
PRICE = "com.bazaar.demo:id/tv_product_price"


def scanner(**kwargs) -> UIScanner:
    return UIScanner(BAZAAR, density_scale=2.0, **kwargs)


def signatures(events) -> list[str]:
    return [event.signature for event in events]


# -- B4 --------------------------------------------------------------------
def test_b4_overlap_is_detected_with_the_ground_truth_pair():
    events = scanner().scan(product_screen())
    assert len(events) == 1
    assert events[0].signature == "overlap:tv_product_price,tv_product_title"
    # Sorted pair; the benchmark compares order-free.
    assert sorted(events[0].rids) == ["tv_product_price", "tv_product_title"]


def test_overlap_smaller_than_eight_dp_squared_is_ignored():
    # 8 dp² at 2x = 32 px². This pair shares 4x4 px = 16 px².
    tiny = screen(
        node(
            "com.bazaar.demo:id/card",
            cls="android.widget.LinearLayout",
            bounds="[0,0][720,400]",
            children=(
                node("com.bazaar.demo:id/tv_a", text="A", bounds="[0,0][100,40]")
                + node("com.bazaar.demo:id/tv_b", text="B", bounds="[96,36][300,100]")
            ),
        )
    )
    assert scanner().scan(tiny) == []


def test_overlap_exactly_at_the_threshold_is_ignored():
    # 8x4 px = 32 px² is not *more* than 8 dp².
    at_threshold = screen(
        node(
            "com.bazaar.demo:id/card",
            cls="android.widget.LinearLayout",
            bounds="[0,0][720,400]",
            children=(
                node("com.bazaar.demo:id/tv_a", text="A", bounds="[0,0][100,40]")
                + node("com.bazaar.demo:id/tv_b", text="B", bounds="[92,36][300,100]")
            ),
        )
    )
    assert scanner().scan(at_threshold) == []


def test_overlap_threshold_is_configurable():
    tight = UIScanner(BAZAAR, density_scale=2.0, overlap_dp2=10000)
    assert tight.scan(product_screen()) == []


def test_contained_labels_are_not_reported():
    # A label inside its own container is normal layout, not an overlap.
    nested = screen(
        node(
            "com.bazaar.demo:id/card",
            cls="android.widget.FrameLayout",
            bounds="[0,0][400,400]",
            children=node("com.bazaar.demo:id/label", text="Inside", bounds="[0,0][400,200]"),
        )
    )
    assert scanner().scan(nested) == []


def test_two_columns_side_by_side_do_not_overlap():
    row = screen(
        node(
            "com.bazaar.demo:id/row",
            cls="android.widget.LinearLayout",
            bounds="[0,0][720,120]",
            children=(
                node("com.bazaar.demo:id/tv_row_title", text="Wardrobe", bounds="[16,16][400,80]")
                + node("com.bazaar.demo:id/tv_row_price", text="Rs 1", bounds="[412,16][688,80]")
            ),
        )
    )
    assert scanner().scan(row) == []


def test_identical_rids_do_not_collide():
    # A list repeats row ids; only distinct sibling ids can describe a pair.
    events = scanner().scan(product_screen())
    assert events[0].rids[0] != events[0].rids[1]


# -- zero-size clickable ---------------------------------------------------
def test_zero_size_clickable_is_reported():
    broken = screen(
        node(
            "com.bazaar.demo:id/btn_hidden",
            cls="android.widget.Button",
            text="Buy",
            bounds="[100,100][100,100]",
            clickable=True,
        )
    )
    assert signatures(scanner().scan(broken)) == ["zero-size:btn_hidden"]


def test_zero_size_text_is_not_reported_unless_clickable():
    hidden_text = screen(
        node("com.bazaar.demo:id/tv_invisible", text="", bounds="[10,10][10,10]")
    )
    assert scanner().scan(hidden_text) == []


def test_disabled_zero_size_control_is_not_reported():
    broken = screen(
        node(
            "com.bazaar.demo:id/btn_hidden",
            cls="android.widget.Button",
            bounds="[0,0][0,0]",
            clickable=True,
            enabled=False,
        )
    )
    assert scanner().scan(broken) == []


# -- clipped text ----------------------------------------------------------
def test_text_cut_by_the_screen_edge_is_reported():
    offscreen = screen(
        node("com.bazaar.demo:id/tv_banner", text="Wide", bounds="[0,100][900,200]")
    )
    assert signatures(scanner().scan(offscreen)) == ["clipped:tv_banner"]


def test_scrollable_content_is_not_clipped():
    scroller = screen(
        node(
            "com.bazaar.demo:id/list",
            cls="android.widget.ListView",
            bounds="[0,128][720,1100]",
            scrollable=True,
            children=node(
                "com.bazaar.demo:id/row", text="Row", bounds="[0,1050][720,1200]"
            ),
        )
    )
    assert scanner().scan(scroller) == []


def test_screen_edge_rounding_is_not_a_clip():
    # 1 px over the edge is layout rounding, not a clipped label.
    rounded = screen(node("com.bazaar.demo:id/tv", text="x", bounds="[0,0][721,40]"))
    assert scanner().scan(rounded) == []


def test_clipping_needs_a_known_screen_size():
    tiny = screen(node("com.bazaar.demo:id/tv", text="x", bounds="[0,0][900,40]"), size=(0, 0))
    assert scanner().scan(tiny) == []


# -- findings --------------------------------------------------------------
def test_b4_finding_confidence_is_heuristic():
    event = scanner().scan(product_screen())[0]
    finding = scanner().to_finding(event, local_id="F4", step_idx=4)
    assert finding.kind == "ui_broken"
    assert finding.confidence == "heuristic"
    assert finding.probe["signature"] == "overlap:tv_product_price,tv_product_title"
    assert sorted(finding.probe["rids"]) == ["tv_product_price", "tv_product_title"]


def test_scan_is_deterministic():
    first = signatures(scanner().scan(product_screen()))
    second = signatures(scanner().scan(product_screen()))
    assert first == second


def test_title_and_price_rids_are_the_bazaar_ones():
    events = scanner().scan(product_screen())
    assert set(events[0].rids) == {TITLE.rsplit("/", 1)[-1], PRICE.rsplit("/", 1)[-1]}
