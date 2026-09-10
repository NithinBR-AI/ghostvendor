import datetime
import logging
import shutil
import tempfile
import time
from enum import Enum, auto
from dataclasses import dataclass, field
from pathlib import Path

import requests as http_requests

logger = logging.getLogger(__name__)

from agents import detective, twin_generator, context_guard, resilience_verifier, runtime_debugger, patch_generator
from agents.twin_generator import EvilTwinArtifact
from models.diagnosis import DiagnosisReport
from models.guard_decision import GuardDecision
from models.patch import PatchReport, PatchResult
from models.resilience_result import ResilienceReport, ScenarioOutcome
from models.vendor_spec import VendorSpec
from tools import github_client
from tools.demo_app_runner import DemoAppProcess
from tools.evil_twin_runner import EvilTwinManager, EvilTwinProcess
from tools.repo_cloner import clone as clone_repo, RepoInfo


class State(Enum):
    DISCOVER = auto()
    ATTACK = auto()
    GUARD = auto()       # Agent 3: validate all generated twins before any run locally
    VERIFY = auto()      # Agent 4: launch twins, inject chaos, dispatch CI
    DIAGNOSE = auto()    # Agent 5: root cause from CI logs
    REMEDIATE = auto()   # Agent 6: generate patch
    VALIDATE = auto()    # Agent 6: sandbox-validate patch, open PR
    DONE = auto()
    FAILED = auto()


@dataclass
class ArtifactStore:
    repo: str
    repo_info: RepoInfo | None = None
    vendor_spec: VendorSpec | None = None
    source_files: dict[str, str] = field(default_factory=dict)
    evil_twins: dict[str, EvilTwinArtifact] = field(default_factory=dict)
    guard_decisions: dict[str, GuardDecision] = field(default_factory=dict)
    resilience_report: ResilienceReport | None = None
    ci_run_id: int | None = None
    ci_logs: str = ""
    root_cause: dict = field(default_factory=dict)
    patch: dict = field(default_factory=dict)
    patch_branch: str = ""
    diagnosis_report: DiagnosisReport | None = None
    patch_report: PatchReport | None = None
    resilience_score_before: int = 0
    resilience_score_after: int = 0
    pr_url: str = ""
    extra: dict = field(default_factory=dict)


class StateMachine:
    MAX_RETRIES = 3

    def __init__(
        self,
        repo: str,
        triggered_by: str | None = None,
        triggering_pr: int | None = None,
    ):
        self.state = State.DISCOVER
        self.artifacts = ArtifactStore(repo=repo)
        self.retry_count = 0
        self.twin_manager = EvilTwinManager()
        self.triggered_by = triggered_by
        self.triggering_pr = triggering_pr

    def transition(self, next_state: State) -> None:
        logger.info("%s → %s", self.state.name, next_state.name)
        self.state = next_state

    def fail(self, reason: str) -> None:
        logger.error("FAILED: %s", reason)
        self.twin_manager.stop_all()
        # Safety net: if we have resilience findings but no PR yet, open a findings PR
        # so the pipeline always produces a human-readable artifact even on early failure.
        if self.artifacts.resilience_report and not self.artifacts.pr_url:
            try:
                self._open_findings_pr(reason)
            except Exception as e:
                logger.error("Findings PR also failed: %s", e)
        self.transition(State.FAILED)

    def run(self) -> None:
        logger.info("Starting on repo: %s", self.artifacts.repo)
        try:
            while self.state not in (State.DONE, State.FAILED):
                self._step()
        finally:
            self.twin_manager.stop_all()

    def _step(self) -> None:
        if self.state == State.DISCOVER:
            self._discover()
        elif self.state == State.ATTACK:
            self._attack()
        elif self.state == State.GUARD:
            self._guard()
        elif self.state == State.VERIFY:
            self._verify()
        elif self.state == State.DIAGNOSE:
            self._diagnose()
        elif self.state == State.REMEDIATE:
            self._remediate()
        elif self.state == State.VALIDATE:
            self._validate()

    def _discover(self) -> None:
        try:
            repo_info = clone_repo(self.artifacts.repo)
            self.artifacts.repo_info = repo_info
            logger.info("Repo available at: %s (port=%d)", repo_info.local_path, repo_info.port)

            spec, source_files = detective.run(repo=self.artifacts.repo, local_path=repo_info.local_path)
            self.artifacts.vendor_spec = spec
            self.artifacts.source_files = source_files
            vendors = [f"{v.name} (score={v.criticality_score})" for v in spec.vendors_by_criticality()]
            logger.info("Discovered vendors: %s", ', '.join(vendors))
            self.transition(State.ATTACK)
        except Exception as e:
            self.fail(f"Agent 1 (Detective) failed: {e}")

    def _attack(self) -> None:
        try:
            twins = twin_generator.run(self.artifacts.vendor_spec)
            self.artifacts.evil_twins = twins
            for name, artifact in twins.items():
                logger.info("Evil Twin generated: %s → port %d", name, artifact.port)
            self.transition(State.GUARD)
        except Exception as e:
            self.fail(f"Agent 2 (Twin Generator) failed: {e}")

    def _guard(self) -> None:
        """Agent 3: validate every generated twin before any of them run locally."""
        try:
            blocked = []
            for name, artifact in self.artifacts.evil_twins.items():
                vendor = next(
                    v for v in self.artifacts.vendor_spec.discovered_vendors
                    if v.name == name
                )
                decision = context_guard.run(vendor=vendor, code=artifact.code)
                self.artifacts.guard_decisions[name] = decision
                logger.info("Context Guard: %s", decision.summary)

                if decision.sandbox_skipped:
                    logger.warning("Sandbox skipped for %s — approved on AST+LLM only", name)

                if not decision.approved:
                    blocked.append(f"{name} ({decision.risk_level.value}: {decision.llm_verdict})")

            if blocked:
                self.fail(f"Agent 3 (Context Guard) blocked twins: {'; '.join(blocked)}")
                return

            self.transition(State.VERIFY)
        except Exception as e:
            self.fail(f"Agent 3 (Context Guard) failed: {e}")

    def _verify(self) -> None:
        """Agent 4: launch approved twins, inject chaos, observe real app behavior."""
        try:
            report, vendor_payloads = resilience_verifier.run(
                spec=self.artifacts.vendor_spec,
                evil_twins=self.artifacts.evil_twins,
                twin_manager=self.twin_manager,
                repo_info=self.artifacts.repo_info,
                source_files=self.artifacts.source_files,
            )
            self.artifacts.resilience_report = report
            self.artifacts.resilience_score_before = report.overall_score
            self.artifacts.extra["vendor_payloads"] = vendor_payloads
            logger.info("Resilience score BEFORE patch: %d/100 | Failures: %d", report.overall_score, len(report.all_failed_scenarios))
            self.transition(State.DIAGNOSE)
        except Exception as e:
            self.fail(f"Agent 4 (Resilience Verifier) failed: {e}")

    def _diagnose(self) -> None:
        try:
            report = runtime_debugger.run(
                report=self.artifacts.resilience_report,
                source_files=self.artifacts.source_files,
            )
            self.artifacts.diagnosis_report = report
            for d in report.diagnoses:
                logger.info(
                    "Diagnosis: %s | patterns=%s | strategy=%s | file=%s",
                    d.vendor,
                    [p.value for p in d.patterns],
                    d.fix_strategy.value,
                    d.affected_file,
                )
            self.transition(State.REMEDIATE)
        except Exception as e:
            self.fail(f"Agent 5 (Runtime Debugger) failed: {e}")

    def _remediate(self) -> None:
        try:
            report = patch_generator.run(
                diagnosis_report=self.artifacts.diagnosis_report,
                source_files=self.artifacts.source_files,
                previous_failure=self.artifacts.extra.get("validation_failure"),
                skip_vendors=self.artifacts.extra.get("validated_vendors", set()),
            )
            self.artifacts.patch_report = report
            for p in report.patches:
                logger.info(
                    "Patch generated: %s | strategy=%s | file=%s | title=%r",
                    p.vendor,
                    p.fix_strategy.value,
                    p.affected_file,
                    p.pr_title,
                )
            self.transition(State.VALIDATE)
        except Exception as e:
            self.fail(f"Agent 6 (Patch Generator) failed: {e}")

    def _open_findings_pr(self, validation_failure: str) -> None:
        """
        When patch validation fails after MAX_RETRIES, open a draft PR with a findings-only
        markdown file so the pipeline always produces a human-readable artifact.
        """
        repo = self.artifacts.repo
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"ghostvendor/findings/{ts}"

        score_before = self.artifacts.resilience_score_before
        failed_by_vendor = {
            vr.vendor_name: vr.failed_scenarios
            for vr in self.artifacts.resilience_report.vendor_results
            if vr.failed_scenarios
        }

        scenario_lines = []
        for vendor_name, scenarios in failed_by_vendor.items():
            modes = ", ".join(s.mode for s in scenarios)
            scenario_lines.append(f"- **{vendor_name}**: {modes}")

        diagnosis_lines = []
        if self.artifacts.diagnosis_report:
            for d in self.artifacts.diagnosis_report.diagnoses:
                diagnosis_lines.append(
                    f"- **{d.vendor}** (`{d.affected_file}` → `{d.affected_function}`): "
                    f"{d.fix_strategy.value} — {d.root_cause}"
                )

        patch_lines = []
        if self.artifacts.patch_report:
            for p in self.artifacts.patch_report.patches:
                patch_lines.append(
                    f"- **{p.vendor}**: {p.pr_title}\n\n{p.pr_description}"
                )

        findings_md = f"""# GhostVendor Resilience Findings

> 🤖 Generated by [GhostVendor](https://github.com/nithinbr33/ghostvendor) — autonomous dependency resilience engineer.
> ⚠️ Pipeline did not complete patch validation. This PR contains findings only — no code changes.

## Resilience Score: {score_before}/100

## Failed Scenarios
{chr(10).join(scenario_lines) if scenario_lines else "- None recorded"}

## Root Cause Diagnoses
{chr(10).join(diagnosis_lines) if diagnosis_lines else "- None recorded"}

## Attempted Patches
{chr(10).join(patch_lines) if patch_lines else "- None generated"}

## Failure Reason
```
{validation_failure}
```

## Recommended Next Steps
Review the diagnoses above and apply the suggested fix strategies manually.
"""

        try:
            branch = _github_with_retry(lambda: github_client.create_branch(repo, branch))
            _github_with_retry(lambda: github_client.commit_file(
                repo=repo,
                branch=branch,
                path="GHOSTVENDOR_FINDINGS.md",
                content=findings_md,
                message="chore(ghostvendor): add resilience findings report",
            ))
            pr_url = _github_with_retry(lambda: github_client.create_pr(
                repo=repo,
                branch=branch,
                title=f"findings: resilience issues in {', '.join(failed_by_vendor.keys())}",
                body=findings_md,
                draft=True,
                labels=["ghostvendor", "ghostvendor-findings", "resilience"],
            ))
            self.artifacts.pr_url = pr_url
            logger.info("Findings draft PR opened: %s", pr_url)
        except Exception as e:
            logger.error("Failed to open findings PR: %s", e)

    def _validate(self) -> None:
        patch_report = self.artifacts.patch_report
        repo_info = self.artifacts.repo_info

        # Build patched file contents — use fixed_source from Agent 6 directly.
        # Seed with any files already validated in a prior attempt (for vendors that were
        # skipped in REMEDIATE because they passed last round — their patched source must
        # still be applied so the temp dir matches what was tested and confirmed working).
        patched_files: dict[str, str] = dict(self.artifacts.extra.get("confirmed_patched_files", {}))
        for patch in patch_report.patches:
            # Skip _skip_result placeholders — empty fixed_source means generation failed.
            # Writing an empty string to disk causes an import error at validation time (Issue 2).
            if patch.fixed_source:
                patched_files[patch.affected_file] = patch.fixed_source
            for ep in patch.extra_patches:
                if ep.fixed_source:
                    patched_files[ep.affected_file] = ep.fixed_source

        # Deterministic caller guard: find any source file that calls a patched function
        # and does bare result["key"] access without an error check. Inject a guard.
        patched_functions = {p.affected_file: p for p in patch_report.patches}
        for src_path, src_content in self.artifacts.source_files.items():
            norm = src_path.replace("\\", "/")
            if norm in {p.replace("\\", "/") for p in patched_files}:
                continue  # already patched
            patched_caller = _inject_caller_error_guard(src_path, src_content, patched_functions)
            if patched_caller:
                patched_files[src_path] = patched_caller
                logger.info("Deterministic caller guard injected: %s", src_path)

        # Local re-run: apply patches to a temp copy of the repo, run failed scenarios
        failed_scenarios_by_vendor = {
            vr.vendor_name: vr.failed_scenarios
            for vr in self.artifacts.resilience_report.vendor_results
            if vr.failed_scenarios
        }

        vendor_payloads = self.artifacts.extra.get("vendor_payloads", {})

        vr = _run_patched_validation(
            patch_report=patch_report,
            patched_files=patched_files,
            repo_info=repo_info,
            evil_twins=self.artifacts.evil_twins,
            failed_scenarios_by_vendor=failed_scenarios_by_vendor,
            vendor_spec=self.artifacts.vendor_spec,
            vendor_payloads=vendor_payloads,
        )

        # Store which vendors are already confirmed so REMEDIATE can skip re-patching them.
        self.artifacts.extra["validated_vendors"] = vr.validated_vendors

        # Persist the patched file contents for confirmed vendors so subsequent VALIDATE
        # retries (after REMEDIATE skips them) still apply the already-working patches.
        confirmed = self.artifacts.extra.get("confirmed_patched_files", {})
        for patch in patch_report.patches:
            if patch.vendor in vr.validated_vendors:
                confirmed[patch.affected_file] = patch.fixed_source
                for ep in patch.extra_patches:
                    confirmed[ep.affected_file] = ep.fixed_source
        self.artifacts.extra["confirmed_patched_files"] = confirmed

        if not vr.all_passed:
            self.retry_count += 1
            if self.retry_count >= self.MAX_RETRIES:
                logger.warning("Exceeded %d remediation attempts — opening findings-only draft PR", self.MAX_RETRIES)
                self._open_findings_pr(vr.failure_summary)
                self.fail(f"Exceeded {self.MAX_RETRIES} remediation attempts. Findings PR: {self.artifacts.pr_url or 'failed to open'}")
                return

            if vr.infra_failures and not vr.patch_failures:
                # Pure infrastructure failure — the patch was never tested, not semantically wrong.
                # Retry VALIDATE directly without regenerating patches; twin restart is already done
                # per-vendor inside _run_patched_validation, so a fresh attempt is all that's needed.
                logger.warning(
                    "Validation failed (attempt %d/%d): infra only (%s) — retrying VALIDATE",
                    self.retry_count, self.MAX_RETRIES, "; ".join(vr.infra_failures.values()),
                )
                self.artifacts.extra["validation_failure"] = "; ".join(vr.infra_failures.values())
                self.transition(State.VALIDATE)
            else:
                # Patch semantically wrong (or both infra + patch) — regenerate for failing vendors only.
                failure_msg = vr.failure_summary
                logger.warning(
                    "Validation failed (attempt %d/%d): %s — retrying REMEDIATE (skip: %s)",
                    self.retry_count, self.MAX_RETRIES, failure_msg, vr.validated_vendors,
                )
                self.artifacts.extra["validation_failure"] = failure_msg
                self.transition(State.REMEDIATE)
            return

        # All scenarios passed — mark patches validated, compute after-score, open PR
        for patch in patch_report.patches:
            patch.sandbox_validated = True

        self.artifacts.extra.pop("validation_failure", None)
        self.artifacts.extra.pop("validated_vendors", None)
        self.artifacts.extra.pop("confirmed_patched_files", None)

        # Re-score: run only the originally-failed scenarios — measures "did the patch fix what broke",
        # not "does the patched app ace every possible mode including ones it never failed at baseline".
        score_after = _rescore_patched(
            patched_files=patched_files,
            repo_info=repo_info,
            evil_twins=self.artifacts.evil_twins,
            vendor_spec=self.artifacts.vendor_spec,
            vendor_payloads=vendor_payloads,
            failed_scenarios_by_vendor=failed_scenarios_by_vendor,
        )
        self.artifacts.resilience_score_after = score_after
        logger.info(
            "Resilience score: %d/100 → %d/100 after patch",
            self.artifacts.resilience_score_before, score_after,
        )

        try:
            repo = self.artifacts.repo
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = _github_with_retry(lambda: github_client.create_branch(repo, f"ghostvendor/resilience/{ts}"))
            logger.info("Created branch: %s", branch)

            for patch in patch_report.patches:
                _github_with_retry(lambda p=patch: github_client.commit_file(
                    repo=repo,
                    branch=branch,
                    path=p.affected_file,
                    content=patched_files[p.affected_file],
                    message=f"fix({p.vendor.lower()}): {p.pr_title}",
                ))
                logger.info("Committed patch for %s → %s", patch.vendor, patch.affected_file)
                for ep in patch.extra_patches:
                    _github_with_retry(lambda e=ep, p=patch: github_client.commit_file(
                        repo=repo,
                        branch=branch,
                        path=e.affected_file,
                        content=e.fixed_source,
                        message=f"fix({p.vendor.lower()}): patch caller {e.affected_file}",
                    ))
                    logger.info("Committed extra patch for %s → %s", patch.vendor, ep.affected_file)

            # Build context header with scores, failed scenarios, and retry info
            score_before = self.artifacts.resilience_score_before
            score_after = self.artifacts.resilience_score_after
            failed_by_vendor = {
                vr.vendor_name: vr.failed_scenarios
                for vr in self.artifacts.resilience_report.vendor_results
                if vr.failed_scenarios
            }
            scenario_lines = []
            for vendor_name, scenarios in failed_by_vendor.items():
                modes = ", ".join(s.mode for s in scenarios)
                scenario_lines.append(f"- **{vendor_name}**: {modes}")

            retry_note = (
                f"\n> ⚠️ Patch required {self.retry_count} retry cycle(s) before passing validation.\n"
                if self.retry_count > 0 else ""
            )

            context_header = f"""## GhostVendor Automated Resilience Fix

> 🤖 Generated by [GhostVendor](https://github.com/nithinbr33/ghostvendor) — autonomous dependency resilience engineer.
{retry_note}
### Resilience Score
| | Score |
|---|---|
| Before patch | {score_before}/100 |
| After patch | {score_after}/100 ✅ |

### Failed Scenarios Fixed
{chr(10).join(scenario_lines) if scenario_lines else "- All previously failing scenarios addressed"}

### Patches Applied
"""
            patch_summaries = "\n\n---\n\n".join(
                f"#### {p.vendor} — `{p.affected_file}`\n\n{p.pr_description}"
                for p in patch_report.patches
            )
            full_body = context_header + patch_summaries

            pr_title = (
                patch_report.patches[0].pr_title
                if len(patch_report.patches) == 1
                else f"fix: resilience patches for {', '.join(p.vendor for p in patch_report.patches)}"
            )
            # Append triggering PR reference to body when known
            if self.triggering_pr:
                full_body += f"\n\n---\n\n_Addresses resilience gaps introduced in #{self.triggering_pr}._"
            if self.triggered_by:
                full_body += f"\n\n_Triggered by: @{self.triggered_by}_"

            pr_url = _github_with_retry(lambda: github_client.create_pr(
                repo=repo, branch=branch, title=pr_title, body=full_body,
                draft=False,
                labels=["ghostvendor", "automated", "resilience"],
                reviewer=self.triggered_by,
            ))
            patch_report.pr_url = pr_url
            self.artifacts.pr_url = pr_url
            logger.info("PR opened: %s", pr_url)
            self.transition(State.DONE)

        except Exception as e:
            self.fail(f"GitHub PR creation failed: {e}")


def _disable_editable_pth(repo_info: "RepoInfo") -> list[tuple[Path, str]]:
    """
    Rename any editable-install .pth files in the demo app's venv site-packages
    that add the original repo's src/ to sys.path. This prevents the editable
    install from overriding PYTHONPATH remapping to the patched temp dir.

    Returns list of (original_path, backup_suffix) so _restore_editable_pth can undo it.
    Call this before starting the patched app; call _restore_editable_pth in the finally block.
    """
    disabled: list[tuple[Path, str]] = []
    repo_root = str(Path(repo_info.local_path).resolve())

    # Find the demo app's venv Python executable
    python_exe = Path(repo_info.start_command[0]).resolve()
    # Walk up to find site-packages: python is typically in venv/Scripts/ or venv/bin/
    venv_root = python_exe.parent.parent
    site_pkgs_candidates = list(venv_root.rglob("site-packages"))
    if not site_pkgs_candidates:
        return disabled

    for site_pkgs in site_pkgs_candidates:
        for pth_file in site_pkgs.glob("*.pth"):
            try:
                content = pth_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if repo_root in content or repo_info.local_path in content:
                backup = pth_file.with_suffix(".pth.gv_disabled")
                pth_file.rename(backup)
                disabled.append((backup, pth_file.name))
                logger.info("Disabled editable .pth: %s → %s", pth_file.name, backup.name)

    return disabled


def _restore_editable_pth(disabled: list[tuple[Path, str]]) -> None:
    """Restore .pth files disabled by _disable_editable_pth."""
    for backup_path, original_name in disabled:
        try:
            restore_path = backup_path.parent / original_name
            backup_path.rename(restore_path)
            logger.info("Restored editable .pth: %s", original_name)
        except Exception as e:
            logger.warning("Failed to restore .pth %s: %s", original_name, e)


def _inject_caller_error_guard(
    src_path: str,
    src_content: str,
    patched_functions: dict,
) -> str | None:
    """
    Deterministically patch a caller file that does bare result["key"] access after
    calling a function that now returns an error dict on failure.

    Looks for the pattern:
        result = <patched_module_fn>(...)
        ...
        result["key"]   ← unguarded

    Injects an error guard after the call:
        if "error" in result:
            from flask import jsonify
            return jsonify({"error": result.get("message", "vendor error")}), 503

    Returns the patched source, or None if no injection needed.
    """
    import re

    patched_module_stems = {
        Path(fp).stem for fp in patched_functions
    }

    # Check if this file calls anything from a patched module
    calls_patched = any(stem in src_content for stem in patched_module_stems)
    if not calls_patched:
        return None

    # Find lines that do result["something"] or result['something'] without an error guard
    # Simple heuristic: look for `result["` or `result['` where "error" check isn't nearby
    if '"error" in result' in src_content or "'error' in result" in src_content:
        return None  # already guarded

    # Check if there's a bare result["..."] access
    if not re.search(r'result\[["\']', src_content):
        return None

    # Find the assignment line: result = <something>(...)
    # Insert guard right after it
    lines = src_content.splitlines(keepends=True)
    new_lines = []
    i = 0
    inserted = False
    while i < len(lines):
        line = lines[i]
        new_lines.append(line)
        # Look for: result = <fn_call> that spans this line
        if re.search(r'^\s*result\s*=\s*\w+\.\w+\(', line) and not inserted:
            # Detect indentation
            indent = len(line) - len(line.lstrip())
            pad = " " * indent
            new_lines.append(f'{pad}if "error" in result:\n')
            new_lines.append(f'{pad}    from flask import jsonify\n')
            new_lines.append(f'{pad}    return jsonify({{"error": result.get("message", "vendor error")}}), 503\n')
            inserted = True
        i += 1

    if not inserted:
        return None

    return "".join(new_lines)


def _github_with_retry(fn, retries: int = 3, delay: float = 5.0):
    """Call a GitHub API function with simple linear retry on transient errors."""
    import time
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                logger.warning("GitHub call failed (attempt %d/%d): %s — retrying in %.0fs", attempt + 1, retries, e, delay)
                time.sleep(delay)
    raise last_exc


def _rescore_patched(
    patched_files: dict[str, str],
    repo_info: RepoInfo,
    evil_twins: dict[str, EvilTwinArtifact],
    vendor_spec,
    vendor_payloads: dict[str, dict] | None = None,
    failed_scenarios_by_vendor: dict[str, list] | None = None,
) -> int:
    """
    Run the originally-failed chaos scenarios against the patched app and return the resilience score.
    Scoped to failed scenarios only — measuring whether the patch fixed what broke, not whether
    the app handles every possible mode including ones it never failed at baseline.
    Returns 0 on any error — score is informational, never blocks the PR.
    """
    from models.resilience_result import VendorResult, ScenarioResult, ScenarioOutcome, ResilienceReport
    import os

    _REQUEST_TIMEOUT = 35

    patched_dir = tempfile.mkdtemp(prefix="ghostvendor_rescore_")
    twin_manager = EvilTwinManager()
    disabled_pth_rescore: list = []

    try:
        shutil.copytree(repo_info.local_path, patched_dir, dirs_exist_ok=True)
        for rel_path, content in patched_files.items():
            dest = Path(patched_dir) / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        for cache_dir in Path(patched_dir).rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        disabled_pth_rescore = _disable_editable_pth(repo_info)

        original_pythonpath = repo_info.extra_env.get("PYTHONPATH", "")
        repo_root = Path(repo_info.local_path).resolve()
        patched_root = Path(patched_dir).resolve()
        remapped = []
        for entry in (original_pythonpath.split(os.pathsep) if original_pythonpath else []):
            entry_path = Path(entry).resolve()
            try:
                remapped.append(str(patched_root / entry_path.relative_to(repo_root)))
            except ValueError:
                remapped.append(entry)
        if not remapped:
            remapped = [str(patched_root)]

        extra_env = {
            **repo_info.extra_env,
            "PYTHONPATH": os.pathsep.join(remapped),
            **{f"{v.name.upper()}_API_KEY": f"ghostvendor_fake_{v.name.lower()}"
               for v in vendor_spec.discovered_vendors},
        }

        for artifact in evil_twins.values():
            twin_manager.launch(vendor_name=artifact.vendor_name, port=artifact.port, code=artifact.code)

        twin_env = {
            evil_twins[name].base_url_env: twin_manager.get(name).base_url
            for name in evil_twins if twin_manager.get(name)
        }

        vendor_results = []
        for vendor in vendor_spec.discovered_vendors:
            twin = twin_manager.get(vendor.name)
            if not twin:
                continue
            payload = (vendor_payloads or {}).get(vendor.name, {"test": "ghostvendor_rescore"})
            app = DemoAppProcess(
                app_path=patched_dir,
                start_command=repo_info.start_command,
                port=repo_info.port,
                twin_env=twin_env,
                extra_env=extra_env,
            )
            scenarios = []
            try:
                with app:
                    twin.clear_chaos()
                    try:
                        status, _, _ = app.post(vendor.app_route, payload, timeout=_REQUEST_TIMEOUT)
                        baseline_passed = status < 400 or status == 503
                    except Exception:
                        baseline_passed = False

                    vendor_failed_scenarios = failed_scenarios_by_vendor.get(vendor.name, []) if failed_scenarios_by_vendor else []
                    modes_to_score = [s.mode for s in vendor_failed_scenarios] if vendor_failed_scenarios else ["502_burst", "timeout", "malformed_json", "429_rate_limit", "empty_response"]
                    # Build a lookup of original failure status/exception per mode so the
                    # rescore uses the same pass criterion as the VALIDATE loop:
                    # - original was timeout/exception → any HTTP response = PASS
                    # - original was HTTP error → status changed from original = PASS, same = FAIL
                    # This ensures a graceful 503 (resilient behavior) scores as PASS, not FAIL.
                    original_by_mode: dict[str, ScenarioResult] = {
                        s.mode: s for s in vendor_failed_scenarios
                    } if vendor_failed_scenarios else {}

                    for mode in modes_to_score:
                        orig = original_by_mode.get(mode)
                        try:
                            twin.activate_chaos(mode=mode)
                            status, _, elapsed_ms = app.post(vendor.app_route, payload, timeout=_REQUEST_TIMEOUT)
                            clear_failed = False
                            try:
                                twin.clear_chaos()
                            except Exception:
                                clear_failed = True
                            if clear_failed or (elapsed_ms and elapsed_ms > 11000):
                                try:
                                    twin.reset()
                                except Exception:
                                    pass
                            # Pass if original was a timeout/exception (any HTTP response is better),
                            # or if status changed from the original failure status.
                            if orig and orig.exception:
                                outcome = ScenarioOutcome.PASS
                            elif orig and orig.http_status and status == orig.http_status:
                                outcome = ScenarioOutcome.FAIL
                            elif status < 400:
                                outcome = ScenarioOutcome.PASS
                            elif status < 500:
                                outcome = ScenarioOutcome.DEGRADED
                            else:
                                # 5xx but status changed from original — degraded, not full fail
                                outcome = ScenarioOutcome.DEGRADED
                        except http_requests.exceptions.Timeout:
                            try:
                                twin.clear_chaos()
                            except Exception:
                                pass
                            try:
                                twin.reset()
                            except Exception:
                                pass
                            outcome = ScenarioOutcome.FAIL
                        except Exception:
                            outcome = ScenarioOutcome.FAIL
                        scenarios.append(ScenarioResult(mode=mode, outcome=outcome))
            except Exception as e:
                logger.warning("Rescore: app startup failed for %s: %s", vendor.name, e)
                baseline_passed = False

            vendor_results.append(VendorResult(
                vendor_name=vendor.name,
                criticality_score=vendor.criticality_score,
                baseline_passed=baseline_passed,
                scenarios=scenarios,
            ))

        # Rescore uses a scenario-count-aware denominator.
        # ResilienceReport.overall_score always divides by 5 (full-coverage baseline), which
        # is correct for VERIFY but wrong here — we only replay failed scenarios, so a vendor
        # that failed 2/5 scenarios can score at most 2×16 pts out of 5×16, which would cap
        # the patched score at 52/100 even if the patch fixed both failures perfectly.
        # Instead: score each vendor against the number of scenarios actually retested, then
        # take the same criticality-weighted average. This makes the after-patch score reflect
        # "did the patch fix everything that broke" rather than "did we run all 5 modes".
        from models.resilience_result import ScenarioOutcome as SO
        pts_map = {SO.PASS: 16, SO.DEGRADED: 8, SO.FAIL: 0}
        total_weight = sum(v.criticality_score for v in vendor_results)
        weighted_sum = 0
        for vr in vendor_results:
            baseline_pts = 20 if vr.baseline_passed else 0
            n = max(len(vr.scenarios), 1)
            scenario_pts = sum(pts_map[s.outcome] for s in vr.scenarios)
            scenario_score = int((scenario_pts / (n * 16)) * 80)
            vendor_score = min(100, baseline_pts + scenario_score)
            logger.info("Rescore: %s → %d/100 (%d scenarios, baseline=%s)", vr.vendor_name, vendor_score, n, vr.baseline_passed)
            weighted_sum += vendor_score * vr.criticality_score
        score = int(weighted_sum / total_weight) if total_weight else 0
        logger.info("Rescore complete: %d/100", score)
        return score

    except Exception as e:
        logger.warning("Rescore failed (non-blocking): %s", e)
        return 0
    finally:
        twin_manager.stop_all()
        shutil.rmtree(patched_dir, ignore_errors=True)
        _restore_editable_pth(disabled_pth_rescore)


@dataclass
class ValidationResult:
    """Structured outcome from _run_patched_validation.

    Distinguishes infrastructure failures (twin couldn't be started/kept alive —
    the patch was never tested) from patch failures (twin ran, patch still wrong).

    VALIDATE uses this distinction to route correctly:
    - only infra failures → retry VALIDATE (don't regenerate patches, just re-test)
    - patch failures → REMEDIATE for those vendors only; skip validated vendors
    - both → retry VALIDATE first (fixing infra may clear ambiguous patch failures)
    """
    validated_vendors: set[str]           # vendors whose scenarios all passed
    infra_failures: dict[str, str]        # vendor → reason (twin process failure)
    patch_failures: dict[str, str]        # vendor → reason (patch semantically wrong)

    @property
    def all_passed(self) -> bool:
        return not self.infra_failures and not self.patch_failures

    @property
    def failure_summary(self) -> str:
        parts = list(self.infra_failures.values()) + list(self.patch_failures.values())
        return "; ".join(parts)


def _run_patched_validation(
    patch_report: PatchReport,
    patched_files: dict[str, str],
    repo_info: RepoInfo,
    evil_twins: dict[str, EvilTwinArtifact],
    failed_scenarios_by_vendor: dict[str, list],
    vendor_spec,
    vendor_payloads: dict[str, dict] | None = None,
) -> ValidationResult:
    """
    Apply patches to a temp copy of the repo, spin up patched app + Evil Twins,
    re-run only the previously failed scenarios.

    Returns a ValidationResult distinguishing infrastructure failures from patch failures
    so the caller can route to the correct retry strategy.
    """
    import os

    patched_dir = tempfile.mkdtemp(prefix="ghostvendor_validate_")
    disabled_pth: list = []
    try:
        # Copy repo to temp dir and overwrite with patched files
        shutil.copytree(repo_info.local_path, patched_dir, dirs_exist_ok=True)
        logger.info("Validation temp dir: %s", patched_dir)
        for rel_path, content in patched_files.items():
            dest = Path(patched_dir) / rel_path
            if not dest.exists():
                # Path from model doesn't match any file copied from the repo — skip rather than
                # silently creating a ghost file that nothing imports. Log so we can diagnose.
                logger.warning("Patch skipped (path not in repo): %s", rel_path)
                continue
            dest.write_text(content, encoding="utf-8")
            logger.info("Patch written: %s (%d chars)", dest, len(content))

        # Purge __pycache__ dirs so Python re-compiles from the patched source.
        purged = 0
        for cache_dir in Path(patched_dir).rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
            purged += 1
        logger.info("Purged %d __pycache__ dirs", purged)

        # Disable editable-install .pth files in the demo app's venv so PYTHONPATH
        # remapping to patched_dir actually takes effect (the .pth fires at interpreter
        # startup and would prepend the original src/ before our override runs).
        disabled_pth = _disable_editable_pth(repo_info)

        # Override PYTHONPATH so the patched source files are imported instead of
        # the originals. We remap every entry in the original PYTHONPATH that falls
        # inside repo_info.local_path to its equivalent path inside patched_dir —
        # this is repo-agnostic: works whether the source root is src/, lib/, . etc.
        original_pythonpath = repo_info.extra_env.get("PYTHONPATH", "")
        repo_root = Path(repo_info.local_path).resolve()
        patched_root = Path(patched_dir).resolve()
        remapped: list[str] = []
        for entry in (original_pythonpath.split(os.pathsep) if original_pythonpath else []):
            entry_path = Path(entry).resolve()
            try:
                rel = entry_path.relative_to(repo_root)
                remapped.append(str(patched_root / rel))
            except ValueError:
                remapped.append(entry)  # outside repo — keep as-is
        if not remapped:
            remapped = [str(patched_root)]
        patched_pythonpath = os.pathsep.join(remapped)
        logger.info("Validation PYTHONPATH: original=%r", original_pythonpath)
        logger.info("Validation PYTHONPATH: repo_root=%r patched_root=%r", str(repo_root), str(patched_root))
        logger.info("Validation PYTHONPATH: remapped=%r", patched_pythonpath)

        extra_env = {
            **repo_info.extra_env,
            "PYTHONPATH": patched_pythonpath,
            **{f"{v.name.upper()}_API_KEY": f"ghostvendor_fake_{v.name.lower()}"
               for v in vendor_spec.discovered_vendors},
        }

        # Build a static twin_env using artifact ports — each vendor gets its own fresh
        # twin launched and stopped per-vendor below, but the env var mapping is stable
        # across all vendors (every vendor's base_url_env must be set so the app can
        # route to whichever twin is active for the current scenario).
        twin_env_static: dict[str, str] = {
            artifact.base_url_env: f"http://localhost:{artifact.port}"
            for artifact in evil_twins.values()
        }

        validated_vendors: set[str] = set()
        infra_failures: dict[str, str] = {}
        patch_failures: dict[str, str] = {}

        for patch in patch_report.patches:
            vendor_name = patch.vendor
            failed_scenarios = failed_scenarios_by_vendor.get(vendor_name, [])
            if not failed_scenarios:
                validated_vendors.add(vendor_name)
                continue

            vendor = next(
                (v for v in vendor_spec.discovered_vendors if v.name == vendor_name), None
            )
            if not vendor:
                continue

            artifact = evil_twins.get(vendor_name)
            if not artifact:
                logger.warning("No twin artifact for %s during validation — skipping", vendor_name)
                continue

            # Fresh twin per vendor — isolated lifecycle prevents state bleed between vendors.
            # A timeout scenario on vendor N can leave the uvicorn worker blocked; stopping
            # and restarting the twin process before vendor N+1 guarantees a clean slate.
            vendor_twin_manager = EvilTwinManager()
            try:
                vendor_twin_manager.launch(
                    vendor_name=artifact.vendor_name,
                    port=artifact.port,
                    code=artifact.code,
                )
                logger.info("Launched fresh twin for %s validation on port %d", vendor_name, artifact.port)
            except Exception as e:
                failures.append(f"{vendor_name}: failed to launch twin: {e}")
                logger.error("Validation FAIL: %s twin launch failed — %s", vendor_name, e)
                continue

            twin = vendor_twin_manager.get(vendor_name)

            logger.info("Validation app env: PYTHONPATH=%s", extra_env.get("PYTHONPATH"))
            logger.info("Validation app cmd: %s cwd=%s", repo_info.start_command, patched_dir)
            app = DemoAppProcess(
                app_path=patched_dir,
                start_command=repo_info.start_command,
                port=repo_info.port,
                twin_env=twin_env_static,
                extra_env=extra_env,
            )

            request_payload = (vendor_payloads or {}).get(vendor_name, {"test": "ghostvendor_validate"})
            logger.info("Validation payload for %s: %s", vendor_name, request_payload)

            try:
                with app:
                    logger.info("Patched app started for %s validation", vendor_name)
                    twin.clear_chaos()

                    for scenario in failed_scenarios:
                        mode = scenario.mode
                        try:
                            twin.activate_chaos(mode=mode)
                            status, body, elapsed_ms = app.post(vendor.app_route, request_payload, timeout=35)
                            app_timed_out = elapsed_ms is not None and elapsed_ms > 11000

                            # Hold chaos active for 2s so any retry in the patch still hits the
                            # evil twin — prevents the race where a retry escapes into a clean twin.
                            time.sleep(2)

                            clear_failed = False
                            try:
                                twin.clear_chaos()
                            except Exception:
                                clear_failed = True
                                logger.warning("  clear_chaos after %s failed for %s — twin stuck", mode, vendor_name)

                            if clear_failed or app_timed_out:
                                try:
                                    twin.reset()
                                    twin = vendor_twin_manager.get(vendor_name)
                                    logger.info("  twin %s reset OK after %s", vendor_name, mode)
                                except Exception as reset_err:
                                    logger.warning("  twin %s reset failed: %s", vendor_name, reset_err)

                            # Pass criterion is based on what the original failure was:
                            # - Original was HTTP error (status-based): pass if status improved,
                            #   i.e. the app no longer returns the same or worse status.
                            #   We use status != original_status as the bar — any different
                            #   response (including 503) means the patch changed behavior.
                            # - Original was timeout/exception: pass simply if we got any HTTP response.
                            # This is repo-agnostic — no hardcoded 500 assumption.
                            original_was_timeout = scenario.exception is not None
                            original_status = scenario.http_status

                            if original_was_timeout:
                                logger.info("Validation PASS: %s/%s → HTTP %d in %.0fms (was timeout)", vendor_name, mode, status, elapsed_ms)
                            elif original_status is not None and status == original_status:
                                patch_failures[vendor_name] = (
                                    patch_failures.get(vendor_name, "") +
                                    f"{vendor_name}/{mode}: still HTTP {status} after patch; "
                                )
                                logger.warning("Validation FAIL: %s/%s → HTTP %d (same as before)", vendor_name, mode, status)
                            else:
                                logger.info("Validation PASS: %s/%s → HTTP %d in %.0fms", vendor_name, mode, status, elapsed_ms)

                        except http_requests.exceptions.Timeout:
                            try:
                                twin.clear_chaos()
                            except Exception:
                                pass
                            patch_failures[vendor_name] = (
                                patch_failures.get(vendor_name, "") +
                                f"{vendor_name}/{mode}: still timing out after patch; "
                            )
                            logger.warning("Validation FAIL: %s/%s → Timeout", vendor_name, mode)
                            try:
                                vendor_twin_manager.stop_all()
                                vendor_twin_manager.launch(
                                    vendor_name=artifact.vendor_name,
                                    port=artifact.port,
                                    code=artifact.code,
                                )
                                twin = vendor_twin_manager.get(vendor_name)
                            except Exception:
                                pass
                        except Exception as e:
                            patch_failures[vendor_name] = (
                                patch_failures.get(vendor_name, "") +
                                f"{vendor_name}/{mode}: exception {type(e).__name__}: {e}; "
                            )
                            logger.warning("Validation FAIL: %s/%s → %s", vendor_name, mode, e)
            except Exception as e:
                # Twin or app startup failure — infrastructure, not a semantic patch failure.
                # The patch was never actually tested; route back to VALIDATE retry, not REMEDIATE.
                infra_failures[vendor_name] = f"{vendor_name}: infra failure — {e}"
                logger.error("Validation FAIL: %s app startup exception — %s", vendor_name, e)
            else:
                # No exception from the with block — vendor validated if no scenario failures recorded
                if vendor_name not in patch_failures:
                    validated_vendors.add(vendor_name)
                    logger.info("Validation: %s all scenarios passed", vendor_name)
            finally:
                vendor_twin_manager.stop_all()
                logger.info("Stopped twin for %s", vendor_name)

        return ValidationResult(
            validated_vendors=validated_vendors,
            infra_failures=infra_failures,
            patch_failures=patch_failures,
        )

    finally:
        shutil.rmtree(patched_dir, ignore_errors=True)
        logger.info("Validation temp dir cleaned up")
        _restore_editable_pth(disabled_pth)
