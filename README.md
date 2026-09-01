# GhostVendor

**GhostVendor haunts your dependencies. Six AI agents impersonate your APIs, inject real failures, diagnose what breaks, patch the code, and open a PR only after CI proves it survives.**

Built with NVIDIA Nemotron models on [Nebius Token Factory](https://tokenfactory.nebius.com) for the [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com) — Coding & Agentic Engineering Track.

---

## How It Works

```
GitHub Repo → Discover → Model → Attack → Observe → Diagnose → Remediate → Re-test → PR
```

Six specialized agents run an autonomous closed loop:

| # | Agent | Model | Responsibility |
|---|---|---|---|
| 1 | Vendor & Repository Detective | Nemotron Ultra 253B | Dependency discovery, criticality scoring, `vendor_spec.json` |
| 2 | Adversarial Twin Generator | Nemotron Super 120B | Evil Twin — stateful FastAPI simulator with chaos modes |
| 3 | Context Guard | Nemotron Nano 30B | Security gate — AST inspection, policy checks |
| 4 | Resilience Verifier | Nemotron Nano 30B | CI-ready resilience test scenarios |
| 5 | Runtime Debugger | Nemotron Ultra 253B | CI log analysis, root-cause isolation |
| 6 | Resilience Strategy & Patch Generator | Nemotron Nano 30B | Pattern selection, minimal patch, PR description |

A Python state machine orchestrates the loop with deterministic control flow, retry limits (max 3), and artifact handoffs between agents. The patch is not accepted until CI passes — the model does not grade its own homework.

---

## Project Structure

```
ghostvendor/
├── main.py                        # Entrypoint — takes a GitHub repo (owner/name)
├── src/
│   ├── agents/                    # One file per agent (detective, twin_generator, context_guard, verifier, debugger, patcher)
│   ├── pipeline/
│   │   └── state_machine.py       # State transitions, retry logic, artifact store
│   ├── tools/
│   │   ├── nebius_client.py       # Nemotron Ultra/Super/Nano via Nebius Token Factory
│   │   ├── github_client.py       # GitHub API — repo read, branch, PR, workflow dispatch
│   │   ├── ast_scanner.py         # Deterministic dependency discovery
│   │   └── secret_redactor.py     # Redacts secrets before LLM calls
│   ├── models/
│   │   └── vendor_spec.py         # vendor_spec.json schema + validation
│   ├── prompts/                   # Agent system prompts (one .txt per agent)
│   └── config/                   # Failure taxonomy, criticality weights, resilience score weights
├── evals/
├── tests/
│   ├── unit/
│   └── integration/
├── pyproject.toml
└── .env.example
```

---

## Setup

```bash
git clone https://github.com/your-username/ghostvendor.git
cd ghostvendor

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e ".[dev]"

cp .env.example .env
# Add NEBIUS_API_KEY and GITHUB_TOKEN to .env

python main.py owner/repo-name
```

---

## NVIDIA & Nebius Usage

- **Models:** NVIDIA Nemotron Ultra 253B, Super 120B, Nano 30B via Nebius Token Factory
- **Endpoint:** `https://api.tokenfactory.nebius.com/v1/` (OpenAI-compatible)
- **Model selection:** Ultra for deep reasoning, Super for code generation, Nano for fast structured output

---

## License

MIT
