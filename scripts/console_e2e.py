"""Operator-console end-to-end evidence: a real browser drives the real console page.

    python scripts/console_e2e.py              # uses capabilities/corelink.member.savings_balance (real artifact)
    python scripts/console_e2e.py --offline    # records the capability with the scripted test double first,
                                               # writes to evidence-offline/ (rehearsal, not evidence)

Scenario (the supervisor-hold replay): automation hits a state only a human may
clear and escalates. A *separate* Playwright browser then plays the operator: it
opens the console link from the intervention notice, claims the intervention
through the page, tries to hand back too early (rejected, keeps control),
clicks "Release Hold" on the *original* session through the console controls,
and hands back. The replay must then finish on the same session, and the old
operator credentials must be dead.

Output (evidence/17-operator-console-e2e/):
    console.webm            video of the operator's browser
    shots/NN-*.png          console screenshots at each proof point
    assertions.json         every expected proof with observed value and pass/fail
    README.md               the same as a table
    run/                    the replay's own run directory (events.jsonl, shots, interventions, result.json)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cua.envfile import load_dotenv as _load_dotenv  # noqa: E402  (must not import cua.config yet)

_load_dotenv()
os.environ.setdefault("CUA_SECRET_CORELINK_USERNAME", "teller01")
os.environ.setdefault("CUA_SECRET_CORELINK_PASSWORD", "demo-only-pass")

PORT = 8930
OPERATOR = "ops.jdoe"
CAP_ID = "corelink.member.savings_balance"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_mockbank() -> None:
    if _port_open(8800):
        return
    import uvicorn
    from mockbank.app import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8800, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not _port_open(8800):
        time.sleep(0.05)


class Proofs:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def check(self, step: str, expected: str, observed: object, passed: bool) -> None:
        self.items.append({"step": step, "expected": expected, "observed": observed, "passed": bool(passed)})
        print(f"  [{'PASS' if passed else 'FAIL'}] {step}: {observed}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return all(i["passed"] for i in self.items)


async def main(a: argparse.Namespace) -> int:
    out = ROOT / ("evidence-offline" if a.offline else "evidence") / "17-operator-console-e2e"
    work = Path(tempfile.mkdtemp(prefix="cua-console-e2e-"))
    os.environ["CUA_RUNS_DIR"] = str(work / "runs")
    if a.offline:
        os.environ["CUA_CAPABILITIES_DIR"] = str(work / "capabilities")

    from playwright.async_api import async_playwright

    from cua import runner, store
    from cua.handoff import control as ctl
    from cua.handoff.control import ControlLost, ControlState, StaleRequest
    from cua.handoff.operator import OperatorConsole
    from cua.handoff.scripted_operator import call
    from cua.replay.engine import ReplayOptions
    from cua.surface.base import Actor

    start_mockbank()
    if a.offline:
        from cua.discovery.agent import parse_param
        from cua.discovery.scripted import ScriptedLLM, savings_balance_script
        res, _ = await runner.discover(tenant="acme", cap_id=CAP_ID, llm=ScriptedLLM(savings_balance_script()),
                                       goal="Look up member {member_id} and read their current savings balance",
                                       params=[parse_param("member_id:string:pii:id=100234")],
                                       operator_port=None, echo=False)
        cap_ref = res.artifact_path
    else:
        cap_ref = str(store.path_of(store.load(CAP_ID)))
    print(f"capability: {cap_ref}", file=sys.stderr)

    if out.exists():
        shutil.rmtree(out)
    (out / "shots").mkdir(parents=True)
    vids = work / "video"
    p = Proofs()
    seen = len(ctl.NOTICES)

    console = OperatorConsole(PORT)          # owned by this script, so it outlives the run for check 9
    await console.__aenter__()
    replay = asyncio.create_task(runner.replay(
        cap_ref, tenant="acme", inputs={"member_id": "100234"}, echo=False, operator_port=None,
        opts=ReplayOptions(on_failure="escalate", escalation_timeout_s=180),
        faults={"supervisor_hold@/mbr/result": 1}))

    # ---- 1. replay hits the hold and escalates: a notice arrives with the console link
    for _ in range(600):
        if len(ctl.NOTICES) > seen or replay.done():
            break
        await asyncio.sleep(0.05)
    if replay.done():
        print(replay.result().model_dump_json(indent=2))
        raise SystemExit("replay finished without escalating")
    notice = ctl.NOTICES[seen]
    iid, token, session_id = notice["intervention"], notice["token"], notice["session_id"]
    c = ctl.REGISTRY[session_id]
    base = f"http://127.0.0.1:{PORT}"
    p.check("1. replay encounters the supervisor hold", "intervention requested with code SUPERVISOR_HOLD "
            "and a failure screenshot", {"code": notice["code"], "screenshot": c.current.screenshot},
            notice["code"] == "SUPERVISOR_HOLD" and bool(c.current.screenshot))

    # ---- 2. automation is paused: the agent cannot act on the session
    try:
        await c.surface.goto(f"{base}/never", Actor.agent)
        refused = "agent action was ADMITTED"
    except ControlLost as e:
        refused = f"ControlLost: {e}"
    p.check("2. automation pauses", "state awaiting_operator; an agent browser action is refused",
            {"state": c.state.value, "agent_action": refused},
            c.state == ControlState.awaiting_operator and refused.startswith("ControlLost"))

    # ---- 3. resume before anyone claimed it
    st, body = await asyncio.to_thread(call, f"{base}/i/{iid}/resolve",
                                       {"decision": "resume", "operator": OPERATOR, "epoch": c.epoch,
                                        "lease": None}, token)
    p.check("3. resume attempted before claim", "HTTP 409 ownership error", {"status": st, "body": body},
            st == 409 and "claim it first" in str(body))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, record_video_dir=str(vids),
                                        record_video_size={"width": 1440, "height": 1000})
        page = await ctx.new_page()

        async def shot(name: str) -> None:
            await page.wait_for_function("() => { const i = document.getElementById('screen');"
                                         " return i && i.complete && i.naturalWidth > 0; }", timeout=10000)
            await page.wait_for_timeout(600)
            await page.screenshot(path=str(out / "shots" / f"{name}.png"))

        await page.goto(notice["link"] or f"{base}/i/{iid}?token={token}")
        await shot("01-intervention-awaiting-claim")

        # ---- 4. claim through the page
        await page.fill("#op", OPERATOR)
        await page.click("#claim")
        await page.wait_for_function(f"() => document.getElementById('owner').textContent === {json.dumps(OPERATOR)}")
        claim_epoch = await page.evaluate("epoch")
        await shot("02-claimed")
        p.check("4. operator claims the intervention", f"owner {OPERATOR}, state human, epoch advanced",
                {"owner": c.current.claimed_by, "state": c.state.value, "epoch": claim_epoch,
                 "notice_epoch": notice["epoch"]},
                c.current.claimed_by == OPERATOR and c.state == ControlState.human and claim_epoch > notice["epoch"])

        # ---- 5. resume while the hold is still on screen
        await page.click("[data-decision=resume]")
        await page.wait_for_function("() => document.getElementById('msg').textContent.includes('rejected')")
        msg = await page.text_content("#msg")
        await shot("03-resume-rejected-hold-still-present")
        p.check("5. resume before releasing the hold", "rejected; the operator keeps control",
                {"console_message": msg, "state": c.state.value, "owner": c.owner},
                "SUPERVISOR_HOLD is still present" in (msg or "") and c.state == ControlState.human
                and c.owner == OPERATOR)

        # ---- 6. Release Hold on the original session, through the console controls
        await page.wait_for_function("() => document.getElementById('els').textContent.includes('Release Hold')")
        els = (await page.text_content("#els")) or ""
        ref = next(line.split(" ")[0] for line in els.splitlines() if '"Release Hold"' in line)
        await page.fill("#ref", ref)
        await page.click("[data-op=click]")
        await page.wait_for_function("() => document.getElementById('acts').textContent.includes('Release Hold')")
        await page.wait_for_timeout(1800)          # let the live screenshot refresh
        await shot("04-hold-released-via-console")
        acts = [x for x in c._actions if x.get("source") == "console"]
        p.check("6. operator clicks Release Hold through the console",
                "human action recorded against the same session, attributed to the owner",
                {"actions": [{k: x.get(k) for k in ("kind", "target", "by", "epoch")} for x in acts],
                 "session": session_id},
                any(x.get("target") == "Release Hold" and x.get("by") == OPERATOR for x in acts))

        old = await page.evaluate("({lease, epoch})")

        # ---- 7. resume again
        await page.click("[data-decision=resume]")
        await page.wait_for_function("() => document.getElementById('msg').textContent.includes('Handed back')")
        msg = await page.text_content("#msg")
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(out / "shots" / "05-handed-back.png"))
        p.check("7. operator resumes", "accepted; the original failure is cleared and a checkpoint verified",
                {"console_message": msg}, "Automation resumed at" in (msg or ""))

        result = await replay
        await ctx.close()
        await browser.close()

    # ---- 8. automation finishes on the same session
    run_dir = Path(result.run_dir)
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    resumed_at = max(i for i, e in enumerate(events) if e["type"] == "control_state" and e["state"] == "automation")
    s4_after = any(e["type"] == "step_ok" and e.get("step_id") == "s4" for e in events[resumed_at:])
    p.check("8. automation finishes", "status success, one browser session, intervention on that session, "
            "s4 completed after the verified hand-back",
            {"status": result.status.value, "sessions_started": types.count("session_started"),
             "intervention_session": session_id, "replay_run_id": result.run_id, "interventions": result.interventions,
             "s4_after_handback": s4_after},
            result.status.value == "success" and types.count("session_started") == 1
            and session_id == result.run_id and result.interventions == [iid] and s4_after)

    # ---- 9. the old operator credentials are dead (console still up; the intervention is closed)
    replayed = {}
    for name, body in [("resolve", {"decision": "resume", "operator": OPERATOR, "epoch": old["epoch"],
                                    "lease": old["lease"]}),
                       ("act", {"op": "click", "ref": ref, "operator": OPERATOR, "epoch": old["epoch"],
                                "lease": old["lease"]}),
                       ("claim", {"operator": OPERATOR, "epoch": old["epoch"]})]:
        replayed[name] = (await asyncio.to_thread(call, f"{base}/i/{iid}/{name}", body, token))[0]
    try:
        c.check_operator(iid, OPERATOR, old["epoch"], old["lease"])
        direct = "ACCEPTED"
    except StaleRequest as e:
        direct = f"StaleRequest: {e}"
    p.check("9. old operator token/epoch reused", "every request rejected",
            {"http": replayed, "control_check": direct},
            all(v in (401, 403, 404, 409) for v in replayed.values()) and direct.startswith("StaleRequest"))
    await console.__aexit__(None, None, None)

    # ---- artefacts
    shutil.copytree(run_dir, out / "run")
    vid = next(vids.glob("*.webm"), None)
    if vid:
        shutil.copy(vid, out / "console.webm")
    report = {"scenario": "supervisor hold → claim → rejected resume → Release Hold via console → resume",
              "capability": cap_ref.split("capabilities/")[-1], "operator": OPERATOR, "intervention": iid,
              "result": {"status": result.status.value, "run_id": result.run_id}, "all_passed": p.ok,
              "proofs": p.items}
    rec = json.loads((run_dir / "result.json").read_text())   # already redacted by the run
    report["result"]["outputs"] = rec["outputs"]
    (out / "assertions.json").write_text(json.dumps(report, indent=2, default=str))
    rows = "\n".join(f"| {'✅' if i['passed'] else '❌'} | {i['step']} | {i['expected']} |" for i in p.items)
    (out / "README.md").write_text(
        f"# Operator console end-to-end\n\n{'**OFFLINE REHEARSAL — scripted capability.**' if a.offline else ''}\n\n"
        "A separate Playwright browser operates the real console page against the live replay session. "
        "`console.webm` is the operator's screen; `shots/` are the proof points; `run/` is the replay's own "
        "evidence; `assertions.json` has the observed values.\n\n| | proof | expected |\n|---|---|---|\n"
        + rows + "\n")
    idx = out.parent / "INDEX.md"
    if idx.exists():
        lines = [x for x in idx.read_text().splitlines() if "17-operator-console-e2e" not in x]
        lines.append(f"| [17-operator-console-e2e](17-operator-console-e2e/) | operator console driven by a real "
                     f"browser: {sum(i['passed'] for i in p.items)}/{len(p.items)} proofs passed; "
                     f"{result.status.value} | `{result.run_id}` |")
        idx.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}  ({'all proofs passed' if p.ok else 'SOME PROOFS FAILED'})", file=sys.stderr)
    return 0 if p.ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="scripted capability; writes to evidence-offline/")
    sys.exit(asyncio.run(main(ap.parse_args())))
