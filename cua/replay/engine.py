"""Deterministic replay — the production execution path. No LLM anywhere here.

Per step, the engine runs the same loop:

    ┌─ detectors? ── business_outcome → return outcome
    │               recoverable      → dismiss | restart_flow | restart_session
    │               hard_failure     → fail
    ├─ resolve target (one pass over the strategy bundle; poll until timeout,
    │  re-checking detectors between polls so an error page is recognised in
    │  ~250ms instead of after a 10s timeout)
    ├─ precondition: `require` (e.g. the record on screen is the requested one)
    ├─ guard: policy + risk re-checked against the *current* policy and the
    │  *live* control name (not just what was recorded)
    ├─ act (expected native dialog armed; irreversible ⇒ state becomes in_doubt)
    ├─ action check: fill/select read back the control; navigate checks the URL
    └─ postcondition: poll `expect` with detectors; timeout ⇒ unexpected_state

Then the success checkpoint *and* every invariant must hold, or the run fails
— "all clicks succeeded" or "the right page title is showing" is never success.

Failure handling is policy-driven (``on_failure``): ``fail`` returns a
structured failure (unattended production); ``escalate`` hands the live
session to a human (see handoff/control.py). Automation regains control only
at a checkpoint proven by ``replay/resume.py``; an attempted irreversible step
is never re-executed.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from .. import contracts, detect, routes, values
from ..handoff.control import ControlLost, Decision, HandbackVerdict, InterventionKind, Resolution as Handback
from ..schema.artifact import (RISK_ORDER, REDACTED_SENSITIVITIES, Capability, ClickStep, Detector,
                               DismissRecovery, ExtractStep, FillStep, LiteralValue, NavigateStep, OutcomeClass,
                               ParamRef, RestartFlowRecovery, RestartSessionRecovery, RiskClass, SecretRef,
                               SelectStep, Step, Target, ValueType)
from ..schema.results import (BlockedDetail, BusinessOutcome, DriftNote, FailureDetail, FailureKind,
                              RecoveryEvent, ReplayResult, ResultStatus, StepTrace)
from ..session import LiveSession
from ..surface.base import Actor, Resolution, TargetUnresolved
from ..tenancy import effective_detectors, specialize
from .resume import ResumePoint, StepFacts, resume_point

POLL_S = 0.25


@dataclass
class ReplayOptions:
    irreversible: Literal["deny", "allow", "ask_operator"] = "deny"
    on_failure: Literal["fail", "escalate"] = "fail"
    use_tenant_overrides: bool = True
    escalation_timeout_s: float = 900
    max_interventions: int = 2

    @property
    def unattended(self) -> bool:
        """No operator will be consulted during this run."""
        return self.on_failure != "escalate" and self.irreversible != "ask_operator"


# ---- control-flow signals (internal)

class _Outcome(Exception):
    def __init__(self, o: BusinessOutcome) -> None:
        self.o = o


class _Fail(Exception):
    def __init__(self, d: FailureDetail) -> None:
        self.d = d


class _Blocked(Exception):
    def __init__(self, d: BlockedDetail) -> None:
        self.d = d


class _Restart(Exception):
    def __init__(self, relogin: bool) -> None:
        self.relogin = relogin


@dataclass
class _RunState:
    committed_irreversible: bool = False
    attempted_irreversible: set[str] = field(default_factory=set)   # step ids; never re-executed
    restarts: dict[str, int] = field(default_factory=dict)
    interventions: int = 0


class ReplayEngine:
    def __init__(self, session: LiveSession, capability: Capability, inputs: dict[str, Any],
                 opts: ReplayOptions | None = None) -> None:
        self.s = session
        self.log = session.log
        self.base_cap = capability
        self.raw_inputs = inputs
        self.opts = opts or ReplayOptions()
        self.overlay_error: str | None = None
        try:
            self.cap, self.overlay_notes = specialize(capability, session.env.tenant,
                                                      use_overrides=self.opts.use_tenant_overrides)
        except ValueError as e:   # OverlayViolation or schema validation of the effective artifact
            self.cap, self.overlay_notes, self.overlay_error = capability, [], str(e).splitlines()[0]
        self.detectors: list[Detector] = effective_detectors(self.cap, session.env.profile, session.env.tenant)
        self.params: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}
        self.recoveries: list[RecoveryEvent] = []
        self.drift: list[DriftNote] = []
        self.trace: list[StepTrace] = []
        self.warnings: list[str] = []
        self.state = _RunState()
        self.started = datetime.now(timezone.utc)
        self._t0 = time.monotonic()

    # ================================================================== public

    async def run(self) -> ReplayResult:
        self.log.event("replay_started", capability=self.cap.id, version=self.cap.version,
                       tenant=self.s.env.tenant.id, content_hash=self.base_cap.content_hash(),
                       approved=self.base_cap.is_approved, overlay=self.overlay_notes,
                       options=self.opts.__dict__)
        try:
            self._preflight()
            await self._establish()
            await self._run_steps()
            await self._verify_success()
            return self._result(ResultStatus.success)
        except _Outcome as e:
            self.log.event("business_outcome", **e.o.model_dump())
            return self._result(ResultStatus.business_outcome, outcome=e.o)
        except _Blocked as e:
            self.log.event("blocked", **e.d.model_dump())
            return self._result(ResultStatus.blocked, blocked=e.d)
        except _Fail as e:
            return self._result(ResultStatus.failed, failure=e.d)
        except ControlLost as e:
            return self._result(ResultStatus.failed, failure=FailureDetail(
                kind=FailureKind.control_lost, code="CONTROL_LOST", observed=str(e)))

    # ================================================================== pre-flight (touches nothing)

    def _preflight(self) -> None:
        cap, env = self.cap, self.s.env
        if self.overlay_error:
            raise _Blocked(BlockedDetail(code="OVERLAY_INVALID", message=self.overlay_error))
        # Workflow contract: the recorded artifact *and* what this tenant will actually run.
        contract = contracts.contract_for(self.base_cap)
        if contract.approval_required and not self.base_cap.is_approved:
            raise _Blocked(BlockedDetail(code="APPROVAL_REQUIRED",
                                         message=f"contract {contract.id} requires an approved capability "
                                                 "(not approved, or edited after approval)"))
        for label, c in (("recorded", self.base_cap), ("tenant-effective", cap)):
            violations = contracts.check(c, contract, approved=True)   # approval handled just above
            if violations:
                raise _Blocked(BlockedDetail(code="CONTRACT_VIOLATION",
                                             message=f"{label} capability violates {contract.id}: "
                                                     + "; ".join(violations)))
        if self.opts.unattended:
            ok, why = contracts.unattended_ok(cap, contract)
            if not ok:
                raise _Blocked(BlockedDetail(code="UNATTENDED_NOT_PERMITTED",
                                             message=f"{why}; run with an operator (on_failure=escalate)"))
        # inputs
        for p in cap.inputs:
            if p.name not in self.raw_inputs:
                if p.required:
                    raise _Blocked(BlockedDetail(code="INVALID_INPUT", message=f"missing required input {p.name!r}"))
                continue
            v = str(self.raw_inputs[p.name])
            if p.type == ValueType.integer and not re.fullmatch(r"-?\d+", v):
                raise _Blocked(BlockedDetail(code="INVALID_INPUT", message=f"{p.name} must be an integer"))
            if p.type == ValueType.decimal:
                try:
                    Decimal(v)
                except InvalidOperation:
                    raise _Blocked(BlockedDetail(code="INVALID_INPUT", message=f"{p.name} must be a decimal"))
            if p.pattern and not re.fullmatch(p.pattern, v):
                raise _Blocked(BlockedDetail(code="INVALID_INPUT",
                                             message=f"{p.name} does not match {p.pattern}"))
            if p.enum and v not in p.enum:
                raise _Blocked(BlockedDetail(code="INVALID_INPUT", message=f"{p.name} must be one of {p.enum}"))
            self.params[p.name] = v
            if p.sensitivity in REDACTED_SENSITIVITIES:
                self.log.redactor.register(v, f"input:{p.name}")
        unknown = set(self.raw_inputs) - {p.name for p in cap.inputs}
        if unknown:
            self.warnings.append(f"ignored unknown inputs: {sorted(unknown)}")
        # product / version binding
        if env.tenant.product != cap.app.product:
            raise _Blocked(BlockedDetail(code="VERSION_UNSUPPORTED",
                                         message=f"tenant runs {env.tenant.product}, capability is for {cap.app.product}"))
        if not _version_ok(env.tenant.version, cap.app.versions):
            raise _Blocked(BlockedDetail(code="VERSION_UNSUPPORTED",
                                         message=f"{cap.app.product} {env.tenant.version} not in {cap.app.versions}"))
        # policy, re-evaluated now (policies change after recording)
        for step in cap.steps:
            name = _target_name(getattr(step, "target", None))
            d = self.s.guard.check(step.action, name, declared=step.risk)
            if not d.allowed:
                raise _Blocked(BlockedDetail(code="POLICY_DENIED", message=d.reason, step_id=step.id))
            if isinstance(step, NavigateStep):
                ok, why = self.s.guard.url_allowed(self.s.url(step.route, self.params))
                if not ok:
                    raise _Blocked(BlockedDetail(code="POLICY_DENIED", message=why, step_id=step.id))
            if RISK_ORDER[d.risk] > RISK_ORDER[step.risk]:
                self.warnings.append(f"{step.id}: current policy raises risk {step.risk.value} -> {d.risk.value}")
        # irreversible gate
        # Approval belongs to the recorded artifact; tenant overlays are config, reviewed separately.
        if cap.side_effects == RiskClass.irreversible:
            if not self.base_cap.is_approved:
                raise _Blocked(BlockedDetail(code="APPROVAL_REQUIRED",
                                             message="capability has irreversible steps and is not approved "
                                                     "(or was edited after approval)"))
            if self.opts.irreversible == "deny":
                raise _Blocked(BlockedDetail(code="IRREVERSIBLE_NOT_AUTHORIZED",
                                             message="caller did not authorise irreversible actions "
                                                     "(irreversible=allow|ask_operator)"))
        elif not self.base_cap.is_approved:
            self.warnings.append("capability is a draft (read-only, so allowed)")

    # ================================================================== session

    async def _establish(self) -> None:
        await self.s.ensure_authenticated()
        actual = await self.s.probe_version()
        if actual and actual != self.s.env.tenant.version:
            self.warnings.append(f"tenant config says v{self.s.env.tenant.version}, app reports v{actual}")
            if not _version_ok(actual, self.cap.app.versions):
                raise _Blocked(BlockedDetail(code="VERSION_UNSUPPORTED",
                                             message=f"app reports v{actual}, capability supports {self.cap.app.versions}"))
        self.log.event("session_ready", app_version=actual)
        await self._goto_entry()

    async def _goto_entry(self) -> None:
        await self.s.surface.goto(self.s.url(self.cap.entry_route, self.params), Actor.agent)
        await self.s.surface.settle()

    # ================================================================== steps

    async def _run_steps(self) -> None:
        i = 0
        while i < len(self.cap.steps):
            step = self.cap.steps[i]
            t0 = time.monotonic()
            try:
                matched = await self._run_step(step)
                self.trace.append(StepTrace(step_id=step.id, action=step.action, status="ok", matched_by=matched,
                                            ms=int((time.monotonic() - t0) * 1000)))
                i += 1
            except _Restart as r:
                if r.relogin:
                    await self.s.login()
                await self._goto_entry()
                self.trace.append(StepTrace(step_id=step.id, action=step.action, status="restarted",
                                            ms=int((time.monotonic() - t0) * 1000)))
                i = 0
            except _Fail as f:
                rp = await self._maybe_escalate(i, step, f.d)
                self.trace.append(StepTrace(step_id=step.id, action=step.action, status=f"handback:{rp.checkpoint}",
                                            ms=int((time.monotonic() - t0) * 1000)))
                if rp.kind == "done":
                    return
                if rp.kind == "restart":
                    await self._goto_entry()
                i = rp.index if rp.index is not None else 0

    async def _run_step(self, step: Step) -> str | None:
        self.log.event("step_started", step_id=step.id, action=step.action, intent=step.intent,
                       risk=step.risk.value)
        await self._check_detectors(step)
        if step.require is not None:
            await self._await_condition(step, step.require, "PRECONDITION_FAILED", FailureKind.unexpected_state,
                                        mismatch_code="RECORD_MISMATCH")
        if isinstance(step, NavigateStep):
            await self.s.surface.goto(self.s.url(step.route, self.params), Actor.agent)
            if step.expect is None:   # the destination itself is the verification
                top = self.s.surface.page.main_frame.url
                if not routes.matches(step.route, top, self.s.env.tenant.base_url, self.params):
                    raise _Fail(await self._failure(step, FailureKind.unexpected_state, "NAVIGATION_FAILED",
                                                    expected=step.route, observed=routes.relative(
                                                        top, self.s.env.tenant.base_url)[0]))
            await self._await_postcondition(step)
            return None

        res = await self._resolve(step, step.target)
        live_name = await _live_name(res)
        decision = self.s.guard.check(step.action, live_name or _target_name(step.target), declared=step.risk)
        if not decision.allowed:
            raise _Blocked(BlockedDetail(code="POLICY_DENIED", message=decision.reason, step_id=step.id))
        risk = max(step.risk, decision.risk, key=RISK_ORDER.__getitem__)

        if risk == RiskClass.irreversible:
            await self._authorize_irreversible(step)

        dialogs_before = len(self.s.surface.dialog_log)
        self.s.surface.expected_dialog = getattr(step, "dialog", None)
        try:
            if risk == RiskClass.irreversible:
                self.state.committed_irreversible = True   # from here on, the outcome is in doubt
                self.state.attempted_irreversible.add(step.id)
                self.log.event("irreversible_commit", step_id=step.id, control=live_name)
            await self._act(step, res)
        finally:
            self.s.surface.expected_dialog = None
        for dlg in self.s.surface.dialog_log[dialogs_before:]:
            self.log.event("dialog", step_id=step.id, **dlg)
            if dlg["handled"] == "unexpected_dismissed":
                raise _Fail(await self._failure(step, FailureKind.hard, "UNEXPECTED_DIALOG",
                                                expected="no confirmation dialog",
                                                observed=f"{dlg['type']}: {dlg['message']}"))
        await self._await_postcondition(step)
        self.log.event("step_ok", step_id=step.id, matched_by=res.strategy_kind)
        return res.strategy_kind

    async def _act(self, step: Step, res: Resolution) -> None:
        sf = self.s.surface
        try:
            if isinstance(step, ClickStep):
                await sf.click(res, Actor.agent)
            elif isinstance(step, (FillStep, SelectStep)):
                v = step.value
                if isinstance(v, ParamRef):
                    value = self.params[v.param]
                elif isinstance(v, SecretRef):
                    value = self.s.secret(v.secret)
                else:
                    value = v.literal
                await (sf.fill(res, value, Actor.agent) if isinstance(step, FillStep)
                       else sf.select(res, value, Actor.agent))
                # Action-specific verification: the control must now show what we put in it.
                shown = await sf.read_value(res)
                if not values.matches(shown, equals=value, pattern=None, how="text"):
                    raise _Fail(await self._failure(step, FailureKind.unexpected_state, "VALUE_NOT_APPLIED",
                                                    expected=f"control shows the {step.action}ed value",
                                                    observed="control shows a different value"))
            elif isinstance(step, ExtractStep):
                raw = await sf.read_text(res)
                self.outputs[step.output] = self._parse(step, raw)
                spec = next((o for o in self.cap.outputs if o.name == step.output), None)
                if spec and spec.sensitivity in REDACTED_SENSITIVITIES:
                    self.log.redactor.register(raw, f"output:{step.output}")
                    self.log.redactor.register(self.outputs[step.output], f"output:{step.output}")
                self.log.event("extracted", step_id=step.id, output=step.output, value=self.outputs[step.output])
        except ControlLost:
            raise
        except _Fail:
            raise
        except Exception as e:
            kind = FailureKind.in_doubt if self.state.committed_irreversible else FailureKind.hard
            raise _Fail(await self._failure(step, kind, "ACTION_FAILED", expected=f"{step.action} succeeds",
                                            observed=f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"))

    def _parse(self, step: ExtractStep, raw: str) -> Any:
        try:
            return values.parse_value(raw, step.parse, step.pattern)
        except values.ParseError as e:
            raise _Fail(FailureDetail(kind=FailureKind.hard, code="EXTRACT_PARSE", step_id=step.id,
                                      expected=f"a {step.parse.value}", observed=str(e)))

    # ---- resolution with detector-aware polling

    async def _resolve(self, step: Step, target: Target) -> Resolution:
        deadline = time.monotonic() + step.timeout_ms / 1000
        attempts: list[str] = []
        while True:
            try:
                res = await self.s.surface.try_resolve(target)
                if res.strategy_index > 0:
                    # A fallback hit can be a race with a page still being parsed (the primary
                    # ran a few ms earlier on an empty document). Let the page settle and give
                    # the primary one more chance before calling it drift.
                    await self.s.surface.settle()
                    try:
                        again = await self.s.surface.try_resolve(target)
                        if again.strategy_index < res.strategy_index:
                            self.log.event("drift_unconfirmed", step_id=step.id, first_match=res.strategy_kind,
                                           settled_match=again.strategy_kind)
                            res = again
                    except TargetUnresolved:
                        pass   # page changed under us: keep the earlier verified match
                if res.strategy_index > 0:
                    note = DriftNote(step_id=step.id, matched_by=res.strategy_kind,
                                     strategy_index=res.strategy_index,
                                     recorded=target.fingerprint.model_dump(exclude_none=True),
                                     note="higher-priority strategies missed: " + "; ".join(res.attempts))
                    self.drift.append(note)
                    self.log.event("drift", **note.model_dump())
                return res
            except TargetUnresolved as e:
                attempts = e.attempts
            if time.monotonic() > deadline:
                kind = FailureKind.in_doubt if self.state.committed_irreversible else FailureKind.target_not_found
                raise _Fail(await self._failure(step, kind, "TARGET_NOT_FOUND",
                                                expected=f"control {target.fingerprint.model_dump(exclude_none=True)}",
                                                observed="; ".join(attempts)))
            await asyncio.sleep(POLL_S)
            await self._check_detectors(step)

    async def _await_postcondition(self, step: Step) -> None:
        if step.expect is None:
            await self.s.surface.settle(1500)
            await self._check_detectors(step)
            return
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            snap = await detect.snapshot(self.s.surface)
            if await detect.holds(step.expect, self.s.surface, self.params, snap):
                # A URL postcondition holds as soon as navigation *commits*, before the new
                # document is parsed. Wait for it to load so the next step sees the real page.
                await self.s.surface.settle()
                return
            await self._check_detectors(step, snap)
            if time.monotonic() > deadline:
                kind = FailureKind.in_doubt if self.state.committed_irreversible else FailureKind.unexpected_state
                frames = {f.path: f.url for f in (await self.s.surface.observe(screenshot=False)).frames}
                raise _Fail(await self._failure(step, kind, "POSTCONDITION_TIMEOUT",
                                                expected=_cond_brief(step.expect), observed=f"frames={frames}"))
            await asyncio.sleep(POLL_S)

    async def _await_condition(self, step: Step | None, cond: Any, code: str, kind: FailureKind,
                               timeout_s: float | None = None, mismatch_code: str | None = None) -> None:
        """Poll a condition with detectors in between; fail with a value-free explanation."""
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else
                                       (step.timeout_ms / 1000 if step else 5))
        wrong = 0
        while True:
            snap = await detect.snapshot(self.s.surface)
            if await detect.holds(cond, self.s.surface, self.params, snap, self.outputs):
                return
            await self._check_detectors(step, snap)
            # A displayed value that contradicts the request on two consecutive polls is an
            # answer, not a page still loading: fail now instead of at the timeout.
            wrong = wrong + 1 if await detect.contradicted(cond, self.s.surface, self.params) else 0
            if wrong >= 2 or time.monotonic() > deadline:
                why = await detect.first_failing(cond, self.s.surface, self.params, snap, self.outputs)
                if wrong >= 2:   # the screen answers, and the answer is a different record / value
                    kind, code = FailureKind.invariant_violated, mismatch_code or code
                if self.state.committed_irreversible:
                    kind = FailureKind.in_doubt
                raise _Fail(await self._failure(step, kind, code, expected=_cond_brief(cond), observed=why))
            await asyncio.sleep(POLL_S)

    async def _verify_success(self) -> None:
        missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
        if missing:
            raise _Fail(FailureDetail(kind=FailureKind.checkpoint_failed, code="OUTPUTS_MISSING",
                                      expected=f"outputs {missing}", observed="not extracted"))
        await self._await_condition(None, self.cap.success, "CHECKPOINT_FAILED", FailureKind.checkpoint_failed,
                                    timeout_s=5)
        self.log.event("checkpoint_ok", condition=_cond_brief(self.cap.success))
        for inv in self.cap.invariants:   # the business result, not just the screen
            await self._await_condition(None, inv.when, f"INVARIANT_FAILED:{inv.id}", FailureKind.invariant_violated,
                                        timeout_s=2)
            self.log.event("invariant_ok", invariant=inv.id)

    # ---- detectors → taxonomy

    async def _check_detectors(self, step: Step | None, snap: detect.Snapshot | None = None) -> None:
        for _ in range(3):  # a dismissal can reveal the next state; re-check a few times
            hit = await detect.first_hit(self.detectors, self.s.surface, self.params,
                                         step.id if step else None, snap)
            if hit is None:
                return
            d = hit.detector
            sid = step.id if step else "checkpoint"
            self.log.event("detector_hit", step_id=sid, detector=d.id, code=d.code,
                           classification=d.classification.value, message=hit.message)
            if d.classification == OutcomeClass.business_outcome:
                if self.state.committed_irreversible:
                    raise _Fail(await self._failure(step, FailureKind.in_doubt, d.code, detector=d.id,
                                                    expected="post-commit success state", observed=hit.message))
                raise _Outcome(BusinessOutcome(code=d.code, message=hit.message, step_id=sid))
            if d.classification == OutcomeClass.hard_failure:
                raise _Fail(await self._failure(step, FailureKind.hard, d.code, detector=d.id,
                                                observed=hit.message or d.description))
            if d.classification == OutcomeClass.requires_human:
                kind = FailureKind.in_doubt if self.state.committed_irreversible else FailureKind.requires_human
                raise _Fail(await self._failure(step, kind, d.code, detector=d.id,
                                                observed=hit.message or d.description,
                                                hint="only an operator can clear this; replay with on_failure=escalate"))
            await self._recover(step, d, sid)
            snap = None

    async def _recover(self, step: Step | None, d: Detector, sid: str) -> None:
        rec = d.recovery
        if isinstance(rec, DismissRecovery):
            n = sum(1 for r in self.recoveries if r.detector == d.id and r.step_id == sid) + 1
            ok = False
            if n <= 3:
                try:
                    res = await self.s.surface.try_resolve(rec.target)
                    await self.s.surface.click(res, Actor.agent)
                    ok = True
                except (TargetUnresolved, Exception):
                    ok = False
            self.recoveries.append(RecoveryEvent(step_id=sid, detector=d.id, action="dismiss", attempt=n, ok=ok))
            self.log.event("recovery", step_id=sid, detector=d.id, action="dismiss", attempt=n, ok=ok)
            if not ok:
                raise _Fail(await self._failure(step, FailureKind.recovery_exhausted, d.code, detector=d.id,
                                                observed=f"could not dismiss after {n} attempts"))
            await asyncio.sleep(0.2)
            return
        if isinstance(rec, (RestartFlowRecovery, RestartSessionRecovery)):
            if self.state.committed_irreversible:
                raise _Fail(await self._failure(step, FailureKind.in_doubt, d.code, detector=d.id,
                                                observed=f"{d.code} after an irreversible step; not retrying",
                                                hint="verify in the app whether the transaction posted"))
            n = self.state.restarts.get(d.id, 0) + 1
            self.state.restarts[d.id] = n
            if n > rec.max_attempts:
                raise _Fail(await self._failure(step, FailureKind.recovery_exhausted, d.code, detector=d.id,
                                                observed=f"{d.code} persisted after {rec.max_attempts} restarts"))
            relogin = isinstance(rec, RestartSessionRecovery)
            self.recoveries.append(RecoveryEvent(step_id=sid, detector=d.id, action=rec.kind, attempt=n, ok=True))
            self.log.event("recovery", step_id=sid, detector=d.id, action=rec.kind, attempt=n,
                           backoff_ms=rec.backoff_ms)
            await asyncio.sleep(rec.backoff_ms / 1000)
            raise _Restart(relogin)
        raise _Fail(await self._failure(step, FailureKind.hard, d.code, detector=d.id,
                                        observed="recoverable detector without a recovery"))

    # ---- risk gate

    async def _authorize_irreversible(self, step: Step) -> None:
        if self.opts.irreversible == "allow":
            self.log.event("irreversible_authorized", step_id=step.id, by="caller")
            return
        if self.opts.irreversible == "ask_operator":
            idx = self.cap.steps.index(step)
            shot = self.log.save_png(f"{step.id}-approval", await self.s.surface.screenshot())

            async def verify(h: Handback) -> HandbackVerdict:
                if h.decision != Decision.approve:
                    return HandbackVerdict(True, "not-approved", "no action will be taken")
                # Approval is for *this* screen: the app must still be exactly before the step.
                rp = await self._resume_point(idx)
                if rp.kind != "continue" or rp.index != idx:
                    return HandbackVerdict(False, rp.checkpoint, f"app state changed during approval: {rp.reason}")
                try:
                    await self.s.surface.try_resolve(step.target)
                except TargetUnresolved:
                    return HandbackVerdict(False, rp.checkpoint, "approved control is no longer on screen")
                return HandbackVerdict(True, rp.checkpoint, rp.reason)

            res = await self.s.control.escalate(
                run_id=self.log.run_id, kind=InterventionKind.approval, subject=self._subject(),
                step_id=step.id, step_intent=step.intent, code="IRREVERSIBLE_STEP",
                reason=f"Step {step.id} '{step.intent}' commits an irreversible change. Approve?",
                screenshot=shot, frames=await self._frames(), timeout_s=self.opts.escalation_timeout_s,
                verify=verify)
            if res.decision == Decision.approve and res.resumed:
                self.log.event("irreversible_authorized", step_id=step.id, by=res.by)
                return
            if res.decision == Decision.approve:
                raise _Blocked(BlockedDetail(code="APPROVAL_STATE_CHANGED", step_id=step.id,
                                             message=f"approval void: {res.verification_detail}"))
            raise _Blocked(BlockedDetail(code="APPROVAL_DENIED", step_id=step.id,
                                         message=f"operator {res.decision.value}: {res.note}"))
        raise _Blocked(BlockedDetail(code="IRREVERSIBLE_NOT_AUTHORIZED", step_id=step.id,
                                     message="irreversible step reached without authorisation"))

    # ---- escalation on failure

    async def _maybe_escalate(self, index: int, step: Step, fail: FailureDetail) -> ResumePoint:
        """Hand the session to a human; return the *proven* point to resume from."""
        escalatable = fail.kind not in (FailureKind.control_lost,) and fail.code != "AUTH_REJECTED"
        if self.opts.on_failure != "escalate" or not escalatable or \
                self.state.interventions >= self.opts.max_interventions:
            raise _Fail(fail)
        self.state.interventions += 1
        found: dict[str, ResumePoint] = {}

        async def verify(h: Handback) -> HandbackVerdict:
            # 1. Intervention-specific clearance: the condition that caused this escalation must be gone.
            #    Otherwise an earlier checkpoint (e.g. the URL of the page *under* a supervisor hold)
            #    would be accepted and automation would just fail again.
            blocking = await self._blocking_condition(fail)
            if blocking:
                return HandbackVerdict(False, "blocked", blocking, recoverable=True)
            # 2. Where may automation resume (pure rule, never repeats an attempted irreversible step)?
            rp = await self._resume_point(index)
            if not rp.ok:
                return HandbackVerdict(False, rp.checkpoint, rp.reason, recoverable=False)   # fail closed
            # 3. The step automation would run next must actually be runnable from here.
            if rp.kind == "continue" and rp.index is not None and rp.index < len(self.cap.steps):
                why = await self._not_ready(self.cap.steps[rp.index])
                if why:
                    return HandbackVerdict(False, rp.checkpoint, why,
                                           recoverable=not self.state.attempted_irreversible)
            found["rp"] = rp
            return HandbackVerdict(True, rp.checkpoint, rp.reason, rp)

        res = await self.s.control.escalate(
            run_id=self.log.run_id, kind=InterventionKind.failure, subject=self._subject(),
            step_id=step.id, step_intent=step.intent, code=fail.code,
            reason="; ".join(x for x in (f"{fail.kind.value} ({fail.code})",
                                         f"expected {fail.expected}" if fail.expected else "",
                                         f"observed {fail.observed}" if fail.observed else "",
                                         fail.hint or "") if x),
            screenshot=fail.evidence[0] if fail.evidence else None, frames=await self._frames(),
            timeout_s=self.opts.escalation_timeout_s, verify=verify)
        if res.resumed:
            rp = found["rp"]
            self.log.event("handback_verified", step_id=step.id, checkpoint=rp.checkpoint, reason=rp.reason)
            if rp.kind == "restart":
                self.state.restarts["handback"] = self.state.restarts.get("handback", 0) + 1
            return rp
        if res.decision == Decision.resume:   # operator said resume, but nothing could be proven
            kind = FailureKind.in_doubt if self.state.attempted_irreversible else FailureKind.handback_unverified
            raise _Fail(await self._failure(step, kind, "HANDBACK_UNVERIFIED", expected="a provable checkpoint",
                                            observed=res.verification_detail,
                                            hint="automation did not resume; the session was terminated"))
        # An attempted irreversible step keeps the run in_doubt whatever the operator chose.
        fail.kind = FailureKind.in_doubt if self.state.attempted_irreversible else FailureKind.escalation_unresolved
        fail.hint = f"operator decision: {res.decision.value} {res.note}".strip()
        raise _Fail(fail)

    async def _blocking_condition(self, fail: FailureDetail) -> str | None:
        """If the escalation was caused by a known detector, is that state still on screen?"""
        if not fail.detector:
            return None
        d = next((x for x in self.detectors if x.id == fail.detector), None)
        if d is None:
            return None
        snap = await detect.snapshot(self.s.surface)
        if await detect.holds(d.when, self.s.surface, self.params, snap):
            return f"{d.code} is still present ({d.description or d.id}); clear it before resuming"
        return None

    async def _not_ready(self, step: Step) -> str | None:
        """Why the next step cannot run from the current screen (None = ready)."""
        snap = await detect.snapshot(self.s.surface)
        if step.require is not None and not await detect.holds(step.require, self.s.surface, self.params, snap,
                                                               self.outputs):
            why = await detect.first_failing(step.require, self.s.surface, self.params, snap, self.outputs)
            return f"{step.id} precondition not met: {why}"
        target = getattr(step, "target", None)
        if target is not None:
            try:
                await self.s.surface.try_resolve(target)
            except TargetUnresolved:
                return f"{step.id} control is not on screen yet"
        return None

    async def _resume_point(self, index: int) -> ResumePoint:
        snap = await detect.snapshot(self.s.surface)
        facts = []
        for st in self.cap.steps:
            holds = st.expect is not None and await detect.holds(st.expect, self.s.surface, self.params, snap,
                                                                 self.outputs)
            facts.append(StepFacts(st.id, st.expect is not None, holds, isinstance(st, ExtractStep),
                                   st.id in self.state.attempted_irreversible))
        success = await detect.holds(self.cap.success, self.s.surface, self.params, snap, self.outputs)
        for inv in self.cap.invariants:
            success = success and await detect.holds(inv.when, self.s.surface, self.params, snap, self.outputs)
        complete = all(o.name in self.outputs for o in self.cap.outputs)
        return resume_point(index, facts, success_holds=success, outputs_complete=complete)

    # ---- helpers

    def _subject(self) -> str:
        return f"{self.cap.id}@{self.cap.version}"

    async def _frames(self) -> dict[str, str]:
        obs = await self.s.surface.observe(screenshot=False)
        return {f.path: routes.relative(f.url, self.s.env.tenant.base_url)[0] for f in obs.frames}

    async def _failure(self, step: Step | None, kind: FailureKind, code: str, *, expected: str | None = None,
                       observed: str | None = None, detector: str | None = None,
                       hint: str | None = None) -> FailureDetail:
        sid = step.id if step else None
        evidence = []
        try:
            name = f"{sid or 'checkpoint'}-{code.lower()}"
            evidence.append(self.log.save_png(name, await self.s.surface.screenshot()))
            texts = await self.s.surface.text_by_frame()
            evidence.append(self.log.save_text(name, "\n".join(f"===== frame {k}\n{v}" for k, v in texts.items())))
        except Exception as e:  # evidence is best-effort, the failure is not
            self.log.event("evidence_error", error=str(e))
        if hint is None and kind == FailureKind.in_doubt:
            hint = "an irreversible step was executed; verify the record in the app before retrying"
        d = FailureDetail(kind=kind, code=code, step_id=sid, step_intent=step.intent if step else None,
                          expected=expected, observed=observed, detector=detector, evidence=evidence, hint=hint)
        self.log.event("failure", **d.model_dump())
        return d

    def _result(self, status: ResultStatus, **kw: Any) -> ReplayResult:
        r = ReplayResult(
            run_id=self.log.run_id, capability_id=self.cap.id, capability_version=self.cap.version,
            tenant=self.s.env.tenant.id, status=status,
            outputs=self.outputs if status == ResultStatus.success else {},
            recoveries=self.recoveries, drift=self.drift, steps=self.trace,
            interventions=[i.id for i in self.s.control.history],
            warnings=self.warnings + [f"overlay: {n}" for n in self.overlay_notes],
            started_at=self.started, duration_ms=int((time.monotonic() - self._t0) * 1000),
            run_dir=str(self.log.dir), **kw)
        self.log.event("replay_finished", status=status.value,
                       outcome=r.outcome.code if r.outcome else None,
                       failure=r.failure.code if r.failure else None,
                       blocked=r.blocked.code if r.blocked else None,
                       recoveries=len(r.recoveries), drift=len(r.drift))
        self.log.write_json("result.json", r)
        return r


def _version_ok(version: str, spec: str) -> bool:
    try:
        return Version(version) in SpecifierSet(spec)
    except (InvalidSpecifier, ValueError):
        return False


def _target_name(t: Target | None) -> str | None:
    return t.fingerprint.name if t is not None else None


async def _live_name(res: Resolution) -> str:
    try:
        return await res.handle.evaluate(
            "el => (el.value && ['submit','button','reset'].includes((el.type||'').toLowerCase()) ? el.value : "
            "(el.getAttribute('aria-label') || el.innerText || '')).trim()")
    except Exception:
        return ""


def _cond_brief(c: Any) -> str:
    d = c.model_dump(exclude_none=True)
    return str(d)[:300]
