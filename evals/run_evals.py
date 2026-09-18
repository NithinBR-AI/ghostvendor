"""
GhostVendor behavioral eval suite.

Runs the full pipeline against NithinBR-AI/ghostvendor-eval-target in dry_run mode
(no GitHub PR opened) and asserts 4 behavioral claims against the agent outputs.

Usage:
    .venv\\Scripts\\python.exe evals/run_evals.py

Exit code 0 = all assertions passed.
Exit code 1 = one or more assertions failed.
"""

import logging
import os
import sys

# Allow running from repo root without pip install -e
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

from pipeline.state_machine import StateMachine

logging.basicConfig(
    level=logging.WARNING,  # suppress pipeline noise; evals prints its own output
    format="%(levelname)s %(name)s: %(message)s",
)

EVAL_REPO = "NithinBR-AI/ghostvendor-eval-target"
SCORE_THRESHOLD = 50  # eval target must score below this to prove fragility


def run_pipeline() -> StateMachine:
    print(f"\n{'='*60}")
    print(f"GhostVendor Eval Suite")
    print(f"Target: {EVAL_REPO}")
    print(f"{'='*60}\n")
    print("Running full pipeline (dry_run=True — no PR will be opened)...")

    sm = StateMachine(repo=EVAL_REPO, dry_run=True)
    sm.run()

    print(f"\nPipeline finished in state: {sm.state.name}\n")
    return sm


def assert_vendor_discovery(sm: StateMachine) -> tuple[bool, str]:
    spec = sm.artifacts.vendor_spec
    if not spec or not spec.discovered_vendors:
        return False, "No vendors discovered — Agent 1 produced an empty vendor_spec"
    names = [v.name for v in spec.discovered_vendors]
    return True, f"Discovered {len(names)} vendor(s): {', '.join(names)}"


def assert_pre_patch_score(sm: StateMachine) -> tuple[bool, str]:
    score = sm.artifacts.resilience_score_before
    if score == 0 and sm.artifacts.resilience_report is None:
        return False, "VERIFY never ran — no resilience_report in artifacts"
    if score >= SCORE_THRESHOLD:
        return False, f"Pre-patch score {score}/100 >= {SCORE_THRESHOLD} — eval target is not fragile enough"
    return True, f"Pre-patch score {score}/100 < {SCORE_THRESHOLD} ✓"


def assert_non_empty_patches(sm: StateMachine) -> tuple[bool, str]:
    report = sm.artifacts.patch_report
    if not report or not report.patches:
        return False, "No patch_report — Agent 6 did not run or produced no patches"
    empty = [p.vendor for p in report.patches if not p.fixed_source or not p.fixed_source.strip()]
    if empty:
        return False, f"Empty fixed_source for vendor(s): {', '.join(empty)}"
    vendors = [p.vendor for p in report.patches]
    return True, f"Non-empty patches for: {', '.join(vendors)} ✓"


def assert_score_improvement(sm: StateMachine) -> tuple[bool, str]:
    before = sm.artifacts.resilience_score_before
    after = sm.artifacts.resilience_score_after
    if after == 0 and sm.state.name != "DONE":
        return False, f"Pipeline did not reach DONE (state={sm.state.name}) — rescore never ran"
    if after <= before:
        return False, f"Score did not improve: {before}/100 -> {after}/100"
    return True, f"Score improved: {before}/100 -> {after}/100 ✓"


def main() -> int:
    sm = run_pipeline()

    assertions = [
        ("Vendor discovery",       assert_vendor_discovery),
        ("Pre-patch score < 50",   assert_pre_patch_score),
        ("Non-empty patches",      assert_non_empty_patches),
        ("Score improvement",      assert_score_improvement),
    ]

    print(f"{'─'*60}")
    print(f"{'Assertion':<30} {'Result':<8} Detail")
    print(f"{'─'*60}")

    passed = 0
    failed = 0
    for name, fn in assertions:
        ok, detail = fn(sm)
        status = "PASS" if ok else "FAIL"
        print(f"{name:<30} {status:<8} {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"{'─'*60}")
    print(f"\n{passed}/{len(assertions)} assertions passed.\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
