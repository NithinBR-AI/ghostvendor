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

    def __init__(self, repo: str):
        self.state = State.DISCOVER
        self.artifacts = ArtifactStore(repo=repo)
        self.retry_count = 0
        self.twin_manager = EvilTwinManager()

    def transition(self, next_state: State) -> None:
        logger.info("%s → %s", self.state.name, next_state.name)
        self.state = next_state

    def fail(self, reason: str) -> None:
        logger.error("FAILED: %s", reason)
        self.twin_manager.stop_all()
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
            report = resilience_verifier.run(
                spec=self.artifacts.vendor_spec,
                evil_twins=self.artifacts.evil_twins,
                twin_manager=self.twin_manager,
                repo_info=self.artifacts.repo_info,
                source_files=self.artifacts.source_files,
            )
            self.artifacts.resilience_report = report
            self.artifacts.resilience_score_before = report.overall_score
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

    def _validate(self) -> None:
        patch_report = self.artifacts.patch_report
        repo_info = self.artifacts.repo_info

        # Build patched file contents — apply each diff to the original source
        patched_files: dict[str, str] = {}
        for patch in patch_report.patches:
            original = self.artifacts.source_files.get(patch.affected_file, "")
            patched_files[patch.affected_file] = _apply_patch_to_source(original, patch.patch_diff)

        # Local re-run: apply patches to a temp copy of the repo, run failed scenarios
        failed_scenarios_by_vendor = {
            vr.vendor_name: vr.failed_scenarios
            for vr in self.artifacts.resilience_report.vendor_results
            if vr.failed_scenarios
        }

        validation_failure = _run_patched_validation(
            patch_report=patch_report,
            patched_files=patched_files,
            repo_info=repo_info,
            evil_twins=self.artifacts.evil_twins,
            failed_scenarios_by_vendor=failed_scenarios_by_vendor,
            vendor_spec=self.artifacts.vendor_spec,
        )

        if validation_failure:
            self.retry_count += 1
            self.artifacts.extra["validation_failure"] = validation_failure
            if self.retry_count >= self.MAX_RETRIES:
                self.fail(f"Exceeded {self.MAX_RETRIES} remediation attempts. Last failure: {validation_failure}")
            else:
                logger.warning(
                    "Validation failed (attempt %d/%d): %s — retrying REMEDIATE",
                    self.retry_count, self.MAX_RETRIES, validation_failure,
                )
                self.transition(State.REMEDIATE)
            return

        # All scenarios passed — mark patches validated, open PR
        for patch in patch_report.patches:
            patch.sandbox_validated = True

        self.artifacts.extra.pop("validation_failure", None)

        try:
            repo = self.artifacts.repo
            branch = f"ghostvendor/fix-{self.retry_count + 1}"
            github_client.create_branch(repo, branch)
            logger.info("Created branch: %s", branch)

            for patch in patch_report.patches:
                github_client.commit_file(
                    repo=repo,
                    branch=branch,
                    path=patch.affected_file,
                    content=patched_files[patch.affected_file],
                    message=f"fix({patch.vendor.lower()}): {patch.pr_title}",
                )
                logger.info("Committed patch for %s → %s", patch.vendor, patch.affected_file)

            all_bodies = "\n\n---\n\n".join(p.pr_description for p in patch_report.patches)
            pr_title = (
                patch_report.patches[0].pr_title
                if len(patch_report.patches) == 1
                else f"fix: resilience patches for {', '.join(p.vendor for p in patch_report.patches)}"
            )
            pr_url = github_client.create_pr(repo=repo, branch=branch, title=pr_title, body=all_bodies)
            patch_report.pr_url = pr_url
            self.artifacts.pr_url = pr_url
            logger.info("PR opened: %s", pr_url)
            self.transition(State.DONE)

        except Exception as e:
            self.fail(f"GitHub PR creation failed: {e}")


def _apply_patch_to_source(original: str, diff: str) -> str:
    """Apply a unified diff to source text. Returns patched file content."""
    import re
    lines = original.splitlines(keepends=True)
    result = list(lines)
    offset = 0
    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    current_start = None
    current_lines = []

    def _flush(start, hunk_lines):
        nonlocal offset
        if start is None:
            return
        idx = start - 1 + offset
        removed = [l for l in hunk_lines if l.startswith("-")]
        added = [l[1:] for l in hunk_lines if l.startswith("+")]
        result[idx:idx + len(removed)] = added
        offset += len(added) - len(removed)

    for line in diff.splitlines():
        m = hunk_header.match(line)
        if m:
            _flush(current_start, current_lines)
            current_start = int(m.group(1))
            current_lines = []
        elif current_start is not None and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current_lines.append(line)

    _flush(current_start, current_lines)
    return "".join(result)


def _run_patched_validation(
    patch_report: PatchReport,
    patched_files: dict[str, str],
    repo_info: RepoInfo,
    evil_twins: dict[str, EvilTwinArtifact],
    failed_scenarios_by_vendor: dict[str, list],
    vendor_spec,
) -> str | None:
    """
    Apply patches to a temp copy of the repo, spin up patched app + Evil Twins,
    re-run only the previously failed scenarios.

    Returns None if all previously-failed scenarios now pass.
    Returns a failure description string if any still fail (fed back to REMEDIATE).
    """
    import os

    patched_dir = tempfile.mkdtemp(prefix="ghostvendor_validate_")
    twin_manager = EvilTwinManager()

    try:
        # Copy repo to temp dir and overwrite with patched files
        shutil.copytree(repo_info.local_path, patched_dir, dirs_exist_ok=True)
        for rel_path, content in patched_files.items():
            dest = Path(patched_dir) / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            logger.info("Applied patch to temp dir: %s", rel_path)

        # Start Evil Twins
        twin_env: dict[str, str] = {}
        for artifact in evil_twins.values():
            twin_manager.launch(
                vendor_name=artifact.vendor_name,
                port=artifact.port,
                code=artifact.code,
            )
            twin_env[artifact.base_url_env] = artifact.local_base_url
            logger.info("Launched twin for validation: %s on port %d", artifact.vendor_name, artifact.port)

        # Fake credentials
        extra_env = {
            **repo_info.extra_env,
            **{f"{v.name.upper()}_API_KEY": f"ghostvendor_fake_{v.name.lower()}"
               for v in vendor_spec.discovered_vendors},
        }

        failures: list[str] = []

        for patch in patch_report.patches:
            vendor_name = patch.vendor
            failed_scenarios = failed_scenarios_by_vendor.get(vendor_name, [])
            if not failed_scenarios:
                continue

            vendor = next(
                (v for v in vendor_spec.discovered_vendors if v.name == vendor_name), None
            )
            if not vendor:
                continue

            twin = twin_manager.get(vendor_name)
            if not twin:
                logger.warning("No twin for %s during validation — skipping", vendor_name)
                continue

            app = DemoAppProcess(
                app_path=patched_dir,
                start_command=repo_info.start_command,
                port=repo_info.port,
                twin_env=twin_env,
                extra_env=extra_env,
            )

            with app:
                logger.info("Patched app started for %s validation", vendor_name)
                twin.clear_chaos()

                for scenario in failed_scenarios:
                    mode = scenario.mode
                    try:
                        twin.activate_chaos(mode=mode)
                        status, body, elapsed_ms = app.post(vendor.app_route, {"test": "ghostvendor_validate"})
                        twin.clear_chaos()

                        # Restart twin if it got stuck (same logic as Agent 4)
                        if elapsed_ms and elapsed_ms > 11000:
                            try:
                                twin.stop()
                                twin.start()
                            except Exception:
                                pass

                        # Pass = app responded without hanging and status < 500
                        if status >= 500:
                            failures.append(
                                f"{vendor_name}/{mode}: still returning HTTP {status} after patch"
                            )
                            logger.warning("Validation FAIL: %s/%s → HTTP %d", vendor_name, mode, status)
                        else:
                            logger.info("Validation PASS: %s/%s → HTTP %d in %.0fms", vendor_name, mode, status, elapsed_ms)

                    except http_requests.exceptions.Timeout:
                        twin.clear_chaos()
                        failures.append(f"{vendor_name}/{mode}: still timing out after patch")
                        logger.warning("Validation FAIL: %s/%s → Timeout", vendor_name, mode)
                        try:
                            twin.stop()
                            twin.start()
                        except Exception:
                            pass
                    except Exception as e:
                        failures.append(f"{vendor_name}/{mode}: exception {type(e).__name__}: {e}")
                        logger.warning("Validation FAIL: %s/%s → %s", vendor_name, mode, e)

        if failures:
            return "; ".join(failures)
        return None

    finally:
        twin_manager.stop_all()
        shutil.rmtree(patched_dir, ignore_errors=True)
        logger.info("Validation temp dir cleaned up")
