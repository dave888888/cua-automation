"""Where may automation safely resume after a human hand-back?

Pure function, no I/O: the engine evaluates each step's postcondition against
the live app and passes the facts in. The answer is one of:

* ``done``      — the success checkpoint (and every declared output) holds.
* ``continue``  — resume at step index ``k``. Either step i's own
                  postcondition holds (the human did step i), or the latest
                  earlier step j whose postcondition holds proves we are
                  exactly at the state before step j+1.
* ``restart``   — no step postcondition holds, but nothing irreversible was
                  attempted, so the entry point is a safe checkpoint by
                  construction.
* ``unsafe``    — nothing can be proven. Automation must not resume.

The irreversibility rule: a step that was *attempted* (committed or not) is
never re-executed. Resuming at k is refused if any attempted irreversible step
lies in [k, i]; restart is refused if any irreversible step was attempted at
all. A step whose postcondition holds is never re-run either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StepFacts:
    step_id: str
    has_expect: bool
    expect_holds: bool
    is_extract: bool
    irreversible_attempted: bool


@dataclass(frozen=True)
class ResumePoint:
    kind: Literal["done", "continue", "restart", "unsafe"]
    index: int | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind != "unsafe"

    @property
    def checkpoint(self) -> str:
        return self.kind if self.index is None else f"{self.kind}@{self.index}"


def resume_point(failed_index: int, steps: list[StepFacts], *, success_holds: bool,
                 outputs_complete: bool) -> ResumePoint:
    i = failed_index
    any_irrev = any(s.irreversible_attempted for s in steps)
    if success_holds and outputs_complete:
        return ResumePoint("done", reason="success checkpoint holds")
    s = steps[i]
    if s.has_expect and s.expect_holds and not s.is_extract:
        return ResumePoint("continue", i + 1, f"{s.step_id} postcondition holds (step completed)")
    for j in range(i - 1, -1, -1):
        p = steps[j]
        if p.has_expect and p.expect_holds:
            blocked = [x.step_id for x in steps[j + 1:i + 1] if x.irreversible_attempted]
            if blocked:
                return ResumePoint("unsafe", reason=f"resuming after {p.step_id} would repeat attempted "
                                                    f"irreversible step(s) {blocked}")
            return ResumePoint("continue", j + 1, f"{p.step_id} postcondition holds")
    if not any_irrev:
        return ResumePoint("restart", 0, "no postcondition holds; flow is read-only so far")
    return ResumePoint("unsafe", reason="no postcondition holds after an irreversible step was attempted")
