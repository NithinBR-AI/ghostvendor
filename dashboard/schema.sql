CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT UNIQUE NOT NULL,
    repo        TEXT NOT NULL,
    pr_number   INTEGER,
    branch      TEXT,
    triggered_by TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    score_before INTEGER,
    score_after  INTEGER,
    pr_url      TEXT,
    error_msg   TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    state       TEXT NOT NULL,
    status      TEXT NOT NULL,
    context_msg TEXT,
    elapsed_ms  INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS vendor_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    vendor_name     TEXT NOT NULL,
    criticality_score INTEGER,
    score_before    INTEGER,
    score_after     INTEGER,
    scenarios       TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_vendor_results_run_id ON vendor_results(run_id);
