"""HTTP-level tests for the operator console endpoints (auth, ownership, epochs, escaping).

A real SessionControl and the real Starlette console run in one event loop;
requests go over HTTP exactly as the console page or an integration would send
them. No browser is needed: these pin the endpoint contract.
"""
from tests import _harness  # noqa: F401

import asyncio
import tempfile
from pathlib import Path

from cua.handoff.control import ControlState, HandbackVerdict, InterventionKind, SessionControl
from cua.handoff.operator import OperatorConsole
from cua.handoff.scripted_operator import call
from cua.redact import Redactor


class _Log:
    def __init__(self) -> None:
        self.redactor = Redactor()

    def event(self, *a, **k) -> None:
        pass


def _scenario(port: int, body, *, verify=None, reason: str = "test", subject: str = "cap@1.0.0"):
    """Start a console + an escalated session, run ``body(c, base, iid)`` against it."""
    async def go():
        c = SessionControl(f"http-{port}", Path(tempfile.mkdtemp()), _Log(), operators=("ops.a", "ops.b"))

        async def ok(res):
            return HandbackVerdict(True, "test")

        async with OperatorConsole(port):
            esc = asyncio.create_task(c.escalate(run_id="r", kind=InterventionKind.failure, subject=subject,
                                                 code="TEST", reason=reason, timeout_s=30, verify=verify or ok))
            while c.state != ControlState.awaiting_operator:
                await asyncio.sleep(0.01)
            out = await body(c, f"http://127.0.0.1:{port}", c.current.id)
            if not esc.done():
                c.terminate("test over")
                esc.cancel()
            c.close()
            return out
    return asyncio.run(go())


async def h(url, body=None, token=None):
    return await asyncio.to_thread(call, url, body, token)


def test_every_endpoint_requires_the_session_token():
    async def body(c, base, iid):
        tok = c.console_token
        out = {
            "status_none": (await h(f"{base}/i/{iid}/status"))[0],
            "status_bad": (await h(f"{base}/i/{iid}/status", None, "nope"))[0],
            "status_ok": (await h(f"{base}/i/{iid}/status", None, tok))[0],
            "list_none": (await h(f"{base}/api/interventions"))[0],
            "claim_none": (await h(f"{base}/i/{iid}/claim", {"operator": "ops.a", "epoch": c.epoch}))[0],
            "resolve_bad": (await h(f"{base}/i/{iid}/resolve", {"operator": "ops.a", "epoch": c.epoch},
                                    "nope"))[0],
        }
        # the query-string token is accepted for GET pages/images only, never for mutations
        q = await h(f"{base}/i/{iid}/claim?token={tok}", {"operator": "ops.a", "epoch": c.epoch})
        out["claim_query_token"] = q[0]
        assert c.state == ControlState.awaiting_operator      # nothing above changed the session
        return out
    out = _scenario(8941, body)
    assert out == {"status_none": 401, "status_bad": 401, "status_ok": 200, "list_none": 401,
                   "claim_none": 401, "resolve_bad": 401, "claim_query_token": 401}


def test_resume_before_claim_and_missing_epoch_are_refused():
    async def body(c, base, iid):
        tok = c.console_token
        return {
            "resume_unclaimed": (await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                                      "epoch": c.epoch, "lease": "x"}, tok)),
            "act_unclaimed": (await h(f"{base}/i/{iid}/act", {"op": "click", "operator": "ops.a",
                                                               "epoch": c.epoch, "lease": "x"}, tok))[0],
            "claim_no_epoch": (await h(f"{base}/i/{iid}/claim", {"operator": "ops.a"}, tok)),
            "state": c.state.value,
        }
    out = _scenario(8942, body)
    assert out["resume_unclaimed"][0] == 409 and "claim it first" in out["resume_unclaimed"][1]["error"]
    assert out["act_unclaimed"] == 409
    assert out["claim_no_epoch"][0] == 400 and "epoch is required" in out["claim_no_epoch"][1]["error"]
    assert out["state"] == "awaiting_operator"


def test_ownership_roster_double_claim_foreign_operator_and_stale_epoch():
    async def body(c, base, iid):
        tok, ep0 = c.console_token, c.epoch
        r = {"mallory": (await h(f"{base}/i/{iid}/claim", {"operator": "mallory", "epoch": ep0}, tok))[0]}
        st, cl = await h(f"{base}/i/{iid}/claim", {"operator": "ops.a", "epoch": ep0}, tok)
        r["claim_a"], lease, ep = st, cl["lease"], cl["epoch"]
        r["owner"] = cl["owner"]
        r["claim_b"] = (await h(f"{base}/i/{iid}/claim", {"operator": "ops.b", "epoch": ep}, tok))[0]
        r["b_with_a_lease"] = (await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.b",
                                                                    "epoch": ep, "lease": lease}, tok))[0]
        r["forged_lease"] = (await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                                  "epoch": ep, "lease": "forged"}, tok))[0]
        r["stale_epoch"] = (await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                                 "epoch": ep0, "lease": lease}, tok))[0]
        r["state_after_refusals"] = c.state.value
        st, out = await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                       "epoch": ep, "lease": lease}, tok)
        r["resume"], r["resumed"] = st, out["resumed"]
        # replaying the old credentials after hand-back: the intervention is gone
        r["replayed"] = (await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                              "epoch": ep, "lease": lease}, tok))[0]
        r["old_token_status"] = (await h(f"{base}/i/{iid}/status", None, tok))[0]
        return r
    r = _scenario(8943, body)
    assert r == {"mallory": 403, "claim_a": 200, "owner": "ops.a", "claim_b": 409, "b_with_a_lease": 403,
                 "forged_lease": 403, "stale_epoch": 409, "state_after_refusals": "human", "resume": 200,
                 "resumed": True, "replayed": 404, "old_token_status": 404}


def test_rejected_resume_is_409_and_operator_keeps_control():
    cleared = {"v": False}

    async def verify(res):
        if not cleared["v"]:
            return HandbackVerdict(False, "blocked", "SUPERVISOR_HOLD is still present", recoverable=True)
        return HandbackVerdict(True, "continue@3")

    async def body(c, base, iid):
        tok = c.console_token
        _, cl = await h(f"{base}/i/{iid}/claim", {"operator": "ops.a", "epoch": c.epoch}, tok)
        lease = cl["lease"]
        st1, r1 = await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                       "epoch": cl["epoch"], "lease": lease}, tok)
        state_between = c.state.value
        cleared["v"] = True
        st2, r2 = await h(f"{base}/i/{iid}/resolve", {"decision": "resume", "operator": "ops.a",
                                                       "epoch": r1["epoch"], "lease": lease}, tok)
        return st1, r1, state_between, st2, r2, c.state.value
    st1, r1, between, st2, r2, final = _scenario(8944, body, verify=verify)
    assert st1 == 409 and r1["accepted"] is False and r1["owner"] == "ops.a" and "still present" in r1["reason"]
    assert between == "human"
    assert st2 == 200 and r2["resumed"] and final == "automation"


def test_console_page_escapes_dynamic_fields():
    evil = '<script>alert("x")</script>'
    attr = '"><img src=x onerror=alert(1)>'

    async def body(c, base, iid):
        import urllib.request
        url = f"{base}/i/{iid}?token={c.console_token}"
        page = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).read().decode())
        def status_without_token():
            try:
                urllib.request.urlopen(f"{base}/i/{iid}", timeout=10)
                return 200
            except urllib.error.HTTPError as e:
                return e.code
        import urllib.error
        return page, await asyncio.to_thread(status_without_token)
    page, noauth_status = _scenario(8945, body, reason=evil, subject=attr)
    assert evil not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert attr not in page and "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    assert page.count("<script>") == 1                 # only the console's own script
    assert noauth_status == 401
