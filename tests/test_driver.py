"""Hierarchy parsing, state hashing and the selector ladder (M1)."""

from __future__ import annotations

from conftest import login_spec, make_xml, screen_of

from litmus.runner.driver import (
    AdbError,
    Driver,
    parse_hierarchy,
    resolve_ladder,
    resolve_selector,
    selector_ladder,
)


def test_parses_app_nodes_and_ids(login_screen) -> None:
    assert login_screen.package == "com.bazaar.demo"
    ids = login_screen.resource_ids()
    assert "com.bazaar.demo:id/et_email" in ids
    assert "com.bazaar.demo:id/btn_continue" in ids


def test_parses_every_window_root() -> None:
    # uiautomator2 dumps the status bar window next to the app window.
    status_bar = (
        '<node class="android.widget.FrameLayout" package="com.android.systemui" '
        'resource-id="" text="" content-desc="" bounds="[0,0][720,128]" '
        'clickable="false" enabled="true" password="false" />'
    )
    app_root = make_xml(login_spec()).split("<hierarchy rotation=\"0\">")[1].rsplit(
        "</hierarchy>", 1
    )[0]
    xml = '<hierarchy rotation="0">' + status_bar + app_root + "</hierarchy>"
    screen = parse_hierarchy(xml, (720, 1280))
    assert screen.package == "com.bazaar.demo"
    assert len(screen.app_nodes("com.bazaar.demo")) == 5
    assert len(screen.app_nodes("com.android.systemui")) == 1
    # Paths are unique across windows.
    assert len({node.path for node in screen.nodes}) == len(screen.nodes)
    assert "com.bazaar.demo:id/et_email" in screen.resource_ids("com.bazaar.demo")


def test_state_hash_ignores_text_changes(login_screen) -> None:
    filled = screen_of(login_spec(email_text="qa@bazaar.dev", password_text="********"))
    assert login_screen.state_hash() == filled.state_hash()


def test_state_hash_changes_with_navigation(catalog_screen, product_screen) -> None:
    assert catalog_screen.state_hash() != product_screen.state_hash()


def test_framework_ids_do_not_change_the_hash(login_screen) -> None:
    # The soft keyboard adds and removes framework windows; the app screen
    # itself is unchanged, so the hash must not move.
    with_chrome = login_spec()
    with_chrome["children"].append(
        {
            "class": "android.widget.FrameLayout",
            "rid": "android:id/statusBarBackground",
            "bounds": "[0,0][720,128]",
        }
    )
    assert screen_of(with_chrome).state_hash() == login_screen.state_hash()


def test_weak_hierarchy_uses_structure() -> None:
    # Only one id among eight nodes -> id density 0.125, below the 0.15 floor.
    spec = {
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][720,1280]",
        "children": [
            {
                "class": "android.widget.LinearLayout",
                "rid": "com.bazaar.demo:id/root",
                "bounds": "[0,0][720,1280]",
                "children": [
                    {
                        "class": "android.widget.TextView",
                        "text": "a",
                        "bounds": f"[0,{index * 50}][100,{index * 50 + 40}]",
                    }
                    for index in range(7)
                ],
            }
        ],
    }
    screen = screen_of(spec)
    assert screen.id_density() < 0.15
    moved = spec.copy()
    moved["children"] = [
        {
            **spec["children"][0],
            "children": [
                {**child, "bounds": f"[0,{index * 50 + 10}][100,{index * 50 + 50}]"}
                for index, child in enumerate(spec["children"][0]["children"])
            ],
        }
    ]
    assert screen.state_hash() != screen_of(moved).state_hash()


def test_selector_ladder_stores_every_level(login_screen) -> None:
    node = login_screen.find(lambda item: item.resource_id.endswith("btn_continue"))
    ladder = selector_ladder(login_screen, node)
    levels = [entry["level"] for entry in ladder]
    assert levels == ["resource_id", "text", "path", "bounds"]
    assert ladder[0]["value"] == "com.bazaar.demo:id/btn_continue"


def test_selector_ladder_excludes_password_text(login_screen) -> None:
    node = login_screen.find(lambda item: item.resource_id.endswith("et_password"))
    levels = [entry["level"] for entry in selector_ladder(login_screen, node)]
    assert "text" not in levels


def test_resolves_repeated_rows_by_occurrence(catalog_screen) -> None:
    rows = [node for node in catalog_screen.nodes if node.resource_id.endswith("row_product_root")]
    assert len(rows) == 3
    ladder = selector_ladder(catalog_screen, rows[2])
    selector = next(entry for entry in ladder if entry["level"] == "resource_id")
    assert selector["occurrence"] == 2
    resolved = resolve_selector(catalog_screen, selector)
    assert resolved is rows[2]
    assert resolve_ladder(catalog_screen, ladder) is rows[2]


def test_path_selector_round_trips(catalog_screen) -> None:
    rows = [node for node in catalog_screen.nodes if node.resource_id.endswith("row_product_root")]
    ladder = selector_ladder(catalog_screen, rows[1])
    path_selector = next(entry for entry in ladder if entry["level"] == "path")
    resolved = resolve_selector(catalog_screen, path_selector)
    assert resolved is rows[1]


def test_bounds_selector_falls_back_within_five_percent(catalog_screen) -> None:
    rows = [node for node in catalog_screen.nodes if node.resource_id.endswith("row_product_root")]
    bounds = rows[0].bounds.as_list()
    shifted = [bounds[0] + 20, bounds[1] + 20, bounds[2] + 20, bounds[3] + 20]
    resolved = resolve_selector(catalog_screen, {"level": "bounds", "value": shifted})
    assert resolved is rows[0]


def test_missing_selector_resolves_to_none(catalog_screen) -> None:
    assert resolve_selector(catalog_screen, {"level": "resource_id", "value": "nope"}) is None


def test_parse_hierarchy_tolerates_garbage() -> None:
    assert parse_hierarchy("not xml").nodes == []
    assert parse_hierarchy("").nodes == []


def test_status_bar_clock_does_not_break_hashing() -> None:
    # The clock changes every dump and lives outside the app package.
    spec = login_spec()
    spec["children"].append(
        {
            "class": "android.widget.TextView",
            "package": "com.android.systemui",
            "text": "12:00",
            "bounds": "[0,0][100,40]",
        }
    )
    first = make_xml(spec)
    second = first.replace("12:00", "12:01")
    assert parse_hierarchy(first).state_hash() == parse_hierarchy(second).state_hash()
class _BootProbe(Driver):
    """Driver with stubbed adb, for the readiness probe only."""

    def __init__(self, props: dict, pm_responsive: bool = True) -> None:
        self.props = props
        self.pm_responsive = pm_responsive

    def getprop(self, name: str) -> str:
        return self.props.get(name, "")

    def shell(self, *args: str, timeout: int | None = None, check: bool = True) -> str:
        if args[:2] == ("pm", "path"):
            if not self.pm_responsive:
                raise AdbError("adb timed out: shell pm path android")
            return "package:/system/framework/framework-res.apk"
        return ""


def test_booted_requires_a_finished_boot_animation() -> None:
    props = {"sys.boot_completed": "1", "init.svc.bootanim": "running"}
    assert _BootProbe(props).is_booted() is False


def test_booted_requires_the_package_manager_to_answer() -> None:
    props = {"sys.boot_completed": "1", "init.svc.bootanim": "stopped"}
    assert _BootProbe(props, pm_responsive=False).is_booted() is False


def test_booted_when_android_is_up_and_responsive() -> None:
    props = {"sys.boot_completed": "1", "init.svc.bootanim": "stopped"}
    assert _BootProbe(props).is_booted() is True


def test_not_booted_before_boot_completed() -> None:
    assert _BootProbe({"init.svc.bootanim": "stopped"}).is_booted() is False
