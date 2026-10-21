"""Deterministic LLM stand-in for tests.

It answers through exactly the same tool interface as the real model, but its
"decisions" are a fixed script of element lookups. It exists so the loop,
recorder, guardrails and replay can be tested offline and in CI.

It is NOT evidence of discovery: runs it produces are labelled with
model="scripted-test-double" in the log and in the artifact provenance.
"""
from __future__ import annotations

from typing import Any, Callable

from ..surface.base import Observation
from .llm import LLMTurn, ToolCall

Step = Callable[[Observation], tuple[str, dict[str, Any]]]


def find(obs: Observation, *, role: str | None = None, name: str | None = None, frame: str | None = None,
         text: str | None = None, label: str | None = None) -> str:
    for e in obs.elements:
        if role and e.role != role:
            continue
        if frame and e.frame != frame:
            continue
        if name is not None and e.name.strip().rstrip(":").lower() != name.lower():
            continue
        if text is not None and text.lower() not in e.text.lower():
            continue
        if label is not None and e.label.strip().rstrip(":").lower() != label.lower():
            continue
        return e.ref
    raise LookupError(f"scripted step could not find role={role} name={name} text={text} label={label}")


class ScriptedLLM:
    model = "scripted-test-double"

    def __init__(self, script: list[Step]) -> None:
        self.script = script
        self.i = 0
        self.seen: list[Any] = []      # every message history it was shown (for leak tests)

    async def decide(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     observation: Any = None) -> LLMTurn:
        self.seen.append(messages)
        if self.i >= len(self.script):
            name, inp = "ask_human", {"reason": "script exhausted", "intent": "test double ran out of steps"}
        else:
            name, inp = self.script[self.i](observation)
        self.i += 1
        call = ToolCall(f"tool_{self.i}", name, inp)
        return LLMTurn("", [call], [{"type": "tool_use", "id": call.id, "name": name, "input": inp}])


# --------------------------------------------------------------------------- canned scripts

def savings_balance_script() -> list[Step]:
    return [
        lambda o: ("click", {"ref": find(o, role="link", name="Member Search"), "intent": "Open member search"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Member ID", frame="main"),
                            "param": "member_id", "intent": "Enter the member ID"}),
        lambda o: ("click", {"ref": find(o, role="button", name="Search", frame="main"), "intent": "Run the search"}),
        lambda o: ("extract", {"ref": find(o, role="cell", label="Share Savings", frame="main"),
                               "output": "savings_balance", "type": "decimal",
                               "description": "Available balance of the member's share savings",
                               "intent": "Read the savings balance"}),
        lambda o: ("finish", {"checkpoint_ref": find(o, text="Member Detail", frame="main"),
                              "summary": "Looks up a member by ID and returns their share savings balance.",
                              "intent": "Goal reached"}),
    ]


def open_share_script(submit: bool = True) -> list[Step]:
    steps: list[Step] = [
        lambda o: ("click", {"ref": find(o, role="link", name="Member Search"), "intent": "Open member search"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Member ID", frame="main"),
                            "param": "member_id", "intent": "Enter the member ID"}),
        lambda o: ("click", {"ref": find(o, role="button", name="Search", frame="main"), "intent": "Search"}),
        lambda o: ("click", {"ref": find(o, role="link", name="Open Sub-Account", frame="main"),
                             "intent": "Start a new sub-account"}),
        lambda o: ("select", {"ref": find(o, role="combobox", frame="main"),
                              "param": "share_type", "intent": "Choose the share type"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Nickname", frame="main"),
                            "param": "nickname", "intent": "Name the share"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Opening Deposit", frame="main"),
                            "param": "opening_deposit", "intent": "Enter the opening deposit"}),
        lambda o: ("click", {"ref": find(o, role="button", name="Continue", frame="main"), "intent": "Review"}),
    ]
    if submit:
        steps += [
            lambda o: ("click", {"ref": find(o, role="button", name="Submit", frame="main"), "commits": True,
                                 "intent": "Post the new share"}),
            lambda o: ("extract", {"ref": find(o, role="cell", label="Confirmation No.", frame="main"),
                                   "output": "confirmation_number", "type": "string",
                                   "description": "Confirmation number of the new share",
                                   "intent": "Read the confirmation number"}),
            lambda o: ("finish", {"checkpoint_ref": find(o, text="Share Added", frame="main"),
                                  "summary": "Opens a new sub-account (share) for a member and returns the "
                                             "confirmation number.", "intent": "Goal reached"}),
        ]
    else:
        steps += [lambda o: ("finish", {"checkpoint_ref": find(o, text="Confirm New Share", frame="main"),
                                        "summary": "Prepares a new sub-account for a member up to the "
                                                   "confirmation screen (does not submit).",
                                        "intent": "Reached the confirmation screen"})]
    return steps
