"""Discovery: an LLM proposes actions; the trusted host validates, executes,
verifies and only then records them.

    login (app profile recipe; credentials never reach the model)
      → entry route
      → per turn:
          Observe   product detectors, element list + masked screenshot; parameter
                    values appear only as {{name}} symbols, sensitive fields redacted
          Decide    the model picks ONE tool (refs + symbolic params, never values)
          Validate  ref in latest observation, action fits the control's role,
                    params declared, policy + risk; irreversible ⇒ human approval
          Execute   host binds the real value and acts under the session lease
          Verify    action-specific proof (verify.py); detectors after the action
          Record    only verified steps enter the capability, with their proof
      → finish: checkpoint + record-identity / confirmation invariants, each
        checked live; compile, schema-validate, lint, save

Stopping conditions: finish, report_outcome, max steps, wall-clock limit,
no-progress (same screen state N times), repeated policy blocks. Being stuck
escalates to a human on the same live session when an operator console is
attached; otherwise the run ends with a clear reason.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .. import detect, routes, store
from ..handoff.control import Decision, HandbackVerdict, InterventionKind, Resolution as Handback
from ..redact import Redactor
from ..schema.artifact import (REDACTED_SENSITIVITIES, DialogExpectation, DismissRecovery, Invariant, LiteralValue,
                               OutcomeClass, OutputCondition, ParamRef, RestartFlowRecovery, RestartSessionRecovery,
                               RiskClass, Target, ValueCondition, ValueType, Verification)
from ..session import LiveSession
from ..surface.base import Actor, Element, Observation, Resolution, TargetUnresolved
from . import verify
from .llm import LLM, LLMTurn
from .prompts import SYSTEM, TOOLS
from .recorder import DeclaredParam, Recorder

ROLE_FOR = {"fill": {"textbox"}, "select": {"combobox", "listbox"}}
_SYMBOL = re.compile(r"^\{\{([a-z][a-z0-9_]*)\}\}$")


@dataclass
class DiscoveryResult:
    status: str                       # recorded | business_outcome | gave_up | aborted
    reason: str = ""
    artifact_path: str | None = None
    capability_id: str | None = None
    version: str | None = None
    outcome: dict | None = None
    steps_taken: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ActionPlan:
    """Output of the Validate stage: everything the host needs to execute, model-free."""
    tool: str                          # click | fill | select | extract
    el: Element
    res: Resolution
    intent: str
    risk: RiskClass
    value: str | None = None           # bound by the host (from a param or a vetted literal)
    value_ref: ParamRef | LiteralValue | None = None
    output: str | None = None
    vtype: ValueType | None = None
    description: str = ""


class DiscoveryAgent:
    def __init__(self, session: LiveSession, llm: LLM, *, goal: str, params: list[DeclaredParam], cap_id: str,
                 title: str | None = None, escalate_when_stuck: bool = True) -> None:
        self.s = session
        self.log = session.log
        self.llm = llm
        self.goal_template = goal
        self.params = params
        self.cap_id = cap_id
        self.title = title or goal.split(".")[0][:80]
        self.escalate_when_stuck = escalate_when_stuck
        self.rec = Recorder(session.env.tenant.base_url, params)
        self.messages: list[dict[str, Any]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.limits = session.env.policy.discovery
        self._carry = ""
        # Model-facing view of parameter values: always {{name}}, never the value.
        self.symbols = Redactor(use_patterns=False, template="{{{{{label}}}}}")
        allow_sensitive = session.env.policy.model_exposure.allow_sensitive_values
        self.exposed: set[str] = set()
        for p in params:
            self.symbols.register(p.value, p.name)
            if p.sensitivity in REDACTED_SENSITIVITIES:
                self.log.redactor.register(p.value, f"input:{p.name}")
            if p.expose:
                if p.sensitivity in REDACTED_SENSITIVITIES and not allow_sensitive:
                    self.log.event("exposure_denied", param=p.name, sensitivity=p.sensitivity.value,
                                   reason="policy model_exposure.allow_sensitive_values is false")
                else:
                    self.exposed.add(p.name)
                    self.log.event("exposure_granted", param=p.name, sensitivity=p.sensitivity.value)

    # ================================================================== model-facing text

    @property
    def goal(self) -> str:
        g = re.sub(r"(?<!\{)\{([a-z][a-z0-9_]*)\}(?!\})", r"{{\1}}", self.goal_template)
        lines = [g, "", "PARAMETERS (pass param=<name> to fill/select; the host enters the value):"]
        for p in self.params:
            flags = [p.type.value, p.sensitivity.value] + (["identifies the record"] if p.identifies_record else [])
            shown = f'value "{p.value}"' if p.name in self.exposed else "value hidden"
            lines.append(f"  - {{{{{p.name}}}}}: {', '.join(flags)}; {shown}")
        if not self.params:
            lines.append("  (none)")
        return "\n".join(lines)

    def model_text(self, text: str) -> str:
        """Symbolize parameter values, then redact everything sensitive."""
        return self.log.redactor.text(self.symbols.text(text))

    # ================================================================== loop

    async def run(self) -> DiscoveryResult:
        self.log.event("discovery_started", goal_template=self.goal_template, capability=self.cap_id,
                       model=self.llm.model, tenant=self.s.env.tenant.id,
                       params={p.name: {"sensitivity": p.sensitivity.value, "exposed": p.name in self.exposed}
                               for p in self.params})
        await self.s.ensure_authenticated()
        self.version = await self.s.probe_version()
        self.log.event("session_ready", app_version=self.version)
        await self._goto_entry()

        t_end = time.monotonic() + self.limits.max_wall_seconds
        same_state, last_hash, last_recorded, blocked = 0, None, 0, 0
        pending_results: list[dict[str, Any]] = []
        note = ""
        for turn in range(1, self.limits.max_steps + 1):
            if time.monotonic() > t_end:
                return await self._stuck("wall-clock limit reached", turn) or \
                    DiscoveryResult(status="gave_up", reason="wall-clock limit", usage=self.usage)
            note += self._carry + await self._auto_detectors()
            self._carry = ""
            obs = await self.s.surface.observe()                                   # Observe
            shot = self.log.save_png(f"turn{turn:02d}", obs.screenshot) if obs.screenshot else None
            self.log.event("observation", turn=turn, state=obs.state_hash, screenshot=shot,
                           frames={f.path: routes.relative(f.url, self.s.env.tenant.base_url)[0] for f in obs.frames},
                           elements=len(obs.elements))
            # Progress = the screen changed or a verified step was recorded (an extract changes nothing).
            progressed = obs.state_hash != last_hash or len(self.rec.steps) != last_recorded
            same_state = 0 if progressed else same_state + 1
            last_hash, last_recorded = obs.state_hash, len(self.rec.steps)
            if same_state >= self.limits.max_no_progress:
                r = await self._stuck(f"no visible progress for {same_state} turns", turn)
                if r is not None:
                    return r
                same_state = 0
                continue

            self._push_user(pending_results, self._render(obs, note, turn), obs.screenshot)
            pending_results, note = [], ""
            turn_out = await self.llm.decide(SYSTEM, self.messages, TOOLS, observation=obs)   # Decide
            self._account(turn_out)
            self.messages.append({"role": "assistant", "content": turn_out.assistant_content or
                                  [{"type": "text", "text": turn_out.text or "(no output)"}]})
            if not turn_out.tool_calls:
                note = "You must call exactly one tool."
                continue
            call, extra = turn_out.tool_calls[0], turn_out.tool_calls[1:]
            self.log.event("decision", turn=turn, tool=call.name, input=call.input, text=turn_out.text[:300])
            try:
                outcome, msg = await self._execute(call.name, call.input, obs)
            except _Stop as st:
                return st.result
            except TargetUnresolved as e:
                outcome, msg = None, f"error: element not found ({'; '.join(e.attempts)[:200]}). Re-observe."
            except Exception as e:  # report to the model; never crash the run on one bad action
                outcome, msg = None, f"error: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
            msg = self.model_text(msg)
            if msg.startswith("blocked"):
                blocked += 1
            pending_results.append({"type": "tool_result", "tool_use_id": call.id, "content": msg,
                                    **({"is_error": True} if msg.startswith(("error", "blocked", "rejected"))
                                       else {})})
            for c in extra:
                pending_results.append({"type": "tool_result", "tool_use_id": c.id,
                                        "content": "ignored: one tool call per turn", "is_error": True})
            self.log.event("action_result", turn=turn, tool=call.name, result=msg[:300])
            if outcome is not None:
                return outcome
            if blocked >= 3:
                r = await self._stuck("three actions blocked by policy", turn)
                if r is not None:
                    return r
                blocked = 0
        return await self._stuck(f"max steps ({self.limits.max_steps}) reached", self.limits.max_steps) or \
            DiscoveryResult(status="gave_up", reason="max steps", usage=self.usage)

    # ================================================================== tools

    async def _execute(self, name: str, a: dict[str, Any], obs: Observation) -> tuple[DiscoveryResult | None, str]:
        if name == "finish":
            return await self._finish(a, obs)
        if name == "report_outcome":
            code = re.sub(r"[^A-Z0-9_]", "_", str(a.get("code", "OUTCOME")).upper())
            self.log.event("business_outcome", code=code, message=a.get("message"))
            return DiscoveryResult(status="business_outcome", reason=str(a.get("message")),
                                   outcome={"code": code, "message": a.get("message")}, usage=self.usage), "ok"
        if name == "ask_human":
            res = await self._escalate(InterventionKind.stuck, "MODEL_ASKED", str(a.get("reason")))
            if res is None:
                return DiscoveryResult(status="gave_up", reason="model asked for help; no operator",
                                       usage=self.usage), ""
            if not res.resumed:
                return DiscoveryResult(status="aborted", reason=res.note or res.verification_detail,
                                       usage=self.usage), ""
            return None, self._handback_note(res)
        if name == "click_at":
            ref = await self.s.surface.element_at(float(a["x"]), float(a["y"]))
            if not ref:
                return None, "error: nothing clickable at that point"
            name, a = "click", {**a, "ref": ref}
        if name not in ("click", "fill", "select", "extract"):
            return None, f"error: unknown tool {name}"

        plan = await self._validate(name, a, obs)                                  # Validate
        if isinstance(plan, str):
            return None, plan
        if plan.tool == "extract":
            return None, await self._extract(plan)
        if plan.risk == RiskClass.irreversible:
            denied = await self._approve(plan)
            if denied:
                return None, denied
        executed = await self._perform(plan)                                       # Execute
        verdict = await self._verify(plan, executed)                               # Verify
        if not verdict.accept:
            self.log.event("step_rejected", tool=plan.tool, reason=verdict.message)
            return None, f"rejected: {verdict.message}"
        target = executed["target"]                                                # Record
        sid = self.rec.record(plan.tool, target, intent=plan.intent, risk=plan.risk, expect=verdict.expect,
                              verification=verdict.verification, value_ref=plan.value_ref, dialog=executed["dialog"])
        self.log.event("recorded", step_id=sid, action=plan.tool, risk=plan.risk.value,
                       verification=verdict.verification, strategies=[s.kind for s in target.strategies],
                       expect=verdict.expect)
        msg = f"ok ({sid}): {verdict.message}"
        if executed["dialog"]:
            d = executed["dialog"]
            msg += f"; a {d['type']} dialog appeared ('{d['message']}') and was {d['handled']}"
        if verdict.verification and verdict.verification.review_required:
            msg += " [recorded, flagged for human review]"
        return None, msg

    # ---- Validate

    async def _validate(self, tool: str, a: dict[str, Any], obs: Observation) -> ActionPlan | str:
        ref = str(a.get("ref", ""))
        el = next((e for e in obs.elements if e.ref == ref), None)
        if el is None:
            return f"error: ref {ref} is not in the latest observation"
        if tool in ROLE_FOR and el.role not in ROLE_FOR[tool]:
            return f"error: {tool} needs a {'/'.join(sorted(ROLE_FOR[tool]))}, but {ref} is a {el.role}"
        if not el.enabled:
            return f"error: {ref} is disabled"
        intent = str(a.get("intent", "")).strip()[:200] or tool
        declared = RiskClass.irreversible if a.get("commits") else None
        decision = self.s.guard.check(tool, el.name or el.text, declared=declared)
        if not decision.allowed:
            self.log.event("policy_blocked_action", tool=tool, target=el.name, reason=decision.reason)
            return f"blocked by policy: {decision.reason}"
        plan = ActionPlan(tool, el, await self.s.surface.resolve_ref(ref), intent, decision.risk)
        if tool in ("fill", "select"):
            bound = self._bind(a.get("param"), a.get("value") if tool == "fill" else a.get("option"))
            if isinstance(bound, str):
                return bound
            plan.value, plan.value_ref = bound
            if tool == "select" and el.options and plan.value not in el.options:
                return f"error: {self.symbols.text(plan.value)!s} is not an option of {ref}"
        if tool == "extract":
            try:
                plan.vtype = ValueType(a.get("type", "string"))
            except ValueError:
                return "error: type must be string, decimal or integer"
            plan.output = re.sub(r"[^a-z0-9_]", "_", str(a.get("output", "")).lower()).strip("_")
            if not plan.output or not re.match(r"^[a-z]", plan.output):
                return "error: output must be a snake_case name"
            plan.description = str(a.get("description", ""))
        return plan

    def _bind(self, param: Any, literal: Any) -> tuple[str, ParamRef | LiteralValue] | str:
        """Symbolic param -> real value, resolved by the host. Literals are allowed only
        for non-sensitive text the caller did not supply."""
        if isinstance(literal, str) and (m := _SYMBOL.match(literal.strip())):
            param = m.group(1)
        if param:
            p = self.rec.param(str(param))
            if p is None:
                return f"error: unknown parameter {param!r}; declared: {[x.name for x in self.params]}"
            return p.value, ParamRef(param=p.name)
        if literal is None or str(literal) == "":
            return "error: give param=<name> (preferred) or a literal value"
        text = str(literal)
        if self.log.redactor.is_sensitive_text(text):
            return "rejected: that literal looks sensitive; caller data must be entered via param=<name>"
        return text, self.rec.value_ref(text)

    # ---- Approve (irreversible), Execute, Verify

    async def _approve(self, plan: ActionPlan) -> str | None:
        before = (await self.s.surface.observe(screenshot=False)).state_hash
        ref = plan.el.ref

        async def same_screen(h: Handback) -> HandbackVerdict:
            if h.decision != Decision.approve:
                return HandbackVerdict(True, "not-approved")
            now = (await self.s.surface.observe(screenshot=False)).state_hash
            try:
                await self.s.surface.resolve_ref(ref)
            except TargetUnresolved:
                return HandbackVerdict(False, "changed", "approved control is no longer on screen")
            if now != before:
                return HandbackVerdict(False, "changed", "the screen changed while awaiting approval")
            return HandbackVerdict(True, "unchanged-screen")

        r = await self._escalate(InterventionKind.approval, "IRREVERSIBLE_STEP",
                                 f"The agent wants to click '{plan.el.name}' ({plan.intent}). This commits a change.",
                                 verify_fn=same_screen)
        if r is None or r.decision != Decision.approve or not r.resumed:
            self.log.event("irreversible_denied", target=plan.el.name,
                           reason=(r.verification_detail if r else "no operator"))
            if r is not None and not r.resumed and r.decision != Decision.deny:
                raise _Stop(DiscoveryResult(status="aborted", reason=r.verification_detail, usage=self.usage))
            return (f"blocked: operator did not approve ({r.decision.value if r else 'no operator'}). "
                    "Do not retry this; finish or report_outcome if the goal cannot be met otherwise.")
        # The human approved this commit, so the app's own "are you sure?" is accepted and its
        # exact text recorded as the step's expected dialog.
        self.s.surface.expected_dialog = DialogExpectation(type="confirm", message_pattern=".*", accept=True)
        return None

    async def _perform(self, plan: ActionPlan) -> dict[str, Any]:
        sf = self.s.surface
        avoid = self.rec.avoid_values() + ([plan.value] if plan.value else [])
        target = await sf.ground(plan.el.ref, "act", avoid)      # grounded *before* the page changes
        before_urls = await self._frame_urls()
        before_state = (await sf.observe(screenshot=False)).state_hash
        dialogs_before = len(sf.dialog_log)
        try:
            if plan.tool == "click":
                await sf.click(plan.res, Actor.agent)
            elif plan.tool == "fill":
                await sf.fill(plan.res, plan.value or "", Actor.agent)
            else:
                await sf.select(plan.res, plan.value or "", Actor.agent)
        finally:
            await asyncio.sleep(0.3)
            await sf.settle()
            sf.expected_dialog = None
        new = sf.dialog_log[dialogs_before:]
        return {"target": target, "before_urls": before_urls, "after_urls": await self._frame_urls(),
                "before_state": before_state, "after_state": (await sf.observe(screenshot=False)).state_hash,
                "dialog": new[-1] if new else None}

    async def _verify(self, plan: ActionPlan, ex: dict[str, Any]) -> verify.Verdict:
        bad = await verify.detectors_after(self.s.surface, self.s.env.profile)
        if bad is not None:
            return bad
        if plan.tool in ("fill", "select"):
            try:
                res = await self.s.surface.try_resolve(ex["target"])   # through the *recorded* locator
            except TargetUnresolved:
                return verify.reject("the recorded locator no longer finds the control after the action")
            return await verify.readback(self.s.surface, res, plan.value or "",
                                         method="readback" if plan.tool == "fill" else "selected")
        v = await verify.transition(self.s.surface, base_url=self.s.env.tenant.base_url, params=self.rec.values,
                                    before_urls=ex["before_urls"], after_urls=ex["after_urls"],
                                    before_state=ex["before_state"], after_state=ex["after_state"],
                                    frame_paths=self._frame_paths(), dialog=ex["dialog"])
        if v.accept and plan.risk == RiskClass.irreversible and v.expect is None:
            return verify.reject("the commit produced no verifiable state change; it cannot be recorded "
                                 "(ask_human to confirm what happened)")
        return v

    async def _extract(self, plan: ActionPlan) -> str:
        assert plan.vtype is not None and plan.output is not None
        raw = await self.s.surface.read_text(plan.res)
        verdict = verify.parse_check(raw, plan.vtype)
        if not verdict.accept:
            return f"rejected: {verdict.message}"
        target = await self.s.surface.ground(plan.el.ref, "extract", self.rec.avoid_values())
        require = await self._identity_condition(plan.el.frame)      # read from the right record
        sid = self.rec.record_extract(target, output=plan.output, vtype=plan.vtype, description=plan.description,
                                      intent=plan.intent, require=require, verification=verdict.verification)
        self.log.redactor.register(raw, f"output:{plan.output}")
        self.log.event("recorded", step_id=sid, action="extract", strategies=[s.kind for s in target.strategies],
                       record_identity=require is not None)
        who = " (record identity checked)" if require is not None else ""
        return f"ok ({sid}): output {plan.output!r} recorded; value validated as {plan.vtype.value}{who}"

    # ================================================================== business assertions

    async def _value_conditions(self, frame: str, names: list[str]) -> dict[str, ValueCondition]:
        """For each param whose value is displayed in ``frame``, a verified ValueCondition."""
        obs = await self.s.surface.observe(screenshot=False)
        found: dict[str, ValueCondition] = {}
        for name in names:
            p = self.rec.param(name)
            if p is None or not p.value:
                continue
            for e in obs.elements:
                if e.frame != frame or e.role not in ("cell", "text"):
                    continue
                if re.sub(r"\s+", " ", e.text).strip().lower() != p.value.strip().lower():
                    continue
                try:
                    t = await self.s.surface.ground(e.ref, "extract", self.rec.avoid_values())
                except TargetUnresolved:
                    continue
                if not any(s.kind == "label" for s in t.strategies):
                    continue        # must be anchored on a label, not on layout alone
                cond = ValueCondition(target=t, equals=ParamRef(param=name))
                if await self.s.surface.check(cond, self.rec.values):
                    found[name] = cond
                    break
        return found

    async def _identity_condition(self, frame: str) -> ValueCondition | None:
        ids = [p.name for p in self.params if p.identifies_record]
        got = await self._value_conditions(frame, ids)
        return next(iter(got.values()), None)

    async def _invariants(self, frame: str) -> list[Invariant]:
        """Assertions on the final screen, each checked live: it shows the requested record
        and reflects the submitted values; string outputs keep their observed shape."""
        out = [Invariant(id=f"shows_{n}", description=f"final screen shows the requested {n}", when=c)
               for n, c in (await self._value_conditions(frame, [p.name for p in self.params])).items()]
        for o in self.rec.outputs:
            if o.type != ValueType.string:
                continue
            step = next(s for s in self.rec.steps if s.id == o.from_step)
            try:
                raw = await self.s.surface.read_text(await self.s.surface.try_resolve(step.target))
            except Exception:
                continue
            shape = verify.output_shape(raw.strip())
            if shape:
                out.append(Invariant(id=f"{o.name}_shape", description=f"{o.name} has the observed format",
                                     when=OutputCondition(output=o.name, pattern=shape)))
        return out

    async def _finish(self, a: dict[str, Any], obs: Observation) -> tuple[DiscoveryResult | None, str]:
        ref = str(a.get("checkpoint_ref", ""))
        el = next((e for e in obs.elements if e.ref == ref), None)
        if el is None:
            return None, "error: checkpoint_ref not in the latest observation"
        text = (el.text or el.name).strip()
        if not text or any(p.value and p.value in text for p in self.params) or \
                self.log.redactor.is_sensitive_text(text) or re.search(r"\d{3,}", text):
            return None, ("error: checkpoint text must be a stable screen title/label, not member data. "
                          "Pick a heading such as the page title.")
        frame = next((f for f in self.s.surface.frames() if self.s.surface.frame_label(f) == el.frame), None)
        if frame is None:
            return None, "error: checkpoint frame vanished"
        invariants = await self._invariants(el.frame)
        try:
            cap = self.rec.compile(
                cap_id=self.cap_id, version=store.next_version(self.cap_id), title=self.title,
                summary=str(a.get("summary", "")), goal_template=self.goal_template,
                product=self.s.env.tenant.product, recorded_version=self.version,
                entry_route=self.s.env.profile.entry_route, checkpoint_frame=self.s.surface.frame_path(frame),
                checkpoint_url=frame.url, checkpoint_text=text, invariants=invariants, run_id=self.log.run_id,
                tenant=self.s.env.tenant.id, model=self.llm.model)
        except ValidationError as e:
            msg = "; ".join(err["msg"] for err in e.errors())[:400]
            self.log.event("compile_rejected", reason=msg)
            return None, (f"error: the capability cannot be proven correct from this screen: {msg}. "
                          "Reach a screen that displays the requested record, or ask_human.")
        try:
            path = store.save(cap, self.log.redactor)
        except store.UnsafeArtifact as e:
            self.log.event("artifact_rejected", reason=str(e))
            return DiscoveryResult(status="gave_up", reason=str(e), usage=self.usage), "rejected"
        self.log.write_json("artifact.json", cap)
        self.log.event("artifact_saved", path=str(path), id=cap.id, version=cap.version,
                       content_hash=cap.content_hash(), steps=len(cap.steps), inputs=[p.name for p in cap.inputs],
                       outputs=[o.name for o in cap.outputs], invariants=[i.id for i in cap.invariants],
                       review_required=cap.review_required_steps, side_effects=cap.side_effects.value)
        return DiscoveryResult(status="recorded", artifact_path=str(path), capability_id=cap.id,
                               version=cap.version, steps_taken=len(cap.steps), usage=self.usage), "ok"

    # ================================================================== detectors, stuck, handoff

    async def _auto_detectors(self) -> str:
        """Product detectors run in discovery too, so recordings stay clean:
        interstitials are dismissed (not recorded as steps), expired sessions
        re-login and restart. Business outcomes are only *reported* to the model."""
        notes = []
        for _ in range(3):
            hit = await detect.first_hit(self.s.env.profile.detectors, self.s.surface, {}, None)
            if hit is None:
                break
            d = hit.detector
            self.log.event("detector_hit", detector=d.id, code=d.code, classification=d.classification.value)
            if d.classification == OutcomeClass.recoverable and isinstance(d.recovery, DismissRecovery):
                try:
                    await self.s.surface.click(await self.s.surface.try_resolve(d.recovery.target), Actor.agent)
                    notes.append(f"(harness dismissed a known '{d.code}' notice)")
                    await asyncio.sleep(0.2)
                    continue
                except Exception:
                    notes.append(f"Notice: {d.code} present and could not be dismissed automatically.")
                    break
            if d.classification == OutcomeClass.recoverable and isinstance(d.recovery, RestartSessionRecovery):
                await self.s.login()
                await self._goto_entry()
                self.rec.reset()
                notes.append("(session expired; harness signed on again and returned to the start — "
                             "redo the task from the beginning)")
                continue
            if d.classification == OutcomeClass.recoverable and isinstance(d.recovery, RestartFlowRecovery):
                await asyncio.sleep(d.recovery.backoff_ms / 1000)
                await self._goto_entry()
                self.rec.reset()
                notes.append(f"(transient {d.code}; harness returned to the start — redo the task)")
                continue
            if d.classification == OutcomeClass.requires_human:
                notes.append(f"{d.code}: {d.description} Only a human can clear this — call ask_human.")
                break
            notes.append(f"Detector notice: {d.code} — {hit.message or d.description}")
            break
        return " ".join(notes)

    async def _stuck(self, reason: str, turn: int) -> DiscoveryResult | None:
        self.log.event("stuck", reason=reason, turn=turn)
        if not self.escalate_when_stuck:
            return DiscoveryResult(status="gave_up", reason=reason, usage=self.usage)
        res = await self._escalate(InterventionKind.stuck, "STUCK", reason)
        if res is None or not res.resumed:
            return DiscoveryResult(status="gave_up", reason=res.verification_detail if res else reason,
                                   usage=self.usage)
        self._carry = self._handback_note(res) + " "
        return None

    async def _handback_ok(self, h: Handback) -> HandbackVerdict:
        """Discovery has no fixed plan to resume, so the proof is: still signed on, and
        any operator action outside the grounded console marks the recording for review."""
        if not await self.s.surface.check(self.s.env.profile.authenticated, {}):
            return HandbackVerdict(False, "signed-out", "the session is signed out; sign on again before resuming",
                                   recoverable=True)
        hit = await detect.first_hit(self.s.env.profile.detectors, self.s.surface, {}, None)
        if hit and hit.detector.classification == OutcomeClass.requires_human:
            return HandbackVerdict(False, "blocked", f"{hit.detector.code} is still present", recoverable=True)
        if any(x.get("source") == "browser" for x in h.human_actions):
            self.rec.mark_gap("operator acted in the browser window (ungrounded); path continuity unverified")
        return HandbackVerdict(True, "authenticated", "model re-observes before acting")

    async def _escalate(self, kind: InterventionKind, code: str, reason: str, verify_fn: Any = None):
        if not self.escalate_when_stuck and kind != InterventionKind.approval:
            return None
        shot = self.log.save_png(f"escalation-{code.lower()}", await self.s.surface.screenshot())
        self.s.control.on_human_step = self._record_human_step
        try:
            return await self.s.control.escalate(
                run_id=self.log.run_id, kind=kind, subject=f"discovery: {self.goal_template}", code=code,
                reason=reason, screenshot=shot, verify=verify_fn or self._handback_ok,
                frames={k: routes.relative(v, self.s.env.tenant.base_url)[0]
                        for k, v in (await self._frame_urls()).items()})
        finally:
            self.s.control.on_human_step = None

    async def _record_human_step(self, action: str, target: Target, value: str | None, before: dict,
                                 after: dict) -> None:
        """Operator console actions are verified the same way before entering the artifact."""
        if action in ("fill", "select"):
            try:
                v = await verify.readback(self.s.surface, await self.s.surface.try_resolve(target), value or "",
                                          method="readback" if action == "fill" else "selected")
            except TargetUnresolved:
                v = verify.reject("control not found after operator action")
            if not v.accept:
                v = verify.Verdict(True, v.message, None, Verification(method="unverified", review_required=True,
                                                                       note=f"operator {action}: {v.message}"))
        else:
            v = await verify.transition(self.s.surface, base_url=self.s.env.tenant.base_url, params=self.rec.values,
                                        before_urls=before, after_urls=after, before_state="", after_state="",
                                        frame_paths=self._frame_paths(), dialog=None, human=True)
        what = target.fingerprint.name or target.fingerprint.neighbor_text or ""
        sid = self.rec.record(action, target, intent=f"(operator) {action} {what!r}".strip(), risk=RiskClass.read,
                              expect=v.expect, verification=v.verification,
                              value_ref=self.rec.value_ref(value) if value is not None else None, provenance="human")
        self.log.event("recorded", step_id=sid, action=action, provenance="human", verification=v.verification)

    def _handback_note(self, res: Any) -> str:
        acts = "; ".join(f"{h.get('kind') or h.get('type')} {h.get('target') or h.get('name') or ''}".strip()
                         for h in res.human_actions) or "nothing"
        return (f"A human operator held the session and handed it back (decision: {res.decision.value}). "
                f"They did: {acts}. Note: {res.note or '-'}. Re-read the screen before acting.")

    # ================================================================== rendering

    def _render(self, obs: Observation, note: str, turn: int) -> str:
        base = self.s.env.tenant.base_url
        # The goal carries only symbols and values the policy explicitly exposed; everything
        # read off the screen goes through model_text (symbolized, then redacted).
        head = f"GOAL: {self.goal}\nTurn {turn}/{self.limits.max_steps}.\n\n"
        lines = ["FRAMES:"]
        for f in obs.frames:
            path, query = routes.relative(f.url, base)
            lines.append(f"  [{f.path}] {path}{'?' + query if query else ''}")
        lines.append("ELEMENTS (ref role \"name\" ...):")
        cur = None
        for e in obs.elements:
            if e.frame != cur:
                cur = e.frame
                lines.append(f"  -- frame {cur}")
            lines.append("  " + e.brief())
        if obs.dialogs:
            lines.append(f"DIALOGS so far: {obs.dialogs[-3:]}")
        if note:
            lines.append(f"NOTICE: {note}")
        recorded = [f"{s.id}:{s.action}" for s in self.rec.steps]
        lines.append(f"RECORDED SO FAR: {recorded or 'nothing'}")
        return head + self.model_text("\n".join(lines))

    def _push_user(self, results: list[dict[str, Any]], text: str, png: bytes | None) -> None:
        # Bound context: only the latest screenshot stays in the conversation.
        for m in self.messages:
            if m["role"] == "user":
                m["content"] = [c if c.get("type") != "image" else {"type": "text", "text": "[older screenshot omitted]"}
                                for c in m["content"]]
        content: list[dict[str, Any]] = list(results) + [{"type": "text", "text": text}]
        if png:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(png).decode()}})
        self.messages.append({"role": "user", "content": content})

    def _account(self, t: LLMTurn) -> None:
        for k, v in t.usage.items():
            self.usage[k] = self.usage.get(k, 0) + v

    async def _frame_urls(self) -> dict[str, str]:
        return {self.s.surface.frame_label(f): f.url for f in self.s.surface.frames()}

    def _frame_paths(self) -> dict[str, list]:
        return {self.s.surface.frame_label(f): self.s.surface.frame_path(f) for f in self.s.surface.frames()}

    async def _goto_entry(self) -> None:
        await self.s.surface.goto(self.s.url(self.s.env.profile.entry_route), Actor.agent)
        await self.s.surface.settle()


class _Stop(Exception):
    def __init__(self, result: DiscoveryResult) -> None:
        self.result = result


def parse_param(spec: str) -> DeclaredParam:
    """name[:type[:sensitivity[:flags]]]=value, flags joined by '+':
    ``id`` (identifies the record), ``expose`` (show the value to the model, subject to policy).
    e.g. member_id:string:pii:id=100234"""
    from ..schema.artifact import Sensitivity
    left, _, value = spec.partition("=")
    parts = left.split(":")
    p = DeclaredParam(name=parts[0], value=value)
    if len(parts) > 1 and parts[1]:
        p.type = ValueType(parts[1])
    if len(parts) > 2 and parts[2]:
        p.sensitivity = Sensitivity(parts[2])
    flags = set(parts[3].split("+")) if len(parts) > 3 and parts[3] else set()
    unknown = flags - {"id", "expose"}
    if unknown:
        raise ValueError(f"unknown param flags {sorted(unknown)}")
    p.identifies_record, p.expose = "id" in flags, "expose" in flags
    return p


def dumps(o: Any) -> str:
    return json.dumps(o, default=str, indent=2)
