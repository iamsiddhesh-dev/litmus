"""Postgres store for the control plane (PLAN §9-§10).

The API is stateless: everything it serves comes from these queries. The worker
writes through the same store, so the two processes cannot disagree about the
shape of a run.

asyncpg has no implicit JSONB codec, so payloads are written as JSON text and
cast with ``::jsonb`` - the same convention ``litmus.db.repo`` uses.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from litmus.config import Settings, get_settings

RUN_COLUMNS = """
    id, target_id, status, step_budget, verify_mode, started_at, ended_at,
    steps_taken, llm_calls, tokens, cost_usd, error, created_at
"""


def _json_loads(value: Any) -> Any:
    """Decode a JSONB column that asyncpg returned as raw text."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "payload_json" in data:
        data["payload_json"] = _json_loads(data["payload_json"])
    if "selector_json" in data:
        data["selector_json"] = _json_loads(data["selector_json"])
    return data


class Store:
    """Thin async wrapper around one asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- lifecycle --------------------------------------------------------
    @classmethod
    async def connect(cls, settings: Settings | None = None) -> Store:
        settings = settings or get_settings()
        if not settings.has_database:
            raise RuntimeError("DATABASE_URL is not configured")
        pool = await asyncpg.create_pool(
            settings.postgres_dsn(),
            min_size=1,
            max_size=5,
            # The Supabase session pooler rejects prepared statements (PLAN §18).
            statement_cache_size=0,
        )
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # -- targets ----------------------------------------------------------
    async def create_target(self, target: dict[str, Any]) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            INSERT INTO targets (
                id, kind, package, version, min_sdk, abis, source, storage_path, profile
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE SET
                package = EXCLUDED.package,
                version = EXCLUDED.version,
                min_sdk = EXCLUDED.min_sdk,
                abis = EXCLUDED.abis,
                source = EXCLUDED.source,
                storage_path = EXCLUDED.storage_path,
                profile = EXCLUDED.profile
            RETURNING id, kind, package, version, min_sdk, abis, source,
                      storage_path, profile, created_at
            """,
            target["id"],
            target.get("kind", "apk"),
            target.get("package"),
            target.get("version"),
            target.get("min_sdk"),
            target.get("abis"),
            target.get("source"),
            target.get("storage_path"),
            target.get("profile"),
        )
        return dict(row)

    async def get_target(self, target_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT id, kind, package, version, min_sdk, abis, source,
                   storage_path, profile, created_at
            FROM targets WHERE id = $1
            """,
            target_id,
        )
        return dict(row) if row else None

    async def list_targets(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, kind, package, version, source, storage_path, profile, created_at
            FROM targets ORDER BY created_at DESC LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    # -- runs -------------------------------------------------------------
    async def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO runs (id, target_id, status, step_budget, verify_mode)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING {RUN_COLUMNS}
            """,
            run["id"],
            run["target_id"],
            run.get("status", "queued"),
            run.get("step_budget", 25),
            run.get("verify_mode", "pm_clear"),
        )
        return dict(row)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            f"SELECT {RUN_COLUMNS} FROM runs WHERE id = $1", run_id
        )
        return dict(row) if row else None

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT r.id, r.target_id, r.status, r.step_budget, r.verify_mode,
                   r.started_at, r.ended_at, r.steps_taken, r.llm_calls,
                   r.tokens, r.cost_usd, r.error, r.created_at,
                   t.package, t.version,
                   (SELECT count(*) FROM findings f
                     WHERE f.run_id = r.id AND f.verdict = 'confirmed') AS confirmed,
                   (SELECT count(*) FROM findings f
                     WHERE f.run_id = r.id AND f.verdict = 'flaky') AS flaky
            FROM runs r
            LEFT JOIN targets t ON t.id = r.target_id
            ORDER BY r.created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def set_run_status(self, run_id: str, status: str, **fields: Any) -> None:
        """Move a run through the state machine, recording the first start."""
        sets = ["status = $2"]
        args: list[Any] = [run_id, status]
        if status == "provisioning":
            sets.append("started_at = COALESCE(started_at, now())")
        if status in {"done", "failed", "cancelled"}:
            sets.append("ended_at = COALESCE(ended_at, now())")
        for name in ("steps_taken", "error", "verify_mode"):
            if name in fields:
                args.append(fields[name])
                sets.append(f"{name} = ${len(args)}")
        await self.pool.execute(
            f"UPDATE runs SET {', '.join(sets)} WHERE id = $1", *args
        )

    # -- events -----------------------------------------------------------
    async def append_event(
        self, run_id: str, type_: str, payload: dict[str, Any] | None = None
    ) -> int:
        """Persist an event and return its sequence number.

        The worker calls this *before* the action that the event describes, so a
        dashboard that connects late still sees a complete history.
        """
        seq = await self.pool.fetchval(
            """
            INSERT INTO events (run_id, type, payload_json)
            VALUES ($1, $2, $3::jsonb)
            RETURNING seq
            """,
            run_id,
            type_,
            json.dumps(payload or {}),
        )
        return int(seq)

    async def events_since(
        self, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT seq, type, payload_json, ts FROM events
            WHERE run_id = $1 AND seq > $2
            ORDER BY seq
            LIMIT $3
            """,
            run_id,
            after_seq,
            limit,
        )
        return [_row_to_dict(row) for row in rows]

    async def latest_event_seq(self, run_id: str) -> int:
        value = await self.pool.fetchval(
            "SELECT COALESCE(max(seq), 0) FROM events WHERE run_id = $1", run_id
        )
        return int(value or 0)

    # -- steps, findings, verdicts ---------------------------------------
    async def run_steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT run_id, idx, state_hash, activity, action, selector_json,
                   result, screenshot_path, ts
            FROM steps WHERE run_id = $1 ORDER BY idx
            """,
            run_id,
        )
        return [_row_to_dict(row) for row in rows]

    async def run_findings(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, run_id, kind, signature, fingerprint, screen,
                   first_step_idx, severity, confidence, observed_once, verdict
            FROM findings WHERE run_id = $1 ORDER BY id
            """,
            run_id,
        )
        return [dict(row) for row in rows]

    async def run_verdicts(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT v.finding_id, v.attempt, v.reproduced, v.valid, v.state_hash,
                   v.evidence_path, v.ts
            FROM verdicts v
            JOIN findings f ON f.id = v.finding_id
            WHERE f.run_id = $1
            ORDER BY v.finding_id, v.attempt
            """,
            run_id,
        )
        return [dict(row) for row in rows]

    async def finding(self, finding_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT id, run_id, kind, signature, fingerprint, screen,
                   first_step_idx, severity, confidence, observed_once, verdict
            FROM findings WHERE id = $1
            """,
            finding_id,
        )
        return dict(row) if row else None

    async def finding_verdicts(self, finding_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT finding_id, attempt, reproduced, valid, state_hash,
                   evidence_path, ts
            FROM verdicts WHERE finding_id = $1 ORDER BY attempt
            """,
            finding_id,
        )
        return [dict(row) for row in rows]

    async def finding_artifacts(self, finding_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, run_id, finding_id, kind, path, bytes, sha256
            FROM artifacts WHERE finding_id = $1 ORDER BY kind
            """,
            finding_id,
        )
        return [dict(row) for row in rows]

    async def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, run_id, finding_id, kind, path, bytes, sha256
            FROM artifacts WHERE run_id = $1 ORDER BY finding_id, kind
            """,
            run_id,
        )
        return [dict(row) for row in rows]