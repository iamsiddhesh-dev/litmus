"""Shared finding model and detector contract (PLAN §7).

Confidence is ``hard`` for crash / ANR / HTTP 5xx and ``heuristic`` for dead
taps, broken UI and 4xx. Replay proves a finding survives 3/3 on a clean state;
it does not prove a heuristic is logically correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


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
            "evidence_dir": str(self.evidence_dir) if self.evidence_dir else None,
        }


class Detector(Protocol):
    """A detector consumes one signal and returns zero or more candidates."""

    name: str

    def detect(self, *args: Any, **kwargs: Any) -> list[Finding]:
        ...