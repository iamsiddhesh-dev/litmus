"""Broken-layout detector (PLAN §7, contract §4 B4).

Signals, all heuristic:

* **overlap** - two sibling text views whose bounds intersect by more than
  8 dp². Siblings only: an ancestor legitimately contains its children, so
  ancestor/descendant pairs are never reported.
* **zero-size clickable** - a control a user can tap but that occupies no
  pixels (unreachable by touch).
* **clipped text** - a leaf text view cut by the screen edge and not inside a
  scrollable, where partial visibility is the normal affordance.

Bazaar's B4 is ``tv_product_title`` (two lines) overlapped by
``tv_product_price`` (a stale -16 dp margin): 656x32 px = 5248 dp² at 2x
density on the reference AVD.
"""

from __future__ import annotations

from dataclasses import dataclass

from litmus.detectors.base import Finding
from litmus.runner.driver import Bounds, Node, Screen

#: PLAN §7: intersect by more than 8 dp².
DEFAULT_OVERLAP_DP2 = 8.0
#: Rounding at the screen edge is not a clip.
DEFAULT_CLIP_TOLERANCE_DP = 2.0
CLICKABLE_CLASSES = ("Button", "ImageButton", "CheckBox", "RadioButton", "Switch", "TextView")


def short_rid(resource_id: str) -> str:
    """``com.bazaar.demo:id/tv_product_title`` -> ``tv_product_title``."""
    return (resource_id or "").rsplit("/", 1)[-1]


def _parent_path(path: str) -> str:
    head, _, _ = path.rpartition("/")
    return head


def _is_ancestor(candidate: Node, node: Node) -> bool:
    """True when ``candidate`` contains ``node`` (paths are structural)."""
    return bool(candidate.path) and node.path.startswith(candidate.path + "/")


def _is_text(node: Node) -> bool:
    return bool((node.text or "").strip())


def _intersection(a: Bounds, b: Bounds) -> tuple[int, int]:
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    return max(0, width), max(0, height)


@dataclass(slots=True)
class UIEvent:
    """One broken-layout signal on a screen."""

    kind: str
    signature: str
    fingerprint: str
    detail: str
    rids: tuple[str, ...] = ()
    bounds: tuple[list[int], ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
            "detail": self.detail,
            "rids": list(self.rids),
            "bounds": [list(bounds) for bounds in self.bounds],
        }


class UIScanner:
    """Scans one hierarchy for layout defects (no device access)."""

    name = "ui_broken"

    def __init__(
        self,
        package: str,
        density_scale: float = 2.0,
        overlap_dp2: float = DEFAULT_OVERLAP_DP2,
        clip_tolerance_dp: float = DEFAULT_CLIP_TOLERANCE_DP,
    ) -> None:
        self.package = package
        self.density_scale = density_scale if density_scale > 0 else 2.0
        self.overlap_dp2 = overlap_dp2
        self.clip_tolerance_dp = clip_tolerance_dp

    # -- thresholds -------------------------------------------------------
    @property
    def overlap_px2(self) -> float:
        return self.overlap_dp2 * self.density_scale * self.density_scale

    @property
    def clip_tolerance_px(self) -> int:
        return int(round(self.clip_tolerance_dp * self.density_scale))

    # -- public API -------------------------------------------------------
    def scan(self, screen: Screen) -> list[UIEvent]:
        app = [node for node in screen.app_nodes(self.package) if node.bounds is not None]
        events = [*self._overlaps(app), *self._zero_size(app), *self._clipped(screen, app)]
        return sorted(events, key=lambda event: event.signature)

    def to_finding(
        self, event: UIEvent, local_id: str, step_idx: int | None = None
    ) -> Finding:
        return Finding(
            local_id=local_id,
            kind="ui_broken",
            signature=event.signature,
            fingerprint=event.fingerprint,
            confidence="heuristic",
            step_idx=step_idx,
            detail=event.detail,
            probe={"signature": event.signature, "rids": list(event.rids)},
        )

    # -- rules ------------------------------------------------------------
    def _overlaps(self, nodes: list[Node]) -> list[UIEvent]:
        texts = [node for node in nodes if _is_text(node) and node.has_area]
        events: list[UIEvent] = []
        for index, first in enumerate(texts):
            for second in texts[index + 1 :]:
                if _parent_path(first.path) != _parent_path(second.path):
                    # PLAN §7 says *sibling* bounds: containment is legal layout,
                    # and cross-level overlap is usually a container and its label.
                    continue
                width, height = _intersection(first.bounds, second.bounds)
                if width * height <= self.overlap_px2:
                    continue
                rids = sorted(
                    {
                        short_rid(first.resource_id) or first.class_name.rsplit(".", 1)[-1],
                        short_rid(second.resource_id) or second.class_name.rsplit(".", 1)[-1],
                    }
                )
                events.append(
                    UIEvent(
                        kind="ui_broken",
                        signature=f"overlap:{','.join(rids)}",
                        fingerprint=f"overlap:{','.join(rids)}",
                        detail=(
                            f"{rids[0]} and {rids[1]} overlap by {width * height} px "
                            f"({width}x{height}); "
                            f"{round(width * height / (self.density_scale ** 2), 1)} dp²"
                        ),
                        rids=tuple(rids),
                        bounds=(first.bounds.as_list(), second.bounds.as_list()),
                    )
                )
        return events

    def _zero_size(self, nodes: list[Node]) -> list[UIEvent]:
        events: list[UIEvent] = []
        for node in nodes:
            if not (node.clickable and node.enabled):
                continue
            if node.bounds is None or node.bounds.area > 0:
                continue
            name = short_rid(node.resource_id) or node.label()
            events.append(
                UIEvent(
                    kind="ui_broken",
                    signature=f"zero-size:{name}",
                    fingerprint=f"zero-size:{name}",
                    detail=f"clickable {node.class_name} has an empty bounds rectangle",
                    rids=(name,),
                    bounds=(node.bounds.as_list(),),
                )
            )
        return events

    def _clipped(self, screen: Screen, nodes: list[Node]) -> list[UIEvent]:
        width, height = screen.screen_size
        if not width or not height:
            return []
        screen_bounds = Bounds(0, 0, width, height)
        scrollables = [node for node in nodes if node.scrollable]
        events: list[UIEvent] = []
        for node in nodes:
            if not _is_text(node) or not node.has_area:
                continue
            if any(_is_ancestor(scroller, node) for scroller in scrollables):
                # Lists scroll: a row cut by the viewport edge is intended.
                continue
            bounds = node.bounds
            over_left = screen_bounds.left - bounds.left
            over_top = screen_bounds.top - bounds.top
            over_right = bounds.right - screen_bounds.right
            over_bottom = bounds.bottom - screen_bounds.bottom
            if max(over_left, over_top, over_right, over_bottom) <= self.clip_tolerance_px:
                continue
            name = short_rid(node.resource_id) or node.label()
            events.append(
                UIEvent(
                    kind="ui_broken",
                    signature=f"clipped:{name}",
                    fingerprint=f"clipped:{name}",
                    detail=f"text {node.text[:40]!r} is cut by the screen edge {bounds}",
                    rids=(name,),
                    bounds=(bounds.as_list(),),
                )
            )
        return events
