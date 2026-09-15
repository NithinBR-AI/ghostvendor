"""
GhostVendor monitoring dashboard — read-only Streamlit app.
Polls SQLite every 3 seconds. Does NOT trigger the pipeline.
Run from ghostvendor/ root: streamlit run dashboard/app.py
"""

import time
import json
import datetime
import streamlit as st
from dashboard import db
from dashboard import viz

st.set_page_config(
    page_title="GhostVendor",
    page_icon="👻",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    "<link rel='preconnect' href='https://fonts.googleapis.com'>"
    "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900"
    "&family=JetBrains+Mono:wght@400;500&display=swap' rel='stylesheet'>"
    "<style>"
    "html,body{font-family:'Inter',sans-serif!important;background:#0f172a!important}"
    "[data-testid='stApp']{background:#0f172a!important}"
    "[data-testid='stAppViewContainer']{background:#0f172a!important}"
    "[data-testid='stHeader']{background:#0f172a!important;border-bottom:none!important}"
    ".stAppToolbar{top:0!important;background:transparent!important;z-index:10}"
    "[data-testid='stMainBlockContainer']{padding:0!important;max-width:100%!important}"
    ".block-container{padding:0!important;max-width:100%!important}"
    "section[data-testid='stSidebar']{background:#1e293b;border-right:1px solid #334155}"
    "[data-testid='stTabs'] [data-baseweb='tab-list']{"
    "background:#1e293b;border-bottom:1px solid #334155;gap:0;padding:0 24px}"
    "[data-testid='stTabs']{margin-top:60px!important}"
    "[data-testid='stTabs'] [data-baseweb='tab']{"
    "background:transparent!important;border:none!important;"
    "box-shadow:none!important;"
    "color:#64748b;font-family:Inter,sans-serif;font-size:13px;"
    "font-weight:500;padding:14px 20px;letter-spacing:0.02em}"
    "[data-testid='stTabs'] [aria-selected='true']{"
    "color:#38bdf8!important;background:transparent!important;box-shadow:none!important}"
    ".react-aria-SelectionIndicator{background:#38bdf8!important;height:2px!important}"
    "[data-testid='stTabs'] [data-baseweb='tab']:hover{"
    "color:#7dd3fc!important;background:transparent!important}"
    "[data-testid='stTabs'] [data-baseweb='tab-panel']{background:#0f172a;padding:0}"
    "[data-testid='stExpander']{"
    "background:#1e293b!important;border:1px solid #334155!important;border-radius:8px!important}"
    "::-webkit-scrollbar{width:4px;height:4px}"
    "::-webkit-scrollbar-track{background:#0f172a}"
    "::-webkit-scrollbar-thumb{background:#334155;border-radius:2px}"
    "hr{border-color:#334155!important;margin:12px 0}"
    "p,li{color:#94a3b8}"
    "</style>",
    unsafe_allow_html=True,
)

_STATES = ["DISCOVER", "ATTACK", "GUARD", "VERIFY", "DIAGNOSE", "REMEDIATE", "VALIDATE"]


def _events_to_state(events):
    state = {}
    for ev in events:
        name = ev.get("state", "").upper()
        if name not in _STATES:
            continue
        existing = state.get(name, {})
        if ev.get("status") in ("complete", "failed") or not existing:
            state[name] = {
                "status":      ev.get("status", "pending"),
                "context_msg": ev.get("context_msg"),
                "elapsed_ms":  ev.get("elapsed_ms"),
            }
        elif ev.get("status") == "active" and existing.get("status") == "pending":
            state[name] = {"status": "active", "context_msg": ev.get("context_msg"), "elapsed_ms": None}
    return state


def _pill(status):
    cfg = {
        "running":       ("#0ea5e9", "rgba(14,165,233,0.12)", "RUNNING"),
        "complete":      ("#22c55e", "rgba(34,197,94,0.10)",  "DONE"),
        "failed":        ("#ef4444", "rgba(239,68,68,0.10)",  "FAILED"),
        "findings_only": ("#f59e0b", "rgba(245,158,11,0.10)", "FINDINGS"),
    }
    col, bg, label = cfg.get(status, ("#8b949e", "rgba(139,148,158,0.12)", status.upper()[:10]))
    return (
        f'<span style="padding:3px 10px;border-radius:20px;background:{bg};'
        f'border:1px solid {col}40;font-size:11px;font-weight:600;color:{col};'
        f"font-family:'JetBrains Mono',monospace;letter-spacing:0.06em\">{label}</span>"
    )


def _score(val, size="sm"):
    if val is None:
        return '<span style="color:#30363d">—</span>'
    pct = min(max(val, 0), 100)
    col = "#22c55e" if pct >= 70 else "#f59e0b" if pct >= 40 else "#ef4444"
    fs = "22px" if size == "lg" else "14px"
    fw = "900" if size == "lg" else "700"
    return (
        f'<span style="font-size:{fs};font-weight:{fw};color:{col};'
        f"font-family:'Inter',sans-serif;font-variant-numeric:tabular-nums\">{pct}</span>"
        f'<span style="font-size:10px;color:#30363d;margin-left:2px">/100</span>'
    )


def _delta(before, after):
    if before is None or after is None:
        return ""
    d = after - before
    col = "#22c55e" if d > 0 else "#ef4444" if d < 0 else "#8b949e"
    sign = "+" if d > 0 else ""
    return f'<span style="font-size:12px;color:{col};font-weight:600">{sign}{d}</span>'


def _fmt_ts(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%b %d %H:%M")
    except Exception:
        return ""


# ── Header (rendered once, outside tabs) ─────────────────────────────────────
def _header():
    st.markdown("""
<div style="display:flex;align-items:center;justify-content:space-between;
    padding:14px 28px 12px;background:#1e293b;border-bottom:1px solid #334155;margin-top:60px;">
  <div style="display:flex;align-items:center;gap:12px;">
    <span style="font-size:22px;line-height:1">👻</span>
    <div>
      <div style="font-size:17px;font-weight:900;color:#f1f5f9;letter-spacing:-0.02em;line-height:1.1">GhostVendor</div>
      <div style="font-size:11px;color:#64748b;font-family:'JetBrains Mono',monospace;letter-spacing:0.05em;margin-top:1px">autonomous resilience engineer</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="padding:3px 10px;border-radius:20px;background:rgba(139,92,246,0.12);
        border:1px solid rgba(139,92,246,0.3);font-size:11px;font-weight:600;color:#a78bfa;
        font-family:'JetBrains Mono',monospace;letter-spacing:0.04em">Nemotron Ultra</span>
    <span style="padding:3px 10px;border-radius:20px;background:rgba(245,158,11,0.1);
        border:1px solid rgba(245,158,11,0.3);font-size:11px;font-weight:600;color:#fbbf24;
        font-family:'JetBrains Mono',monospace;letter-spacing:0.04em">Nemotron Nano</span>
    <span style="padding:3px 10px;border-radius:20px;background:rgba(34,197,94,0.08);
        border:1px solid rgba(34,197,94,0.25);font-size:11px;font-weight:600;color:#4ade80;
        font-family:'JetBrains Mono',monospace;letter-spacing:0.04em">DeepSeek Pro</span>
  </div>
</div>
""", unsafe_allow_html=True)


# ── Tab 1: Live Run (fragment = only this reruns on polling) ──────────────────
@st.fragment(run_every=4)
def _tab_live():
    run = db.get_active_run()
    is_active = run is not None and run.get("status") == "running"

    if is_active:
        events = db.get_run_events(run["run_id"])
        state = _events_to_state(events)

        try:
            started = datetime.datetime.fromisoformat(run["started_at"])
            secs = int((datetime.datetime.utcnow() - started).total_seconds())
            elapsed_total = f"{secs // 60}m {secs % 60}s"
        except Exception:
            elapsed_total = ""

        repo = run.get("repo", "") or ""
        repo_short = repo.split("/")[-1] if "/" in repo else repo
        pr = f"PR #{run['pr_number']}" if run.get("pr_number") else ""
        branch = run.get("branch", "") or ""
        meta = " · ".join(filter(None, [repo_short, branch, pr]))

        st.markdown(
            f"<div style='display:flex;align-items:center;justify-content:space-between;"
            f"padding:8px 20px;background:#1e293b;border-bottom:1px solid #334155'>"
            f"<div style='display:flex;align-items:center;gap:16px'>"
            f"{_pill('running')}"
            f"<span style='color:#94a3b8;font-size:12px;font-family:JetBrains Mono,monospace'>{meta}</span>"
            f"</div>"
            f"<span style='font-size:12px;color:#94a3b8;font-family:JetBrains Mono,monospace'>{elapsed_total}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        html = viz.render(state, run.get("score_before"), run.get("score_after") or None, height=600)
        st.components.v1.html(html, height=600, scrolling=False)

    else:
        all_runs = db.get_all_runs()
        last_run = all_runs[0] if all_runs else None

        if last_run:
            events = db.get_run_events(last_run["run_id"])
            state = _events_to_state(events)
            status = last_run.get("status", "")
            repo = last_run.get("repo", "") or ""
            repo_short = repo.split("/")[-1] if "/" in repo else repo
            pr = f"PR #{last_run['pr_number']}" if last_run.get("pr_number") else ""
            ts = _fmt_ts(last_run.get("started_at", ""))

            show_scores = False
            finished = last_run.get("finished_at")
            if finished:
                try:
                    ft = datetime.datetime.fromisoformat(finished.replace("Z", ""))
                    if (datetime.datetime.utcnow() - ft).total_seconds() < 600:
                        show_scores = True
                except Exception:
                    pass
            sb = last_run.get("score_before") if show_scores else None
            sa = last_run.get("score_after") if show_scores else None
            html = viz.render(state, sb, sa, dim=True, height=600)
            st.components.v1.html(html, height=600, scrolling=False)

        else:
            st.markdown(
                "<div style='display:flex;flex-direction:column;align-items:center;"
                "justify-content:center;height:600px;color:#30363d;"
                "font-family:JetBrains Mono,monospace;font-size:13px;letter-spacing:0.1em'>"
                "<div style='font-size:48px;margin-bottom:24px;opacity:0.2'>👻</div>"
                "<div>NO RUNS YET</div>"
                "<div style='font-size:11px;margin-top:8px;color:#1c2333'>"
                "Open a PR on the demo app to trigger the pipeline</div></div>",
                unsafe_allow_html=True,
            )


# ── Tab 2: Run History ────────────────────────────────────────────────────────
def _tab_history():
    st.markdown(
        "<div style='padding:24px 28px 12px'>"
        "<div style='font-size:13px;font-weight:700;color:#8b949e;letter-spacing:0.1em'>RUN HISTORY</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    runs = db.get_all_runs()
    if not runs:
        st.markdown(
            "<p style='color:#30363d;padding:0 28px;font-size:13px'>No runs recorded yet.</p>",
            unsafe_allow_html=True,
        )
        return

    cols = "140px 1fr 100px 80px 80px 70px"
    st.markdown(
        f"<div style='display:grid;grid-template-columns:{cols};padding:8px 28px;"
        "background:#1e293b;border-bottom:1px solid #334155;"
        "font-size:10px;font-weight:700;color:#64748b;"
        "font-family:JetBrains Mono,monospace;letter-spacing:0.1em'>"
        "<div>TIME</div><div>REPO / BRANCH / PR</div>"
        "<div style='text-align:center'>STATUS</div>"
        "<div style='text-align:center'>BEFORE</div>"
        "<div style='text-align:center'>AFTER</div>"
        "<div style='text-align:right'>DELTA</div></div>",
        unsafe_allow_html=True,
    )

    for idx, run in enumerate(runs):
        ts = _fmt_ts(run.get("started_at", ""))
        repo = run.get("repo", "") or ""
        repo_short = repo.split("/")[-1] if "/" in repo else repo
        pr = f"PR #{run['pr_number']}" if run.get("pr_number") else ""
        branch = run.get("branch", "") or ""
        desc = " · ".join(filter(None, [repo_short, branch, pr]))
        status = run.get("status", "")
        before = run.get("score_before")
        after = run.get("score_after")
        row_bg = "#162032" if idx % 2 == 0 else "#0f172a"

        st.markdown(
            f"<div style='display:grid;grid-template-columns:{cols};"
            f"padding:11px 28px;background:{row_bg};border-bottom:1px solid #1e293b;align-items:center'>"
            f"<div style='font-size:12px;color:#94a3b8;font-family:JetBrains Mono,monospace'>{ts}</div>"
            f"<div style='font-size:13px;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{desc}</div>"
            f"<div style='text-align:center'>{_pill(status)}</div>"
            f"<div style='text-align:center'>{_score(before)}</div>"
            f"<div style='text-align:center'>{_score(after)}</div>"
            f"<div style='text-align:right'>{_delta(before, after)}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        with st.expander("Details", expanded=False):
            c1, c2 = st.columns(2, gap="medium")
            with c1:
                st.markdown(
                    "<div style='font-size:11px;font-weight:700;color:#8b949e;"
                    "letter-spacing:0.08em;margin-bottom:8px'>VENDORS</div>",
                    unsafe_allow_html=True,
                )
                vendors = db.get_vendor_results(run["run_id"])
                if vendors:
                    for v in vendors:
                        vname = v.get("vendor_name", "")
                        vb, va = v.get("score_before"), v.get("score_after")
                        try:
                            n_sc = len(json.loads(v.get("scenarios", "[]") or "[]"))
                        except Exception:
                            n_sc = 0
                        st.markdown(
                            f"<div style='display:flex;align-items:center;justify-content:space-between;"
                            f"padding:8px 12px;margin-bottom:4px;background:#0f172a;"
                            f"border:1px solid #334155;border-radius:6px'>"
                            f"<span style='font-size:12px;color:#e2e8f0;font-weight:600'>{vname}</span>"
                            f"<div style='display:flex;align-items:center;gap:8px'>"
                            f"<span style='font-size:11px;color:#8b949e'>{n_sc} scenarios</span>"
                            f"{_score(vb)} <span style='color:#30363d'>→</span> {_score(va)}"
                            f"</div></div>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown("<span style='font-size:12px;color:#30363d'>No vendor data</span>", unsafe_allow_html=True)

            with c2:
                st.markdown(
                    "<div style='font-size:11px;font-weight:700;color:#8b949e;"
                    "letter-spacing:0.08em;margin-bottom:8px'>PIPELINE TIMELINE</div>",
                    unsafe_allow_html=True,
                )
                ev_state = _events_to_state(db.get_run_events(run["run_id"]))
                for aname in _STATES:
                    s = ev_state.get(aname, {})
                    st_a = s.get("status", "pending")
                    el = s.get("elapsed_ms")
                    el_str = (f"{el/1000:.1f}s" if el and el < 60000 else
                              f"{el//60000}m{(el%60000)//1000}s" if el else "")
                    dot = {"complete": "#22c55e", "active": "#0ea5e9", "failed": "#ef4444"}.get(st_a, "#1c2333")
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:8px;padding:4px 0'>"
                        f"<div style='width:8px;height:8px;border-radius:50%;background:{dot};flex-shrink:0'></div>"
                        f"<span style='font-size:12px;color:#c9d1d9;font-family:JetBrains Mono,monospace;flex:1'>{aname}</span>"
                        f"<span style='font-size:11px;color:#64748b;font-family:JetBrains Mono,monospace'>{el_str}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )


# ── Tab 3: About ──────────────────────────────────────────────────────────────
def _tab_about():
    # Build as concatenated string — no triple-quote with single-quoted font-family inside
    mono = "JetBrains Mono,monospace"
    inter = "Inter,sans-serif"

    def agent_card(badge_color, badge_bg, badge, title, desc):
        return (
            f"<div style='padding:14px 16px;background:#1e293b;border:1px solid #334155;border-radius:8px'>"
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px'>"
            f"<span style='font-size:10px;font-weight:700;color:{badge_color};font-family:{mono};"
            f"background:{badge_bg};padding:2px 7px;border-radius:4px'>{badge}</span>"
            f"<span style='font-size:13px;font-weight:700;color:#f1f5f9;font-family:{inter}'>{title}</span>"
            f"</div>"
            f"<p style='font-size:12px;color:#8b949e;margin:0;line-height:1.6;font-family:{inter}'>{desc}</p>"
            f"</div>"
        )

    agents = (
        agent_card("#a78bfa","rgba(139,92,246,0.1)","A1 · Ultra","Discover","Scans the repo for vendor dependencies and ranks by blast radius.")
        + agent_card("#a78bfa","rgba(139,92,246,0.1)","A2 · Ultra","Attack","Generates Evil Twin scenarios — outages, latency spikes, malformed responses.")
        + agent_card("#fbbf24","rgba(245,158,11,0.1)","A3 · Nano","Guard","Deploys Evil Twins as runtime dependencies to probe existing resilience patterns.")
        + agent_card("#a78bfa","rgba(139,92,246,0.1)","A4 · Ultra","Verify","Scores each vendor's resilience before patching — baseline for delta measurement.")
        + agent_card("#a78bfa","rgba(139,92,246,0.1)","A5 · Ultra","Diagnose","Identifies root causes and produces structured findings for the code patch.")
        + agent_card("#4ade80","rgba(34,197,94,0.08)","A6 · DeepSeek","Remediate","Writes retries, circuit breakers, and fallbacks — then opens a fix PR.")
    )

    label = f"font-size:11px;font-weight:700;color:#0ea5e9;letter-spacing:0.12em;font-family:{inter}"

    html = (
        f"<div style='max-width:860px;margin:0 auto;padding:36px 28px 60px'>"

        f"<div style='margin-bottom:32px'>"
        f"<div style='{label};margin-bottom:10px'>WHAT IS GHOSTVENDOR</div>"
        f"<p style='font-size:15px;color:#c9d1d9;line-height:1.75;margin:0;font-family:{inter}'>"
        f"GhostVendor is a six-agent autonomous pipeline that detects fragile third-party dependencies, "
        f"attacks them with synthesized failure scenarios, verifies resilience, and ships hardened code "
        f"as a pull request without human intervention.</p>"
        f"</div>"

        f"<div style='margin-bottom:32px'>"
        f"<div style='{label};margin-bottom:14px'>THE PIPELINE</div>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px'>{agents}</div>"
        f"</div>"

        f"<div style='margin-bottom:32px'>"
        f"<div style='{label};margin-bottom:14px'>RESILIENCE SCORE</div>"
        f"<div style='padding:18px 22px;background:#1e293b;border:1px solid #334155;border-radius:8px;"
        f"font-family:{mono};font-size:13px;color:#7dd3fc;line-height:1.9'>"
        f"score = 100 x (scenarios_passed / scenarios_total)<br>"
        f"weighted by criticality<br>"
        f"<span style='color:#8b949e;font-size:11px'>measured before patch (baseline) and after (delta)</span>"
        f"</div></div>"

        f"<div>"
        f"<div style='{label};margin-bottom:12px'>MODELS</div>"
        f"<div style='display:flex;gap:10px;flex-wrap:wrap'>"
        f"<div style='padding:10px 16px;background:#1e293b;border:1px solid rgba(139,92,246,0.3);border-radius:8px'>"
        f"<div style='font-size:11px;font-weight:700;color:#a78bfa;font-family:{mono}'>NVIDIA Nemotron Ultra</div>"
        f"<div style='font-size:11px;color:#94a3b8;margin-top:3px;font-family:{inter}'>Agents 1, 2, 4, 5 — reasoning &amp; analysis</div>"
        f"</div>"
        f"<div style='padding:10px 16px;background:#1e293b;border:1px solid rgba(245,158,11,0.3);border-radius:8px'>"
        f"<div style='font-size:11px;font-weight:700;color:#fbbf24;font-family:{mono}'>NVIDIA Nemotron Nano</div>"
        f"<div style='font-size:11px;color:#94a3b8;margin-top:3px;font-family:{inter}'>Agent 3 — fast injection &amp; guard</div>"
        f"</div>"
        f"<div style='padding:10px 16px;background:#1e293b;border:1px solid rgba(34,197,94,0.25);border-radius:8px'>"
        f"<div style='font-size:11px;font-weight:700;color:#4ade80;font-family:{mono}'>DeepSeek Pro</div>"
        f"<div style='font-size:11px;color:#94a3b8;margin-top:3px;font-family:{inter}'>Agent 6 — code generation &amp; patching</div>"
        f"</div></div></div>"
        f"</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def main():
    db.init_db()
    tab1, tab2, tab3 = st.tabs(["  Live Run  ", "  Run History  ", "  About  "])
    with tab1:
        _tab_live()
    with tab2:
        _tab_history()
    with tab3:
        _tab_about()


if __name__ == "__main__":
    main()
