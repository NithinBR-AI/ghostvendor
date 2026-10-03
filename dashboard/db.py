"""
GhostVendor persistence layer.

- Local (GHOSTVENDOR_LOCAL_DEV=1 or no SUPABASE_URL): SQLite
- Cloud (SUPABASE_URL set): Supabase (Postgres via REST API)
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).parent / "ghostvendor.db"
_SCHEMA = Path(__file__).parent / "schema.sql"

# ── Backend detection ─────────────────────────────────────────────────────────

def _use_supabase() -> bool:
    return bool(os.environ.get("SUPABASE_URL")) and not os.environ.get("GHOSTVENDOR_LOCAL_DEV")


def _supabase():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_ANON_KEY"]
    return create_client(url, key)


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db() -> None:
    if _use_supabase():
        _seed_dummy_data_supabase()
    else:
        with _connect() as conn:
            conn.executescript(_SCHEMA.read_text())
        _seed_dummy_data()


# ── Write API ─────────────────────────────────────────────────────────────────

def start_run(run_id: str, repo: str, pr_number: int | None, branch: str | None, triggered_by: str | None) -> None:
    if _use_supabase():
        _supabase().table("runs").upsert({
            "run_id": run_id, "repo": repo, "pr_number": pr_number,
            "branch": branch, "triggered_by": triggered_by,
            "started_at": _now(), "status": "running",
        }, on_conflict="run_id").execute()
    else:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, repo, pr_number, branch, triggered_by, started_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running')",
                (run_id, repo, pr_number, branch, triggered_by, _now()),
            )


def record_event(run_id: str, state: str, status: str, context_msg: str | None = None, elapsed_ms: int | None = None) -> None:
    if _use_supabase():
        _supabase().table("run_events").insert({
            "run_id": run_id, "ts": _now(), "state": state,
            "status": status, "context_msg": context_msg, "elapsed_ms": elapsed_ms,
        }).execute()
    else:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, ts, state, status, context_msg, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, _now(), state, status, context_msg, elapsed_ms),
            )


def update_score_before(run_id: str, score_before: int) -> None:
    if _use_supabase():
        _supabase().table("runs").update({"score_before": score_before}).eq("run_id", run_id).execute()
    else:
        with _connect() as conn:
            conn.execute("UPDATE runs SET score_before=? WHERE run_id=?", (score_before, run_id))


def update_score_after(run_id: str, score_after: int) -> None:
    if _use_supabase():
        _supabase().table("runs").update({"score_after": score_after}).eq("run_id", run_id).execute()
    else:
        with _connect() as conn:
            conn.execute("UPDATE runs SET score_after=? WHERE run_id=?", (score_after, run_id))


def finish_run(run_id: str, status: str, score_before: int | None, score_after: int | None, pr_url: str | None, error_msg: str | None = None) -> None:
    if _use_supabase():
        _supabase().table("runs").update({
            "status": status, "score_before": score_before, "score_after": score_after,
            "pr_url": pr_url, "error_msg": error_msg, "finished_at": _now(),
        }).eq("run_id", run_id).execute()
    else:
        with _connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, score_before=?, score_after=?, pr_url=?, error_msg=?, finished_at=? WHERE run_id=?",
                (status, score_before, score_after, pr_url, error_msg, _now(), run_id),
            )


def save_vendor_results(run_id: str, vendors: list[dict]) -> None:
    if _use_supabase():
        sb = _supabase()
        for v in vendors:
            sb.table("vendor_results").upsert({
                "run_id": run_id,
                "vendor_name": v["vendor_name"],
                "criticality_score": v.get("criticality_score"),
                "score_before": v.get("score_before"),
                "score_after": v.get("score_after"),
                "scenarios": json.dumps(v.get("scenarios", [])),
            }, on_conflict="run_id,vendor_name").execute()
    else:
        with _connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vendor_results (run_id, vendor_name, criticality_score, score_before, score_after, scenarios) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (run_id, v["vendor_name"], v.get("criticality_score"), v.get("score_before"), v.get("score_after"), json.dumps(v.get("scenarios", [])))
                    for v in vendors
                ],
            )


# ── Read API ──────────────────────────────────────────────────────────────────

_STALE_RUN_MINUTES = 30


def get_active_run() -> dict | None:
    import datetime
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=_STALE_RUN_MINUTES)).isoformat()
    if _use_supabase():
        res = _supabase().table("runs").select("*").eq("status", "running").gt("started_at", cutoff).order("started_at", desc=True).limit(1).execute()
        return res.data[0] if res.data else None
    else:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE status='running' AND started_at > ? ORDER BY started_at DESC LIMIT 1",
                (cutoff,)
            ).fetchone()
            return dict(row) if row else None


def get_run_events(run_id: str) -> list[dict]:
    if _use_supabase():
        res = _supabase().table("run_events").select("*").eq("run_id", run_id).order("ts").execute()
        return res.data or []
    else:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id=? ORDER BY ts ASC", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]


def get_all_runs(limit: int = 50) -> list[dict]:
    if _use_supabase():
        res = _supabase().table("runs").select("*").neq("status", "running").order("started_at", desc=True).limit(limit).execute()
        return res.data or []
    else:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status != 'running' ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) if r else {} for r in rows]


def get_vendor_results(run_id: str) -> list[dict]:
    if _use_supabase():
        res = _supabase().table("vendor_results").select("*").eq("run_id", run_id).execute()
        results = []
        for r in (res.data or []):
            r["scenarios"] = json.loads(r.get("scenarios") or "[]")
            results.append(r)
        return results
    else:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vendor_results WHERE run_id=?", (run_id,)
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["scenarios"] = json.loads(d.get("scenarios") or "[]")
                results.append(d)
            return results


def get_latest_run_state(run_id: str) -> dict[str, dict]:
    events = get_run_events(run_id)
    state_map: dict[str, dict] = {}
    for e in events:
        state_map[e["state"]] = {
            "status": e["status"],
            "context_msg": e.get("context_msg"),
            "elapsed_ms": e.get("elapsed_ms"),
        }
    return state_map


# ── Dummy seed ────────────────────────────────────────────────────────────────

def _seed_dummy_data_supabase() -> None:
    sb = _supabase()
    try:
        res = sb.table("runs").select("run_id").eq("run_id", "demo-run-001").execute()
        if res.data:
            return
    except Exception:
        return
    _insert_dummy(sb)


def _seed_dummy_data() -> None:
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if count > 0:
        return
    _insert_dummy(None)


def _insert_dummy(sb) -> None:
    runs = [
        {"run_id": "demo-run-001", "repo": "NithinBR-AI/ghostvendor-demo-app", "pr_number": 23, "branch": "feature/add-vendor-integrations", "triggered_by": "NithinBR-AI", "started_at": "2026-09-13T14:02:11Z", "finished_at": "2026-09-13T14:09:44Z", "status": "done", "score_before": 23, "score_after": 87, "pr_url": "https://github.com/NithinBR-AI/ghostvendor-demo-app/pull/38"},
        {"run_id": "demo-run-002", "repo": "NithinBR-AI/ghostvendor-demo-app", "pr_number": 21, "branch": "feature/add-sendgrid", "triggered_by": "NithinBR-AI", "started_at": "2026-09-12T10:15:00Z", "finished_at": "2026-09-12T10:22:31Z", "status": "findings_only", "score_before": 15, "score_after": None, "pr_url": "https://github.com/NithinBR-AI/ghostvendor-demo-app/pull/35"},
        {"run_id": "demo-run-003", "repo": "NithinBR-AI/ghostvendor-demo-app", "pr_number": 19, "branch": "feature/stripe-integration", "triggered_by": "NithinBR-AI", "started_at": "2026-09-11T08:44:00Z", "finished_at": "2026-09-11T08:51:10Z", "status": "done", "score_before": 10, "score_after": 100, "pr_url": "https://github.com/NithinBR-AI/ghostvendor-demo-app/pull/31"},
    ]
    dummy_events = {
        "demo-run-001": [("DISCOVER","complete","Found 2 vendors: Stripe, SendGrid",47000),("ATTACK","complete","Generated 2 Evil Twins",94000),("GUARD","complete","All twins approved — AST + LLM",28000),("VERIFY","complete","Score: 23/100 — 7 failures across 2 vendors",158000),("DIAGNOSE","complete","Root cause identified for Stripe + SendGrid",63000),("REMEDIATE","complete","Patches generated — 2 files modified",118000),("VALIDATE","complete","Score: 87/100 — patch validated",133000)],
        "demo-run-002": [("DISCOVER","complete","Found 1 vendor: SendGrid",41000),("ATTACK","complete","Generated 1 Evil Twin",78000),("GUARD","complete","Twin approved",24000),("VERIFY","complete","Score: 15/100 — 4 failures",142000),("DIAGNOSE","complete","Root cause identified",55000),("REMEDIATE","complete","Patch generated",104000),("VALIDATE","failed","Patch validation failed after 3 retries — findings PR opened",126000)],
        "demo-run-003": [("DISCOVER","complete","Found 1 vendor: Stripe",38000),("ATTACK","complete","Generated 1 Evil Twin",72000),("GUARD","complete","Twin approved",22000),("VERIFY","complete","Score: 10/100 — 5 failures",149000),("DIAGNOSE","complete","Root cause: no timeout handling in stripe_client.py",51000),("REMEDIATE","complete","Circuit breaker + retry patch generated",97000),("VALIDATE","complete","Score: 100/100 — all scenarios pass",121000)],
    }
    dummy_vendors = {
        "demo-run-001": [{"vendor_name":"Stripe","criticality_score":95,"score_before":20,"score_after":80,"scenarios":[{"mode":"timeout","outcome":"PASS"},{"mode":"502_burst","outcome":"PASS"},{"mode":"429_rate_limit","outcome":"DEGRADED"}]},{"vendor_name":"SendGrid","criticality_score":70,"score_before":30,"score_after":100,"scenarios":[{"mode":"timeout","outcome":"PASS"},{"mode":"malformed_json","outcome":"PASS"}]}],
        "demo-run-002": [{"vendor_name":"SendGrid","criticality_score":70,"score_before":15,"score_after":None,"scenarios":[{"mode":"timeout","outcome":"FAIL"},{"mode":"502_burst","outcome":"FAIL"}]}],
        "demo-run-003": [{"vendor_name":"Stripe","criticality_score":95,"score_before":10,"score_after":100,"scenarios":[{"mode":"timeout","outcome":"PASS"},{"mode":"502_burst","outcome":"PASS"},{"mode":"429_rate_limit","outcome":"PASS"},{"mode":"malformed_json","outcome":"PASS"},{"mode":"empty_response","outcome":"PASS"}]}],
    }
    for r in runs:
        if sb:
            sb.table("runs").upsert(r, on_conflict="run_id").execute()
            for state, status, msg, elapsed in dummy_events.get(r["run_id"], []):
                sb.table("run_events").insert({"run_id": r["run_id"], "ts": _now(), "state": state, "status": status, "context_msg": msg, "elapsed_ms": elapsed}).execute()
            for v in dummy_vendors.get(r["run_id"], []):
                sb.table("vendor_results").upsert({"run_id": r["run_id"], "vendor_name": v["vendor_name"], "criticality_score": v.get("criticality_score"), "score_before": v.get("score_before"), "score_after": v.get("score_after"), "scenarios": json.dumps(v.get("scenarios", []))}, on_conflict="run_id,vendor_name").execute()
        else:
            with _connect() as conn:
                conn.execute("INSERT OR IGNORE INTO runs (run_id, repo, pr_number, branch, triggered_by, started_at, finished_at, status, score_before, score_after, pr_url) VALUES (:run_id, :repo, :pr_number, :branch, :triggered_by, :started_at, :finished_at, :status, :score_before, :score_after, :pr_url)", r)
            for state, status, msg, elapsed in dummy_events.get(r["run_id"], []):
                record_event(r["run_id"], state, status, msg, elapsed)
            save_vendor_results(r["run_id"], dummy_vendors.get(r["run_id"], []))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
