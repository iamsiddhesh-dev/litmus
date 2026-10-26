"""SQLAlchemy 2 (async) ORM models mirroring ``litmus/db/schema.sql``.

The schema is applied with ``CREATE TABLE IF NOT EXISTS`` from ``schema.sql``;
these models are the typed view of the same tables for the worker and API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every Litmus table."""


class Target(Base):
    """A build under test (APK path, Play URL, or local package)."""

    __tablename__ = "targets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, default="apk")
    package: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    min_sdk: Mapped[int | None] = mapped_column(Integer)
    abis: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    storage_path: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    """One explore -> detect -> verify cycle."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, ForeignKey("targets.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="queued")
    step_budget: Mapped[int] = mapped_column(Integer, default=25)
    verify_mode: Mapped[str] = mapped_column(String, default="pm_clear")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps_taken: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Double, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)


class Step(Base):
    """One explore step, recorded with the selector used and the resulting state hash."""

    __tablename__ = "steps"

    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_hash: Mapped[str | None] = mapped_column(String)
    activity: Mapped[str | None] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    selector_json: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Finding(Base):
    """A candidate issue raised during a run."""

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String)
    signature: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    screen: Mapped[str | None] = mapped_column(String)
    first_step_idx: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String, default="medium")
    confidence: Mapped[str] = mapped_column(String, default="heuristic")
    observed_once: Mapped[bool] = mapped_column(Boolean, default=False)


class Verdict(Base):
    """One replay attempt of a finding."""

    __tablename__ = "verdicts"

    finding_id: Mapped[str] = mapped_column(
        String, ForeignKey("findings.id", ondelete="CASCADE"), primary_key=True
    )
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    reproduced: Mapped[bool] = mapped_column(Boolean)
    state_hash: Mapped[str | None] = mapped_column(String)
    evidence_path: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    """A file in the evidence bundle."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id", ondelete="CASCADE"))
    finding_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("findings.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String)


class Event(Base):
    """An SSE event; the API tails this table by ``seq``."""

    __tablename__ = "events"

    run_id: Mapped[str] = mapped_column(String, ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Worker(Base):
    """A registered worker process."""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    runner_kind: Mapped[str] = mapped_column(String)
    profile_json: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="idle")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
