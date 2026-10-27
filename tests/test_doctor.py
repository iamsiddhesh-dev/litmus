"""Tests for the environment preflight (M0)."""

from __future__ import annotations

import sys

from litmus.config import Settings
from litmus.doctor import (
    Check,
    Status,
    _check_disk,
    _check_python,
    _check_redis,
    doctor_exit_code,
)


def test_python_is_312_in_this_env() -> None:
    check = _check_python()
    assert check.status is Status.PASS, check.detail
    assert sys.version_info[:2] == (3, 12)


def test_exit_code_zero_when_all_pass() -> None:
    checks = [Check("a", Status.PASS, "ok"), Check("b", Status.WARN, "meh", required=False)]
    assert doctor_exit_code(checks) == 0


def test_exit_code_one_on_required_failure() -> None:
    checks = [Check("a", Status.PASS, "ok"), Check("b", Status.FAIL, "broken")]
    assert doctor_exit_code(checks) == 1


def test_optional_failure_does_not_break_exit_code() -> None:
    checks = [Check("a", Status.PASS, "ok"), Check("b", Status.FAIL, "broken", required=False)]
    assert doctor_exit_code(checks) == 0


def test_disk_check_reports_free_space() -> None:
    settings = Settings(artifacts_dir="artifacts")
    check = _check_disk(settings)
    assert check.status in (Status.PASS, Status.FAIL)
    assert "GB free" in check.detail


async def test_redis_down_warns_instead_of_failing(monkeypatch) -> None:
    """Redis is an accelerator: without it the API runs lifecycles in-process."""
    import redis.asyncio as redis_asyncio

    class Unreachable:
        async def ping(self):
            raise ConnectionError("Error 22 connecting to 127.0.0.1:6379")

        async def aclose(self):
            return None

    monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **k: Unreachable())
    settings = Settings(redis_url="redis://127.0.0.1:6379")
    check = await _check_redis(settings)
    assert check.status is Status.WARN, check.detail
    assert check.required is False
    # A developer without Redis can still drive the dashboard and the CLI.
    assert doctor_exit_code([Check("python", Status.PASS, "ok"), check]) == 0


async def test_redis_missing_url_warns_not_fails() -> None:
    settings = Settings(redis_url="")
    check = await _check_redis(settings)
    assert check.status is Status.WARN
    assert "in-process" in check.detail


def test_postgres_dsn_strips_sqlalchemy_driver_prefix() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@host:5432/postgres"
    )
    assert settings.postgres_dsn() == "postgresql://user:pass@host:5432/postgres"


def test_missing_database_url_is_not_configured() -> None:
    assert not Settings(database_url="").has_database
    assert Settings(database_url="postgresql://x").has_database
