"""Postgres persistence for a completed run (best effort).

The runner must work without a database (a laptop in the middle of a demo may
have Postgres down), so callers treat any failure here as a warning, never as a
run failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from litmus.config import Settings
from litmus.db.session import apply_schema


async def persist_run(settings: Settings, record: dict[str, Any]) -> None:
    """Write target, run, steps, findings, verdicts and artifacts."""
    import asyncpg

    await apply_schema(settings)
    conn = await asyncpg.connect(settings.postgres_dsn())
    try:
        async with conn.transaction():
            target = record["target"]
            await conn.execute(
                """
                INSERT INTO targets (id, kind, package, version, source, storage_path, profile)
                VALUES ($1, 'apk', $2, $3, 'apk', $4, $5)
                ON CONFLICT (id) DO UPDATE SET
                    package = EXCLUDED.package,
                    version = EXCLUDED.version,
                    storage_path = EXCLUDED.storage_path,
                    profile = EXCLUDED.profile
                """,
                target["id"],
                target.get("package"),
                target.get("version"),
                target.get("storage_path"),
                target.get("profile"),
            )

            run = record["run"]
            await conn.execute(
                """
                INSERT INTO runs (
                    id, target_id, status, step_budget, verify_mode, started_at, ended_at,
                    steps_taken, llm_calls, tokens, cost_usd, error
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 0, $9)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    ended_at = EXCLUDED.ended_at,
                    steps_taken = EXCLUDED.steps_taken,
                    error = EXCLUDED.error
                """,
                record["run_id"],
                target["id"],
                run["status"],
                run["step_budget"],
                run.get("verify_mode", "pm_clear"),
                run.get("started_at"),
                run.get("ended_at"),
                run["steps_taken"],
                run.get("error"),
            )

            for step in record.get("steps", []):
                await conn.execute(
                    """
                    INSERT INTO steps (
                        run_id, idx, state_hash, activity, action, selector_json,
                        result, screenshot_path, ts
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, to_timestamp($9))
                    ON CONFLICT (run_id, idx) DO UPDATE SET
                        state_hash = EXCLUDED.state_hash,
                        activity = EXCLUDED.activity,
                        action = EXCLUDED.action,
                        selector_json = EXCLUDED.selector_json,
                        result = EXCLUDED.result,
                        screenshot_path = EXCLUDED.screenshot_path
                    """,
                    record["run_id"],
                    step["idx"],
                    step.get("state_hash"),
                    step.get("activity"),
                    step.get("action"),
                    step.get("selector_json", "{}"),
                    step.get("result"),
                    step.get("screenshot_path"),
                    step.get("ts", datetime.now().timestamp()),
                )

            for finding in record.get("findings", []):
                await conn.execute(
                    """
                    INSERT INTO findings (
                        id, run_id, kind, signature, fingerprint, screen,
                        first_step_idx, severity, confidence, observed_once
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (id) DO UPDATE SET
                        signature = EXCLUDED.signature,
                        fingerprint = EXCLUDED.fingerprint,
                        confidence = EXCLUDED.confidence
                    """,
                    finding["id"],
                    record["run_id"],
                    finding["kind"],
                    finding["signature"],
                    finding["fingerprint"],
                    finding.get("screen"),
                    finding.get("first_step_idx"),
                    finding.get("severity", "high"),
                    finding.get("confidence", "heuristic"),
                    finding.get("observed_once", True),
                )

            for verdict in record.get("verdicts", []):
                await conn.execute(
                    """
                    INSERT INTO verdicts (
                        finding_id, attempt, reproduced, state_hash, evidence_path, ts
                    )
                    VALUES ($1, $2, $3, $4, $5, to_timestamp($6))
                    ON CONFLICT (finding_id, attempt) DO UPDATE SET
                        reproduced = EXCLUDED.reproduced,
                        state_hash = EXCLUDED.state_hash,
                        evidence_path = EXCLUDED.evidence_path
                    """,
                    verdict["finding_id"],
                    verdict["attempt"],
                    verdict["reproduced"],
                    verdict.get("state_hash"),
                    verdict.get("evidence_path"),
                    verdict.get("ts", datetime.now().timestamp()),
                )

            for artifact in record.get("artifacts", []):
                await conn.execute(
                    """
                    INSERT INTO artifacts (id, run_id, finding_id, kind, path, bytes, sha256)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO UPDATE SET
                        path = EXCLUDED.path,
                        bytes = EXCLUDED.bytes,
                        sha256 = EXCLUDED.sha256
                    """,
                    artifact["id"],
                    record["run_id"],
                    artifact.get("finding_id"),
                    artifact["kind"],
                    artifact["path"],
                    artifact.get("bytes", 0),
                    artifact.get("sha256"),
                )
    finally:
        await conn.close()