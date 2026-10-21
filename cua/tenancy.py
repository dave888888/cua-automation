"""Cross-tenant reuse: specialise one recorded capability for a tenant.

An artifact is recorded once against a *vendor product*. A tenant running the
same product supplies an overlay; ``specialize`` produces the effective
capability for that tenant and a list of what was changed, so a reviewer can
see exactly how tenant B differs from the recording.

Overlay precedence (most specific wins):
    tenant.step_overrides[capability][step]  >  tenant.label_aliases  >  recorded

Text in a strategy is compared ignoring case and a trailing ':' so one alias
("Member ID" -> "Member Number") covers both the label cell "Member ID:" and a
button named "Member ID". Fallback strategies (css/xpath) are never rewritten:
they are what still works when an alias is missing, and they are where drift
shows up first.
"""
from __future__ import annotations

from typing import Any

from .schema.artifact import (Capability, Detector, DismissRecovery, LabelStrategy, RoleStrategy, Target,
                              TextCondition, TextStrategy)
from .schema.config import AppProfile, TenantConfig


def _key(s: str) -> str:
    return s.strip().rstrip(":").strip().lower()


def _alias(text: str, aliases: dict[str, str]) -> str | None:
    k = _key(text)
    for src, dst in aliases.items():
        if _key(src) == k:
            return dst + (":" if text.strip().endswith(":") and not dst.endswith(":") else "")
    return None


def _apply_target(t: Target, aliases: dict[str, str]) -> tuple[Target, list[str]]:
    changes: list[str] = []
    out = []
    for s in t.strategies:
        if isinstance(s, RoleStrategy) and (new := _alias(s.name, aliases)):
            changes.append(f"role name '{s.name}' -> '{new}'")
            s = s.model_copy(update={"name": new})
        elif isinstance(s, LabelStrategy) and (new := _alias(s.anchor, aliases)):
            changes.append(f"label anchor '{s.anchor}' -> '{new}'")
            s = s.model_copy(update={"anchor": new})
        elif isinstance(s, TextStrategy) and (new := _alias(s.text, aliases)):
            changes.append(f"text '{s.text}' -> '{new}'")
            s = s.model_copy(update={"text": new})
        out.append(s)
    return t.model_copy(update={"strategies": out}), changes


class OverlayViolation(ValueError):
    """A tenant overlay tried to change more than *how controls are found*."""


def _alias_targets(node: Any, aliases: dict[str, str], where: str, notes: list[str]) -> Any:
    """Rewrite every Target (step targets *and* targets inside conditions) in a dumped capability."""
    if isinstance(node, dict):
        if "strategies" in node and isinstance(node["strategies"], list):
            t, ch = _apply_target(Target.model_validate(node), aliases)
            notes.extend(f"{where}: {c}" for c in ch)
            return t.model_dump(mode="json")
        return {k: _alias_targets(v, aliases, where, notes) for k, v in node.items()}
    if isinstance(node, list):
        return [_alias_targets(v, aliases, where, notes) for v in node]
    return node


def specialize(cap: Capability, tenant: TenantConfig, *, use_overrides: bool = True) -> tuple[Capability, list[str]]:
    """Effective capability for a tenant. The result is re-validated against the
    schema and checked by ``check_overlay_preserves``: an overlay may change how
    controls are *found*, never what the workflow does or asserts."""
    if not use_overrides:
        return cap, []
    notes: list[str] = []
    data = cap.model_dump(mode="json")
    overrides = tenant.step_overrides.get(cap.id, {})
    for i, step in enumerate(data["steps"]):
        sid = step["id"]
        if tenant.label_aliases:
            step = _alias_targets(step, tenant.label_aliases, sid, notes)
        if sid in overrides and "target" in step:   # an explicit override wins for the step's own target
            step["target"] = overrides[sid].model_dump(mode="json")
            notes.append(f"{sid}: target replaced by tenant override")
        data["steps"][i] = step
    if tenant.label_aliases:
        data["success"] = _alias_targets(data["success"], tenant.label_aliases, "success", notes)
        data["invariants"] = _alias_targets(data["invariants"], tenant.label_aliases, "invariants", notes)
    eff = Capability.model_validate(data)
    check_overlay_preserves(cap, eff)
    return eff, notes


def _shape(node: Any) -> Any:
    """Everything except how controls are located (strategies, fingerprints, frame paths)."""
    if isinstance(node, dict):
        if "strategies" in node:
            return {"target": True, "sensitive": node.get("sensitive", False)}
        return {k: _shape(v) for k, v in node.items() if k not in ("review", "provenance", "status")}
    if isinstance(node, list):
        return [_shape(v) for v in node]
    return node


def check_overlay_preserves(base: Capability, eff: Capability) -> None:
    """Approved workflow invariants survive specialisation: same steps, actions, risk,
    values, dialogs, pre/postconditions, success checkpoint and invariants."""
    a, b = _shape(base.model_dump(mode="json")), _shape(eff.model_dump(mode="json"))
    if a != b:
        diff = [k for k in a if a.get(k) != b.get(k)]
        raise OverlayViolation(f"tenant overlay changes workflow semantics in {diff}")


def effective_detectors(cap: Capability, profile: AppProfile, tenant: TenantConfig) -> list[Detector]:
    """Capability-specific detectors first (more specific), then the product's."""
    out: list[Detector] = []
    for d in [*cap.outcomes, *profile.detectors]:
        if d.id in tenant.detector_overrides and isinstance(d.when, TextCondition):
            d = d.model_copy(update={"when": d.when.model_copy(update={"pattern": tenant.detector_overrides[d.id]})})
        if isinstance(d.recovery, DismissRecovery) and tenant.label_aliases:
            t, _ = _apply_target(d.recovery.target, tenant.label_aliases)
            d = d.model_copy(update={"recovery": d.recovery.model_copy(update={"target": t})})
        out.append(d)
    return out


def describe_overlay(notes: list[str]) -> dict[str, Any]:
    return {"overrides_applied": len(notes), "details": notes}
