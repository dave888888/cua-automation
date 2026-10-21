"""Produce /evidence: a REAL LLM discovery run, the saved artifact, and replays.

    export ANTHROPIC_API_KEY=...          # your key
    python scripts/make_evidence.py        # starts the mock app itself

    python scripts/make_evidence.py --with-share   # also discover the irreversible
                                                   # open-share flow (approval given by
                                                   # the scripted operator via the console API)
    python scripts/make_evidence.py --scripted     # offline dry run with the test double;
                                                   # writes to evidence-offline/, NOT evidence/

Ends by running scripts/console_e2e.py (a real browser operating the console):
17-operator-console-e2e/ with video, screenshots and assertions. --no-console skips it.

Every run directory is copied verbatim (events.jsonl, masked screenshots,
redacted DOM text, result.json, interventions/). An INDEX.md is generated.
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


async def main(a: argparse.Namespace) -> None:
    out = ROOT / ("evidence-offline" if a.scripted else "evidence")
    work = Path(tempfile.mkdtemp(prefix="cua-evidence-"))
    os.environ["CUA_RUNS_DIR"] = str(work / "runs")
    if a.scripted:
        os.environ["CUA_CAPABILITIES_DIR"] = str(work / "capabilities")
    # imports after env is set
    from cua import runner, store
    from cua.discovery.agent import parse_param
    from cua.handoff.scripted_operator import Plan, run_operator
    from cua.redact import Redactor
    from cua.replay.engine import ReplayOptions

    if not a.scripted and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set: add it to .env in the repo root (see .env.example) "
                         "or export it, then rerun. Use --scripted for an offline rehearsal.")
    if a.scripted:
        from cua.discovery.scripted import ScriptedLLM, open_share_script, savings_balance_script
        llm_bal, llm_share = ScriptedLLM(savings_balance_script()), ScriptedLLM(open_share_script())
    else:
        from cua.discovery.llm import AnthropicLLM
        llm_bal, llm_share = AnthropicLLM(a.model), AnthropicLLM(a.model)

    start_mockbank()
    out.mkdir(exist_ok=True)
    index: list[tuple[str, str, str]] = []

    def keep(name: str, run_dir: str, summary: str) -> None:
        dst = out / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(run_dir, dst)
        index.append((name, summary, Path(run_dir).name))
        print(f"[evidence] {name}: {summary}")

    # 1. discovery (the real thing)
    res, run_dir = await runner.discover(
        tenant="acme", cap_id="corelink.member.savings_balance", title="Member savings balance",
        goal="Look up member {member_id} and read their current savings balance",
        params=[parse_param("member_id:string:pii:id=100234")], llm=llm_bal, operator_port=8900, echo=True)
    keep("01-discovery-savings-balance", run_dir, f"discovery → {res.status} ({res.steps_taken} steps, "
                                                  f"model={llm_bal.model}, tokens={res.usage})")
    if res.status != "recorded":
        raise SystemExit(f"discovery did not record an artifact: {res}")
    cap_path = Path(res.artifact_path)
    (out / "artifacts").mkdir(exist_ok=True)
    for f in (cap_path, cap_path.with_suffix(".md")):
        shutil.copy(f, out / "artifacts" / f"corelink.member.savings_balance@{f.name}")
    ref = str(cap_path)

    async def rep(name: str, inputs: dict, *, tenant: str = "acme", faults=None, opts=None, port=None, plans=None):
        op = asyncio.create_task(run_operator(port, plans)) if plans else None
        r = await runner.replay(ref, tenant=tenant, inputs=inputs,
                                faults=faults, opts=opts, operator_port=port, echo=False)
        if op:
            await op
        # Summarise from the *persisted* result.json, which the run's redactor already wrote:
        # the index can never show more than the evidence itself (single redaction boundary).
        persisted = json.loads((Path(r.run_dir) / "result.json").read_text())
        what = persisted["outputs"] or (r.outcome and r.outcome.code) or \
            (r.failure and f"{r.failure.kind.value}/{r.failure.code}") or (r.blocked and r.blocked.code)
        keep(name, r.run_dir, f"{r.status.value}: {what}; recoveries={[x.action for x in r.recoveries]}; "
                              f"drift={len(r.drift)}; interventions={len(r.interventions)}")
        return r

    # 2..8 replays (no LLM)
    await rep("02-replay-success", {"member_id": "100234"})
    await rep("03-replay-not-found", {"member_id": "100999"})
    await rep("04-replay-access-denied", {"member_id": "100777"})
    await rep("05-replay-bad-input", {"member_id": "12ab"})
    await rep("06-replay-transient-error-recovered", {"member_id": "100234"}, faults={"error500@/mbr/result": 1})
    await rep("07-replay-interstitial-dismissed", {"member_id": "100234"}, faults={"interstitial@/mbr/result": 1})
    await rep("08-replay-session-expired-relogin", {"member_id": "100456"}, faults={"session_expire@/mbr/result": 1})
    await rep("09-replay-persistent-error-hard-fail", {"member_id": "100234"}, faults={"error500@/mbr/find": 5})
    await rep("10-replay-tenant-blueriver-overlay", {"member_id": "100234"}, tenant="blueriver")
    await rep("11-replay-tenant-blueriver-no-overlay-drift", {"member_id": "100234"}, tenant="blueriver",
              opts=ReplayOptions(use_tenant_overrides=False))
    await rep("12-replay-human-handoff", {"member_id": "100234"}, faults={"supervisor_hold@/mbr/result": 1},
              opts=ReplayOptions(on_failure="escalate"), port=8901,
              plans=[Plan("resume", "SUPERVISOR_HOLD", resume_first=True,
                          actions=[{"op": "click", "ref_finder": '"Release Hold"'}],
                          note="supervisor released the hold")])

    await rep("12b-replay-wrong-record-invariant", {"member_id": "100234"}, faults={"wrong_record@/mbr/result": 1})

    if a.with_share:
        op = asyncio.create_task(run_operator(8902, [Plan("approve", "IRREVERSIBLE_STEP", note="sandbox approval")]))
        res2, run2 = await runner.discover(
            tenant="acme", cap_id="corelink.member.open_share", title="Open a sub-account (share)",
            goal="Open a new {share_type} sub-account named {nickname} with an opening deposit of "
                 "{opening_deposit} for member {member_id}, submit it, and read the confirmation number",
            params=[parse_param("member_id:string:pii:id=100456"), parse_param("share_type:string:public=Savings Club"),
                    parse_param("nickname=Holiday fund"), parse_param("opening_deposit:decimal=25.00")],
            llm=llm_share, operator_port=8902, echo=True)
        await op
        keep("13-discovery-open-share-with-approval", run2, f"discovery → {res2.status} (irreversible step approved "
                                                            "via operator console)")
        if res2.status == "recorded":
            cap2 = store.load(res2.artifact_path)
            store.save(store.approve(cap2, "evidence-script"), Redactor())
            shutil.copy(res2.artifact_path, out / "artifacts" / f"corelink.member.open_share@{Path(res2.artifact_path).name}")
            inputs = {"member_id": "100456", "share_type": "Savings Club", "nickname": "Holiday fund",
                      "opening_deposit": "25.00"}
            ref = res2.artifact_path
            await rep("14-replay-irreversible-blocked", inputs)
            await rep("15-replay-irreversible-allowed", inputs, opts=ReplayOptions(irreversible="allow"))
            await rep("16-replay-lost-response-in-doubt", inputs, opts=ReplayOptions(irreversible="allow"),
                      faults={"lost_response": 1})

    lines = ["# Evidence index", "",
             ("**OFFLINE DRY RUN — scripted test double, not a model. Not submission evidence.**\n"
              if a.scripted else ""),
             "Each folder is a run directory copied verbatim: `events.jsonl` (redacted structured log), "
             "`shots/` (masked screenshots), `dom/` (redacted visible text on failures), `result.json`, "
             "`interventions/` (handoff records). Operator actions in handoff runs were performed by "
             "`cua/handoff/scripted_operator.py` through the operator console HTTP API (the same calls the "
             "console page makes) so the demo is reproducible.", "",
             "| folder | result | run id |", "|---|---|---|"]
    lines += [f"| [{n}]({n}/) | {s} | `{rid}` |" for n, s, rid in index]
    (out / "INDEX.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}/INDEX.md")

    # 17: the operator console driven by a real browser (separate process: its own run/capability dirs)
    if not a.no_console:
        import subprocess
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "console_e2e.py")]
                            + (["--offline"] if a.scripted else []), cwd=ROOT).returncode
        if rc != 0:
            raise SystemExit("console end-to-end proofs failed; see evidence*/17-operator-console-e2e/README.md")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true")
    ap.add_argument("--with-share", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-console", action="store_true", help="skip 17-operator-console-e2e")
    asyncio.run(main(ap.parse_args()))
