"""``mqa doctor`` -- environment preflight.

Checks, in order: Python version, adb, ``ANDROID_HOME``, a defined AVD
(emulator may be off), Postgres reachability, Redis reachability, and free disk
space. Exits 0 when every required check passes.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from litmus.config import REPO_ROOT, Settings, get_settings


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    required: bool = True

    @property
    def ok(self) -> bool:
        """True when this check must not fail the doctor run."""
        if self.status is not Status.FAIL:
            return True
        return not self.required


REQUIRED_PYTHON = (3, 12)
MIN_FREE_DISK_GB = 5.0


def _check_python() -> Check:
    version = sys.version_info
    got = (version.major, version.minor)
    if got == REQUIRED_PYTHON:
        return Check("python", Status.PASS, f"{version.major}.{version.minor}.{version.micro}")
    return Check(
        "python",
        Status.FAIL,
        f"{version.major}.{version.minor}.{version.micro} "
        f"(need {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]})",
    )


def _adb_path(settings: Settings) -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    for root in (settings.android_home, settings.android_sdk_root):
        if root:
            candidate = Path(root) / "platform-tools" / "adb.exe"
            if candidate.exists():
                return str(candidate)
    return None


def _check_adb(settings: Settings) -> Check:
    adb = _adb_path(settings)
    if not adb:
        return Check("adb", Status.FAIL, "adb not found on PATH or under ANDROID_HOME")
    try:
        result = subprocess.run(
            [adb, "version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("adb", Status.FAIL, f"adb present but not runnable: {exc}")
    first_line = (result.stdout or result.stderr).strip().splitlines()[:1]
    return Check("adb", Status.PASS, first_line[0] if first_line else adb)


def _check_android_home(settings: Settings) -> Check:
    root = settings.android_home or settings.android_sdk_root
    if not root:
        return Check(
            "ANDROID_HOME",
            Status.FAIL,
            "ANDROID_HOME / ANDROID_SDK_ROOT not set (see .env.example)",
        )
    path = Path(root)
    if not path.is_dir():
        return Check("ANDROID_HOME", Status.FAIL, f"{path} is not a directory")
    return Check("ANDROID_HOME", Status.PASS, str(path))


def _emulator_path(settings: Settings) -> str | None:
    found = shutil.which("emulator")
    if found:
        return found
    for root in (settings.android_home, settings.android_sdk_root):
        if root:
            candidate = Path(root) / "emulator" / "emulator.exe"
            if candidate.exists():
                return str(candidate)
    return None


def _list_avds(settings: Settings) -> tuple[list[str], str | None]:
    emulator = _emulator_path(settings)
    if not emulator:
        return [], "emulator binary not found under ANDROID_HOME"
    try:
        result = subprocess.run(
            [emulator, "-list-avds"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"could not list AVDs: {exc}"
    avds = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return avds, None


def _check_avd(settings: Settings) -> Check:
    avds, error = _list_avds(settings)
    if error:
        return Check("avd", Status.FAIL, error)
    if settings.litmus_avd in avds:
        return Check(
            "avd",
            Status.PASS,
            f"{settings.litmus_avd} defined ({len(avds)} available; emulator may be off)",
        )
    if avds:
        return Check(
            "avd",
            Status.FAIL,
            f"LITMUS_AVD={settings.litmus_avd} not defined; available: {', '.join(avds)}",
        )
    return Check("avd", Status.FAIL, f"LITMUS_AVD={settings.litmus_avd} is not defined")


def _check_adb_devices() -> Check:
    adb = shutil.which("adb")
    if not adb:
        return Check("device", Status.WARN, "adb not found; cannot list devices", required=False)
    try:
        result = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("device", Status.WARN, f"could not list devices: {exc}", required=False)
    lines = [line for line in (result.stdout or "").splitlines()[1:] if line.strip()]
    booted = [line for line in lines if line.endswith("device")]
    if booted:
        return Check("device", Status.PASS, f"{len(booted)} device(s) attached")
    return Check("device", Status.WARN, "no device attached (emulator may be off)", required=False)


async def _check_postgres(settings: Settings) -> Check:
    if not settings.has_database:
        return Check(
            "postgres",
            Status.FAIL,
            "DATABASE_URL not set - copy .env.example to .env and fill it in",
        )
    try:
        import asyncpg
    except ImportError:
        return Check("postgres", Status.FAIL, "asyncpg is not installed")
    try:
        conn = await asyncpg.connect(settings.postgres_dsn(), timeout=10)
    except Exception as exc:  # noqa: BLE001 - any connection failure is a doctor failure
        return Check("postgres", Status.FAIL, f"unreachable: {type(exc).__name__}: {exc}")
    try:
        version = await conn.fetchval("select version()")
    except Exception as exc:  # noqa: BLE001
        return Check("postgres", Status.FAIL, f"query failed: {exc}")
    finally:
        await conn.close()
    short = str(version).split(",")[0]
    return Check("postgres", Status.PASS, short)


async def _check_redis(settings: Settings) -> Check:
    """Redis is an accelerator, not a requirement.

    With Redis down the API runs one lifecycle in-process (the documented
    single-process dev fallback), so an unreachable Redis is a warning: a
    developer without Redis can still drive the dashboard and the CLI.
    """
    if not settings.has_redis:
        return Check(
            "redis",
            Status.WARN,
            "REDIS_URL not set - the API will run lifecycles in-process",
            required=False,
        )
    try:
        import redis.asyncio as redis
    except ImportError:
        return Check(
            "redis",
            Status.WARN,
            "redis package is not installed - in-process fallback",
            required=False,
        )
    client = redis.from_url(settings.redis_url, socket_connect_timeout=10)
    try:
        pong = await client.ping()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "redis",
            Status.WARN,
            f"unreachable: {type(exc).__name__}: {exc} - the API will run lifecycles in-process",
            required=False,
        )
    finally:
        await client.aclose()
    return Check("redis", Status.PASS, "PING ok" if pong else "PING returned falsy")


def _check_disk(settings: Settings) -> Check:
    target = settings.artifacts_dir
    probe = target if target.exists() else target.parent
    probe = probe if probe.exists() else REPO_ROOT
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / (1024**3)
    if free_gb < MIN_FREE_DISK_GB:
        return Check(
            "disk", Status.FAIL, f"{free_gb:.1f} GB free on {probe} (need {MIN_FREE_DISK_GB} GB)"
        )
    return Check("disk", Status.PASS, f"{free_gb:.1f} GB free on {probe}")


def _check_model_key(settings: Settings) -> Check:
    if settings.has_model_key:
        return Check("model_key", Status.PASS, "a model provider key is configured")
    return Check(
        "model_key",
        Status.WARN,
        "no OPENROUTER_API_KEY / TOGETHER_API_KEY - LLM-assisted steps are disabled",
        required=False,
    )


async def run_doctor(settings: Settings | None = None) -> list[Check]:
    """Run every environment check and return the results."""
    settings = settings or get_settings()
    checks = [
        _check_python(),
        _check_adb(settings),
        _check_android_home(settings),
        _check_avd(settings),
        _check_adb_devices(),
        await _check_postgres(settings),
        await _check_redis(settings),
        _check_disk(settings),
        _check_model_key(settings),
    ]
    return checks


def doctor_exit_code(checks: list[Check]) -> int:
    """Return 0 when no required check failed."""
    return 0 if all(check.ok for check in checks) else 1


def run_doctor_sync(settings: Settings | None = None) -> list[Check]:
    """Synchronous wrapper around :func:`run_doctor`."""
    return asyncio.run(run_doctor(settings))
