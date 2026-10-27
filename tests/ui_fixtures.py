"""Hierarchy builders shared by the detector tests (no device)."""

from __future__ import annotations

from litmus.runner.driver import Screen, parse_hierarchy

BAZAAR = "com.bazaar.demo"


def node(
    rid: str = "",
    *,
    cls: str = "android.widget.TextView",
    text: str = "",
    desc: str = "",
    bounds: str = "[0,0][100,40]",
    clickable: bool = False,
    enabled: bool = True,
    checked: bool = False,
    selected: bool = False,
    scrollable: bool = False,
    package: str = BAZAAR,
    children: str = "",
) -> str:
    attrs = (
        f'index="0" text="{text}" resource-id="{rid}" class="{cls}" package="{package}" '
        f'content-desc="{desc}" checkable="false" checked="{str(checked).lower()}" '
        f'clickable="{str(clickable).lower()}" enabled="{str(enabled).lower()}" '
        f'focusable="false" focused="false" scrollable="{str(scrollable).lower()}" '
        f'long-clickable="false" password="false" selected="{str(selected).lower()}" '
        f'bounds="{bounds}"'
    )
    return f"<node {attrs}>{children}</node>"


def hierarchy(*roots: str) -> str:
    return '<hierarchy rotation="0">' + "".join(roots) + "</hierarchy>"


def screen(*roots: str, size: tuple[int, int] = (720, 1280)) -> Screen:
    return parse_hierarchy(hierarchy(*roots), size)


def product_screen(overlap_px: int = 32) -> Screen:
    """A Bazaar ProductActivity-like screen.

    ``overlap_px`` is how far the price climbs into the two-line title
    (B4 is a -16 dp margin = 32 px at 2x).
    """
    title_bottom = 623
    price_top = title_bottom - overlap_px
    return screen(
        node(
            "com.bazaar.demo:id/product_root",
            cls="android.widget.ScrollView",
            bounds="[0,128][720,1280]",
            scrollable=True,
            children=(
                node(
                    "com.bazaar.demo:id/tv_product_title",
                    text="Handcrafted Mahogany Wardrobe with Brass",
                    bounds=f"[32,512][688,{title_bottom}]",
                )
                + node(
                    "com.bazaar.demo:id/tv_product_price",
                    text="Rs 48,900",
                    bounds=f"[32,{price_top}][688,645]",
                )
            ),
        )
    )


def cart_screen(dead: bool = True) -> Screen:
    return screen(
        node(
            "com.bazaar.demo:id/cart_root",
            cls="android.widget.LinearLayout",
            bounds="[0,128][720,1280]",
            children=(
                node(
                    "com.bazaar.demo:id/tv_cart_summary",
                    text="Items: 1" if not dead else "Items: 0",
                    bounds="[0,128][720,180]",
                )
                + node(
                    "com.bazaar.demo:id/btn_apply_coupon",
                    cls="android.widget.Button",
                    text="Apply coupon",
                    bounds="[32,900][688,980]",
                    clickable=True,
                )
            ),
        )
    )
