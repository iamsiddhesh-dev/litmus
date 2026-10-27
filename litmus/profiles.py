"""Profile registry: which profile drives which package.

A profile is *how* the platform drives an app (selectors, login, allowed hosts),
never what is wrong with it. A target uploaded from the dashboard arrives with no
profile attached, so the platform picks one by package: an exact ``package:``
match in ``profiles/*.yaml`` wins, and the caller falls back to ``generic``.

That keeps the dashboard form to one input - the APK - without hiding a decision
the user would have had to make in a dropdown they could not evaluate.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERIC = "generic"


@lru_cache(maxsize=1)
def package_index() -> dict[str, str]:
    """``package -> profile name`` for every profile that declares a package."""
    index: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "profiles").glob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        package = str(data.get("package") or "").strip()
        name = str(data.get("profile_name") or path.stem).strip()
        if package and name and name != GENERIC:
            index.setdefault(package, name)
    return index


def profile_for_package(package: str | None) -> str | None:
    """The profile that claims this package, or ``None`` when nothing matches."""
    if not package:
        return None
    return package_index().get(package.strip())
