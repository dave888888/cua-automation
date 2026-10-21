"""Command line: `python -m cua <command>` (or `cua <command>` once installed)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .envfile import load_dotenv as _load_dotenv  # noqa: F401  (kept under the old name)


def _kv(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = v
    return out


def cmd_mockbank(a: argparse.Namespace) -> int:
    from mockbank.app import main
    print(f"CoreLink Teller mock on http://127.0.0.1:{a.port}/  (tenants: acme, blueriver)")
    main(a.port)
    return 0


def cmd_discover(a: argparse.Namespace) -> int:
    from .discovery.agent import parse_param
    from .runner import discover

    if a.scripted:
        from .discovery import scripted
        llm = scripted.ScriptedLLM({"savings_balance": scripted.savings_balance_script,
                                    "open_share": scripted.open_share_script}[a.scripted]())
        print("NOTE: using the scripted test double, not a model. This is not a real discovery run.", file=sys.stderr)
    else:
        from .discovery.llm import AnthropicLLM
        llm = AnthropicLLM(model=a.model)
    params = [parse_param(p) for p in a.param or []]
    result, run_dir = asyncio.run(discover(tenant=a.tenant, goal=a.goal, params=params, cap_id=a.id, llm=llm,
                                           title=a.title, headed=a.headed,
                                           operator_port=None if a.no_operator else a.operator_port))
    print(json.dumps({**result.__dict__, "run_dir": run_dir}, indent=2, default=str))
    return 0 if result.status == "recorded" else 2


def cmd_replay(a: argparse.Namespace) -> int:
    from .replay.engine import ReplayOptions
    from .runner import replay

    faults = {k: int(v) for k, v in _kv(a.fault).items()} or None
    opts = ReplayOptions(irreversible=a.irreversible, on_failure=a.on_failure,
                         use_tenant_overrides=not a.no_overrides)
    needs_console = a.on_failure == "escalate" or a.irreversible == "ask_operator"
    r = asyncio.run(replay(a.capability, tenant=a.tenant, inputs=_kv(a.input), opts=opts, headed=a.headed,
                           operator_port=a.operator_port if needs_console else None, faults=faults))
    print(r.model_dump_json(indent=2))
    return {"success": 0, "business_outcome": 0, "blocked": 3, "failed": 4}[r.status.value]


def cmd_approve(a: argparse.Namespace) -> int:
    from . import store
    from .redact import Redactor
    cap = store.load(a.capability)
    try:
        approved = store.approve(cap, a.by, a.notes)
    except store.ContractViolation as e:
        print(f"NOT approved: {e}", file=sys.stderr)
        return 3
    path = store.save(approved, Redactor())
    print(f"approved {cap.id}@{cap.version} ({cap.content_hash()}) -> {path}")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    from . import store
    print(store.review_card(store.load(a.capability)))
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    from . import store
    for c in store.list_all():
        print(f"{c.id:40} {c.version:8} {c.status.value:9} approved={c.is_approved!s:5} side_effects={c.side_effects.value}")
    return 0


def cmd_catalog(a: argparse.Namespace) -> int:
    from .catalog import catalog
    print(json.dumps(catalog(), indent=2))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn
    from .catalog import make_app
    print(f"capability API on http://127.0.0.1:{a.port}/capabilities", file=sys.stderr)
    uvicorn.run(make_app(), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def cmd_schema(a: argparse.Namespace) -> int:
    from .schema.artifact import capability_json_schema
    print(json.dumps(capability_json_schema(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="cua", description="Computer-use capability recorder / replayer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mockbank", help="run the CoreLink Teller mock app")
    p.add_argument("--port", type=int, default=8800)
    p.set_defaults(fn=cmd_mockbank)

    p = sub.add_parser("discover", help="LLM-driven run that records a capability")
    p.add_argument("--tenant", required=True)
    p.add_argument("--id", required=True, help="capability id, e.g. corelink.member.savings_balance")
    p.add_argument("--goal", required=True, help="goal with {param} placeholders")
    p.add_argument("--param", action="append",
                   help="name[:type[:sensitivity[:flags]]]=value, flags id+expose (repeatable), "
                        "e.g. member_id:string:pii:id=100234")
    p.add_argument("--title")
    p.add_argument("--model", default=None, help="model id (default $CUA_MODEL or claude-sonnet-5-5)")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--operator-port", type=int, default=8900)
    p.add_argument("--no-operator", action="store_true", help="no console; give up instead of escalating")
    p.add_argument("--scripted", choices=["savings_balance", "open_share"],
                   help="offline test double instead of a model (NOT a real discovery run)")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("replay", help="deterministic replay of a saved capability (no LLM)")
    p.add_argument("capability", help="id, id@version, or path to .yaml")
    p.add_argument("--tenant", required=True)
    p.add_argument("--input", action="append", help="name=value (repeatable)")
    p.add_argument("--irreversible", choices=["deny", "allow", "ask_operator"], default="deny")
    p.add_argument("--on-failure", choices=["fail", "escalate"], default="fail")
    p.add_argument("--no-overrides", action="store_true", help="ignore tenant overlay (shows drift)")
    p.add_argument("--fault", action="append", help="inject a mock-app fault, e.g. error500@/mbr/result=1")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--operator-port", type=int, default=8900)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("approve", help="approve a capability (pins its content hash)")
    p.add_argument("capability")
    p.add_argument("--by", required=True)
    p.add_argument("--notes", default="")
    p.set_defaults(fn=cmd_approve)

    for name, fn, hlp in [("show", cmd_show, "print the review card")]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("capability")
        p.set_defaults(fn=fn)
    sub.add_parser("list", help="list saved capabilities").set_defaults(fn=cmd_list)
    sub.add_parser("catalog", help="print agent tool definitions").set_defaults(fn=cmd_catalog)
    sub.add_parser("schema", help="print the artifact JSON Schema").set_defaults(fn=cmd_schema)
    p = sub.add_parser("serve", help="serve the capability catalog API")
    p.add_argument("--port", type=int, default=8700)
    p.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
