"""Dead-tap detector (PLAN §7, contract §4 B3).

Navigation hash unchanged is **not** enough. Bazaar's first add-to-cart tap
leaves the navigation hash identical (same resource ids) but raises a Toast -
which lives in its own window and never enters the hierarchy - and B1 only
fires on the *second* tap. A rule that stopped at the navigation hash would
report the first tap as a dead tap, and replay would confirm it 3/3: a
deterministic false positive that replay cannot catch.

So a tap is dead only when *all* of these hold:

* the navigation hash is unchanged;
* the tapped node's content hash (text, content-desc, checked, selected,
  enabled) is unchanged, and the node is still on screen;
* the whole screen's content hash is unchanged (a cart counter elsewhere in
  the hierarchy counts as a visible effect);
* no IME was raised and no Toast was shown (they are the visible effect of a
  successful tap that does not touch the hierarchy);
* no network request was in flight in the observation window.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from litmus.detectors.base import Finding
from litmus.detectors.ui import short_rid
from litmus.runner.driver import Node, Screen


def node_content(node: Node) -> str:
    """The tapped node's visible content, in a stable order (PLAN §7)."""
    return "|".join(
        [
            (node.text or "").strip(),
            (node.content_desc or "").strip(),
            "1" if node.checked else "0",
            "1" if node.selected else "0",
            "1" if node.enabled else "0",
        ]
    )


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def node_content_hash(node: Node) -> str:
    return content_hash(node_content(node))


def screen_content_hash(screen: Screen, package: str = "") -> str:
    """Content hash of every app node, in document order.

    Unlike the navigation hash this moves when text, a checked state, or the
    set of visible nodes changes - which is exactly what a successful tap does.
    """
    parts = []
    for node in screen.app_nodes(package or None):
        parts.append(f"{node.resource_id}|{node_content(node)}")
    return content_hash("\n".join(parts))


@dataclass(slots=True)
class TapObservation:
    """Everything one tap did (or did not do)."""

    rid: str
    label: str
    clickable: bool
    node_present_after: bool
    content_before: str
    content_after: str
    nav_before: str
    nav_after: str
    screen_before: str
    screen_after: str
    ime_raised: bool = False
    toast_seen: bool = False
    network_activity: bool = False


@dataclass(slots=True)
class DeadTapEvent:
    signature: str
    fingerprint: str
    detail: str
    rid: str
    nav_hash: str
    screen_hash: str
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "rid": self.rid,
            "detail": self.detail,
            "nav_hash": self.nav_hash,
            "screen_hash": self.screen_hash,
            "content_hash": self.content_hash,
        }


def is_dead_tap(observation: TapObservation) -> tuple[bool, str]:
    """Decide whether one tap was dead. Returns ``(dead, reason)``."""
    if not observation.clickable:
        return False, "node is not clickable"
    if not observation.node_present_after:
        return False, "the tapped node disappeared"
    if observation.nav_before != observation.nav_after:
        return False, "navigation changed"
    if observation.content_before != observation.content_after:
        return False, "the tapped node's content changed"
    if observation.screen_before != observation.screen_after:
        return False, "screen content changed"
    if observation.ime_raised:
        return False, "the tap raised the IME"
    if observation.toast_seen:
        return False, "the tap showed a Toast"
    if observation.network_activity:
        return False, "the tap started a network request"
    return True, "no navigation, content, IME, Toast or network effect"


class DeadTapDetector:
    """Turns a tap observation into a finding for one node."""

    name = "deadtap"

    def __init__(self, package: str) -> None:
        self.package = package

    def evaluate(self, observation: TapObservation) -> DeadTapEvent | None:
        dead, reason = is_dead_tap(observation)
        if not dead:
            return None
        name = short_rid(observation.rid) or observation.label or "node"
        signature = f"deadtap:{name}"
        return DeadTapEvent(
            signature=signature,
            fingerprint=signature,
            detail=f"tap on {name} had no effect: {reason}",
            rid=name,
            nav_hash=observation.nav_before,
            screen_hash=observation.screen_before,
            content_hash=observation.content_before,
        )

    def to_finding(
        self, event: DeadTapEvent, local_id: str, step_idx: int | None = None
    ) -> Finding:
        return Finding(
            local_id=local_id,
            kind="deadtap",
            signature=event.signature,
            fingerprint=event.fingerprint,
            confidence="heuristic",
            step_idx=step_idx,
            detail=event.detail,
            screen_hash=event.nav_hash,
            probe={
                "rid": event.rid,
                "nav_hash": event.nav_hash,
                "screen_hash": event.screen_hash,
                "content_hash": event.content_hash,
            },
        )
