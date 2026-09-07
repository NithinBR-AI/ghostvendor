import logging
from enum import Enum, auto
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from agents import detective, twin_generator, context_guard, resilience_verifier
from agents.twin_generator import EvilTwinArtifact
from models.guard_decision import GuardDecision
from models.resilience_result import ResilienceReport
from models.vendor_spec import VendorSpec
from tools.evil_twin_runner import EvilTwinManager
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
        # Agent 5 (Runtime Debugger) — Phase 5
        self.transition(State.REMEDIATE)

    def _remediate(self) -> None:
        # Agent 6 (Patch Generator) — Phase 5
        self.transition(State.VALIDATE)

    def _validate(self) -> None:
        # Agent 6: Contree sandbox validation + PR — Phase 5
        if self.retry_count < self.MAX_RETRIES:
            self.transition(State.DONE)
        else:
            self.fail(f"Exceeded {self.MAX_RETRIES} remediation attempts")
