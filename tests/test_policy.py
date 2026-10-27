"""Heuristic action policy (PLAN §5.1, §5.5)."""

from __future__ import annotations

from conftest import catalog_spec, login_spec, screen_of

from litmus.worker.policy import HeuristicPolicy

PROFILE = {
    "package": "com.bazaar.demo",
    "credentials": {"email": "qa@bazaar.dev", "password": "test1234"},
}


def policy() -> HeuristicPolicy:
    return HeuristicPolicy(PROFILE, "com.bazaar.demo")


def test_login_fills_email_first(login_screen) -> None:
    action = policy().next_action(login_screen, login_screen.state_hash(), ime_shown=False)
    assert action is not None
    assert action.kind == "input"
    assert action.text == "qa@bazaar.dev"
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/et_email"


def test_login_fills_password_next(login_email_filled) -> None:
    action = policy().next_action(
        login_email_filled, login_email_filled.state_hash(), ime_shown=True
    )
    assert action is not None
    assert action.kind == "input"
    assert action.text == "test1234"
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/et_password"


def test_login_hides_keyboard_before_submit(login_ready) -> None:
    action = policy().next_action(login_ready, login_ready.state_hash(), ime_shown=True)
    assert action is not None
    assert action.kind == "hide_ime"
    assert action.target == "ime"


def test_login_submits_after_keyboard_is_hidden(login_ready) -> None:
    action = policy().next_action(login_ready, login_ready.state_hash(), ime_shown=False)
    assert action is not None
    assert action.kind == "tap"
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/btn_continue"


def test_missing_submit_means_the_keyboard_is_covering_it() -> None:
    # dumpsys can lag behind input; the hierarchy is the source of truth.
    spec = login_spec(email_text="qa@bazaar.dev", password_text="********")
    spec["children"][0]["children"] = [
        child for child in spec["children"][0]["children"]
        if not child.get("rid", "").endswith("btn_continue")
    ]
    screen = screen_of(spec)
    action = policy().next_action(screen, screen.state_hash(), ime_shown=False)
    assert action is not None
    assert action.kind == "hide_ime"
    assert action.force is True


def test_catalog_taps_a_list_row_before_the_buttons(catalog_screen) -> None:
    action = policy().next_action(catalog_screen, catalog_screen.state_hash())
    assert action is not None
    assert action.kind == "tap"
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/row_product_root"
    assert action.selectors[0]["occurrence"] == 0


def test_list_rows_are_found_without_the_scrollable_flag() -> None:
    # Live ListViews often report scrollable="false" in a uiautomator dump.
    spec = catalog_spec()
    spec["children"][0]["children"][0]["scrollable"] = False
    screen = screen_of(spec)
    action = policy().next_action(screen, screen.state_hash())
    assert action is not None
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/row_product_root"


def test_scrollview_content_is_not_mistaken_for_rows(product_screen) -> None:
    action = policy().next_action(product_screen, product_screen.state_hash())
    assert action is not None
    assert action.selectors[0]["value"] == "com.bazaar.demo:id/btn_add_cart"


def test_repeat_tap_when_the_screen_did_not_change(product_screen) -> None:
    policy_ = policy()
    state = product_screen.state_hash()
    first = policy_.next_action(product_screen, state)
    assert first is not None
    policy_.observe(first, state, state)

    second = policy_.next_action(product_screen, state)
    assert second is not None
    assert second.kind == "tap"
    assert second.reason == "repeat: screen did not change"
    assert second.selectors == first.selectors


def test_repeat_is_capped(product_screen) -> None:
    policy_ = policy()
    state = product_screen.state_hash()
    first = policy_.next_action(product_screen, state)
    assert first is not None
    policy_.observe(first, state, state)
    for _ in range(3):
        follow_up = policy_.next_action(product_screen, state)
        assert follow_up is not None
        if follow_up.kind != "tap":
            break
        policy_.observe(follow_up, state, state)
    assert policy_.repeat_count <= policy_.max_repeats
    action = policy_.next_action(product_screen, state)
    assert action is not None
    assert action.kind != "tap" or action.reason != "repeat: screen did not change"


def test_navigation_change_resets_repeat(product_screen, catalog_screen) -> None:
    policy_ = policy()
    product_state = product_screen.state_hash()
    action = policy_.next_action(product_screen, product_state)
    assert action is not None
    policy_.observe(action, product_state, catalog_screen.state_hash())
    assert policy_.repeat_count == 0


def test_missing_credentials_does_not_type_garbage(login_screen) -> None:
    empty = HeuristicPolicy({"package": "com.bazaar.demo"}, "com.bazaar.demo")
    action = empty.next_action(login_screen, login_screen.state_hash())
    # Falls through to the normal actionable scan instead of typing "".
    assert action is not None
    assert action.kind != "input"