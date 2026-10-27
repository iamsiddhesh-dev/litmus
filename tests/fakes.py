"""In-memory store used by the API tests.

It implements the subset of ``litmus.db.store.Store`` the routes call, with the
same return shapes, so the control plane can be tested without Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

TARGET_FIELDS = (
    "kind",
    "package",
    "version",
    "min_sdk",
    "abis",
    "source",
    "storage_path",
    "profile",
)


class FakePool:
    """Health check pings only."""

    async def fetchval(self, *args: Any, **kwargs: Any) -> int:
        return 1

    async def fetch(self, *args: Any, **kwargs: Any) -> list:
        return []


class FakeStore:
    def __init__(
        self,
        runs: dict[str, dict] | None = None,
        targets: dict[str, dict] | None = None,
        events: list[dict] | None = None,
        findings: dict[str, dict] | None = None,
        verdicts: list[dict] | None = None,
        artifacts: list[dict] | None = None,
    ) -> None:
        self.pool = FakePool()
        self.runs = runs or {}
        self.targets = targets or {}
        self.events = list(events or [])
        self.findings = findings or {}
        self.verdicts = list(verdicts or [])
        self.artifacts = list(artifacts or [])
        self._seq = max((event["seq"] for event in self.events), default=0)

    # -- targets ----------------------------------------------------------
    async def create_target(self, target: dict) -> dict:
        row = {"created_at": datetime.now(UTC)}
        row.update({field: target.get(field) for field in TARGET_FIELDS})
        row.update({key: target[key] for key in ("id",) if key in target})
        self.targets[row["id"]] = row
        return row

    async def get_target(self, target_id: str) -> dict | None:
        return self.targets.get(target_id)

    # -- runs -------------------------------------------------------------
    async def create_run(self, run: dict) -> dict:
        row = {
            "id": run["id"],
            "target_id": run["target_id"],
            "status": run.get("status", "queued"),
            "step_budget": run.get("step_budget", 25),
            "verify_mode": run.get("verify_mode", "pm_clear"),
            "started_at": None,
            "ended_at": None,
            "steps_taken": 0,
            "llm_calls": 0,
            "tokens": 0,
            "cost_usd": 0.0,
            "error": None,
            "created_at": datetime.now(UTC),
        }
        self.runs[row["id"]] = row
        return row

    async def get_run(self, run_id: str) -> dict | None:
        return self.runs.get(run_id)

    async def list_runs(self, limit: int = 50) -> list[dict]:
        rows = sorted(self.runs.values(), key=lambda row: row["created_at"], reverse=True)
        result = []
        for row in rows[:limit]:
            target = self.targets.get(row["target_id"]) or {}
            confirmed = sum(
                1
                for finding in self.findings.values()
                if finding["run_id"] == row["id"] and finding.get("verdict") == "confirmed"
            )
            result.append(
                {**row, "package": target.get("package"), "version": target.get("version"),
                 "confirmed": confirmed, "flaky": 0}
            )
        return result

    async def set_run_status(self, run_id: str, status: str, **fields: Any) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        run["status"] = status
        for key, value in fields.items():
            if value is not None:
                run[key] = value

    # -- events -----------------------------------------------------------
    async def append_event(self, run_id: str, type_: str, payload: dict | None = None) -> int:
        self._seq += 1
        self.events.append(
            {
                "run_id": run_id,
                "seq": self._seq,
                "type": type_,
                "payload_json": payload or {},
                "ts": datetime.now(UTC),
            }
        )
        return self._seq

    async def events_since(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        rows = [
            event
            for event in self.events
            if event["run_id"] == run_id and event["seq"] > after_seq
        ]
        return rows[:limit]

    # -- report inputs ----------------------------------------------------
    async def run_findings(self, run_id: str) -> list[dict]:
        return [finding for finding in self.findings.values() if finding["run_id"] == run_id]

    async def run_verdicts(self, run_id: str) -> list[dict]:
        ids = {finding["id"] for finding in self.findings.values() if finding["run_id"] == run_id}
        return [verdict for verdict in self.verdicts if verdict["finding_id"] in ids]

    async def run_artifacts(self, run_id: str) -> list[dict]:
        return [artifact for artifact in self.artifacts if artifact["run_id"] == run_id]

    async def finding(self, finding_id: str) -> dict | None:
        return self.findings.get(finding_id)

    async def finding_verdicts(self, finding_id: str) -> list[dict]:
        return [verdict for verdict in self.verdicts if verdict["finding_id"] == finding_id]

    async def finding_artifacts(self, finding_id: str) -> list[dict]:
        return [artifact for artifact in self.artifacts if artifact["finding_id"] == finding_id]