from enum import Enum, auto
from dataclasses import dataclass, field

from agents import detective, twin_generator
from agents.twin_generator import EvilTwinArtifact
from models.vendor_spec import VendorSpec
from tools.evil_twin_runner import EvilTwinManager


class State(Enum):
    DISCOVER = auto()
    ATTACK = auto()
    VERIFY = auto()
    DIAGNOSE = auto()
    REMEDIATE = auto()
    VALIDATE = auto()
    DONE = auto()
    FAILED = auto()


@dataclass
class ArtifactStore:
    repo: str
    vendor_spec: VendorSpec | None = None
    evil_twins: dict[str, EvilTwinArtifact] = field(default_factory=dict)
    security_decision: dict = field(default_factory=dict)
    resilience_tests: dict = field(default_factory=dict)
    ci_logs: str = ""
    root_cause: dict = field(default_factory=dict)
    patch: dict = field(default_factory=dict)
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

    def transition(self, next_state: State):
        print(f"[ghostvendor] {self.state.name} → {next_state.name}")
        self.state = next_state

    def fail(self, reason: str):
        print(f"[ghostvendor] FAILED: {reason}")
        self.twin_manager.stop_all()
        self.transition(State.FAILED)

    def run(self):
        print(f"[ghostvendor] Starting on repo: {self.artifacts.repo}")
        try:
            while self.state not in (State.DONE, State.FAILED):
                self._step()
        finally:
            self.twin_manager.stop_all()

    def _step(self):
        if self.state == State.DISCOVER:
            self._discover()
        elif self.state == State.ATTACK:
            self._attack()
        elif self.state == State.VERIFY:
            self._verify()
        elif self.state == State.DIAGNOSE:
            self._diagnose()
        elif self.state == State.REMEDIATE:
            self._remediate()
        elif self.state == State.VALIDATE:
            self._validate()

    def _discover(self):
        try:
            spec = detective.run(repo=self.artifacts.repo)
            self.artifacts.vendor_spec = spec
            vendors = [f"{v.name} (score={v.criticality_score})" for v in spec.vendors_by_criticality()]
            print(f"[ghostvendor] Discovered vendors: {', '.join(vendors)}")
            self.transition(State.ATTACK)
        except Exception as e:
            self.fail(f"Agent 1 (Detective) failed: {e}")

    def _attack(self):
        try:
            twins = twin_generator.run(self.artifacts.vendor_spec)
            self.artifacts.evil_twins = twins
            for name, artifact in twins.items():
                print(f"[ghostvendor] Evil Twin generated: {name} → port {artifact.port}")
            self.transition(State.VERIFY)
        except Exception as e:
            self.fail(f"Agent 2 (Twin Generator) failed: {e}")

    def _verify(self):
        # Agent 3 (Context Guard) + Agent 4 (Resilience Verifier) + CI dispatch — Phase 4
        self.transition(State.DIAGNOSE)

    def _diagnose(self):
        # Agent 5 (Runtime Debugger) — Phase 5
        self.transition(State.REMEDIATE)

    def _remediate(self):
        # Agent 6 (Patch Generator) — Phase 5
        self.transition(State.VALIDATE)

    def _validate(self):
        # Re-run CI — did the patch survive chaos? — Phase 5
        if self.retry_count < self.MAX_RETRIES:
            self.transition(State.DONE)
        else:
            self.fail(f"Exceeded {self.MAX_RETRIES} remediation attempts")
