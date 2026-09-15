# GhostVendor

**Vendor failures are invisible until production blows up.**

Every Python web app depends on external vendors — Stripe for payments, SendGrid for email, Twilio for SMS. When those vendors return a 502, hang for 30 seconds, or send malformed JSON, most apps crash, hang, or silently corrupt state. The failure is discovered by a user, not a test.

GhostVendor fixes this at the PR level. Open a pull request — GhostVendor automatically discovers your vendor dependencies (grounded in real-world reliability data from **[Tavily](https://tavily.com)**), impersonates each one with a chaos-injecting Evil Twin, scores how your app handles real failure modes, diagnoses what broke, patches the code, and opens a validated fix PR. No human intervention. No mocking. No guessing.

Built with NVIDIA Nemotron models on [Nebius Token Factory](https://tokenfactory.nebius.com) for the [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com) — Coding & Agentic Engineering Track.

---

## Architecture

```
GitHub PR → DISCOVER → ATTACK → GUARD → VERIFY → DIAGNOSE → REMEDIATE → VALIDATE → PR opened
```

A Python state machine orchestrates six specialized agents in a deterministic closed loop. Each agent owns a distinct responsibility and hands structured artifacts to the next. The LLM never decides what runs next — the orchestrator does. This is intentional: auditability, recoverability, and safety over autonomy.

| # | Agent | Model | Responsibility |
|---|---|---|---|
| 1 | Vendor & Repository Detective | Nemotron Ultra | AST scan + Tavily web intel + LLM enrichment — discovers vendors, scores criticality, builds `vendor_spec` |
| 2 | Adversarial Twin Generator | Nemotron Ultra | Generates Evil Twin — stateful FastAPI mock server with 5 chaos modes per vendor |
| 3 | Context Guard | Nemotron Nano | Security gate — AST inspection → Contree sandbox execution → LLM policy review (3 layers, in order) |
| 4 | Resilience Verifier | Nemotron Ultra | Sense→Reason→Act loop: launches twins, injects chaos, observes real app behavior, scores 0–100 |
| 5 | Runtime Debugger | Nemotron Ultra | Root-cause analysis on all failed scenarios, per vendor |
| 6 | Patch Generator | DeepSeek-V4-Pro | Generates minimal patch, validates locally against failed scenarios, opens PR only after proof it passes |

---

## Key Design Decisions

**Python orchestration, not LLM orchestration.** The state machine controls every transition. LLMs are specialized workers — they process structured input and return structured output. This makes the pipeline auditable (every state is logged), recoverable (retry at the failed stage, not from scratch), and safe (no LLM can decide to skip the security gate).

**Sandbox before execution.** Agent 2 generates code. Agent 3 runs that code in an isolated Nebius Contree sandbox before it ever touches the local machine. A malicious or hallucinated twin that exfiltrates env vars or opens a reverse shell is caught in the cloud, not on the developer's machine.

**Validate before PR.** Agent 6 applies the patch to an isolated temp copy of the repo, spins up fresh Evil Twins, and re-runs every previously-failed scenario. The PR is opened only after all scenarios pass. The fix is proven, not proposed.

**Findings PR fallback.** If patching fails after 3 retry cycles, GhostVendor opens a draft PR with a structured findings report — root cause, failed scenarios, attempted patches, exact validation failure. The pipeline always produces a human-readable artifact. No run ends silently.

**Tavily-grounded criticality.** Vendor criticality scores are not just business-logic guesses. Tavily searches real-world reliability data — known outages, SLA breach reports, documented failure patterns — and feeds that intel to Agent 1 before the LLM assigns scores. A vendor with a documented history of cascading failures ranks higher within its category.

---

## Tavily — Real-World Vendor Intelligence

After the AST scan identifies candidate vendors from env var names, GhostVendor runs two targeted Tavily searches per vendor:

1. **Risk profile:** `{vendor} API failures incidents reliability SLA downtime`
2. **Failure modes:** `{vendor} API timeout rate limit errors 503 502 common failures`

The results are injected into the Agent 1 prompt alongside AST findings and source files. The LLM uses this to calibrate criticality scores and inform the `expected_resilience` contract for each vendor endpoint — grounding the attack plan in real failure history, not generic patterns.

If `TAVILY_API_KEY` is not set, this layer is silently skipped and the pipeline continues with AST-only enrichment.

---

## Evil Twin Chaos Modes

Each vendor gets its own FastAPI mock server that the demo app is pointed at via env var override. The Resilience Verifier runs a Sense→Reason→Act loop: observe baseline, plan attack, execute scenarios, decide whether to continue/escalate/stop after each result.

| Mode | What it injects |
|---|---|
| `timeout` | 30 s async sleep — forces the app to hit its timeout or hang forever |
| `502_burst` | Returns HTTP 502 Bad Gateway |
| `429_rate_limit` | Returns HTTP 429 Too Many Requests |
| `malformed_json` | Returns HTTP 200 with a broken JSON body |
| `empty_response` | Returns HTTP 200 with an empty body |

Evil Twin processes run in isolated temp directories and are fully cleaned up after each use, including on restart after timeout scenarios.

---

## Context Guard — Security Gate for LLM-Generated Code

Before any Evil Twin runs locally, Agent 3 puts it through a 3-layer security gate:

| Layer | Mechanism | What it catches |
|---|---|---|
| 1 — AST Inspection | Python `ast` module static analysis | `exec`, `eval`, `__import__`, subprocess calls, sensitive path references |
| 2 — Contree Sandbox | Nebius-hosted secure execution environment | Actual runtime behavior — network calls, process spawning, anything AST missed |
| 3 — LLM Policy Review | Nemotron Nano | Intent analysis — flags code that is structurally safe but semantically suspicious |

All three layers must pass. Any HIGH or BLOCKED risk discards the twin and halts the run. The Nebius Contree sandbox executes the twin in an isolated cloud environment — nothing it does can reach the local machine.

---

## Resilience Score

```
vendor_score   = baseline_pts (20) + scenario_score (0–80)   [capped at 100]
overall_score  = criticality-weighted average across all vendors
```

The VERIFY pass runs all 5 chaos modes. The rescore pass (after patching) runs only originally-failed modes and uses the actual failure count as the denominator — a vendor that failed 2 scenarios and fixed both scores 100, not a fraction. See [`docs/SCORING.md`](docs/SCORING.md) for the complete formula and outcome classification table.

---

## Validation Loop

After generating a patch, Agent 6 validates it locally before opening any PR:

1. Target repo copied to an isolated temp directory
2. Patched files written into the temp copy
3. `__pycache__` purged — Python compiles from patched source, not stale bytecode
4. `PYTHONPATH` remapped from original repo root to temp dir (repo-agnostic — works with any source layout: `src/`, `lib/`, `.`, etc.)
5. Evil Twins launched fresh
6. Patched app started from temp dir against the twins
7. Only previously-failed scenarios re-run
8. Pass = status code changed from original failure; fail = same failure feeds back to Agent 6 for the next retry

If validation passes, PR is opened. If it fails 3 times, the findings PR fallback fires.

---

## PR Strategy

### Success path — validated patch PR

When the patched app passes all previously-failed chaos scenarios:

- Branch `ghostvendor/resilience/<timestamp>` created on the target repo
- Each patched file committed with a descriptive message
- **Ready-for-review PR** (not draft) opened with resilience score before and after patch, every fixed scenario per vendor, patch strategy and root-cause summary, retry cycles needed, and reference to the triggering PR
- Triggering author requested as reviewer
- A human reviews the diff and merges when satisfied

### Fallback path — findings-only draft PR

When patching fails after 3 retry cycles:

- Branch `ghostvendor/findings/<timestamp>` created
- `GHOSTVENDOR_FINDINGS.md` committed with score, failed scenarios, root-cause diagnoses, attempted patches, and exact validation failure detail
- Draft PR opened — human reads findings and applies the fix manually or triggers a follow-up run

---

## Evals

The `evals/` directory contains a behavioral assertion suite that validates the pipeline end-to-end against [`NithinBR-AI/ghostvendor-eval-target`](https://github.com/NithinBR-AI/ghostvendor-eval-target) — a minimal Flask app with intentionally undefended vendor integrations (no timeouts, no retries, no fallbacks).

| # | Assertion | What it proves |
|---|---|---|
| 1 | Agent 1 discovers at least one vendor | AST scan + LLM enrichment works on the eval target |
| 2 | VERIFY score is below threshold (< 50/100) | The eval target is genuinely fragile — chaos modes expose real failures |
| 3 | VALIDATE score improves after patch | The patch fixed what broke — not just noise |
| 4 | A PR is opened with `ghostvendor` label | End-to-end pipeline completes and produces a GitHub artifact |
| 5 | Patch diff is non-empty for each vendor | Agent 6 produced a real change, not a zero-diff pass |

Evals run the full pipeline — no mocking, no stubs. Every assertion is a behavioral claim against real agent output.

---

## Setup & Test Build

### 1. Clone and install

```bash
git clone https://github.com/NithinBR-AI/ghostvendor.git
cd ghostvendor

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e ".[dev]"
```

### 2. Configure environment

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Open `.env` and fill in:

```
NEBIUS_API_KEY=...          # Nebius Token Factory API key — all LLM calls go through this
GITHUB_TOKEN=...            # GitHub personal access token (repo + pull_request scopes)
NEBIUS_PROJECT_ID=...       # Nebius project ID — required for Contree sandbox execution
TAVILY_API_KEY=...          # Tavily API key — optional, skipped gracefully if not set
GHOSTVENDOR_LOCAL_DEV=1     # Set to 1 for local runs; omit in GitHub Actions (uses workflow env)
```

### 3. Run unit tests

```bash
.venv\Scripts\python.exe -m pytest tests/unit/ -v
```

### 4. Start the dashboard

```bash
.venv\Scripts\python.exe -m streamlit run dashboard/app.py
```

Open `http://localhost:8501` — the dashboard pre-seeds demo run history on first launch.

### 5. Run the pipeline

Against the canonical demo app:

```bash
.venv\Scripts\python.exe main.py NithinBR-AI/ghostvendor-demo-app --triggered-by NithinBR-AI
```

Against any public Flask/FastAPI repo:

```bash
.venv\Scripts\python.exe main.py owner/repo-name
```

With triggering PR context (mirrors what GitHub Actions sends):

```bash
.venv\Scripts\python.exe main.py owner/repo-name --triggered-by github-login --pr 42 --branch feature/my-branch
```

### 6. GitHub Actions trigger (demo only)

The repo includes a GitHub Actions workflow (`.github/workflows/ghostvendor.yml`) that fires on `pull_request` events and calls `main.py` with the PR context. The self-hosted runner is registered on the author's machine — opening a PR on `NithinBR-AI/ghostvendor-demo-app` will trigger a live run during the demo, but cannot be reproduced by a reviewer without registering their own runner. Steps 1–5 above are fully self-contained and reproducible.

---

## Project Structure

```
ghostvendor/
├── main.py                          # Entrypoint: python main.py owner/repo [--triggered-by LOGIN] [--pr NUMBER] [--branch BRANCH]
├── Makefile                         # make run — canonical demo-app shortcut
├── pyproject.toml                   # Dependencies and package config
├── .env.example                     # Required env vars template
├── dashboard/
│   ├── app.py                       # Streamlit dashboard — live pipeline visualization
│   ├── viz.py                       # D3.js SVG pipeline graph (Voronoi core + hex nodes)
│   ├── db.py                        # SQLite read/write API for pipeline runs and events
│   └── schema.sql                   # DB schema — runs, run_events, vendor_results
├── docs/
│   ├── SCORING.md                   # Authoritative resilience score formula + outcome classification
│   └── architecture.html            # Interactive architecture diagram
├── evals/                           # Behavioral eval suite — 5 assertions against ghostvendor-eval-target
├── scripts/                         # Dev/debug scripts (not part of production pipeline)
├── src/
│   ├── agents/
│   │   ├── detective.py             # Agent 1 — AST scan + Tavily intel + LLM vendor enrichment
│   │   ├── twin_generator.py        # Agent 2 — Evil Twin FastAPI code generation
│   │   ├── context_guard.py         # Agent 3 — AST + Contree sandbox + LLM security gate
│   │   ├── resilience_verifier.py   # Agent 4 — Sense→Reason→Act chaos injection + scoring loop
│   │   ├── runtime_debugger.py      # Agent 5 — root cause analysis per vendor
│   │   └── patch_generator.py       # Agent 6 — patch generation + syntax validation + retry loop
│   ├── pipeline/
│   │   └── state_machine.py         # State transitions, retry logic, PR strategy, artifact store
│   ├── tools/
│   │   ├── evil_twin_runner.py      # Evil Twin subprocess lifecycle (start/chaos/restart/stop)
│   │   ├── evil_twin_template.py    # Evil Twin harness assembly (chaos globals injection)
│   │   ├── demo_app_runner.py       # Demo app subprocess lifecycle
│   │   ├── repo_cloner.py           # Clone repo, detect startup command + port
│   │   ├── ast_scanner.py           # Deterministic HTTP call + env var extraction
│   │   ├── github_client.py         # GitHub API — branch, commit, PR (draft + regular)
│   │   └── guardrails.py            # Pre-flight repo safety checks
│   ├── utils/
│   │   ├── nebius_client.py         # Nemotron Ultra/Super/Nano + DeepSeek-V4-Pro via Token Factory
│   │   └── logging_config.py        # Structured logging setup
│   ├── models/
│   │   ├── vendor_spec.py           # VendorSpec / Vendor schema
│   │   ├── resilience_result.py     # ScenarioResult / VendorResult / ResilienceReport
│   │   ├── diagnosis.py             # DiagnosisResult / DiagnosisReport schema
│   │   ├── patch.py                 # PatchResult / PatchReport / ExtraPatch schema
│   │   └── guard_decision.py        # GuardDecision / ASTFinding / RiskLevel schema
│   └── prompts/
│       ├── detective.txt            # Agent 1 system prompt
│       ├── twin_generator.txt       # Agent 2 system prompt
│       ├── context_guard.txt        # Agent 3 system prompt
│       ├── resilience_verifier.txt  # Agent 4 system prompt
│       ├── runtime_debugger.txt     # Agent 5 system prompt
│       └── patch_generator.txt      # Agent 6 system prompt
└── tests/
    ├── unit/                        # Unit tests — scoring, AST scanner, guard, evil twin template
    └── integration/                 # Integration tests — state machine end-to-end
```

---

## Scope & Constraints

| Constraint | Detail |
|---|---|
| **Python only** | AST scanning, PYTHONPATH remapping, and patch generation are Python-specific. Node.js, Go, and other runtimes are not currently supported. |
| **Flask or FastAPI** | Startup detection looks for `app.run()` or `uvicorn.run()` calls to identify the server port. Other frameworks or custom entrypoints may need manual port hints. |
| **Env-var-based vendor URLs** | Agent 1 finds vendors by detecting `os.environ.get(...)` calls near outbound HTTP calls. Vendors with hardcoded base URLs in source are not detected. |
| **HTTP vendors** | Chaos modes are HTTP-level. Non-HTTP dependencies such as databases, message queues, or gRPC services are out of scope. |

These constraints reflect deliberate scoping for the hackathon submission, not fundamental architectural limits.

---

## NVIDIA & Nebius

### NVIDIA Nemotron Models

GhostVendor uses the full Nemotron tier stack, with each model matched to what that agent actually needs:

| Model | Tier | Agent | Why this model |
|---|---|---|---|
| `nvidia/Nemotron-3-Ultra-550b-a55b` | Ultra | A1, A2, A4, A5 | 550B parameters — needed for complex multi-step reasoning: tracing vendor criticality from env vars through source code, planning a chaos attack sequence, making mid-attack Sense→Reason→Act decisions, and producing root-cause diagnoses with specific function names and fix strategies |
| `nvidia/nemotron-3-super-120b-a12b` | Super | A2 fallback | Activates when Ultra is rate-limited or slow — same Evil Twin generation task at 120B, preserving the 5-mode chaos harness structure |
| `nvidia/Nemotron-3_5-Lightning` | Nano | A3 | Context Guard needs speed, not depth — Nano processes the security gate in ~1s vs 8–12s for Ultra, and the task (classify code intent as SAFE/LOW/MEDIUM/HIGH/BLOCKED) is well-suited to a smaller model with a tight structured output schema |
| `deepseek-ai/DeepSeek-V4-Pro` | — | A6 | Patch generation sends the full source file (2,000–4,000 tokens input) and expects a complete fixed file back (same scale output). DeepSeek-V4-Pro on Token Factory consistently returns valid JSON at these token counts where Ultra occasionally truncates |

**Why Nemotron Ultra over other frontier models for A4:** The Resilience Verifier runs multiple LLM calls in a single pipeline run — one to plan the attack, one mid-attack to decide whether to continue, escalate, or stop after observing each scenario result. Ultra's instruction-following fidelity at each decision point prevents the cascading hallucinations that shorter models produce when the tool context grows large (30+ scenarios across multiple vendors).

### Nebius Token Factory

Token Factory is the single inference endpoint for all five models in the pipeline — `https://api.tokenfactory.nebius.com/v1/` with an OpenAI-compatible API. This meant:

- **Zero integration friction** — `nebius_client.py` is 80 lines. Every agent calls the same `ultra()`, `nano()`, `deepseek_pro()`, or `super_()` wrapper. Switching a model is one line change.
- **Tier selection under one key** — Ultra, Super, Nano, and DeepSeek all authenticate with a single `NEBIUS_API_KEY`. No per-model credentials, no separate rate limit pools to track.
- **Speed for iteration** — Token Factory latency on Ultra is low enough that the tight edit→run→observe loop is feasible during development. Each LLM call in the pipeline returns fast enough that the bottleneck is process startup and HTTP chaos injection, not inference wait time.
- **Fallback model switching** — Super activates automatically when Ultra hits a transient error. Because both are on the same endpoint, the fallback is a model name swap, not a credentials or base-URL change.

### Nebius Contree Sandbox

Agent 2 generates Python code (the Evil Twin FastAPI server). That code runs on the developer's machine — but only after Agent 3 clears it through the Contree sandbox first.

Contree is a Nebius-hosted isolated execution environment. GhostVendor sends the generated Evil Twin to Contree, runs it there, and observes whether it attempts network exfiltration, subprocess spawning, sensitive file access, or any other behavior that AST analysis missed. If Contree flags anything, the twin is discarded and the pipeline halts before the code ever reaches the local environment.

This is the only part of the pipeline that is not reproducible with any commodity cloud service — it is a Nebius-specific capability that makes the "execute LLM-generated code safely" guarantee possible.

---

## Stability Features

| Feature | Detail |
|---|---|
| GitHub retry wrapper | All GitHub API calls retried up to 3× with 5 s delay on transient failures |
| Port TIME_WAIT handling | Falls back to OS ephemeral port if preferred port is in TIME_WAIT |
| Full-file ownership | Agent 6 returns the complete fixed file — no partial diffs, no NameError from missing imports |
| Zero-diff detection | Agent 6 rejects patches that are identical to the original source — forces a real change |
| JSON repair | Agent 6 auto-repairs truncated or malformed JSON from the LLM before giving up |
| LLM wrapper stripping | All agents use shared `strip_llm_wrapper()` — handles missing `</think>` tags and absent fences |
| Caller contract patching | When Agent 6 changes a function's return shape, it patches all call sites — prevents 500s in route handlers after the client is fixed |
| Editable install isolation | `.pth` files in the demo app's venv are disabled during validation so `PYTHONPATH` remapping to the patched temp dir takes effect |
| Rescore twin reset | Each Evil Twin is launched fresh before the rescore loop — eliminates half-open connection corruption from VALIDATE's timeout scenarios |

---

## What's Next

### Stage 2 — Broader Language & Vendor Support
- **Node.js / TypeScript** — AST scanner and startup detector for Express/Fastify apps
- **Non-HTTP vendors** — chaos modes for Redis timeouts, SQS delivery failures, database connection drops
- **Hardcoded URL detection** — detect vendors not using env vars via static analysis of string literals
- **Multi-endpoint vendors** — attack each endpoint independently, not just the primary one

### Stage 3 — Production Deployment
- **GitHub App** — registers webhooks on `pull_request.opened`, `push`, and `schedule`; filters to only fire when vendor client files change; supports `/ghostvendor rerun` comment command
- **Job queue** — SQS-backed async dispatch; pipeline runs as ECS Fargate tasks
- **State persistence** — DynamoDB + S3 for run history: every pipeline run stored with score before/after, patches generated, PR opened, token cost, and duration
- **Secrets management** — AWS Secrets Manager per-tenant for Nebius API keys and GitHub tokens

### Stage 4 — Multi-Tenant & Observability
- **Multi-repo support** — per-repo config (vendor criticality overrides, excluded paths, minimum score threshold)
- **Run history dashboard** — score trajectory, PR links, time-to-fix, token cost; exportable for compliance
- **Alerting** — page on-call if rescore drops below threshold after a dependency upgrade
- **Feedback loop** — merged patches promote to golden examples for Agent 6 prompt improvement

---

## License

MIT
