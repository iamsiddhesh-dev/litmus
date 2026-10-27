"""Heuristic-first action policy (PLAN §5.1).

Order of preference:

1. unvisited actionable element on this screen (rid/desc/text, then structural);
2. unfilled form field (credentials from the profile);
3. navigation / back / scroll;
4. LLM tie-break - deferred, M1 is heuristic only;
5. restart the app after three identical hashes.

The policy is deliberately free of target-specific knowledge: a Bazaar-only
rule would belong in ``profiles/`` or ``fixtures/``, not here.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from litmus.runner.driver import Node, Screen, selector_ladder

#: Container classes whose children behave like list rows.
LIST_CONTAINERS = ("ListView", "RecyclerView")

#: Text that marks a submit button when a form is present.
SUBMIT_HINTS = ("continue", "sign in", "signin", "log in", "login", "submit", "next")


@dataclass(slots=True)
class Action:
    """One thing the runner does to the device."""

    kind: str  # tap | input | key | scroll | hide_ime | launch
    selectors: list[dict] = field(default_factory=list)
    text: str | None = None
    keycode: str | None = None
    target: str = ""
    reason: str = ""
    force: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "text": self.text,
            "keycode": self.keycode,
            "reason": self.reason,
            "selectors": self.selectors,
            "force": self.force,
        }


@dataclass(slots=True)
class StepRecord:
    """One recorded explore step."""

    idx: int
    action: Action
    state_before: str
    state_after: str = ""
    activity: str = ""
    result: str = "ok"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "action": self.action.to_dict(),
            "state_before": self.state_before,
            "state_after": self.state_after,
            "activity": self.activity,
            "result": self.result,
            "ts": self.ts,
        }


def _node_key(node: Node) -> str:
    """Identity of a node inside one screen, used to avoid re-tapping it."""
    return f"{node.resource_id}|{node.path}"


def _contains(outer, inner) -> bool:
    """True when ``inner`` sits entirely inside ``outer``."""
    return (
        outer.left <= inner.left
        and outer.top <= inner.top
        and outer.right >= inner.right
        and outer.bottom >= inner.bottom
    )


class HeuristicPolicy:
    """Chooses the next action from the current screen."""

    def __init__(self, profile: dict[str, Any] | None = None, package: str = "") -> None:
        self.profile = profile or {}
        self.package = package or self.profile.get("package", "")
        credentials = self.profile.get("credentials") or {}
        self.email = str(credentials.get("email", "") or "")
        self.password = str(credentials.get("password", "") or "")
        self.max_repeats = 2
        self.visited: dict[str, set[str]] = defaultdict(set)
        self.last_action: Action | None = None
        self.last_state_hash: str = ""
        self.repeat_count = 0
        self.back_count = 0
        self.scrolled_states: set[str] = set()
        self.hash_history: list[str] = []

    # -- public API -------------------------------------------------------
    def next_action(
        self, screen: Screen, state_hash: str, ime_shown: bool = False
    ) -> Action | None:
        """Pick the next action for the current screen."""
        self.hash_history.append(state_hash)
        if len(self.hash_history) > 3:
            self.hash_history.pop(0)

        form_action = self._form_action(screen, ime_shown)
        if form_action is not None:
            return form_action

        for node in self._candidates(screen):
            key = _node_key(node)
            if key in self.visited[state_hash]:
                continue
            return Action(
                kind="tap",
                selectors=selector_ladder(screen, node),
                target=node.label(),
                reason="unvisited actionable element",
            )

        # A tap that changed nothing is worth one more try (double-tap bugs).
        if (
            self.last_action is not None
            and self.last_action.kind == "tap"
            and state_hash == self.last_state_hash
            and self.repeat_count < self.max_repeats
        ):
            return Action(
                kind="tap",
                selectors=list(self.last_action.selectors),
                target=self.last_action.target,
                reason="repeat: screen did not change",
            )

        if self._has_scrollable(screen) and state_hash not in self.scrolled_states:
            self.scrolled_states.add(state_hash)
            return Action(kind="scroll", target="screen", reason="scroll for more content")

        if self.back_count < 2:
            self.back_count += 1
            return Action(kind="key", keycode="4", target="back", reason="navigate back")

        if len(self.hash_history) == 3 and len(set(self.hash_history)) == 1:
            return Action(kind="launch", target="app", reason="three identical hashes")

        return None

    def observe(self, action: Action, state_before: str, state_after: str) -> None:
        """Record what an executed action did, so the policy can adapt."""
        if action.kind == "tap" and action.selectors:
            self.visited[state_before].add(self._key_of_action(action))
        if state_after == state_before:
            self.repeat_count += 1
        else:
            self.repeat_count = 0
            self.back_count = 0
        self.last_action = action
        self.last_state_hash = state_after

    def abandon_repeat(self) -> None:
        """Stop re-tapping the last action (its selector no longer resolves)."""
        self.repeat_count = self.max_repeats
        self.last_action = None

    @staticmethod
    def _key_of_action(action: Action) -> str:
        rid = ""
        path = ""
        for selector in action.selectors:
            if selector.get("level") == "resource_id":
                rid = str(selector.get("value", ""))
            elif selector.get("level") == "path":
                path = str(selector.get("value", ""))
        return f"{rid}|{path}"

    # -- form handling ----------------------------------------------------
    def _form_action(self, screen: Screen, ime_shown: bool) -> Action | None:
        """Fill a login form, hide the keyboard, then submit."""
        fields = screen.edit_texts(self.package)
        if not fields:
            return None
        password_field = self._find_password_field(fields)
        if password_field is None:
            return None

        email_field = self._find_email_field(fields, password_field)
        if email_field is not None and not email_field.is_filled_field():
            if not self.email:
                return None
            return Action(
                kind="input",
                selectors=selector_ladder(screen, email_field),
                text=self.email,
                target=email_field.label(),
                reason="fill email from profile credentials",
            )

        if not password_field.is_filled_field():
            if not self.password:
                return None
            return Action(
                kind="input",
                selectors=selector_ladder(screen, password_field),
                text=self.password,
                target=password_field.label(),
                reason="fill password from profile credentials",
            )

        submit = self._find_submit(screen)
        if submit is None:
            # The form is filled but no submit button is on screen: on a 640 dp
            # device the keyboard is covering it (contract §6). Trust the
            # hierarchy over ``dumpsys``, which lags behind a fast input.
            return Action(
                kind="hide_ime",
                force=True,
                target="ime",
                reason="submit button hidden by the keyboard",
            )
        if ime_shown:
            # The keyboard covers the submit button on a 640 dp screen.
            return Action(kind="hide_ime", target="ime", reason="dismiss the soft keyboard")
        return Action(
            kind="tap",
            selectors=selector_ladder(screen, submit),
            target=submit.label(),
            reason="submit the form",
        )

    @staticmethod
    def _find_password_field(fields: list[Node]) -> Node | None:
        for node in fields:
            if node.password:
                return node
        for node in fields:
            hint = f"{node.resource_id} {node.text}".lower()
            if "password" in hint or "passwd" in hint:
                return node
        return None

    @staticmethod
    def _find_email_field(fields: list[Node], password_field: Node) -> Node | None:
        for node in fields:
            if node is password_field:
                continue
            hint = f"{node.resource_id} {node.text}".lower()
            if "email" in hint or "e-mail" in hint:
                return node
        for node in fields:
            if node is not password_field:
                return node
        return None

    @staticmethod
    def _find_submit(screen: Screen) -> Node | None:
        buttons = [
            node
            for node in screen.app_nodes()
            if node.clickable and node.enabled and node.has_area and "Button" in node.class_name
        ]
        if not buttons:
            return None
        for node in buttons:
            text = (node.text or "").strip().lower()
            if any(hint in text for hint in SUBMIT_HINTS):
                return node
        return buttons[0]

    # -- candidate extraction ---------------------------------------------
    def _candidates(self, screen: Screen) -> list[Node]:
        """Actionable elements, in document order."""
        candidates: list[Node] = []
        for node in screen.app_nodes(self.package):
            if not node.enabled or not node.has_area:
                continue
            if node.clickable or node.long_clickable or node.is_edit_text:
                candidates.append(node)
        candidates.extend(self._row_candidates(screen))
        return self._dedupe(candidates)

    def _row_candidates(self, screen: Screen) -> list[Node]:
        """Rows of a list - often not marked clickable, sometimes not scrollable."""
        rows: list[Node] = []
        for node in screen.app_nodes(self.package):
            is_list = any(marker in node.class_name for marker in LIST_CONTAINERS)
            if not node.scrollable and not is_list:
                continue
            for child in node.children:
                if not child.enabled or not child.has_area:
                    continue
                if is_list or self._looks_like_row(child):
                    rows.append(child)
        return rows

    @staticmethod
    def _looks_like_row(node: Node) -> bool:
        """A row carries an id and at least one text label below it."""
        if not node.resource_id:
            return False
        for child in node.children:
            if "TextView" in child.class_name:
                return True
        return False

    @staticmethod
    def _dedupe(candidates: list[Node]) -> list[Node]:
        """Keep one node per tap target: the innermost real control wins."""
        ordered = sorted(candidates, key=lambda node: node.order)
        kept: list[Node] = []
        for node in ordered:
            duplicate = False
            for position, existing in enumerate(kept):
                if existing.center == node.center:
                    # Same target reachable two ways: prefer the one with an id.
                    if node.resource_id and not existing.resource_id:
                        kept[position] = node
                    duplicate = True
                    break
                if existing.bounds and node.bounds and _contains(existing.bounds, node.bounds):
                    if node.resource_id or node.is_edit_text:
                        kept[position] = node
                    duplicate = True
                    break
            if not duplicate:
                kept.append(node)
        return kept

    @staticmethod
    def _has_scrollable(screen: Screen) -> bool:
        return any(node.scrollable for node in screen.app_nodes())
