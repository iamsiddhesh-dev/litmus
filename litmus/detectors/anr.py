"""ANR detector (PLAN §7).

Signal: ``ANR in <package>`` in the logcat buffers, with a 5 s grace applied by
the caller's poll. Bazaar seeds no ANR (contract §3.3), so this exists for real
apps; a stray ``ANR in com.android.systemui`` is never attributed to the app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from litmus.detectors.base import Finding

_ANR_RE = re.compile(r"ANR in\s+([\w.]+)")
_REASON_RE = re.compile(r"Reason:\s*(.+)$")


@dataclass(slots=True)
class AnrEvent:
    package: str
    reason: str = ""
    raw: str = ""

    @property
    def signature(self) -> str:
        return f"ANR in {self.package}"

    @property
    def fingerprint(self) -> str:
        return f"anr:{self.package}:{self.reason}".rstrip(":")

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "reason": self.reason,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
        }


def parse_anrs(logcat: str, package: str) -> list[AnrEvent]:
    """Every ANR block for ``package`` in a logcat dump, de-duplicated."""
    lines = (logcat or "").splitlines()
    events: list[AnrEvent] = []
    for index, line in enumerate(lines):
        match = _ANR_RE.search(line)
        if not match or match.group(1) != package:
            # Never attribute a system-server or launcher ANR to the app.
            continue
        reason = ""
        for follow in lines[index + 1 : index + 6]:
            found = _REASON_RE.search(follow)
            if found:
                reason = found.group(1).strip()
                break
        events.append(
            AnrEvent(package=package, reason=reason, raw="\n".join(lines[index : index + 6]))
        )

    seen: set[str] = set()
    unique: list[AnrEvent] = []
    for event in events:
        if event.fingerprint in seen:
            continue
        seen.add(event.fingerprint)
        unique.append(event)
    return unique


class AnrDetector:
    """Turns logcat text into ANR findings for one package."""

    name = "anr"

    def __init__(self, package: str) -> None:
        self.package = package

    def detect(self, logcat: str) -> list[AnrEvent]:
        return parse_anrs(logcat, self.package)

    def to_finding(self, event: AnrEvent, local_id: str, step_idx: int | None = None) -> Finding:
        return Finding(
            local_id=local_id,
            kind="anr",
            signature=event.signature,
            fingerprint=event.fingerprint,
            confidence="hard",
            step_idx=step_idx,
            detail=event.reason or "the app stopped responding",
            log_text=event.raw,
            probe={"package": self.package, "reason": event.reason},
        )
