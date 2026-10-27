"""Shared screen fixtures built programmatically, in Bazaar's shape."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from litmus.runner.driver import Screen, parse_hierarchy

PACKAGE = "com.bazaar.demo"
RID = PACKAGE + ":id/"


def make_xml(tree: dict, package: str = PACKAGE) -> str:
    """Render a nested node spec as a ``uiautomator dump`` document."""

    def build(spec: dict) -> ET.Element:
        element = ET.Element("node")
        element.set("class", spec.get("class", "android.widget.LinearLayout"))
        element.set("package", spec.get("package", package))
        element.set("resource-id", spec.get("rid", ""))
        element.set("text", spec.get("text", ""))
        element.set("content-desc", spec.get("desc", ""))
        element.set("bounds", spec.get("bounds", "[0,0][10,10]"))
        flags = {
            "clickable": False,
            "long-clickable": False,
            "scrollable": False,
            "enabled": True,
            "focusable": False,
            "focused": False,
            "checkable": False,
            "checked": False,
            "selected": False,
            "password": False,
        }
        for key, default in flags.items():
            value = spec.get(key.replace("-", "_"), default)
            element.set(key, "true" if value else "false")
        for child in spec.get("children", []):
            element.append(build(child))
        return element

    root = ET.Element("hierarchy", rotation="0")
    root.append(build(tree))
    return ET.tostring(root, encoding="unicode")


def login_spec(email_text: str = "Email", password_text: str = "Password") -> dict:
    return {
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][720,1280]",
        "children": [
            {
                "class": "android.widget.LinearLayout",
                "rid": RID + "login_root",
                "bounds": "[0,240][720,1232]",
                "children": [
                    {
                        "class": "android.widget.EditText",
                        "rid": RID + "et_email",
                        "text": email_text,
                        "clickable": True,
                        "focusable": True,
                        "bounds": "[48,645][672,750]",
                    },
                    {
                        "class": "android.widget.EditText",
                        "rid": RID + "et_password",
                        "text": password_text,
                        "password": True,
                        "clickable": True,
                        "focusable": True,
                        "bounds": "[48,774][672,879]",
                    },
                    {
                        "class": "android.widget.Button",
                        "rid": RID + "btn_continue",
                        "text": "Continue",
                        "clickable": True,
                        "bounds": "[48,919][672,1015]",
                    },
                ],
            }
        ],
    }


def catalog_spec() -> dict:
    rows = []
    for index, title in enumerate(("Wardrobe", "Dining Table", "Bookshelf")):
        top = 308 + index * 142
        rows.append(
            {
                "class": "android.widget.LinearLayout",
                "rid": RID + "row_product_root",
                "bounds": f"[0,{top}][720,{top + 140}]",
                "children": [
                    {
                        "class": "android.widget.TextView",
                        "rid": RID + "tv_row_title",
                        "text": title,
                        "bounds": f"[32,{top + 32}][531,{top + 108}]",
                    },
                    {
                        "class": "android.widget.TextView",
                        "rid": RID + "tv_row_price",
                        "text": "Rs 1",
                        "bounds": f"[555,{top + 32}][688,{top + 73}]",
                    },
                ],
            }
        )
    return {
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][720,1280]",
        "children": [
            {
                "class": "android.widget.LinearLayout",
                "rid": RID + "catalog_root",
                "bounds": "[0,240][720,1232]",
                "children": [
                    {
                        "class": "android.widget.ListView",
                        "rid": RID + "list_products",
                        "scrollable": True,
                        "bounds": "[0,308][720,936]",
                        "children": rows,
                    },
                    {
                        "class": "android.widget.Button",
                        "rid": RID + "btn_open_cart",
                        "text": "Cart (0)",
                        "clickable": True,
                        "bounds": "[32,976][688,1072]",
                    },
                    {
                        "class": "android.widget.Button",
                        "rid": RID + "btn_express",
                        "text": "Express checkout (beta)",
                        "clickable": True,
                        "bounds": "[32,1104][688,1200]",
                    },
                ],
            }
        ],
    }


def product_spec() -> dict:
    return {
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][720,1280]",
        "children": [
            {
                "class": "android.widget.ScrollView",
                "rid": RID + "product_root",
                "scrollable": True,
                "bounds": "[0,240][720,1232]",
                "children": [
                    {
                        "class": "android.widget.TextView",
                        "rid": RID + "tv_product_title",
                        "text": "Handcrafted Mahogany Wardrobe",
                        "bounds": "[32,512][688,623]",
                    },
                    {
                        "class": "android.widget.TextView",
                        "rid": RID + "tv_product_price",
                        "text": "Rs 48,900",
                        "bounds": "[32,591][688,645]",
                    },
                    {
                        "class": "android.widget.Button",
                        "rid": RID + "btn_add_cart",
                        "text": "Add to cart",
                        "clickable": True,
                        "bounds": "[32,1104][688,1200]",
                    },
                ],
            }
        ],
    }


def screen_of(spec: dict) -> Screen:
    return parse_hierarchy(make_xml(spec), (720, 1280))


@pytest.fixture
def login_screen() -> Screen:
    return screen_of(login_spec())


@pytest.fixture
def login_email_filled() -> Screen:
    return screen_of(login_spec(email_text="qa@bazaar.dev"))


@pytest.fixture
def login_ready() -> Screen:
    return screen_of(login_spec(email_text="qa@bazaar.dev", password_text="********"))


@pytest.fixture
def catalog_screen() -> Screen:
    return screen_of(catalog_spec())


@pytest.fixture
def product_screen() -> Screen:
    return screen_of(product_spec())