"""ADB-backed device driver: hierarchy dumps, taps, text, logcat, screenshots.

Deliberately adb-only (no atx-agent, no uiautomator2 server): the primitives
here are exactly what a standalone ``repro.py`` can call, so a replayed trace
and a hand-run reproduction script exercise the same mechanism (PLAN §8).

The device contract comes from the target's fixture (``E:\\bazaar\\contract.md``):
classic Android Views, stable ``resource-id``s, plain-HTTP backend, and a
soft keyboard that covers the submit button on a 640 dp screen.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

#: Selectors are stored as a ladder; every level is tried in order.
LADDER_ORDER = ("resource_id", "content_desc", "text", "path", "bounds")

#: An app node density below this means the hierarchy carries no usable ids.
WEAK_ID_DENSITY = 0.15

#: Placeholder texts that Android reports as the ``text`` of an empty field.
PLACEHOLDERS = frozenset(
    {
        "email",
        "e-mail",
        "email address",
        "enter email",
        "password",
        "enter password",
        "name",
        "username",
        "user name",
        "search",
        "phone",
        "phone number",
    }
)


class AdbError(RuntimeError):
    """Raised when an adb invocation fails."""


@dataclass(frozen=True, slots=True)
class Bounds:
    """Screen rectangle of a node, in device pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def parse(cls, raw: str) -> Bounds | None:
        match = BOUNDS_RE.match(raw or "")
        if not match:
            return None
        left, top, right, bottom = (int(value) for value in match.groups())
        return cls(left, top, right, bottom)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def as_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]

    def __str__(self) -> str:
        return f"[{self.left},{self.top}][{self.right},{self.bottom}]"


@dataclass(slots=True)
class Node:
    """One node of a dumped UI hierarchy."""

    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    text: str = ""
    content_desc: str = ""
    bounds: Bounds | None = None
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    password: bool = False
    depth: int = 0
    sibling_index: int = 0
    path: str = ""
    order: int = 0
    children: list[Node] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        return self.bounds.center if self.bounds else (0, 0)

    @property
    def is_edit_text(self) -> bool:
        return self.class_name.endswith("EditText")

    @property
    def has_area(self) -> bool:
        return self.bounds is not None and self.bounds.area > 0

    def label(self) -> str:
        """Short human label for logs."""
        if self.resource_id:
            return self.resource_id.rsplit("/", 1)[-1]
        if self.text:
            return self.text[:32]
        if self.content_desc:
            return self.content_desc[:32]
        return self.class_name.rsplit(".", 1)[-1]

    def is_filled_field(self) -> bool:
        """True when an input field holds something other than a hint."""
        if not self.is_edit_text:
            return False
        text = (self.text or "").strip()
        if not text:
            return False
        if text.lower() in PLACEHOLDERS:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "class": self.class_name,
            "package": self.package,
            "text": self.text,
            "content_desc": self.content_desc,
            "bounds": self.bounds.as_list() if self.bounds else None,
            "clickable": self.clickable,
            "long_clickable": self.long_clickable,
            "scrollable": self.scrollable,
            "enabled": self.enabled,
            "focusable": self.focusable,
            "focused": self.focused,
            "checked": self.checked,
            "selected": self.selected,
            "password": self.password,
            "depth": self.depth,
            "path": self.path,
        }


@dataclass(slots=True)
class Screen:
    """A dumped hierarchy, flattened for querying."""

    nodes: list[Node] = field(default_factory=list)
    package: str = ""
    xml: str = ""
    screen_size: tuple[int, int] = (0, 0)

    def app_nodes(self, package: str | None = None) -> list[Node]:
        """Nodes belonging to the app under test (system chrome is dropped)."""
        target = package or self.package
        return [node for node in self.nodes if node.package == target]

    def by_resource_id(self, resource_id: str) -> list[Node]:
        return [node for node in self.nodes if node.resource_id == resource_id]

    def resource_ids(self, package: str | None = None) -> list[str]:
        """App-scoped resource ids of this screen.

        Framework ids (``android:id/...``) are excluded when the app has its own
        ids: they come and go with the system UI - the soft keyboard, the
        action bar, the navigation bar - and would make the hash flap without
        the app changing at all.
        """
        target = package or self.package
        nodes = self.app_nodes(target)
        scoped = sorted(
            {
                node.resource_id
                for node in nodes
                if node.resource_id.startswith(f"{target}:id/")
            }
        )
        if scoped:
            return scoped
        return sorted({node.resource_id for node in nodes if node.resource_id})

    def id_density(self, package: str | None = None) -> float:
        app = self.app_nodes(package)
        if not app:
            return 0.0
        with_id = sum(1 for node in app if node.resource_id)
        return with_id / len(app)

    def weak_signature(self, package: str | None = None) -> str:
        """Structural signature for hierarchies that carry no usable ids."""
        parts: list[str] = []
        for node in self.app_nodes(package):
            if node.resource_id:
                continue
            bucket = ""
            if node.bounds:
                bucket = (
                    f"{node.bounds.left // 40},{node.bounds.top // 40},"
                    f"{node.bounds.right // 40},{node.bounds.bottom // 40}"
                )
            parts.append(
                f"{node.class_name}:{node.depth}:{node.sibling_index}:{bucket}:{node.content_desc}"
            )
        return "|".join(parts)

    def state_hash(self, package: str | None = None) -> str:
        """Navigation hash (PLAN §5.3).

        ``sha1(package + "|" + sorted(app_resource_ids) + "|" + weak_signature)``.
        The activity is deliberately excluded: it is slow to read and unstable
        across dialogs. The status-bar clock changes every dump, so raw XML
        equality never settles - this hash does.
        """
        import hashlib

        target = package or self.package
        ids = self.resource_ids(target)
        weak = ""
        if self.id_density(target) < WEAK_ID_DENSITY:
            weak = self.weak_signature(target)
        payload = f"{target}|{','.join(ids)}|{weak}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def is_rendered(self, package: str | None = None) -> bool:
        """True when the app window has actually drawn its content.

        During an activity transition the app window exists but is empty (no
        ids, no labels). Acting on that dump taps into the void, so callers
        treat it as "not settled yet".
        """
        target = package or self.package
        app = self.app_nodes(target)
        if not app:
            return False
        if any(node.resource_id.startswith(f"{target}:id/") for node in app):
            return True
        labelled = sum(1 for node in app if node.text or node.content_desc)
        return labelled >= 2

    def find(self, predicate) -> Node | None:
        for node in self.nodes:
            if predicate(node):
                return node
        return None

    def edit_texts(self, package: str | None = None) -> list[Node]:
        return [
            node
            for node in self.app_nodes(package)
            if node.is_edit_text and node.enabled and node.has_area
        ]


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def parse_hierarchy(xml: str, screen_size: tuple[int, int] = (0, 0)) -> Screen:
    """Parse a ``uiautomator dump`` document into a :class:`Screen`.

    A dump can carry several window roots: uiautomator2 includes the status bar
    window alongside the app window, and a dialog is its own root. Every root
    is walked; each path is prefixed with its root index so paths stay unique
    across windows. ``screen.package`` is the package of the largest window
    (normally the app under test).
    """
    screen = Screen(package="", xml=xml, screen_size=screen_size)
    if not xml.strip():
        return screen
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return screen
    counter = [0]

    def walk(element: ET.Element, depth: int, index: int, parent_path: str) -> Node:
        bounds = Bounds.parse(element.get("bounds", ""))
        node = Node(
            resource_id=element.get("resource-id", "") or "",
            class_name=element.get("class", "") or "",
            package=element.get("package", "") or "",
            text=element.get("text", "") or "",
            content_desc=element.get("content-desc", "") or "",
            bounds=bounds,
            clickable=_bool(element.get("clickable")),
            long_clickable=_bool(element.get("long-clickable")),
            scrollable=_bool(element.get("scrollable")),
            enabled=_bool(element.get("enabled")),
            focusable=_bool(element.get("focusable")),
            focused=_bool(element.get("focused")),
            checkable=_bool(element.get("checkable")),
            checked=_bool(element.get("checked")),
            selected=_bool(element.get("selected")),
            password=_bool(element.get("password")),
            depth=depth,
            sibling_index=index,
            path=f"{parent_path}/{element.get('class', '?')}[{index}]",
            order=counter[0],
        )
        counter[0] += 1
        screen.nodes.append(node)
        for child_index, child in enumerate(list(element)):
            if child.tag != "node":
                continue
            node.children.append(walk(child, depth + 1, child_index, node.path))
        return node

    roots = [child for child in list(root) if child.tag == "node"]
    walked: list[Node] = []
    for root_index, element in enumerate(roots):
        walked.append(walk(element, 0, 0, f"#{root_index}"))
    if walked:
        largest = max(
            walked,
            key=lambda node: node.bounds.area if node.bounds else 0,
        )
        screen.package = largest.package or walked[0].package
    return screen


def _occurrence(nodes: list[Node], node: Node) -> int:
    """Index of ``node`` among the nodes sharing its resource id."""
    same = [item for item in nodes if item.resource_id == node.resource_id]
    for position, item in enumerate(same):
        if item is node:
            return position
    return 0


def selector_ladder(screen: Screen, node: Node) -> list[dict]:
    """Capture every selector level for a node (PLAN §5.4).

    Resolution order is ``resource-id -> content-desc -> text -> path -> bounds``;
    the whole ladder is stored so a replay can degrade instead of failing.
    """
    ladder: list[dict] = []
    if node.resource_id:
        ladder.append(
            {
                "level": "resource_id",
                "value": node.resource_id,
                "occurrence": _occurrence(screen.nodes, node),
            }
        )
    if node.content_desc:
        ladder.append(
            {
                "level": "content_desc",
                "value": node.content_desc,
                "occurrence": _occurrence(
                    [item for item in screen.nodes if item.content_desc == node.content_desc],
                    node,
                ),
            }
        )
    if node.text and not node.password:
        ladder.append(
            {
                "level": "text",
                "value": node.text,
                "occurrence": _occurrence(
                    [item for item in screen.nodes if item.text == node.text], node
                ),
            }
        )
    ladder.append({"level": "path", "value": node.path, "occurrence": 0})
    if node.bounds:
        ladder.append(
            {
                "level": "bounds",
                "value": node.bounds.as_list(),
                "occurrence": 0,
                # Bounds alone can land on a sibling; the class must agree too.
                "class": node.class_name,
            }
        )
    return ladder


def _bounds_match(node: Node, wanted: list[int], screen_size: tuple[int, int]) -> bool:
    if not node.bounds:
        return False
    width, height = screen_size if screen_size and screen_size[0] else (720, 1280)
    tolerance_x = max(8, int(width * 0.05))
    tolerance_y = max(8, int(height * 0.05))
    node_center = node.center
    target_center = ((wanted[0] + wanted[2]) // 2, (wanted[1] + wanted[3]) // 2)
    return (
        abs(node_center[0] - target_center[0]) <= tolerance_x
        and abs(node_center[1] - target_center[1]) <= tolerance_y
    )


def resolve_selector(
    screen: Screen, selector: dict, screen_size: tuple[int, int] | None = None
) -> Node | None:
    """Resolve one selector level against a freshly dumped screen."""
    level = selector.get("level")
    value = selector.get("value")
    occurrence = int(selector.get("occurrence", 0) or 0)
    size = screen_size or screen.screen_size
    if level == "resource_id":
        matches = [node for node in screen.nodes if node.resource_id == value]
    elif level == "content_desc":
        matches = [node for node in screen.nodes if node.content_desc == value]
    elif level == "text":
        matches = [node for node in screen.nodes if node.text == value]
    elif level == "path":
        matches = [node for node in screen.nodes if node.path == value]
    elif level == "bounds":
        matches = [
            node
            for node in screen.nodes
            if node.bounds
            and _bounds_match(node, list(value), size)
            and (not selector.get("class") or node.class_name == selector["class"])
        ]
    else:
        return None
    if not matches:
        return None
    return matches[min(occurrence, len(matches) - 1)]


def resolve_ladder(
    screen: Screen, ladder: list[dict], screen_size: tuple[int, int] | None = None
) -> Node | None:
    """Try every selector level in order and return the first match."""
    for selector in ladder:
        node = resolve_selector(screen, selector, screen_size)
        if node is not None and node.has_area:
            return node
    return None


def _shell_quote(value: str) -> str:
    """Quote a value for the device shell."""
    return "'" + value.replace("'", "'\\''") + "'"


class Driver:
    """Thin, explicit wrapper around ``adb``."""

    def __init__(self, serial: str, adb: str | None = None, timeout: int = 30) -> None:
        self.serial = serial
        self.adb_path = adb or shutil.which("adb") or "adb"
        self.timeout = timeout
        self.package = ""
        self._screen_size: tuple[int, int] | None = None
        self._density: float | None = None
        self._u2 = None
        self._u2_failed = False

    # -- plumbing ---------------------------------------------------------
    def run(
        self,
        args: list[str],
        timeout: int | None = None,
        check: bool = True,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
        cmd = [self.adb_path, "-s", self.serial, *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout or self.timeout,
                check=False,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - device hang
            raise AdbError(f"adb timed out: {' '.join(args)}") from exc
        if check and result.returncode != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise AdbError(f"adb failed ({result.returncode}): {' '.join(args)}: {stderr}")
        return result

    def shell(self, *args: str, timeout: int | None = None, check: bool = True) -> str:
        result = self.run(["shell", *args], timeout=timeout, check=check)
        return (result.stdout or "").strip()

    # -- device state -----------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        if self._screen_size is None:
            size = (0, 0)
            try:
                out = self.shell("wm", "size")
            except AdbError:
                out = ""
            match = re.search(r"(\d+)x(\d+)", out)
            if match:
                size = (int(match.group(1)), int(match.group(2)))
            self._screen_size = size
        return self._screen_size

    def getprop(self, name: str) -> str:
        try:
            return self.shell("getprop", name)
        except AdbError:
            return ""

    def is_booted(self) -> bool:
        """True when Android is up *and* answering commands.

        ``sys.boot_completed`` flips while the package manager is still
        scanning on a SwiftShader AVD; the first ``am start`` after that then
        blocks past its timeout. Requiring the boot animation to be finished
        and a cheap ``pm`` call to answer closes that window.
        """
        if self.getprop("sys.boot_completed").strip() != "1":
            return False
        if self.getprop("init.svc.bootanim").strip() == "running":
            return False
        try:
            self.shell("pm", "path", "android", timeout=15)
        except AdbError:
            return False
        return True

    def current_activity(self) -> str:
        """Best-effort foreground activity, e.g. ``com.bazaar.demo/.ProductActivity``."""
        for args in (
            ("dumpsys", "activity", "activities"),
            ("dumpsys", "window", "windows"),
        ):
            try:
                out = self.shell(*args, timeout=20)
            except AdbError:
                continue
            match = re.search(r"\b([A-Za-z][\w.]*\.[A-Za-z][\w.]*)/([\w.$]+)", out)
            if match:
                return f"{match.group(1)}/{match.group(2)}"
        return ""

    def ime_shown(self) -> bool:
        """True when the soft keyboard is up (it covers the submit button)."""
        try:
            out = self.shell("dumpsys", "input_method", timeout=20)
        except AdbError:
            return False
        match = re.search(r"mInputShown=(\w+)", out)
        return bool(match and match.group(1).lower() == "true")

    def toast_visible(self) -> bool:
        """True while a Toast window is on screen.

        A Toast is its own window and never appears in the hierarchy, so it is
        the only evidence that a tap with no hierarchical effect did something
        (Bazaar's first add-to-cart tap raises "Added to cart"). Sampled while a
        tap settles - a Toast lasts about a second.
        """
        try:
            out = self.shell("dumpsys", "window", "windows", timeout=20)
        except AdbError:
            return False
        return bool(
            re.search(r"Window\{[^}]*\bToast\}", out) or "appop=TOAST_WINDOW" in out
        )

    def density_scale(self) -> float:
        """Screen density in dp units, e.g. ``2.0`` for a 320 dpi device."""
        if self._density is None:
            scale = 2.0
            try:
                out = self.shell("wm", "density")
            except AdbError:
                out = ""
            match = re.search(r"(?:Physical|Override) density:\s*(\d+)", out)
            if match:
                scale = int(match.group(1)) / 160.0
            self._density = scale
        return self._density

    # -- hierarchy --------------------------------------------------------
    def _u2_device(self):
        """Lazy uiautomator2 connection; ``None`` means use plain adb."""
        if self._u2_failed:
            return None
        if self._u2 is None:
            try:
                import uiautomator2 as u2

                self._u2 = u2.connect(self.serial)
            except Exception:  # noqa: BLE001 - optional acceleration
                self._u2_failed = True
                return None
        return self._u2

    def dump(self, retries: int = 3) -> Screen:
        """Dump the current hierarchy. Returns an empty screen on failure.

        uiautomator2 keeps a UiAutomator server warm on the device, so a dump
        takes ~250 ms instead of ~4 s for ``uiautomator dump``. The XML is the
        same document either way. Any failure falls back to plain adb.
        """
        device = self._u2_device()
        if device is not None:
            try:
                xml = device.dump_hierarchy()
            except Exception:  # noqa: BLE001 - degrade to adb, do not fail the run
                self._u2_failed = True
                xml = ""
            if xml and "<hierarchy" in xml:
                return parse_hierarchy(xml, self.screen_size())

        for attempt in range(retries):
            try:
                self.shell(
                    "uiautomator",
                    "dump",
                    "/sdcard/litmus_dump.xml",
                    timeout=25,
                )
                xml = self.run(
                    ["exec-out", "cat", "/sdcard/litmus_dump.xml"],
                    timeout=25,
                ).stdout
            except AdbError:
                xml = ""
            if xml and "<hierarchy" in xml:
                return parse_hierarchy(xml, self.screen_size())
            time.sleep(0.4 * (attempt + 1))
        return Screen(screen_size=self.screen_size())

    # -- actions ----------------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def tap_node(self, node: Node) -> None:
        x, y = node.center
        self.tap(x, y)

    def input_text(self, text: str) -> None:
        """Type text. Spaces become ``%s`` (the shell form ``input text`` wants)."""
        if not text:
            return
        escaped = text.replace(" ", "%s")
        self.shell("input", "text", _shell_quote(escaped))

    def keyevent(self, keycode: int | str) -> None:
        self.shell("input", "keyevent", str(keycode))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def hide_ime(self, force: bool = False) -> bool:
        """Dismiss the soft keyboard with BACK.

        ``force`` is used when the hierarchy says the keyboard is covering the
        form but ``dumpsys`` still reports it as hidden (it lags behind input).
        """
        if not self.ime_shown():
            if not force:
                return False
            for _ in range(5):
                time.sleep(0.2)
                if self.ime_shown():
                    break
        self.keyevent(4)
        for _ in range(10):
            if not self.ime_shown():
                return True
            time.sleep(0.2)
        return True

    def execute(self, action, screen: Screen | None = None, component: str = "") -> str | None:
        """Execute a policy action. Returns an error string, or None on success.

        Actions are duck-typed (``kind``, ``selectors``, ``text``, ``keycode``)
        so the driver stays independent of the worker package.
        """
        kind = getattr(action, "kind", "")
        if kind in {"tap", "input"}:
            if screen is None:
                return "no screen to resolve the selector against"
            node = resolve_ladder(screen, action.selectors, screen.screen_size)
            if node is None:
                return "selector not found"
            self.tap_node(node)
            if kind == "input":
                self.input_text(getattr(action, "text", "") or "")
            return None
        if kind == "hide_ime":
            # Replay intent, not the literal key: only dismiss it when it is up.
            self.hide_ime(force=bool(getattr(action, "force", False)))
            return None
        if kind == "key":
            self.keyevent(getattr(action, "keycode", "4") or "4")
            return None
        if kind == "scroll":
            width, height = screen.screen_size if screen else self.screen_size()
            if width and height:
                self.swipe(width // 2, int(height * 0.72), width // 2, int(height * 0.35))
            return None
        if kind == "launch":
            package = self.package or (screen.package if screen else "")
            if package:
                self.force_stop(package)
            if component:
                self.launch(component)
            return None
        return f"unsupported action: {kind}"

    def screenshot(self, path: Path) -> Path:
        """Write a PNG screenshot to ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        device = self._u2_device()
        if device is not None:
            try:
                device.screenshot(str(path))
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except Exception:  # noqa: BLE001 - fall back to adb
                self._u2_failed = True
        result = self.run(["exec-out", "screencap", "-p"], timeout=30, binary=True)
        data = result.stdout or b""
        if not data.startswith(b"\x89PNG"):
            # Some devices need the two-step form.
            self.shell("screencap", "-p", "/sdcard/litmus_screen.png", timeout=30)
            data = self.run(
                ["exec-out", "cat", "/sdcard/litmus_screen.png"], timeout=30, binary=True
            ).stdout
        path.write_bytes(data)
        return path

    # -- app control ------------------------------------------------------
    def install(self, apk: Path) -> str:
        """Install (or reinstall) the APK, replacing an incompatible build."""
        result = self.run(
            ["install", "-r", "-t", "-d", str(apk)], timeout=300, check=False
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "Success" in output:
            return output.strip()
        incompatible = (
            "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in output
            or "INSTALL_FAILED_VERSION_DOWNGRADE" in output
        )
        if incompatible:
            self.uninstall(self.package_of_apk())
            result = self.run(["install", "-t", "-d", str(apk)], timeout=300, check=False)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0 and "Success" in output:
                return output.strip()
        raise AdbError(f"install failed: {output.strip()}")

    def uninstall(self, package: str) -> None:
        self.run(["uninstall", package], timeout=120, check=False)

    def clear_app(self, package: str) -> None:
        self.shell("pm", "clear", package, timeout=60)

    def launch(self, component: str) -> None:
        self.shell("am", "start", "-W", "-n", component, timeout=60)

    def force_stop(self, package: str) -> None:
        self.shell("am", "force-stop", package, timeout=30)

    def resolve_launcher(self, package: str) -> str:
        """Find the launcher activity of a package."""
        try:
            out = self.shell(
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
                timeout=30,
            )
        except AdbError:
            out = ""
        for line in reversed(out.splitlines()):
            line = line.strip()
            if "/" in line and not line.startswith("priority"):
                return line
        return f"{package}/.MainActivity"

    def package_of_apk(self) -> str:
        """The package under test, when the caller set it."""
        return self.package

    # -- logcat -----------------------------------------------------------
    def logcat(self, buffer: str = "crash", lines: int | None = None) -> str:
        args = ["logcat", "-b", buffer, "-d", "-v", "threadtime"]
        if lines:
            args += ["-t", str(lines)]
        result = self.run(args, timeout=30, check=False)
        return result.stdout or ""

    def clear_logcat(self, buffer: str = "crash") -> None:
        self.run(["logcat", "-b", buffer, "-c"], timeout=30, check=False)
