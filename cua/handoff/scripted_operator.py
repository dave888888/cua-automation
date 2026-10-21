"""A scripted "human" that drives the operator console over HTTP.

Used by tests and by the evidence script to make handoff demos reproducible.
It receives intervention notices the way an operator would (the notice carries
the session-scoped console token), then uses only the public console API —
claim → act → resolve, with the lease token and epochs the console hands out.
So the control-transfer path exercised is the real one; only the person is
simulated.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import control as ctl


def call(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, Any]:
    headers = {"content-type": "application/json"}
    if token:
        headers["x-operator-token"] = token
    req = urllib.request.Request(url, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


@dataclass
class Plan:
    """What to do for the next intervention matching ``code`` (or any if None)."""
    decision: str
    code: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)   # console /act bodies
    note: str = ""
    resume_first: bool = False     # try to hand back *before* acting (expect a 409 rejection)


async def run_operator(port: int, plans: list[Plan], *, operator: str = "ops.jdoe", poll_s: float = 0.3,
                       timeout_s: float = 120) -> list[dict[str, Any]]:
    base = f"http://127.0.0.1:{port}"
    done: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    pending = list(plans)
    seen = len(ctl.NOTICES)
    claimed: dict[str, dict[str, Any]] = {}       # intervention id -> {lease, epoch, token}
    while pending and loop.time() < deadline:
        for n in ctl.NOTICES[seen:]:
            if n["intervention"] not in claimed:
                claimed[n["intervention"]] = {"token": n["token"], "lease": None, "code": n["code"]}
        for iid, st in list(claimed.items()):
            if st.get("closed"):
                continue
            plan = next((p for p in pending if p.code in (None, st["code"])), None)
            if plan is None:
                continue
            tok = st["token"]
            code, s = await asyncio.to_thread(call, f"{base}/i/{iid}/status", None, tok)
            if code != 200:
                continue           # console not up yet / intervention closed
            if st["lease"] is None:
                code, r = await asyncio.to_thread(call, f"{base}/i/{iid}/claim",
                                                  {"operator": operator, "epoch": s["epoch"]}, tok)
                if code != 200:
                    continue
                st["lease"], st["epoch"] = r["lease"], r["epoch"]
            record: dict[str, Any] = {"intervention": iid, "code": st["code"], "steps": []}

            async def resolve(decision: str, note: str) -> tuple[int, Any]:
                c, r = await asyncio.to_thread(call, f"{base}/i/{iid}/resolve",
                                               {"decision": decision, "note": note, "operator": operator,
                                                "epoch": st["epoch"], "lease": st["lease"]}, tok)
                if isinstance(r, dict) and "epoch" in r:
                    st["epoch"] = r["epoch"]
                record["steps"].append({"resolve": decision, "status": c, "result": r})
                return c, r

            if plan.resume_first:
                await resolve("resume", "attempted before clearing the blocking condition")
            for a in plan.actions:
                body = dict(a)
                if "ref_finder" in body:  # pick a ref from the live element list
                    _, els = await asyncio.to_thread(call, f"{base}/i/{iid}/elements", None, tok)
                    needle = body.pop("ref_finder")
                    body["ref"] = next(e.split(" ")[0] for e in els if needle in e)
                c, r = await asyncio.to_thread(call, f"{base}/i/{iid}/act",
                                               {**body, "operator": operator, "epoch": st["epoch"],
                                                "lease": st["lease"]}, tok)
                if isinstance(r, dict) and "epoch" in r:
                    st["epoch"] = r["epoch"]
                record["steps"].append({"act": body.get("op"), "status": c})
                await asyncio.sleep(0.3)
            c, r = await resolve(plan.decision, plan.note)
            record["result"] = r
            done.append(record)
            pending.remove(plan)
            if c != 409:           # 409 = rejected, operator still owns it: a later plan may continue
                st["closed"] = True
            break
        await asyncio.sleep(poll_s)
    return done
