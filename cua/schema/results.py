"""The replay result contract — what a calling agent gets back.

Exactly one of four statuses. The caller branches on ``status`` first:

* ``success``           — checkpoint verified; ``outputs`` populated.
* ``business_outcome``  — the app gave a legitimate answer that is not the happy
                          path (``NOT_FOUND``, ``ACCESS_DENIED``, ``VALIDATION_ERROR``).
                          Not an error: don't retry, tell the user.
* ``blocked``           — we refused before acting (policy, approval, bad input).
                          Nothing was touched.
* ``failed``            — hard failure or exhausted recovery. ``failure`` says
                          which step, what was expected, what was observed, and
                          whether the app state is ``in_doubt`` (an irreversible
                          step may or may not have happened — never auto-retry).

Recoverable conditions never surface as a status: they appear in
``recoveries`` so operators can see how often a capability needed help.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ResultStatus(str, Enum):
    success = "success"
    business_outcome = "business_outcome"
    blocked = "blocked"
    failed = "failed"


class FailureKind(str, Enum):
    hard = "hard"                                    # known-bad state, e.g. APP_ERROR
    unexpected_state = "unexpected_state"            # nothing we know matches
    target_not_found = "target_not_found"            # every strategy failed
    recovery_exhausted = "recovery_exhausted"        # recoverable, but kept recurring
    checkpoint_failed = "checkpoint_failed"          # all steps ran, success not proven
    in_doubt = "in_doubt"                            # failed after an irreversible step
    escalation_unresolved = "escalation_unresolved"  # human aborted or timed out
    handback_unverified = "handback_unverified"      # human handed back, but no safe checkpoint could be proven
    invariant_violated = "invariant_violated"        # screen/record does not match the request (wrong record, ...)
    requires_human = "requires_human"                # a known blocking state only a person may clear
    control_lost = "control_lost"                    # lease revoked mid-run


class BusinessOutcome(BaseModel):
    code: str
    message: str | None = None
    step_id: str | None = None


class FailureDetail(BaseModel):
    kind: FailureKind
    code: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    detector: str | None = None
    evidence: list[str] = Field(default_factory=list, description="Paths relative to the run directory.")
    hint: str | None = None


class BlockedDetail(BaseModel):
    # POLICY_DENIED | APPROVAL_REQUIRED | INVALID_INPUT | VERSION_UNSUPPORTED | IRREVERSIBLE_NOT_AUTHORIZED |
    # APPROVAL_DENIED | APPROVAL_STATE_CHANGED | CONTRACT_VIOLATION | UNATTENDED_NOT_PERMITTED
    code: str
    message: str
    step_id: str | None = None


class RecoveryEvent(BaseModel):
    step_id: str
    detector: str
    action: str
    attempt: int
    ok: bool


class DriftNote(BaseModel):
    step_id: str
    matched_by: str          # strategy kind that resolved the target
    strategy_index: int      # 0 == primary
    recorded: dict[str, Any]
    note: str


class StepTrace(BaseModel):
    step_id: str
    action: str
    status: str              # ok | skipped_human | failed
    matched_by: str | None = None
    ms: int = 0


class ReplayResult(BaseModel):
    run_id: str
    capability_id: str
    capability_version: str
    tenant: str
    status: ResultStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    failure: FailureDetail | None = None
    blocked: BlockedDetail | None = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drift: list[DriftNote] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime
    duration_ms: int = 0
    run_dir: str | None = None
