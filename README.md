# GhostVendor

**GhostVendor haunts your dependencies. Six AI agents impersonate your APIs, inject real failures, diagnose what breaks, patch the code, and open a PR only after validation proves it survives.**

Built with NVIDIA Nemotron models on [Nebius Token Factory](https://tokenfactory.nebius.com) for the [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com) — Coding & Agentic Engineering Track.

---

## How It Works

```
GitHub Repo → DISCOVER → ATTACK → GUARD → VERIFY → DIAGNOSE → REMEDIATE → VALIDATE → DONE
```

Six specialized agents run an autonomous closed loop:

| # | Agent | Model | Responsibility |
|---|---|---|---|
| 1 | Vendor & Repository Detective | Nemotron Ultra | AST scan + LLM enrichment — discovers vendors, scores criticality, builds `vendor_spec` |
| 2 | Adversarial Twin Generator | Nemotron Ultra | Generates Evil Twin — stateful FastAPI mock server with 5 chaos modes per vendor |
| 3 | Context Guard | Nemotron Ultra | Security gate — AST inspection → Contree sandbox execution → LLM policy review (3 layers, in order) |
| 4 | Resilience Verifier | Nemotron Ultra | Launches twins locally, injects chaos, runs demo app, scores resilience 0–100 |
| 5 | Runtime Debugger | Nemotron Ultra | Root-cause analysis on all failed scenarios, per vendor |
| 6 | Patch Generator | DeepSeek-V4-Pro | Generates minimal patch, validates locally, opens PR with full context |

A Python state machine orchestrates the loop with deterministic control flow, retry limits (max 3 remediation cycles), and structured artifact handoffs between agents.

---

## Pipeline States

```
DISCOVER   Agent 1 — clone repo, AST-scan source, LLM-enrich vendor specs
ATTACK     Agent 2 — generate one Evil Twin (FastAPI) per vendor
GUARD      Agent 3 — 3-layer safety check: AST + Contree sandbox + LLM review
VERIFY     Agent 4 — Sense→Reason→Act loop: chaos injection + resilience scoring
DIAGNOSE   Agent 5 — root-cause analysis on failures
REMEDIATE  Agent 6 — patch generation (retried up to 3×, with failure context fed back)
VALIDATE   Agent 6 — apply patch to isolated temp dir, re-run failed scenarios
DONE / FAILED
```

---

## PR Strategy — Human-in-the-Loop by Design

GhostVendor never merges code automatically. Every output is a **draft PR** that requires human review before merge. This is an intentional architectural decision, not a limitation.

### Success path — validated patch PR

When the patched app passes all previously-failed chaos scenarios:

- A branch `ghostvendor/fix-{n}` is created on the target repo
- Each patched file is committed with a descriptive message
- A **ready-for-review PR** (not draft) is opened with:
  - Resilience score before and after patch (e.g. 23/100 → 87/100)
  - Every failed scenario that was fixed, per vendor
  - The patch strategy and root-cause summary for each vendor
  - Number of retry cycles needed before validation passed
  - Reference to the triggering PR (when `--pr` is provided)
- The person who triggered the run is requested as a reviewer (when `--triggered-by` is provided)
- A human reviews the diff, reads the context, and merges when satisfied

The fix PR is not a draft because it has already been validated: the pipeline proved the patch survives all chaos scenarios before opening it. This is HITL (human-in-the-loop) at the merge gate.

### Fallback path — findings-only draft PR

When Agent 6 cannot produce a patch that passes validation after 3 retry cycles:

- A branch `ghostvendor/findings-{n}` is created
- A `GHOSTVENDOR_FINDINGS.md` file is committed containing:
  - Resilience score
  - Every failed scenario per vendor
  - Root-cause diagnoses from Agent 5
  - All attempted patches with their descriptions
  - The exact validation failure detail from the last attempt
- A **draft PR** is opened with this file as its only change
- A human can read the findings and apply the fix manually, or use them as context for a follow-up run

This ensures the pipeline **always produces a human-readable artifact**, even when automated patching fails. No run ends silently.

---

## Validation Loop

After generating a patch, Agent 6 validates it locally before opening any PR:

1. The target repo is copied to an isolated temp directory
2. Patched files are written into the temp copy
3. `__pycache__` is purged so Python compiles from patched source, not stale bytecode
4. `PYTHONPATH` is remapped from the original repo root to the temp dir (repo-agnostic — works with any source layout: `src/`, `lib/`, `.`, etc.)
5. Evil Twins are launched fresh
6. The patched app is started from the temp dir against the twins
7. Only the previously-failed scenarios are re-run
8. Pass = app responded without hanging AND the status code changed from its original failure (repo-agnostic — a 503 from graceful error handling counts as pass; only the same failure as before counts as fail); fail = feeds the failure detail back to Agent 6 for the next retry

If validation passes, the PR is opened. If it fails 3 times, the findings PR fallback fires.

---

## Evil Twin Chaos Modes

Each vendor gets its own FastAPI mock that the demo app points at (via env var override). The verifier cycles through up to 5 scenarios per vendor:

| Mode | What it injects |
|---|---|
| `timeout` | 30 s async sleep — forces the app to hit its timeout (or hang forever) |
| `502_burst` | Returns HTTP 502 Bad Gateway |
| `429_rate_limit` | Returns HTTP 429 Too Many Requests |
| `malformed_json` | Returns HTTP 200 with a broken JSON body |
| `empty_response` | Returns HTTP 200 with an empty body |

The Ultra LLM observes each result and decides: **continue**, **escalate**, or **stop_early**. High-criticality vendors (score ≥ 80) require at least 4 scenarios before any early stop.

Evil Twin processes run in isolated temp directories and are fully cleaned up after each use (including on restart after timeout scenarios).

---

## Resilience Score

```
vendor_score   = baseline_pts (20) + scenario_score (0–80)   [capped at 100]
overall_score  = criticality-weighted average across all vendors
```

The VERIFY pass always runs all 5 chaos modes; the rescore pass runs only originally-failed modes and uses the actual count as the denominator so a fully-fixed vendor scores 100, not a fraction of 100. See [`docs/SCORING.md`](docs/SCORING.md) for the complete formula, outcome classification table, and worked examples.

---

## Stability Features

| Feature | Detail |
|---|---|
| GitHub retry wrapper | All GitHub API calls retried up to 3× with 5 s delay on transient failures |
| Port TIME_WAIT handling | `_pick_free_port()` falls back to OS ephemeral port if preferred port is in TIME_WAIT |
| Port-free guard | `_wait_for_port_free()` raises immediately if a port won't clear — no silent 15 s startup waste |
| Import merge | `_restore_unchanged_lines()` preserves model-added imports (e.g. `import time`) when restoring the file header — prevents `NameError` at runtime |
| Source cap | Agent 5 receives at most 8 source files × 4000 chars each to stay within context limits |
| LLM wrapper stripping | All agents use shared `strip_llm_wrapper()` — handles missing `</think>` tags and absent fences without crashing |
| Blocking sleep fix | `_fix_blocking_sleep()` post-processor rewrites any `time.sleep()` in generated Evil Twin code to `await asyncio.sleep()` — prevents event loop stalls that cause `/chaos DELETE` to hang during VALIDATE |
| Health endpoint fix | `_fix_health_endpoint()` post-processor replaces generated `/health` route bodies with a safe minimal return — prevents `KeyError` from model-generated `log_request()` helpers |

---

## Project Structure

```
ghostvendor/
├── main.py                          # Entrypoint: python main.py owner/repo [--triggered-by LOGIN] [--pr NUMBER]
├── src/
│   ├── agents/
│   │   ├── detective.py             # Agent 1 — vendor discovery + criticality scoring
│   │   ├── twin_generator.py        # Agent 2 — Evil Twin FastAPI code generation
│   │   ├── context_guard.py         # Agent 3 — AST + sandbox + LLM security gate
│   │   ├── resilience_verifier.py   # Agent 4 — chaos injection + scoring loop
│   │   ├── runtime_debugger.py      # Agent 5 — root cause analysis per vendor
│   │   └── patch_generator.py       # Agent 6 — patch generation + syntax validation
│   ├── pipeline/
│   │   └── state_machine.py         # State transitions, retry logic, PR strategy, artifact store
│   ├── tools/
│   │   ├── evil_twin_runner.py      # Evil Twin subprocess lifecycle (start/chaos/restart/stop + temp dir cleanup)
│   │   ├── demo_app_runner.py       # Demo app subprocess lifecycle
│   │   ├── repo_cloner.py           # Clone repo, detect startup command + port
│   │   ├── ast_scanner.py           # Deterministic HTTP call + env var extraction
│   │   └── github_client.py         # GitHub API — branch, commit, PR (draft + regular)
│   ├── utils/
│   │   ├── nebius_client.py         # Nemotron Ultra/Super/Nano + DeepSeek-V4-Pro via Nebius Token Factory + strip_llm_wrapper
│   │   └── logging_config.py        # Structured logging setup
│   ├── models/
│   │   ├── vendor_spec.py           # VendorSpec / VendorInfo schema
│   │   ├── resilience_result.py     # ScenarioResult / VendorResult / ResilienceReport
│   │   ├── diagnosis.py             # DiagnosisResult / DiagnosisReport schema
│   │   ├── patch.py                 # PatchResult / PatchReport schema
│   │   └── guard_decision.py        # GuardDecision schema
│   └── prompts/
│       ├── detective.txt            # Agent 1 system prompt
│       ├── twin_generator.txt       # Agent 2 system prompt
│       ├── context_guard.txt        # Agent 3 system prompt
│       ├── resilience_verifier.txt  # Agent 4 system prompt
│       ├── runtime_debugger.txt     # Agent 5 system prompt
│       └── patch_generator.txt      # Agent 6 system prompt
├── tests/
├── pyproject.toml
└── .env.example
```

---

## Setup

```bash
git clone https://github.com/nithinbr33/ghostvendor.git
cd ghostvendor

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e ".[dev]"

cp .env.example .env
# Fill in NEBIUS_API_KEY, NEBIUS_PROJECT_ID, GITHUB_TOKEN
```

Run against the demo app (canonical dev command):

```bash
python main.py NithinBR-AI/ghostvendor-demo-app --triggered-by NithinBR-AI
```

Or via Make:

```bash
make run
```

Run against any public Flask/FastAPI repo:

```bash
python main.py owner/repo-name
```

With triggering PR context:

```bash
python main.py owner/repo-name --triggered-by github-login --pr 42
```

For local development (skips GitHub clone, uses local sibling directory):

```bash
GHOSTVENDOR_LOCAL_DEV=1 python main.py owner/repo-name
```

---

## NVIDIA & Nebius Usage

| Tier | Model | Used for |
|---|---|---|
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | Vendor enrichment, LLM security review, attack planning, Sense→Reason→Act decisions, root-cause analysis, Evil Twin code generation |
| DeepSeek | `deepseek-ai/DeepSeek-V4-Pro` | Patch generation (Agent 6) — chosen for reliable structured JSON output at high token counts |
| Super | `nvidia/nemotron-3-super-120b-a12b` | Fallback for Evil Twin generation |
| Nano | `nvidia/Nemotron-3_5-Lightning` | Fast structured output (reserved for high-volume classification tasks) |

Each tier has a fallback model. All calls go through `https://api.tokenfactory.nebius.com/v1/` (OpenAI-compatible).

---

## License

MIT
