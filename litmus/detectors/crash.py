"""Crash detector (PLAN §7, contract §3.2).

Signal: ``logcat -b crash`` containing ``FATAL EXCEPTION`` for the app package.
Fingerprint: exception class + top app frame, with the PID and line numbers
removed so the same bug collapses to one finding across replays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from litmus.detectors.base import Finding

#: ``E/AndroidRuntime( 9503): ...`` (brief) or ``... E AndroidRuntime: ...``
#: (threadtime). Everything before the message is transport noise.
_LOG_PREFIX_RE = re.compile(r"AndroidRuntime[^:]*:\s?(.*)$")
_FATAL_RE = re.compile(r"FATAL EXCEPTION:\s*(.*)")
_PROCESS_RE = re.compile(r"Process:\s*([\w.]+),\s*PID:\s*(\d+)")
_EXCEPTION_RE = re.compile(
    r"^(?:Caused by:\s*)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)"
    r"(?::\s*(.*))?$"
)
_FRAME_RE = re.compile(r"^\s*at\s+([\w$.]+)\.([\w$<>]+)\(([^)]*)\)")
_METHOD_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\(\)")


@dataclass(slots=True)
class CrashEvent:
    """One ``FATAL EXCEPTION`` block for the app under test."""

    package: str
    exception: str
    message: str
    thread: str = "main"
    pid: str = ""
    frames: list[tuple[str, str, str]] = field(default_factory=list)
    raw: str = ""

    @property
    def app_frames(self) -> list[tuple[str, str, str]]:
        prefix = self.package + "."
        return [frame for frame in self.frames if frame[0].startswith(prefix)]

    @property
    def top_app_frame(self) -> str:
        frames = self.app_frames
        if not frames:
            return ""
        return f"{frames[0][0]}.{frames[0][1]}"

    @property
    def fingerprint(self) -> str:
        """Exception class + top app frame; no PID, no line numbers."""
        top = self.top_app_frame
        return f"{self.exception}@{top}" if top else self.exception

    @property
    def signature(self) -> str:
        """Human/ground-truth signature, e.g. ``NullPointerException:Integer.intValue``."""
        return f"{self.exception}:{_short_reason(self.message)}"

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "exception": self.exception,
            "message": self.message,
            "thread": self.thread,
            "pid": self.pid,
            "top_app_frame": self.top_app_frame,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
        }


def _short_reason(message: str) -> str:
    """Reduce an exception message to a stable short token.

    B1's message mentions ``java.lang.Integer.intValue()``; the ground truth
    signature is ``Integer.intValue``. B5 carries a free-form message, which is
    kept verbatim.
    """
    message = (message or "").strip()
    if not message:
        return "fatal exception"
    calls = _METHOD_CALL_RE.findall(message)
    if calls:
        parts = calls[-1].split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return message


def _message_of(line: str) -> str:
    """Strip the logcat transport prefix from one crash-buffer line."""
    match = _LOG_PREFIX_RE.search(line)
    return (match.group(1) if match else line).rstrip()


def parse_crashes(logcat: str, package: str) -> list[CrashEvent]:
    """Parse every app crash from a ``logcat -b crash`` dump."""
    events: list[CrashEvent] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        event = _parse_block(block, package)
        if event is not None:
            events.append(event)
        block.clear()

    for raw_line in (logcat or "").splitlines():
        line = _message_of(raw_line)
        if "FATAL EXCEPTION" in line:
            flush()
            block.append(line)
        elif block:
            if line.startswith("---------"):
                flush()
            elif line.strip():
                block.append(line)
    flush()

    # One bug observed in three replays is one finding, not three.
    seen: set[str] = set()
    unique: list[CrashEvent] = []
    for event in events:
        if event.fingerprint in seen:
            continue
        seen.add(event.fingerprint)
        unique.append(event)
    return unique


def _parse_block(lines: list[str], package: str) -> CrashEvent | None:
    thread = "main"
    process = ""
    pid = ""
    exception = ""
    message = ""
    frames: list[tuple[str, str, str]] = []

    for line in lines:
        stripped = line.strip()
        match = _FATAL_RE.search(stripped)
        if match:
            thread = match.group(1).strip() or "main"
            continue
        match = _PROCESS_RE.search(stripped)
        if match:
            process, pid = match.group(1), match.group(2)
            continue
        match = _FRAME_RE.match(line)
        if match:
            frames.append((match.group(1), match.group(2), match.group(3)))
            continue
        if not exception and not stripped.startswith(("at ", "Caused by:", "Suppressed:")):
            match = _EXCEPTION_RE.match(stripped)
            if match:
                exception = match.group(1)
                message = (match.group(2) or "").strip()

    if not exception:
        return None

    app_prefix = package + "."
    belongs = process == package or any(
        frame[0].startswith(app_prefix) for frame in frames
    )
    if not belongs:
        # Never confirm a crash from the launcher, System UI, or another app.
        return None

    return CrashEvent(
        package=package,
        exception=exception,
        message=message,
        thread=thread,
        pid=pid,
        frames=frames,
        raw="\n".join(lines),
    )


class CrashDetector:
    """Turns crash-buffer text into findings for one package."""

    name = "crash"

    def __init__(self, package: str) -> None:
        self.package = package
        self._seen: set[str] = set()

    def detect(self, logcat: str) -> list[CrashEvent]:
        return parse_crashes(logcat, self.package)

    def to_finding(self, event: CrashEvent, local_id: str, step_idx: int | None = None) -> Finding:
        return Finding(
            local_id=local_id,
            kind="crash",
            signature=event.signature,
            fingerprint=event.fingerprint,
            confidence="hard",
            screen=self._screen_name(event),
            step_idx=step_idx,
            detail=event.message,
            observed_once=True,
            log_text=event.raw,
        )

    @staticmethod
    def _screen_name(event: CrashEvent) -> str:
        top = event.top_app_frame
        if not top:
            return ""
        activity = top.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        return activity