"""Operator console: a real control surface on the *same* live session.

Deliberately small (the brief scopes a full co-browsing console out), but the
control contract is enforced, not just recorded:

* **Authentication.** Every live session has its own console token
  (``SessionControl.console_token``), delivered with the intervention notice.
  GET pages and images accept it as ``?token=``; every mutating request must
  send it in the ``X-Operator-Token`` header. Missing or wrong → 401.
* **Ownership.** An operator on the tenant's roster must *claim* the
  intervention (→ lease token + new epoch). Acting and resolving require the
  owner's name, that lease and the current epoch: another operator or a bad
  lease → 403, an unclaimed / stale / already-resolved intervention → 409,
  a missing epoch → 400.
* **Hand-back.** ``resolve`` runs the caller's verification before control
  moves. A recoverable rejection (e.g. the supervisor hold is still showing)
  returns 409 with the reason and leaves the operator in control.
* **Output.** Every dynamic field is HTML-escaped; values embedded in the
  page script are JSON-encoded with ``<``, ``>`` and ``&`` escaped.

What the operator can do: a live masked screenshot (click it to act), act on an
element by ref, fill/select/press, and hand back (resume/approve/deny/abort).
Every action goes through the guarded surface under the HUMAN lease and is
recorded against the session (and, during discovery, verified into the
artifact as a ``provenance: human`` step).
"""
from __future__ import annotations

import asyncio
import html
import json
import secrets
import sys
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from ..surface.base import Actor
from . import control as ctl
from .control import ControlLost, Decision, NotAuthorized, SessionControl, StaleRequest, open_interventions

TOKEN_HEADER = "x-operator-token"


class Unauthenticated(RuntimeError):
    pass


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _js(v: Any) -> str:
    """JSON for embedding inside <script>: cannot close the tag or open a comment."""
    return json.dumps(v).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _find(iid: str) -> SessionControl:
    for c, req in open_interventions():
        if req.id == iid:
            return c
    raise KeyError(iid)


def _auth(request: Request, c: SessionControl, *, mutating: bool) -> None:
    token = request.headers.get(TOKEN_HEADER)
    if token is None and not mutating:
        token = request.query_params.get("token")
    if not token or not secrets.compare_digest(token, c.console_token):
        raise Unauthenticated("missing or invalid operator token")


def _session_by_token(request: Request) -> SessionControl | None:
    token = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
    for c in ctl.REGISTRY.values():
        if token and secrets.compare_digest(token, c.console_token):
            return c
    return None


async def _body(request: Request) -> dict[str, Any]:
    try:
        b = json.loads(await request.body() or b"{}")
        return b if isinstance(b, dict) else {}
    except json.JSONDecodeError:
        return {}


def _err(e: Exception) -> JSONResponse:
    code = {Unauthenticated: 401, NotAuthorized: 403, StaleRequest: 409, ControlLost: 409,
            KeyError: 404}.get(type(e), 400)
    return JSONResponse({"error": str(e).strip("'")}, code)


def _need_epoch(b: dict[str, Any]) -> int:
    if b.get("epoch") is None:
        raise ValueError("epoch is required")
    return int(b["epoch"])


# --------------------------------------------------------------------------- operator actions

async def human_act(control: SessionControl, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """One operator action on the live session. Every browser call inside goes through the
    surface lease as Actor.human, so it is refused unless the owner holds the lease, and it
    counts as in-flight for the hand-back barrier."""
    sf = control.surface
    control.require(Actor.human)
    control.suppress_browser_until = time.monotonic() + 5
    before = {sf.frame_label(f): f.url for f in sf.frames()}
    value = args.get("value")
    ref = args.get("ref")
    if op == "click_at":
        ref = await sf.element_at(float(args["x"]), float(args["y"]))
    target, desc, res = None, {}, None
    if ref:
        got = await sf.describe(ref)
        if got:
            desc = {"role": got[1]["role"], "name": got[1]["name"] or got[1]["text"][:60]}
        try:
            target = await sf.ground(ref, "act", [value] if value else [])
        except Exception:
            target = None
        res = await sf.resolve_ref(ref)
    if op in ("click", "click_at"):
        if res is not None:
            await sf.click(res, Actor.human)
        else:
            await sf.click_point(float(args["x"]), float(args["y"]), Actor.human)
    elif op == "fill" and res is not None:
        await sf.fill(res, str(value), Actor.human)
    elif op == "select" and res is not None:
        await sf.select(res, str(value), Actor.human)
    elif op == "press":
        await sf.press(str(args.get("key", "Enter")), Actor.human)
    elif op == "type":
        await sf.type_text(str(value), Actor.human)
    else:
        raise ValueError(f"unknown or incomplete op {op!r}")
    await asyncio.sleep(0.3)
    await sf.settle()
    after = {sf.frame_label(f): f.url for f in sf.frames()}
    control.suppress_browser_until = time.monotonic() + 0.5
    action = {"source": "console", "kind": "click" if op == "click_at" else op, "target": desc.get("name"),
              "role": desc.get("role"), "ref": ref, "value": value}
    control.record_human_action(action)
    if control.on_human_step and target is not None and action["kind"] in ("click", "fill", "select"):
        await control.on_human_step(action["kind"], target, value, before, after)
    return action


# --------------------------------------------------------------------------- HTTP: reads

def _status(c: SessionControl) -> dict[str, Any]:
    r = c.current
    return {"intervention": r.id if r else None, "state": c.state.value, "epoch": c.epoch,
            "owner": r.claimed_by if r else None, "code": r.code if r else None,
            "rejected_resumes": [x.reason for x in r.rejected_resumes] if r else []}


async def index(request: Request) -> Response:
    c = _session_by_token(request)
    if c is None:
        return HTMLResponse(PAGE_HEAD + "<h1>Operator queue</h1><p class=muted>Open the link from your "
                            "intervention notice (it carries the session token).</p></body></html>", 401)
    r = c.current
    tok = _e(c.console_token)
    row = (f"<tr><td><a href='/i/{_e(r.id)}?token={tok}'>{_e(r.id)}</a></td><td>{_e(r.kind.value)}</td>"
           f"<td>{_e(r.code)}</td><td>{_e(r.subject)}</td><td>{_e(r.step_id)}</td><td>{_e(r.status)}</td>"
           f"<td>{_e(r.claimed_by)}</td><td>{_e(r.reason[:160])}</td></tr>") if r else \
        "<tr><td colspan=8 class=muted>No open intervention on this session.</td></tr>"
    return HTMLResponse(PAGE_HEAD + f"""<h1>Operator queue</h1>
<p class=muted>Session {_e(c.session_id)} &middot; state {_e(c.state.value)}. Refreshes every 2s.</p>
<table><tr><th>id</th><th>kind</th><th>code</th><th>subject</th><th>step</th><th>status</th><th>owner</th>
<th>reason</th></tr>{row}</table>
<script>setTimeout(()=>location.reload(),2000)</script></body></html>""")


async def api_list(request: Request) -> Response:
    c = _session_by_token(request)
    if c is None:
        return JSONResponse({"error": "missing or invalid operator token"}, 401)
    return JSONResponse([] if c.current is None else [{**c.current.model_dump(mode="json"), **_status(c)}])


async def status(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=False)
        return JSONResponse(_status(c))
    except Exception as e:
        return _err(e)


async def console(request: Request) -> Response:
    iid = request.path_params["iid"]
    try:
        c = _find(iid)
        _auth(request, c, mutating=False)
    except KeyError:
        return HTMLResponse(PAGE_HEAD + "<p>Intervention resolved or unknown.</p></body></html>", 404)
    except Unauthenticated:
        return HTMLResponse(PAGE_HEAD + "<p>Missing or invalid operator token.</p></body></html>", 401)
    r = c.current
    assert r is not None
    buttons = "".join(f"<button class='d {_e(d.value)}' data-decision='{_e(d.value)}'>{_e(d.value)}</button>"
                      for d in r.allowed_decisions)
    cfg = {"iid": r.id, "token": c.console_token, "epoch": c.epoch}
    return HTMLResponse(PAGE_HEAD + f"""
<h1>{_e(r.kind.value)}: {_e(r.code)}</h1>
<p><b>{_e(r.subject)}</b> &middot; step {_e(r.step_id or '-')}
 {('&ldquo;' + _e(r.step_intent) + '&rdquo;') if r.step_intent else ''}</p>
<p class=reason id=reason>{_e(r.reason)}</p>
<p class=muted>Session {_e(r.session_id)} &middot; state <b id=state>{_e(c.state.value)}</b>
 &middot; owner <b id=owner>{_e(r.claimed_by or 'unclaimed')}</b> &middot; epoch <b id=epoch>{c.epoch}</b></p>
<div id=msg class=msg></div>
<div class=wrap>
 <div><img id=screen alt="live session (masked)"></div>
 <div class=side>
  <label>Operator <input id=op value="ops.jdoe"></label>
  <div><button id=claim class=claim>Claim intervention</button></div>
  <h3>Act</h3>
  <p class=muted>Click the screenshot, or use an element ref:</p>
  <input id=ref placeholder="ref e.g. main-e3"> <input id=val placeholder="value (fill/select)">
  <div><button data-op=click>click ref</button><button data-op=fill>fill</button>
  <button data-op=select>select</button><button data-op=press>Enter</button></div>
  <h3>Hand back</h3><input id=note placeholder="note for the record"><div id=decisions>{buttons}</div>
  <h3>Your actions</h3><ol id=acts></ol>
  <details open><summary>Elements</summary><pre id=els></pre></details>
 </div>
</div>
<script>
const CFG = {_js(cfg)};
let epoch = CFG.epoch, lease = null;
const $ = (id) => document.getElementById(id);
const op = () => $("op").value;
async function call(path, body) {{
  const r = await fetch("/i/" + CFG.iid + path, {{method: "POST", headers: {{"content-type": "application/json",
    "x-operator-token": CFG.token}}, body: JSON.stringify(Object.assign({{operator: op(), epoch, lease}}, body))}});
  const j = await r.json(); if (j.epoch !== undefined) epoch = j.epoch; return [r.status, j];
}}
function say(kind, text) {{ const m = $("msg"); m.className = "msg " + kind; m.textContent = text; }}
async function refresh() {{
  $("screen").src = "/i/" + CFG.iid + "/screen.png?token=" + encodeURIComponent(CFG.token) + "&t=" + Date.now();
  const r = await fetch("/i/" + CFG.iid + "/status", {{headers: {{"x-operator-token": CFG.token}}}});
  if (r.status !== 200) return;
  const s = await r.json(); epoch = s.epoch;
  $("state").textContent = s.state; $("owner").textContent = s.owner || "unclaimed"; $("epoch").textContent = s.epoch;
  const e = await fetch("/i/" + CFG.iid + "/elements", {{headers: {{"x-operator-token": CFG.token}}}});
  if (e.status === 200) $("els").textContent = (await e.json()).join("\\n");
}}
$("claim").onclick = async () => {{
  const [st, j] = await call("/claim", {{}});
  if (st === 200) {{ lease = j.lease; $("claim").disabled = true; $("op").readOnly = true;
                     say("ok", "Claimed by " + op() + ". You hold the session."); }}
  else say("err", "Claim refused (" + st + "): " + j.error);
  refresh();
}};
async function act(o, extra) {{
  const [st, j] = await call("/act", Object.assign({{op: o, ref: $("ref").value, value: $("val").value}}, extra || {{}}));
  const li = document.createElement("li");
  li.textContent = st === 200 ? (j.kind + " " + (j.target || "")) : ("refused " + st + ": " + j.error);
  $("acts").appendChild(li);
  if (st === 200) say("ok", "Done: " + j.kind + " " + (j.target || "") + ". Resume when the screen is ready.");
  else say("err", "Action refused (" + st + "): " + j.error);
  refresh();
}}
document.querySelectorAll("[data-op]").forEach(b => b.onclick = () =>
  act(b.dataset.op, b.dataset.op === "press" ? {{key: "Enter"}} : {{}}));
$("screen").onclick = (e) => {{
  const img = e.target, r = img.getBoundingClientRect();
  act("click_at", {{x: (e.clientX - r.left) * img.naturalWidth / r.width,
                    y: (e.clientY - r.top) * img.naturalHeight / r.height}});
}};
document.querySelectorAll("[data-decision]").forEach(b => b.onclick = async () => {{
  const [st, j] = await call("/resolve", {{decision: b.dataset.decision, note: $("note").value}});
  if (st === 200 && j.accepted && j.resumed) say("ok", "Handed back. Automation resumed at " + j.checkpoint + ".");
  else if (st === 200) say("ok", "Closed: " + (j.reason || j.state));
  else if (st === 409 && j.accepted === false) say("err", "Hand-back rejected, you still hold the session: " + j.reason);
  else say("err", "Refused (" + st + "): " + j.error);
  refresh();
}});
refresh(); setInterval(refresh, 1500);
</script></body></html>""")


async def screen(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=False)
    except Exception as e:
        return _err(e)
    return Response(await c.surface.screenshot(), media_type="image/png", headers={"cache-control": "no-store"})


async def elements(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=False)
    except Exception as e:
        return _err(e)
    obs = await c.surface.observe(screenshot=False)
    return JSONResponse([c.log.redactor.text(e.brief()) for e in obs.elements])


# --------------------------------------------------------------------------- HTTP: mutations

async def claim(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=True)
        b = await _body(request)
        lease, epoch = c.claim(request.path_params["iid"], str(b.get("operator", "")), _need_epoch(b))
        return JSONResponse({"ok": True, "lease": lease, "epoch": epoch, "owner": c.owner, "state": c.state.value})
    except Exception as e:
        return _err(e)


async def act(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=True)
        b = await _body(request)
        c.check_operator(request.path_params["iid"], str(b.get("operator", "")), _need_epoch(b), b.get("lease"))
        action = await human_act(c, str(b.get("op", "click")), b)
        return JSONResponse({**c.log.redactor.obj(action), "epoch": c.epoch})
    except Exception as e:
        return _err(e)


async def resolve(request: Request) -> Response:
    try:
        c = _find(request.path_params["iid"])
        _auth(request, c, mutating=True)
        b = await _body(request)
        out = await c.resolve(request.path_params["iid"], Decision(b.get("decision", "resume")),
                              operator=str(b.get("operator", "")), epoch=_need_epoch(b), lease=b.get("lease"),
                              note=str(b.get("note", ""))[:500])
        # 409 = rejected, but the operator still holds the session (e.g. the hold is still showing)
        return JSONResponse(c.log.redactor.obj(out), 200 if out["accepted"] or out["state"] != "human" else 409)
    except Exception as e:
        return _err(e)


app = Starlette(routes=[
    Route("/", index),
    Route("/api/interventions", api_list),
    Route("/i/{iid}", console),
    Route("/i/{iid}/status", status),
    Route("/i/{iid}/screen.png", screen),
    Route("/i/{iid}/elements", elements),
    Route("/i/{iid}/claim", claim, methods=["POST"]),
    Route("/i/{iid}/act", act, methods=["POST"]),
    Route("/i/{iid}/resolve", resolve, methods=["POST"]),
])


class OperatorConsole:
    """Runs the console inside the automation's event loop, so console handlers
    operate the live Playwright session directly. Bound to localhost."""

    def __init__(self, port: int = 8900) -> None:
        import uvicorn
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "OperatorConsole":
        self.server.config.install_signal_handlers = False
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        ctl.CONSOLE_URL = f"http://127.0.0.1:{self.port}"
        print(f"  operator console: {ctl.CONSOLE_URL}/ (each intervention notice carries a session-token link)",
              file=sys.stderr)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        if ctl.CONSOLE_URL == f"http://127.0.0.1:{self.port}":
            ctl.CONSOLE_URL = None
        if self.task:
            await self.task


PAGE_HEAD = """<!doctype html><html><head><meta charset=utf-8><title>Operator console</title>
<style>
 body{font:14px/1.45 system-ui,sans-serif;margin:20px;color:#1d2433;background:#f6f7f9}
 h1{font-size:20px;margin:0 0 6px} h3{font-size:14px;margin:14px 0 6px}
 table{border-collapse:collapse;background:#fff} td,th{border:1px solid #d6dae1;padding:4px 8px;text-align:left}
 .muted{color:#667085} .reason{background:#fff4e5;border:1px solid #f5c27a;padding:8px;border-radius:6px}
 .msg{padding:8px;border-radius:6px;margin:8px 0;min-height:1em} .msg.ok{background:#e7f6ec;border:1px solid #7cc79a}
 .msg.err{background:#fdecea;border:1px solid #f1998e}
 .wrap{display:flex;gap:16px;align-items:flex-start} img{max-width:900px;width:100%;border:1px solid #c9ced6;cursor:crosshair}
 .side{min-width:300px} input{padding:4px;margin:2px 0} button{margin:3px;padding:5px 10px;cursor:pointer}
 .claim{background:#1d4ed8;color:#fff;border:0} .claim:disabled{background:#94a3b8;cursor:default}
 .d.resume,.d.approve{background:#1f7a4d;color:#fff;border:0} .d.abort,.d.deny{background:#b42318;color:#fff;border:0}
 pre{font-size:11px;max-height:300px;overflow:auto;background:#fff;border:1px solid #ddd;padding:6px}
</style></head><body>"""
