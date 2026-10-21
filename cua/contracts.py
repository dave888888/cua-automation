"""Workflow contracts: reviewed constraints on what a capability may be.

A contract is trusted configuration (``config/contracts/<product>.yaml``),
matched to a capability by id glob — first match wins, and the file ends with
a catch-all, so no capability is uncontracted. ``check`` is a plain function
over the typed artifact: no code from the model, no expression language.

Enforced:
* at approval (``store.approve``): a violating capability cannot be approved;
* at replay pre-flight, on the recorded artifact *and* on the tenant-effective
  one, so a tenant overlay cannot produce a capability the contract forbids;
* for unattended use (no operator attached / agent API).
"""
from __future__ import annotations

import fnmatch
from functools import lru_cache
from pathlib import Path

import yaml

from .config import CONFIG_DIR
from .schema.artifact import (RISK_ORDER, Capability, ExtractStep, NavigateStep, PointStrategy, RiskClass,
                              UrlCondition, iter_conditions, must_hold)
from .schema.config import ContractSet, WorkflowContract


@lru_cache(maxsize=None)
def _load(product: str, root: str) -> ContractSet | None:
    p = Path(root) / "contracts" / f"{product}.yaml"
    if not p.exists():
        return None
    return ContractSet.model_validate(yaml.safe_load(p.read_text()))


DEFAULT = WorkflowContract(id="builtin.default", applies_to="*", description="No contract configured: read-only, "
                           "identity must be bound, no unverified steps.")


def contract_for(cap: Capability, root: Path | None = None) -> WorkflowContract:
    cs = _load(cap.app.product, str(root or CONFIG_DIR))
    if cs is not None:
        for c in cs.contracts:
            if fnmatch.fnmatchcase(cap.id, c.applies_to):
                return c
    return DEFAULT


def _route_path(route: str) -> str:
    return route.split("?", 1)[0]


def _routes(cap: Capability) -> list[str]:
    out = [cap.entry_route]
    conds = cap.all_conditions()
    out += [c.route for c in conds if isinstance(c, UrlCondition)]
    out += [s.route for s in cap.steps if isinstance(s, NavigateStep)]
    return [_route_path(r) for r in out]


def _displayed_by_invariants(cap: Capability) -> set[str]:
    shown: set[str] = set()
    for inv in cap.invariants:
        for leaf in must_hold(inv.when):
            ref = getattr(leaf, "equals", None)
            if leaf.kind == "value" and ref is not None and hasattr(ref, "param"):
                shown.add(ref.param)
    return shown


def check(cap: Capability, contract: WorkflowContract | None = None, *, approved: bool | None = None) -> list[str]:
    """All the ways ``cap`` violates its contract (empty list = compliant).

    ``approved`` overrides ``cap.is_approved`` — at approval time (True) and for a
    tenant-effective artifact, whose approval is the recorded artifact's."""
    c = contract or contract_for(cap)
    v: list[str] = []
    if RISK_ORDER[cap.side_effects] > RISK_ORDER[c.max_side_effects]:
        v.append(f"side effects {cap.side_effects.value} exceed {c.max_side_effects.value}")
    n_irrev = sum(1 for s in cap.steps if s.risk == RiskClass.irreversible)
    if n_irrev > c.max_irreversible_steps:
        v.append(f"{n_irrev} irreversible steps (max {c.max_irreversible_steps})")
    bad_actions = sorted({s.action for s in cap.steps} - set(c.allowed_actions))
    if bad_actions:
        v.append(f"actions {bad_actions} not permitted")
    for r in _routes(cap):
        templ = r.replace("{{", "").replace("}}", "")
        if not any(fnmatch.fnmatchcase(r, g) or fnmatch.fnmatchcase(templ, g) for g in c.route_scope):
            v.append(f"route {r} outside contract scope {c.route_scope}")
    inputs = {p.name: p for p in cap.inputs}
    for name in c.required_inputs:
        if name not in inputs:
            v.append(f"required input {name!r} missing")
    for name in c.identity_inputs:
        if name not in inputs or not inputs[name].identifies_record:
            v.append(f"input {name!r} must be declared identifies_record")
    outputs = {o.name: o for o in cap.outputs}
    for name, rule in c.outputs.items():
        o = outputs.get(name)
        if o is None:
            v.append(f"required output {name!r} missing")
            continue
        if o.type != rule.type:
            v.append(f"output {name!r} is {o.type.value}, contract requires {rule.type.value}")
        step = next((s for s in cap.steps if s.id == o.from_step), None)
        if isinstance(step, ExtractStep) and step.pattern and not rule.allow_pattern:
            v.append(f"output {name!r} uses a regex transformation the contract does not allow")
    shown, bound = cap.identity_binding()
    ids = [p.name for p in cap.inputs if p.identifies_record]
    if c.checkpoint == "identity_bound":
        v += [f"record identity {n!r} not asserted" for n in ids if n not in bound]
    elif c.checkpoint == "identity_displayed":
        v += [f"record identity {n!r} must be proven by a displayed value, not only by the route"
              for n in ids if n not in shown]
    for name in c.must_confirm:
        if name not in _displayed_by_invariants(cap):
            v.append(f"final screen is not asserted to show {name!r} (confirmation must reflect the operation)")
    for s in cap.steps:
        for leaf in iter_conditions(s.expect):
            if isinstance(leaf, UrlCondition) and s.risk != RiskClass.irreversible and any(
                    fnmatch.fnmatchcase(_route_path(leaf.route).replace("{{", "").replace("}}", ""), g) or
                    fnmatch.fnmatchcase(_route_path(leaf.route), g) for g in c.irreversible_routes):
                v.append(f"{s.id} reaches {leaf.route} but is not classed irreversible")
    if cap.review_required_steps and not c.allow_review_required:
        v.append(f"steps {cap.review_required_steps} were not verified during discovery and need review")
    if c.approval_required and not (cap.is_approved if approved is None else approved):
        v.append("contract requires an approved capability")
    return v


def unattended_ok(cap: Capability, contract: WorkflowContract | None = None) -> tuple[bool, str]:
    c = contract or contract_for(cap)
    if not c.unattended:
        return False, f"contract {c.id} requires an operator to be attached"
    if cap.review_required_steps:
        return False, f"unverified steps {cap.review_required_steps}"
    if any(isinstance(s, PointStrategy) for st in cap.steps for s in getattr(getattr(st, "target", None),
                                                                             "strategies", [])):
        return False, "coordinate-only locators"
    return True, ""
