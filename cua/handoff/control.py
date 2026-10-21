"""Control-transfer model for a live session.

One lease per live session, modelled as a small state machine:

    AUTOMATION ─escalate─▶ PAUSING ─(agent actions drained)─▶ AWAITING_OPERATOR
                                                                    │ claim(operator, epoch)
                                                                    ▼
        ┌──────────── rejected resume (blocking condition still present) ── HUMAN (owned)
        │                                                                   │ resolve(..., lease, epoch)
        ▼                                                                   ▼
      HUMAN ◀──────────────────────────────────────────────────────── RESUMING
                                                                   │  (human actions drained,
       AUTOMATION ◀── verify() proves a safe checkpoint ───────────┤   verify() runs)
       TERMINATED ◀── abort / timeout / unprovable / irreversible ─┘

Rules this enforces:

* **Exclusive actor.** A browser action runs inside ``acting(actor)``; it is
  admitted only in AUTOMATION (agent) or HUMAN (human). Admission and the
  in-flight count update happen without an ``await`` in between, so on the
  single-threaded event loop they are atomic w.r.t. every other coroutine.
* **In-flight barrier.** A transition first changes state (so no new actions
  are admitted), then drains the previous actor's in-flight actions. An
  action that outlives the drain timeout is recorded, and the hand-back then
  fails closed.
* **Explicit ownership.** Nobody holds the human lease until an operator from
  the roster *claims* the intervention. The claim returns a lease token; every
  operator action and the resolution must present the owner's name, that
  lease token and the current epoch. Double claims, other operators, stale
  epochs and resolving before claiming are refused (``StaleRequest`` /
  ``NotAuthorized``).
* **Verified hand-back, decided before control moves.** ``resolve`` drains the
  operator's actions and runs the caller's ``verify`` *inside the resolution*.
  ``ok`` → AUTOMATION. ``recoverable`` (e.g. the supervisor hold is still on
  screen) → the operator keeps control and is told why. Anything else, abort,
  timeout or an action that outlived the drain → TERMINATED.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from pydantic import BaseModel, Field

from ..surface.base import Actor


class ControlLost(RuntimeError):
    """An actor tried to act without holding the lease (or after termination)."""


class StaleRequest(RuntimeError):
    """The request refers to an intervention/epoch/state that is no longer current (HTTP 409)."""


class NotAuthorized(RuntimeError):
    """Wrong operator, not on the roster, or a lease token that does not match (HTTP 403)."""


class ControlState(str, Enum):
    automation = "automation"
    pausing = "pausing"
    awaiting_operator = "awaiting_operator"
    human = "human"
    resuming = "resuming"
    terminated = "terminated"


class InterventionKind(str, Enum):
    stuck = "stuck"          # discovery can't make progress / model asked for help
    failure = "failure"      # replay hit a state it can't recover from
    approval = "approval"    # an irreversible step needs a human decision


class Decision(str, Enum):
    resume = "resume"        # I fixed it; continue from current state
    approve = "approve"      # go ahead with the risky step
    deny = "deny"            # don't do the risky step
    abort = "abort"          # stop the run


@dataclass
class HandbackVerdict:
    """Result of the caller's post-handoff verification.

    ``recoverable`` only matters when ``ok`` is False: True keeps the operator in
    control (nothing is unsafe, the blocking condition just isn't cleared yet);
    False terminates the session (state unprovable, or an irreversible step is
    involved)."""
    ok: bool
    checkpoint: str
    detail: str = ""
    data: Any = None
    recoverable: bool = False


class Resolution(BaseModel):
    decision: Decision
    note: str = ""
    by: str = "operator"
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    human_actions: list[dict[str, Any]] = Field(default_factory=list)
    timed_out: bool = False
    resumed: bool = False            # did automation get control back?
    checkpoint: str | None = None    # verified checkpoint it resumed from
    verification_detail: str = ""


class RejectedResume(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    by: str
    decision: Decision
    checkpoint: str
    reason: str


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    session_id: str
    epoch: int                       # epoch at which the intervention awaits a claim
    kind: InterventionKind
    subject: str                     # capability id@version or discovery goal
    step_id: str | None = None
    step_intent: str | None = None
    code: str
    reason: str
    screenshot: str | None = None    # path relative to run dir (masked)
    frames: dict[str, str] = Field(default_factory=dict)
    allowed_decisions: list[Decision]
    in_flight_unaccounted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "awaiting_operator"   # awaiting_operator | claimed | resolving | resolved
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    rejected_resumes: list[RejectedResume] = Field(default_factory=list)
    unclaimed_activity: int = 0      # browser events seen while nobody owned the session
    resolution: Resolution | None = None


REGISTRY: dict[str, "SessionControl"] = {}

# The operator "pager": where intervention notices (with the session's console link
# and token) are delivered. Here: stderr + an in-process list that the scripted
# operator and the console e2e read. In production: a ticket/queue/on-call page.
NOTICES: list[dict[str, Any]] = []
CONSOLE_URL: str | None = None

Verify = Callable[[Resolution], Awaitable[HandbackVerdict]]


class SessionControl:
    def __init__(self, session_id: str, run_dir: Path, log: Any, *, operators: Iterable[str] = (),
                 drain_timeout_s: float = 15) -> None:
        self.session_id = session_id
        self.run_dir = run_dir
        self.log = log
        self.operators = frozenset(operators)       # empty roster = nobody can claim (fail closed)
        self.drain_timeout_s = drain_timeout_s
        self.console_token = secrets.token_urlsafe(24)   # session-scoped console credential
        log.redactor.register(self.console_token, "secret")
        self.state = ControlState.automation
        self.epoch = 0
        self.current: InterventionRequest | None = None
        self.history: list[InterventionRequest] = []
        self._inflight: dict[Actor, int] = {Actor.agent: 0, Actor.human: 0}
        self._changed = asyncio.Event()      # pulsed whenever an action finishes
        self._finished: asyncio.Event | None = None
        self._verify: Verify | None = None
        self._lease: str | None = None
        self._actions: list[dict[str, Any]] = []
        self._resolve_lock = asyncio.Lock()
        self.surface: Any = None             # set by the session, used by the operator console
        self.on_human_step: Any = None       # discovery: record operator steps into the artifact
        self.suppress_browser_until = 0.0    # dedupe console actions vs. the in-page listener
        (run_dir / "interventions").mkdir(parents=True, exist_ok=True)
        REGISTRY[session_id] = self

    # ------------------------------------------------------------------ lease

    @property
    def holder(self) -> Actor | None:
        return {ControlState.automation: Actor.agent, ControlState.human: Actor.human}.get(self.state)

    @property
    def owner(self) -> str | None:
        return self.current.claimed_by if self.current and self.state == ControlState.human else None

    def require(self, actor: Actor) -> None:
        if self.holder != actor:
            raise ControlLost(f"{actor.value} may not act: session is {self.state.value} (epoch {self.epoch})")

    @contextlib.asynccontextmanager
    async def acting(self, actor: Actor) -> AsyncIterator[int]:
        """Admit one browser action for ``actor``; yields the epoch it runs under."""
        self.require(actor)                  # no await between check and increment: atomic
        self._inflight[actor] += 1
        epoch = self.epoch
        try:
            yield epoch
        finally:
            self._inflight[actor] -= 1
            self._changed.set()

    def in_flight(self, actor: Actor) -> int:
        return self._inflight[actor]

    async def _drain(self, actor: Actor) -> bool:
        deadline = time.monotonic() + self.drain_timeout_s
        while self._inflight[actor] > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True

    def _set(self, state: ControlState, why: str, *, bump: bool = False) -> None:
        if bump:
            self.epoch += 1
        self.log.event("control_state", state=state.value, prev=self.state.value, epoch=self.epoch, reason=why)
        self.state = state

    def terminate(self, why: str) -> None:
        if self.state != ControlState.terminated:
            self._lease = None
            self._set(ControlState.terminated, why, bump=True)

    # ------------------------------------------------------------------ agent side

    async def escalate(self, *, run_id: str, kind: InterventionKind, subject: str, code: str, reason: str,
                       step_id: str | None = None, step_intent: str | None = None,
                       screenshot: str | None = None, frames: dict[str, str] | None = None,
                       allowed: list[Decision] | None = None, timeout_s: float = 900,
                       verify: Verify | None = None) -> Resolution:
        """Hand the live session to an operator; return once the intervention is *finished*:
        automation verified back in control (``resumed``), or the session terminated."""
        if self.state != ControlState.automation:
            raise ControlLost(f"cannot escalate from state {self.state.value}")
        self._set(ControlState.pausing, f"escalation requested ({code})")
        drained = await self._drain(Actor.agent)
        if not drained:
            self.log.event("in_flight_unaccounted", actor="agent", count=self._inflight[Actor.agent])
        self._set(ControlState.awaiting_operator, f"intervention ({code}) awaiting an operator claim", bump=True)
        req = InterventionRequest(
            id=f"int-{secrets.token_hex(4)}", run_id=run_id, session_id=self.session_id, epoch=self.epoch,
            kind=kind, subject=subject, step_id=step_id, step_intent=step_intent, code=code, reason=reason,
            screenshot=screenshot, frames=frames or {}, in_flight_unaccounted=not drained,
            allowed_decisions=allowed or ([Decision.approve, Decision.deny, Decision.abort]
                                          if kind == InterventionKind.approval
                                          else [Decision.resume, Decision.abort]))
        self.current, self._actions, self._finished = req, [], asyncio.Event()
        self._verify = verify
        self._persist(req)
        self.log.event("intervention_requested", id=req.id, kind=kind.value, code=code, reason=reason,
                       step_id=step_id, epoch=self.epoch)
        self._notify(req)
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            async with self._resolve_lock:          # never race an in-progress resolution
                if not self._finished.is_set():
                    self._set(ControlState.resuming, "operator timeout")
                    drained_h = await self._drain(Actor.human)
                    res = Resolution(decision=Decision.abort, note="no operator response", timed_out=True,
                                     by=req.claimed_by or "nobody",
                                     verification_detail="timed out" + ("" if drained_h else
                                                                       "; operator action still in flight"))
                    self.terminate(f"{req.id}: timed out")
                    self._finish(req, res)
        assert req.resolution is not None
        return req.resolution

    def _notify(self, req: InterventionRequest) -> None:
        link = f"{CONSOLE_URL}/i/{req.id}?token={self.console_token}" if CONSOLE_URL else None
        NOTICES.append({"session_id": self.session_id, "intervention": req.id, "code": req.code,
                        "epoch": req.epoch, "token": self.console_token, "link": link})
        if link:
            print(f"  >> operator needed ({req.code}): {link}", file=sys.stderr)

    def _finish(self, req: InterventionRequest, res: Resolution) -> None:
        res.human_actions = list(self._actions)
        req.resolution, req.status = res, "resolved"
        self._persist(req)
        self.history.append(req)
        self.current, self._verify = None, None
        self.log.event("intervention_resolved", id=req.id, decision=res.decision.value, by=res.by, note=res.note,
                       human_actions=len(res.human_actions), timed_out=res.timed_out, resumed=res.resumed,
                       checkpoint=res.checkpoint, detail=res.verification_detail)
        assert self._finished is not None
        self._finished.set()

    # ------------------------------------------------------------------ operator side

    def _open(self, intervention_id: str, epoch: int | None) -> InterventionRequest:
        if epoch is None:
            raise StaleRequest("epoch is required")
        if self.current is None or self.current.id != intervention_id:
            raise StaleRequest(f"no open intervention {intervention_id} on session {self.session_id}")
        if epoch != self.epoch:
            raise StaleRequest(f"stale epoch {epoch} (current {self.epoch})")
        return self.current

    def claim(self, intervention_id: str, operator: str, epoch: int | None) -> tuple[str, int]:
        """Take ownership. Returns (lease token, new epoch) for every later operator request."""
        req = self._open(intervention_id, epoch)
        if operator not in self.operators:
            raise NotAuthorized(f"{operator!r} is not on this session's operator roster")
        if self.state != ControlState.awaiting_operator:
            raise StaleRequest(f"intervention {intervention_id} is {self.state.value}"
                               + (f", owned by {req.claimed_by}" if req.claimed_by else ""))
        self._lease = secrets.token_urlsafe(24)
        self.log.redactor.register(self._lease, "secret")
        req.claimed_by, req.claimed_at, req.status = operator, datetime.now(timezone.utc), "claimed"
        self._set(ControlState.human, f"claimed by {operator}", bump=True)
        self._persist(req)
        self.log.event("intervention_claimed", id=req.id, by=operator, epoch=self.epoch)
        return self._lease, self.epoch

    def check_operator(self, intervention_id: str, operator: str, epoch: int | None,
                       lease: str | None) -> InterventionRequest:
        """Gate for every operator action after the claim."""
        req = self._open(intervention_id, epoch)
        if self.state != ControlState.human or req.claimed_by is None:
            raise StaleRequest(f"intervention {intervention_id} is {self.state.value}; claim it first")
        if operator != req.claimed_by:
            raise NotAuthorized(f"intervention {intervention_id} is owned by {req.claimed_by}, not {operator}")
        if not lease or self._lease is None or not secrets.compare_digest(lease, self._lease):
            raise NotAuthorized("lease token does not match the claim")
        return req

    def record_human_action(self, action: dict[str, Any]) -> None:
        if self.state == ControlState.awaiting_operator and self.current is not None \
                and action.get("source") == "browser":
            self.current.unclaimed_activity += 1      # someone touched the headed browser without a claim
            self.log.event("unclaimed_browser_activity", **action)
            return
        if self.state != ControlState.human:
            return
        # A console action also fires the in-page listener; keep the grounded console record only.
        if action.get("source") == "browser" and time.monotonic() < self.suppress_browser_until:
            return
        action = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "epoch": self.epoch,
                  "by": self.current.claimed_by if self.current else None, **action}
        self._actions.append(action)
        self.log.event("human_action", **action)

    async def resolve(self, intervention_id: str, decision: Decision, *, operator: str, epoch: int | None,
                      lease: str | None, note: str = "") -> dict[str, Any]:
        """The owner hands back. Verification runs *here*, before control moves:
        accepted → automation; recoverable rejection → owner keeps control; else terminated."""
        async with self._resolve_lock:
            req = self.check_operator(intervention_id, operator, epoch, lease)
            if decision not in req.allowed_decisions:
                raise ValueError(f"{decision.value} not allowed for a {req.kind.value} intervention")
            req.status = "resolving"
            self._set(ControlState.resuming, f"operator {operator}: {decision.value}")   # no new human actions
            res = Resolution(decision=decision, note=note, by=operator)
            if not await self._drain(Actor.human):
                res.verification_detail = "operator action still in flight; state unproven"
                self.terminate(f"{req.id}: {res.verification_detail}")
                self._finish(req, res)
                return self._outcome(res, accepted=False)
            if decision == Decision.abort:
                res.verification_detail = "aborted by operator"
                self.terminate(f"{req.id}: aborted")
                self._finish(req, res)
                return self._outcome(res, accepted=True)
            res.human_actions = list(self._actions)
            verdict = await self._verify(res) if self._verify else HandbackVerdict(True, "unverified-by-caller")
            res.checkpoint, res.verification_detail = verdict.checkpoint, verdict.detail
            self.log.event("handback_verification", id=req.id, decision=decision.value, ok=verdict.ok,
                           recoverable=verdict.recoverable, checkpoint=verdict.checkpoint, detail=verdict.detail)
            if verdict.ok:
                res.resumed = True
                self._lease = None
                self._set(ControlState.automation, f"verified hand-back from {req.id} at {verdict.checkpoint}",
                          bump=True)
                self._finish(req, res)
                return self._outcome(res, accepted=True)
            if verdict.recoverable:
                # Not cleared yet, but nothing unsafe: the owner keeps the session and is told why.
                req.rejected_resumes.append(RejectedResume(by=operator, decision=decision,
                                                           checkpoint=verdict.checkpoint, reason=verdict.detail))
                req.status = "claimed"
                self._set(ControlState.human, f"hand-back rejected: {verdict.detail}", bump=True)
                self._persist(req)
                self.log.event("handback_rejected", id=req.id, by=operator, reason=verdict.detail, epoch=self.epoch)
                return {"accepted": False, "state": self.state.value, "epoch": self.epoch,
                        "owner": operator, "reason": verdict.detail}
            self.terminate(f"{req.id}: hand-back verification failed")
            self._finish(req, res)
            return self._outcome(res, accepted=False)

    def _outcome(self, res: Resolution, *, accepted: bool) -> dict[str, Any]:
        return {"accepted": accepted, "state": self.state.value, "epoch": self.epoch, "resumed": res.resumed,
                "checkpoint": res.checkpoint, "reason": res.verification_detail}

    # ------------------------------------------------------------------ misc

    def _persist(self, req: InterventionRequest) -> None:
        path = self.run_dir / "interventions" / f"{req.id}.json"
        path.write_text(json.dumps(self.log.redactor.obj(req.model_dump(mode="json")), indent=2))

    def close(self) -> None:
        REGISTRY.pop(self.session_id, None)


def open_interventions() -> list[tuple[SessionControl, InterventionRequest]]:
    return [(c, c.current) for c in REGISTRY.values() if c.current is not None]


__all__ = ["ControlLost", "StaleRequest", "NotAuthorized", "ControlState", "InterventionKind", "Decision",
           "HandbackVerdict", "Resolution", "InterventionRequest", "SessionControl", "open_interventions",
           "REGISTRY", "NOTICES"]
