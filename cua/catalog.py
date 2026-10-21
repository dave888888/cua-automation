"""Agent-facing capability catalog (stretch goal).

Saved artifacts become typed tools an AI agent can discover and call by name:

    GET  /capabilities                       → tool definitions (JSON Schema in/out)
    GET  /capabilities/{name}                → one definition + review card
    POST /capabilities/{name}/invoke         → {"tenant": "...", "inputs": {...},
                                                "irreversible": "deny|allow"}
                                              ← ReplayResult (the result contract)

The tool definition is generated from the artifact contract, so what an agent
sees can never drift from what replay enforces: input types/patterns/enums,
the outputs it gets back, the business outcomes it must handle, and whether
the capability has side effects. Only the latest *usable* version is listed:
approved, or draft-but-read-only.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import contracts, store
from .config import CONFIG_DIR, load_profile, load_tenant
from .replay.engine import ReplayOptions
from .schema.artifact import Capability, OutcomeClass, RiskClass, ValueType

_JSON_TYPES = {ValueType.string: "string", ValueType.integer: "integer", ValueType.decimal: "string",
               ValueType.boolean: "boolean", ValueType.enum: "string"}


def tool_name(cap: Capability) -> str:
    return cap.id.replace(".", "__")


def tenants_for(product: str) -> list[str]:
    out = []
    for p in sorted((CONFIG_DIR / "tenants").glob("*.yaml")):
        t = load_tenant(p.stem)
        if t.product == product:
            out.append(t.id)
    return out


def business_outcomes(cap: Capability) -> list[str]:
    codes = [d.code for d in cap.outcomes if d.classification == OutcomeClass.business_outcome]
    try:
        codes += [d.code for d in load_profile(cap.app.product).detectors
                  if d.classification == OutcomeClass.business_outcome]
    except FileNotFoundError:
        pass
    return list(dict.fromkeys(codes))


def usable(cap: Capability) -> bool:
    """Listed for agents only if it satisfies its workflow contract and may run unattended
    (agent calls have no operator attached)."""
    c = contracts.contract_for(cap)
    return (cap.is_approved or cap.side_effects == RiskClass.read) and not contracts.check(cap, c) \
        and contracts.unattended_ok(cap, c)[0]


def tool_definition(cap: Capability) -> dict[str, Any]:
    props: dict[str, Any] = {"tenant": {"type": "string", "enum": tenants_for(cap.app.product),
                                        "description": "Institution to run against"}}
    required = ["tenant"]
    for p in cap.inputs:
        prop: dict[str, Any] = {"type": _JSON_TYPES[p.type], "description": p.description or p.name}
        if p.type == ValueType.decimal:
            prop["pattern"] = r"^-?\d+(\.\d+)?$"
        if p.pattern:
            prop["pattern"] = p.pattern
        if p.enum:
            prop["enum"] = p.enum
        props[p.name] = prop
        if p.required:
            required.append(p.name)
    outcomes = business_outcomes(cap)
    desc = (f"{cap.description} Returns status=success with outputs "
            f"{[o.name for o in cap.outputs]}, or status=business_outcome with one of {outcomes} "
            f"(legitimate answers, do not retry), or blocked/failed with details.")
    if cap.side_effects == RiskClass.irreversible:
        desc += " IRREVERSIBLE: creates a record; only call when the user has clearly asked for it."
    return {
        "name": tool_name(cap),
        "description": desc,
        "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
        "x-capability": {"id": cap.id, "version": cap.version, "status": cap.status.value,
                         "contract": contracts.contract_for(cap).id,
                         "approved": cap.is_approved, "side_effects": cap.side_effects.value,
                         "content_hash": cap.content_hash(),
                         "outputs": {o.name: {"type": _JSON_TYPES[o.type], "sensitivity": o.sensitivity.value,
                                              "description": o.description} for o in cap.outputs},
                         "business_outcomes": outcomes},
    }


def catalog() -> list[dict[str, Any]]:
    return [tool_definition(c) for c in store.list_all() if usable(c)]


def resolve(name: str) -> Capability:
    for c in store.list_all():
        if tool_name(c) == name or c.id == name:
            return c
    raise KeyError(name)


async def invoke(name: str, args: dict[str, Any], *, irreversible: str = "deny", echo: bool = False):
    from .runner import replay
    cap = resolve(name)
    tenant = args.get("tenant")
    inputs = {k: v for k, v in args.items() if k != "tenant"}
    return await replay(cap, tenant=tenant, inputs=inputs,
                        opts=ReplayOptions(irreversible=irreversible), echo=echo)  # type: ignore[arg-type]


def make_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    lock = asyncio.Lock()   # one live browser at a time here; production would pool sessions per tenant

    async def list_caps(request: Request):
        return JSONResponse(catalog())

    async def one(request: Request):
        try:
            cap = resolve(request.path_params["name"])
        except KeyError:
            return JSONResponse({"error": "unknown capability"}, 404)
        if request.query_params.get("format") == "md":
            return PlainTextResponse(store.review_card(cap))
        return JSONResponse(tool_definition(cap))

    async def call(request: Request):
        body = await request.json()
        try:
            resolve(request.path_params["name"])
        except KeyError:
            return JSONResponse({"error": "unknown capability"}, 404)
        args = {"tenant": body.get("tenant"), **(body.get("inputs") or {})}
        irreversible = body.get("irreversible", "deny")
        if irreversible not in ("deny", "allow"):
            return JSONResponse({"error": "irreversible must be deny|allow over the API"}, 400)
        async with lock:
            r = await invoke(request.path_params["name"], args, irreversible=irreversible)
        return JSONResponse(r.model_dump(mode="json"))

    return Starlette(routes=[Route("/capabilities", list_caps), Route("/capabilities/{name}", one),
                             Route("/capabilities/{name}/invoke", call, methods=["POST"])])
