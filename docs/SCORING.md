# GhostVendor Resilience Score — Authoritative Formula

## Per-Vendor Score

```
vendor_score = baseline_pts + scenario_score          (capped at 100)

baseline_pts   = 20   always in rescore (VALIDATE already proved the patched app serves
                      clean traffic; re-testing here would risk corrupting the twin
                      via half-open connections from the patched client's own timeout)

scenario_score = int( (sum_of_scenario_pts / (n × 16)) × 80 )

per scenario outcome:
  PASS      → 16 pts   (app responded without hanging AND status improved from failure)
  DEGRADED  →  8 pts   (app responded; status changed but still an error, or graceful 4xx)
  FAIL      →  0 pts   (same failure as original, or hung again)
```

`n` is the number of chaos scenarios actually run in that pass (see below).

## VERIFY Pass (initial)

`n = 5` always (all five chaos modes: timeout, 502_burst, 429_rate_limit, malformed_json, empty_response).

Running fewer than 5 due to early stop scores proportionally lower — never inflated.

## Rescore Pass (after patch)

`n = number of originally-failed scenarios retested` (only the modes that failed in VERIFY are re-run).

This means a vendor that had 2 failing scenarios and had both fixed receives the same per-scenario score as a vendor that had 5 failing scenarios and had all 5 fixed — the score measures "did the patch fix everything that broke", not "what fraction of all modes did we cover this run".

**Why this matters:** using n=5 in rescore would artificially cap the maximum achievable score. Example:
- Vendor failed 2 of 5 scenarios in VERIFY
- After patch, both previously-failed scenarios PASS
- With n=5: score = int(32/80×80)+20 = 52/100 ❌ (misleading — patch is complete)
- With n=2: score = int(32/32×80)+20 = 100/100 ✓ (correct — all failures fixed)

**Twin lifecycle in rescore:** Each twin is launched fresh and immediately reset before the scenario loop starts. This guarantees a clean process regardless of what the VALIDATE pass left behind — particularly important after `timeout` scenarios where SIGKILL may leave the port briefly in TIME_WAIT.

## Rescore Outcome Classification

When a scenario is re-run after patching, the outcome is classified relative to the **original** scenario result:

| Original result | Rescore result | Outcome |
|---|---|---|
| timeout / exception (hung) | any HTTP response | **PASS** — app no longer hangs |
| HTTP failure (e.g. 503) | same HTTP status | **FAIL** — unchanged |
| HTTP failure | status < 400 | **PASS** — fixed |
| HTTP failure | 400–499 | **DEGRADED** — graceful error handling |
| HTTP failure | 500–599 (different) | **DEGRADED** — changed, but still server error |

A 503 that was the original failure becomes DEGRADED (not FAIL) if the patched app now returns a different 5xx — it changed, which means the patch had an effect.

## Overall Pipeline Score

```
overall_score = int( Σ(vendor_score × criticality) / Σ(criticality) )
```

`criticality` is the vendor's criticality score from Agent 1 (1–100). Higher-criticality vendors dominate the weighted average; a vendor scored 95 contributes roughly 3× as much as one scored 30.

## Scenario Modes

| Mode | What is injected |
|---|---|
| `timeout` | 30s async sleep — forces app to hit its timeout or hang |
| `502_burst` | HTTP 502 Bad Gateway |
| `429_rate_limit` | HTTP 429 Too Many Requests |
| `malformed_json` | HTTP 200 with malformed JSON body |
| `empty_response` | HTTP 200 with empty body |
