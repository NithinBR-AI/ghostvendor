# GhostVendor

**GhostVendor haunts your dependencies. Six AI agents impersonate your APIs, inject real failures, diagnose what breaks, patch the code, and open a PR only after CI proves it survives.**

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
| 2 | Adversarial Twin Generator | Nemotron Super | Generates Evil Twin — stateful FastAPI mock server with 5 chaos modes per vendor |
| 3 | Context Guard | Nemotron Ultra | Security gate — AST inspection → Contree sandbox execution → LLM policy review (3 layers, in order) |
| 4 | Resilience Verifier | Nemotron Ultra | Launches twins locally, injects chaos, runs demo app, scores resilience 0–100 |
| 5 | Runtime Debugger | Nemotron Ultra | Root-cause analysis on all failed scenarios |
| 6 | Patch Generator | Nemotron Super | Generates minimal patch, validates in Contree sandbox, opens PR |

A Python state machine orchestrates the loop with deterministic control flow, retry limits (max 3 remediation cycles), and structured artifact handoffs between agents.

---

## Pipeline States

```
DISCOVER   Agent 1 — clone repo, AST-scan source, LLM-enrich vendor specs
ATTACK     Agent 2 — generate one Evil Twin (FastAPI) per vendor
GUARD      Agent 3 — 3-layer safety check: AST + Contree sandbox + LLM review
VERIFY     Agent 4 — Sense→Reason→Act loop: chaos injection + resilience scoring
DIAGNOSE   Agent 5 — root-cause analysis on failures
REMEDIATE  Agent 6 — patch generation
VALIDATE   Agent 6 — Contree sandbox + PR
DONE / FAILED
```

---

## Evil Twin Chaos Modes

Each vendor gets its own FastAPI mock that the demo app points at (via env var). The verifier cycles through up to 5 scenarios per vendor:

| Mode | What it injects |
|---|---|
| `timeout` | 30 s async sleep — forces the app to hit its timeout (or hang forever) |
| `502_burst` | Returns HTTP 502 Bad Gateway |
| `429_rate_limit` | Returns HTTP 429 Too Many Requests |
| `malformed_json` | Returns HTTP 200 with a broken JSON body |
| `empty_response` | Returns HTTP 200 with an empty body |

The Ultra LLM observes each result and decides: **continue**, **escalate**, or **stop_early**. High-criticality vendors (score ≥ 80) require at least 4 scenarios before any early stop.

---

## Resilience Score

```
Score = baseline_pts + scenario_score

baseline_pts   = 20 if app handles normal traffic, else 0
scenario_score = (sum of scenario pts / (5 scenarios × 16 pts)) × 80

Per scenario:  PASS = 16 pts | DEGRADED = 8 pts | FAIL = 0 pts
```

The denominator is always 5 — running fewer scenarios due to early stop scores proportionally lower, never inflated.

Final score is a criticality-weighted average across all vendors.

---

## Project Structure

```
ghostvendor/
├── main.py                          # Entrypoint: python main.py owner/repo
├── src/
│   ├── agents/
│   │   ├── detective.py             # Agent 1 — vendor discovery + criticality scoring
│   │   ├── twin_generator.py        # Agent 2 — Evil Twin FastAPI code generation
│   │   ├── context_guard.py         # Agent 3 — AST + sandbox + LLM security gate
│   │   ├── resilience_verifier.py   # Agent 4 — chaos injection + scoring loop
│   │   ├── runtime_debugger.py      # Agent 5 — root cause analysis per vendor
│   │   └── patch_generator.py       # Agent 6 — unified diff generation + syntax validation
│   ├── pipeline/
│   │   └── state_machine.py         # State transitions, retry logic, artifact store
│   ├── tools/
│   │   ├── evil_twin_runner.py      # Evil Twin subprocess lifecycle (start/chaos/stop)
│   │   ├── demo_app_runner.py       # Demo app subprocess lifecycle
│   │   ├── repo_cloner.py           # Clone repo, detect startup command + port
│   │   ├── ast_scanner.py           # Deterministic HTTP call + env var extraction
│   │   └── github_client.py         # GitHub API — branch, PR, workflow dispatch
│   ├── utils/
│   │   ├── nebius_client.py         # Nemotron Ultra/Super/Nano via Nebius Token Factory
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

Run against any public Flask/FastAPI repo:

```bash
python main.py owner/repo-name
```

For local development against a repo cloned alongside `ghostvendor/`:

```bash
GHOSTVENDOR_LOCAL_DEV=1 python main.py owner/repo-name
```

---

## NVIDIA & Nebius Usage

| Tier | Model | Used for |
|---|---|---|
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | Vendor enrichment, LLM security review, attack planning, Sense→Reason→Act decisions |
| Super | `nvidia/nemotron-3-super-120b-a12b` | Evil Twin code generation, patch generation |
| Nano | `nvidia/Nemotron-3_5-Lightning` | Fast structured output (reserved for high-volume classification tasks) |

Each tier has a fallback model. All calls go through `https://api.tokenfactory.nebius.com/v1/` (OpenAI-compatible).

---

## License

MIT
