-- Litmus schema (PLAN §9).
-- Applied with CREATE TABLE IF NOT EXISTS at startup, or via scripts/bootstrap_db.py.
-- No migrations in v1.

CREATE TABLE IF NOT EXISTS targets (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'apk',
    package       TEXT,
    version       TEXT,
    min_sdk       INTEGER,
    abis          TEXT,
    source        TEXT,
    storage_path  TEXT,
    profile       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    target_id     TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'queued',
    step_budget   INTEGER NOT NULL DEFAULT 25,
    verify_mode   TEXT NOT NULL DEFAULT 'pm_clear',
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ,
    steps_taken   INTEGER NOT NULL DEFAULT 0,
    llm_calls     INTEGER NOT NULL DEFAULT 0,
    tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS steps (
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    state_hash    TEXT,
    activity      TEXT,
    action        TEXT,
    selector_json JSONB,
    result        TEXT,
    screenshot_path TEXT,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, idx)
);

CREATE TABLE IF NOT EXISTS findings (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    signature       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    screen          TEXT,
    first_step_idx  INTEGER,
    severity        TEXT NOT NULL DEFAULT 'medium',
    confidence      TEXT NOT NULL DEFAULT 'heuristic',
    observed_once   BOOLEAN NOT NULL DEFAULT false,
    -- confirmed | flaky | filtered | not_reproduced; written by the verifier so
    -- the report is reproducible from the database alone (M2).
    verdict         TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);

CREATE TABLE IF NOT EXISTS verdicts (
    finding_id    TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    attempt       INTEGER NOT NULL,
    reproduced    BOOLEAN NOT NULL,
    -- An attempt that resolved every selector on the recorded screen. Invalid
    -- attempts are environment noise and are excluded from the repro rate.
    valid         BOOLEAN NOT NULL DEFAULT true,
    state_hash    TEXT,
    evidence_path TEXT,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (finding_id, attempt)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    finding_id  TEXT REFERENCES findings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    bytes       BIGINT NOT NULL DEFAULT 0,
    sha256      TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);

CREATE TABLE IF NOT EXISTS events (
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq           BIGSERIAL PRIMARY KEY,
    type          TEXT NOT NULL,
    payload_json  JSONB,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    runner_kind     TEXT NOT NULL,
    profile_json    JSONB,
    status          TEXT NOT NULL DEFAULT 'idle',
    last_heartbeat  TIMESTAMPTZ
);

-- Idempotent upgrades for databases created before M2. No migrations in v1.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE findings ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS valid BOOLEAN NOT NULL DEFAULT true;
