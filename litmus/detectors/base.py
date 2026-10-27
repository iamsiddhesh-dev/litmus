"""Shared finding model and detector contract (PLAN §7).

Confidence is ``hard`` for crash / ANR / HTTP 5xx and ``heuristic`` for dead
taps, broken UI and 4xx. Replay proves a finding survives 3/3 on a clean state;
it does not prove a heuristic is logically correct.

A finding carries the data its replay probe needs in :attr:`Finding.probe`:
the shapes are per kind (see ``worker.verify``) so one trace format covers a
crash, an HTTP status, a dead tap and a broken layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: Kinds whose signal is objective (PLAN §7).
HARD_KINDS = frozenset({"crash", "anr", "http_5xx"})
#: Kinds that survive replay but are not proof of a defect.
HEURISTIC_KINDS = frozenset({"deadtap", "ui_broken", "http_4xx"})
#: Signals that describe the environment, never the app under test.
FILTERED_KINDS = frozenset({"unsupported_abi", "pinning"})


def confidence_for(kind: str) -> str:
    return "hard" if kind in HARD_KINDS else "heuristic"


@dataclass(slots=True)
class Finding:
    """A candidate issue raised during a run and later replayed."""

    local_id: str
    kind: str
    signature: str
    fingerprint: str
    confidence: str = "heuristic"
    screen: str = ""
    step_idx: int | None = None
    detail: str = ""
    observed_once: bool = True
    log_text: str = ""
    verdict: str | None = None
    attempts: list[Any] = field(default_factory=list)
    evidence_dir: Path | None = None
    #: Navigation hash of the screen where the symptom is visible/triggered.
    screen_hash: str = ""
    #: First step of the segment this finding belongs to. A run restarts the app
    #: after a crash, so a finding late in the run must not replay the crash
    #: steps that came before its own segment.
    trace_start: int = 0
    #: Kind-specific data the replay probe reads (flow signature, node content
    #: hash, rid pair, ...). Persisted with the trace so evidence is self-contained.
    probe: dict[str, Any] = field(default_factory=dict)
    #: Extra files written into the evidence bundle (name -> JSON payload).
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def short_title(self) -> str:
        return f"{self.local_id} {self.kind} {self.signature}"

    def to_dict(self) -> dict:
        return {
            "id": self.local_id,
            "kind": self.kind,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
            "confidence": self.confidence,
            "screen": self.screen,
            "step_idx": self.step_idx,
            "detail": self.detail,
            "observed_once": self.observed_once,
            "verdict": self.verdict,
            "screen_hash": self.screen_hash,
            "trace_start": self.trace_start,
            "probe": self.probe,
            "evidence_dir": str(self.evidence_dir) if self.evidence_dir else None,
        }


class Detector(Protocol):
    """A detector consumes one signal and returns zero or more candidates."""

    name: str

    def detect(self, *args: Any, **kwargs: Any) -> list[Any]:
        ...
