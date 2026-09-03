from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any

from agents import detective
from models.vendor_spec import VendorSpec


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
    evil_twin_code: str = ""
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

    def transition(self, next_state: State):
        print(f"[ghostvendor] {self.state.name} → {next_state.name}")
        self.state = next_state

    def fail(self, reason: str):
        print(f"[ghostvendor] FAILED: {reason}")
        self.transition(State.FAILED)

    def run(self):
        print(f"[ghostvendor] Starting on repo: {self.artifacts.repo}")
        while self.state not in (State.DONE, State.FAILED):
            self._step()

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
        # Agent 3 (Context Guard gate) + Agent 4 (Verifier) + CI execution
        self.transition(State.VERIFY)

    def _verify(self):
        # Read CI results — did the app fail as expected?
        self.transition(State.DIAGNOSE)

    def _diagnose(self):
        # Agent 5 (Runtime Debugger)
        self.transition(State.REMEDIATE)

    def _remediate(self):
        # Agent 6 (Patch Generator)
        self.transition(State.VALIDATE)

    def _validate(self):
        # Rerun CI — did the patch fix it?
        if self.retry_count < self.MAX_RETRIES:
            # placeholder: assume pass for now
            self.transition(State.DONE)
        else:
            self.fail(f"Exceeded {self.MAX_RETRIES} remediation attempts")
