"""Condition evaluation against one consistent snapshot, and detector matching.

Text conditions are evaluated against a single visible-text snapshot per poll
(one round-trip per frame) instead of one per detector, which keeps polling
cheap enough to run between every resolution attempt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from typing import Any

from . import values
from .schema.artifact import (AllCondition, AnyCondition, Condition, Detector, LiteralValue, OutputCondition,
                              ParamRef, TextCondition, ValueCondition, UrlCondition)
from .surface.web import WebSurface


@dataclass
class Snapshot:
    texts: dict[str, str]           # frame label -> visible text

    def text(self, frame: object) -> str:
        if frame == "*":
            return "\n".join(self.texts.values())
        if isinstance(frame, list):
            key = frame[-1].name if frame and frame[-1].name else ("top" if not frame else None)
            if key and key in self.texts:
                return self.texts[key]
        return ""


async def snapshot(surface: WebSurface) -> Snapshot:
    return Snapshot(await surface.text_by_frame())


def ref_value(ref: Any, params: dict[str, str]) -> str | None:
    if isinstance(ref, ParamRef):
        return params.get(ref.param)
    if isinstance(ref, LiteralValue):
        return ref.literal
    return None


async def holds(cond: Condition, surface: WebSurface, params: dict[str, str], snap: Snapshot,
                outputs: dict[str, Any] | None = None) -> bool:
    if isinstance(cond, TextCondition):
        return re.search(cond.pattern, snap.text(cond.frame), re.I) is not None
    if isinstance(cond, OutputCondition):
        if outputs is None or cond.output not in outputs:
            return False
        return values.matches(outputs[cond.output], equals=ref_value(cond.equals, params),
                              pattern=cond.pattern, how=cond.normalize)
    if isinstance(cond, AllCondition):
        return all([await holds(c, surface, params, snap, outputs) for c in cond.conditions])
    if isinstance(cond, AnyCondition):
        return any([await holds(c, surface, params, snap, outputs) for c in cond.conditions])
    return await surface.check(cond, params)


async def first_failing(cond: Condition, surface: WebSurface, params: dict[str, str], snap: Snapshot,
                        outputs: dict[str, Any] | None = None) -> str | None:
    """Describe the first required leaf that does not hold — *without* the displayed
    value (it may be another customer's data). None if the condition holds."""
    if isinstance(cond, AllCondition):
        for c in cond.conditions:
            why = await first_failing(c, surface, params, snap, outputs)
            if why:
                return why
        return None
    if await holds(cond, surface, params, snap, outputs):
        return None
    return describe(cond)


async def contradicted(cond: Condition, surface: WebSurface, params: dict[str, str]) -> bool:
    """True if a required value assertion is *answered wrongly* (the control is there and
    shows something else) rather than merely not yet visible. Lets callers fail fast."""
    from .schema.artifact import must_hold
    for leaf in must_hold(cond):
        if isinstance(leaf, ValueCondition):
            try:
                await surface.try_resolve(leaf.target)
            except Exception:
                continue
            if not await surface.check(leaf, params):
                return True
    return False


def describe(c: Any) -> str:
    if isinstance(c, ValueCondition):
        anchor = next((getattr(s, "anchor", None) or getattr(s, "name", None) or getattr(s, "text", None)
                       for s in c.target.strategies if getattr(s, "kind", "") in ("label", "role", "text")), "control")
        want = f"{{{{{c.equals.param}}}}}" if isinstance(c.equals, ParamRef) else (
            repr(c.equals.literal) if isinstance(c.equals, LiteralValue) else f"/{c.pattern}/")
        return f"value at '{anchor}' does not show {want}"
    if isinstance(c, OutputCondition):
        return f"output {c.output!r} does not satisfy {c.equals or c.pattern}"
    if isinstance(c, UrlCondition):
        return f"frame {[f.name for f in c.frame]} is not at {c.route}"
    if isinstance(c, TextCondition):
        return f"text /{c.pattern}/ not visible"
    if isinstance(c, AnyCondition):
        return "none of the alternatives holds"
    return f"{c.kind} condition false"


@dataclass
class Hit:
    detector: Detector
    message: str | None


async def first_hit(detectors: list[Detector], surface: WebSurface, params: dict[str, str],
                    step_id: str | None, snap: Snapshot | None = None) -> Hit | None:
    snap = snap or await snapshot(surface)
    for d in detectors:
        if d.steps is not None and step_id not in d.steps:
            continue
        if await holds(d.when, surface, params, snap):
            msg = None
            if d.message_pattern:
                m = re.search(d.message_pattern, snap.text("*"))
                msg = m.group(1).strip() if m else None
            return Hit(d, msg)
    return None
