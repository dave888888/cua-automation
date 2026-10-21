"""Recorder: compiles *verified* discovery steps into a capability artifact.

The recorder never sees the model transcript, and it never infers anything:
the agent hands it steps that already passed the Verify stage (verify.py),
together with the postcondition that was observed to hold and a
``Verification`` record. What it adds is purely mechanical:

* values: a value equal to a declared parameter becomes ``{{param}}``; a
  symbolic param from the model is kept as a ref; anything else is a literal
  and must pass the PII lint on save.
* repeated fills of the same field collapse to the last one.
* business assertions proposed by the agent (record identity on extract
  steps, invariants on the final screen) are attached as given — each was
  checked against the live app before being proposed.
* product version range from the version the run was recorded on
  (4.2.1 -> ">=4.2,<5"): verified there, widened only by review.
* a continuity gap (operator acted outside the grounded console) marks the
  next recorded step ``review_required``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from packaging.version import Version

from .. import routes
from ..schema.artifact import (AllCondition, AppBinding, Capability, ClickStep, Condition, DialogExpectation,
                               ExtractStep, FillStep, Invariant, LiteralValue, OutputSpec, ParamRef, ParamSpec,
                               Provenance, RiskClass, SelectStep, Sensitivity, Target, TextCondition, UrlCondition,
                               ValueType, Verification, utcnow)


@dataclass
class DeclaredParam:
    name: str
    value: str
    type: ValueType = ValueType.string
    sensitivity: Sensitivity = Sensitivity.internal
    description: str = ""
    identifies_record: bool = False
    expose: bool = False             # caller asked for the model to see the value (policy may refuse)


@dataclass
class Recorder:
    base_url: str
    params: list[DeclaredParam]
    steps: list[Any] = field(default_factory=list)
    outputs: list[OutputSpec] = field(default_factory=list)
    human_steps: int = 0
    gap_note: str | None = None

    # ---- helpers

    @property
    def values(self) -> dict[str, str]:
        return {p.name: p.value for p in self.params}

    def param(self, name: str) -> DeclaredParam | None:
        return next((p for p in self.params if p.name == name), None)

    def _next_id(self) -> str:
        return f"s{len(self.steps) + 1}"

    def avoid_values(self) -> list[str]:
        return [p.value for p in self.params if p.value]

    def value_ref(self, value: str, param: str | None = None) -> ParamRef | LiteralValue:
        if param and self.param(param):
            return ParamRef(param=param)
        for p in self.params:
            if value == p.value:
                return ParamRef(param=p.name)
        return LiteralValue(literal=value)

    def mark_gap(self, note: str) -> None:
        self.gap_note = note

    def _with_gap(self, v: Verification | None) -> Verification | None:
        if self.gap_note is None:
            return v
        note, self.gap_note = self.gap_note, None
        base = v or Verification(method="unverified")
        return base.model_copy(update={"review_required": True,
                                       "note": (base.note + "; " if base.note else "") + note})

    # ---- recording (verified steps only)

    def record(self, action: str, target: Target, *, intent: str, risk: RiskClass, expect: Condition | None,
               verification: Verification | None, value_ref: ParamRef | LiteralValue | None = None,
               dialog: dict | None = None, provenance: str = "agent") -> str:
        sid = self._next_id()
        common = dict(id=sid, intent=intent, risk=risk, expect=expect, provenance=provenance,
                      verification=self._with_gap(verification))
        if action == "click":
            dlg = None
            if dialog and dialog.get("handled") in ("accepted", "dismissed"):
                dlg = DialogExpectation(type=dialog["type"], message_pattern="^" + re.escape(dialog["message"]) + "$",
                                        accept=dialog.get("handled") == "accepted")
            step = ClickStep(target=target, dialog=dlg, **common)
        elif action in ("fill", "select"):
            assert value_ref is not None
            if self.steps and getattr(self.steps[-1], "target", None) == target and self.steps[-1].action == action:
                self.steps.pop()      # collapse repeated fills on the same control
                sid = self._next_id()
                common["id"] = sid
            cls = FillStep if action == "fill" else SelectStep
            step = cls(target=target, value=value_ref, **common)
        else:
            raise ValueError(action)
        self.steps.append(step)
        if provenance == "human":
            self.human_steps += 1
        return sid

    def record_extract(self, target: Target, *, output: str, vtype: ValueType, description: str, intent: str,
                       require: Condition | None, verification: Verification) -> str:
        sid = self._next_id()
        name = re.sub(r"[^a-z0-9_]", "_", output.lower()).strip("_")
        self.steps.append(ExtractStep(id=sid, intent=intent, target=target, output=name, parse=vtype,
                                      require=require, verification=self._with_gap(verification)))
        sens = Sensitivity.financial if vtype == ValueType.decimal else Sensitivity.internal
        self.outputs = [o for o in self.outputs if o.name != name]
        self.outputs.append(OutputSpec(name=name, type=vtype, description=description, sensitivity=sens,
                                       from_step=sid))
        return sid

    def reset(self) -> None:
        """Session was restarted mid-discovery: the agent will redo the flow."""
        self.steps.clear()
        self.outputs.clear()

    # ---- compile

    def compile(self, *, cap_id: str, version: str, title: str, summary: str, goal_template: str,
                product: str, recorded_version: str | None, entry_route: str, checkpoint_frame: list,
                checkpoint_url: str, checkpoint_text: str, invariants: list[Invariant], run_id: str, tenant: str,
                model: str) -> Capability:
        v = Version(recorded_version) if recorded_version else None
        versions = f">={v.major}.{v.minor},<{v.major + 1}" if v else ">=0"
        used = {s.value.param for s in self.steps if isinstance(getattr(s, "value", None), ParamRef)}
        for c in [*(inv.when for inv in invariants), *(s.require for s in self.steps if s.require)]:
            ref = getattr(c, "equals", None)
            if isinstance(ref, ParamRef):
                used.add(ref.param)
        inputs = []
        for p in self.params:
            if p.name not in used:
                continue
            pattern = r"^\d+$" if p.value.isdigit() else None
            inputs.append(ParamSpec(name=p.name, type=p.type, description=p.description, sensitivity=p.sensitivity,
                                    pattern=pattern, identifies_record=p.identifies_record,
                                    example=p.value if p.sensitivity == Sensitivity.public else None))
        success = AllCondition(conditions=[
            UrlCondition(frame=checkpoint_frame, route=routes.canonicalize(checkpoint_url, self.base_url, self.values)),
            TextCondition(frame=checkpoint_frame, pattern=re.escape(checkpoint_text)),
        ])
        return Capability(
            id=cap_id, version=version, title=title, description=summary,
            app=AppBinding(product=product, versions=versions, surface="legacy_web"),
            inputs=inputs, outputs=self.outputs, entry_route=entry_route, steps=self.steps, success=success,
            invariants=invariants,
            provenance=Provenance(discovery_run_id=run_id, recorded_on_tenant=tenant,
                                  recorded_on_version=recorded_version, recorded_at=utcnow(), model=model,
                                  goal_template=goal_template, human_steps=self.human_steps))
