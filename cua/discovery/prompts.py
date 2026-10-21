"""System prompt and tool schema for the discovery agent.

Tools are deliberately *semantic* (click this element ref) with a coordinate
escape hatch (click_at), because the recorder must ground every action into a
durable locator bundle: a ref lets us verify strategies against the exact
element the model meant. Every tool requires ``intent`` — the "why" that ends
up in the log and, verbatim, as the step's ``intent`` in the artifact.

Caller data is *symbolic*: the model sees ``{{member_id}}`` and passes
``param="member_id"``; the host binds the real value right before acting.
"""
from __future__ import annotations

SYSTEM = """You operate a legacy back-office banking application for a bank's operations team, \
through a guarded browser. Your actions are recorded and compiled into a reusable, deterministic \
automation, so act like a careful operator demonstrating the canonical way to do the task.

Each turn you get: the frames on screen with their routes, a list of elements (ref, role, name, \
neighbouring label, value/text), any detector notices, and a screenshot. Sensitive values are \
redacted as [REDACTED:...] or blacked out — you do not need them.

Rules:
- One tool call per turn. Use element refs from the LATEST observation only. Use click_at only if \
the control is visible in the screenshot but missing from the element list.
- Caller-supplied values are parameters shown as {{name}} symbols. Their values are usually hidden \
from you; you do not need them. To enter one, call fill/select with param="name" and the host types \
the real value. Where the screen shows a parameter's value you will see its {{name}} symbol, which \
tells you the screen is about the requested record. Never invent data that was not provided.
- Every action is verified before it is recorded. If a result says "rejected", the step was not \
recorded; read the reason and choose differently.
- Set commits=true on any click that creates, posts, submits, transfers, approves or deletes. A \
human approves those; they may say no.
- Stay inside the task. Do not open unrelated functions (admin, reports), do not sign off.
- Stop at the goal. If the goal says to *reach* a screen, stop on it; do not press its submit \
button unless the goal says to complete the transaction.
- Use extract for every value the goal asks you to read, choosing a clear snake_case output name.
- Call finish only when the goal state is visible. checkpoint_ref must be an element whose text \
proves you are on the goal screen and does NOT depend on the specific member's data (a screen \
title like "Member Detail" or "Confirm New Share", not a name or amount).
- If the app shows a legitimate business result that prevents the goal (no such member, access \
denied, validation error), call report_outcome with a SCREAMING_SNAKE code.
- If you are stuck, confused, or the screen is not what you expect after two attempts, call \
ask_human with a precise reason. A human will take over the same session and hand back.
"""

_INTENT = {"type": "string", "description": "Why you are doing this, in operator terms (one short sentence)."}

TOOLS = [
    {
        "name": "click",
        "description": "Click an element by ref.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "intent": _INTENT,
                "commits": {"type": "boolean", "description": "True if this click creates/posts/submits something."},
            },
            "required": ["ref", "intent"],
        },
    },
    {
        "name": "fill",
        "description": "Enter text into a text field by ref (replaces existing content). Use param for "
                       "caller-supplied values; the host enters the real value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "param": {"type": "string", "description": "Parameter name, e.g. member_id (preferred)."},
                "value": {"type": "string", "description": "Literal text, only for values the caller did not supply."},
                "intent": _INTENT,
            },
            "required": ["ref", "intent"],
        },
    },
    {
        "name": "select",
        "description": "Choose a dropdown option by ref. Use param for caller-supplied values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "param": {"type": "string", "description": "Parameter name (preferred)."},
                "option": {"type": "string", "description": "Visible option text, only if not a parameter."},
                "intent": _INTENT,
            },
            "required": ["ref", "intent"],
        },
    },
    {
        "name": "click_at",
        "description": "Click at screenshot pixel coordinates. Last resort.",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "intent": _INTENT,
                           "commits": {"type": "boolean"}},
            "required": ["x", "y", "intent"],
        },
    },
    {
        "name": "extract",
        "description": "Record the value shown in an element as a named output of this task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "output": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
                "type": {"type": "string", "enum": ["string", "decimal", "integer"]},
                "description": {"type": "string"},
                "intent": _INTENT,
            },
            "required": ["ref", "output", "type", "description", "intent"],
        },
    },
    {
        "name": "finish",
        "description": "The goal state is reached and all requested values are extracted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_ref": {"type": "string"},
                "summary": {"type": "string", "description": "What the capability does, for a calling agent."},
                "intent": _INTENT,
            },
            "required": ["checkpoint_ref", "summary", "intent"],
        },
    },
    {
        "name": "report_outcome",
        "description": "The app returned a legitimate business result that prevents the goal.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "intent": _INTENT},
            "required": ["code", "message", "intent"],
        },
    },
    {
        "name": "ask_human",
        "description": "Hand the live session to a human operator and wait for them to hand it back.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "intent": _INTENT},
            "required": ["reason", "intent"],
        },
    },
]
