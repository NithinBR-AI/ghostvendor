-- Run this in the Supabase SQL editor: https://supabase.com/dashboard/project/rrttibauexolyuzahtyl/sql

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    repo         TEXT,
    pr_number    INTEGER,
    branch       TEXT,
    triggered_by TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT DEFAULT 'running',
    score_before INTEGER,
    score_after  INTEGER,
    pr_url       TEXT,
    error_msg    TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    ts          TEXT,
    state       TEXT,
    status      TEXT,
    context_msg TEXT,
    elapsed_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS vendor_results (
    run_id           TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    vendor_name      TEXT,
    criticality_score INTEGER,
    score_before     INTEGER,
    score_after      INTEGER,
    scenarios        TEXT,
    PRIMARY KEY (run_id, vendor_name)
);
