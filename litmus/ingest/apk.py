"""APK inspection for ``POST /v1/targets`` (PLAN §13).

Bazaar is a fixture, not a code path: the platform learns package, version,
minSdk and native ABIs from the file itself with ``aapt2``/``aapt``. When the
Android build tools are missing the upload still succeeds - the profile decides
the package for a run - and the fields stay empty rather than being guessed.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from litmus.config import Settings

logger = logging.getLogger(__name__)

#: Refuse absurd uploads early instead of filling the disk.
MAX_APK_BYTES = 512 * 1024 * 1024


@dataclass(slots=True)
class ApkInfo:
    package: str = ""
    version: str = ""
    min_sdk: int | None = None
    abis: str = ""

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "version": self.version,
            "min_sdk": self.min_sdk,
            "abis": self.abis,
        }


def _aapt_candidates(settings: Settings) -> list[Path]:
    """Build-tools binaries, newest build-tools first."""
    roots = [settings.android_home, settings.android_sdk_root]
    found: list[Path] = []
    for root in roots:
        if not root:
            continue
        build_tools = Path(root) / "build-tools"
        if not build_tools.is_dir():
            continue
        for version in sorted(build_tools.iterdir(), reverse=True):
            for name in ("aapt2.exe", "aapt.exe", "aapt2", "aapt"):
                candidate = version / name
                if candidate.is_file():
                    found.append(candidate)
    for name in ("aapt2", "aapt"):
        which = shutil.which(name)
        if which:
            found.append(Path(which))
    return found


def _dump_badging(tool: Path, apk: Path) -> str:
    commands = [[str(tool), "dump", "badging", str(apk)]]
    if tool.name.startswith("aapt2"):
        commands.insert(0, [str(tool), "dump", "badging", str(apk)])
    for command in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("aapt failed: %s", exc)
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return ""


def parse_badging(output: str) -> ApkInfo:
    """Parse ``aapt dump badging`` output."""
    info = ApkInfo()
    match = re.search(r"^package: name='([^']+)'", output, re.MULTILINE)
    if match:
        info.package = match.group(1)
    match = re.search(r"^package:.*versionName='([^']*)'", output, re.MULTILINE)
    if match:
        info.version = match.group(1)
    match = re.search(r"^sdkVersion:'(\d+)'", output, re.MULTILINE)
    if match:
        info.min_sdk = int(match.group(1))
    abis = re.findall(r"^native-code: (.*)$", output, re.MULTILINE)
    if abis:
        info.abis = abis[0].replace("'", "").strip()
    return info


def inspect_apk(apk: Path, settings: Settings) -> ApkInfo:
    """Read package metadata from an APK, or return empty fields."""
    for tool in _aapt_candidates(settings):
        output = _dump_badging(tool, apk)
        if output:
            info = parse_badging(output)
            if info.package:
                return info
    logger.info("no aapt2/aapt found; uploaded APK is not inspected")
    return ApkInfo()