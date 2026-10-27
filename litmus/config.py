"""Runtime configuration, loaded from the environment and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Settings for the Litmus platform. Every field maps to an env var."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres / Redis
    database_url: str = ""
    redis_url: str = ""

    # Models
    openrouter_api_key: str = ""
    together_api_key: str = ""

    # Android / emulator
    litmus_avd: str = "pricebook_avd"
    android_home: str = ""
    android_sdk_root: str = ""

    # Runtime
    artifacts_dir: Path = Field(default=REPO_ROOT / "artifacts")
    api_token: str = ""
    log_level: str = "INFO"
    #: Single tenant, no auth: loopback only (PLAN §1.2, §10).
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Integrations
    slack_webhook_url: str = ""

    @property
    def has_database(self) -> bool:
        return bool(self.database_url.strip())

    @property
    def has_redis(self) -> bool:
        return bool(self.redis_url.strip())

    @property
    def has_model_key(self) -> bool:
        return bool(self.openrouter_api_key.strip() or self.together_api_key.strip())

    def postgres_dsn(self) -> str:
        """Asyncpg DSN for the configured database URL.

        The SQLAlchemy driver prefix (``postgresql+asyncpg://``) is not valid for
        asyncpg, so it is stripped here.
        """
        dsn = self.database_url.strip()
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if dsn.startswith(prefix):
                return "postgresql://" + dsn[len(prefix) :]
        return dsn


@lru_cache
def get_settings() -> Settings:
    """Return cached settings (cached so the environment is read once)."""
    return Settings()
