"""Artifact URLs and the path guard (PLAN §4).

Evidence is served from ``ARTIFACTS_DIR`` only. Every lookup resolves the path
and re-checks containment, so ``..`` and symlinks cannot escape the directory.
"""

from __future__ import annotations

from pathlib import Path

ARTIFACT_PREFIX = "/artifacts"


def artifact_url(path: str | Path | None, root: Path) -> str | None:
    """Map an absolute artifact path to its served URL, or ``None`` if outside."""
    if not path:
        return None
    try:
        relative = Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return f"{ARTIFACT_PREFIX}/{relative.as_posix()}"


def resolve_artifact(relative: str, root: Path) -> Path | None:
    """Resolve a request path inside ``root``, rejecting escapes and misses."""
    if not relative or "\x00" in relative:
        return None
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None