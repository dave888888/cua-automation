# cua-automation
Computer Use Agent Automation
# Computer-use capabilities for legacy back-office apps

**The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it.**

An LLM drives a hostile, legacy-style banking app (framesets, table layouts,
label-less inputs, no test IDs) to accomplish a goal. The run is compiled into a
typed, versioned **capability artifact**. That artifact is then replayed with
**no model in the loop**. Replay returns a structured result:

- success, with outputs
- a business outcome (`NOT_FOUND`, `ACCESS_DENIED`, `VALIDATION_ERROR`)
- blocked
- a debuggable failure

Replay recovers from known runtime states and hands the *live* session to a
human when it can't.

The trust boundary in one line: **the model only proposes**. The host validates
every proposed action, binds the real values itself, executes it under a session
lease, and records it only after *verifying* its effect. Success then requires the
business result to be proven, for example "the screen shows the requested
member", not just the right page title.

Design write-up: [REPORT.md](REPORT.md). Evidence: [evidence/](evidence/).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install --with-deps chromium
cp .env.example .env        # add ANTHROPIC_API_KEY; the mock credentials are already there
```

Python 3.11+. The only live services are the model API (discovery only) and the
local mock app.

## Demo path

```bash
# terminal 1 — the target app (CoreLink Teller mock, tenants "acme" and "blueriver")
python -m cua mockbank

# terminal 2 — 1) LLM discovery: goal -> recorded capability artifact
python -m cua discover --tenant acme \
  --id corelink.member.savings_balance --title "Member savings balance" \
  --goal "Look up member {member_id} and read their current savings balance" \
  --param member_id:string:pii:id=100234
#   --param name[:type[:sensitivity[:flags]]]=value   flags: id (identifies the record),
#   expose (let the model see the value; pii/financial/secret also need policy opt-in).
#   The model works with {{member_id}}; the host types the value.
#   -> capabilities/corelink.member.savings_balance/0.1.0.yaml (+ .md review card)
#   -> runs/discovery-*/events.jsonl, shots/
#   If it needs a human, the terminal prints an operator link (with the session token).

# 2) deterministic replay (no LLM) with different inputs
python -m cua replay corelink.member.savings_balance --tenant acme --input member_id=100456
python -m cua replay corelink.member.savings_balance --tenant acme --input member_id=100999   # NOT_FOUND
python -m cua replay corelink.member.savings_balance --tenant acme --input member_id=100234 \
  --fault error500@/mbr/result=1                                                              # recovered
python -m cua replay corelink.member.savings_balance --tenant blueriver --input member_id=100234   # other tenant

# 3) human escalation: a "supervisor hold" only a person may clear
python -m cua replay corelink.member.savings_balance --tenant acme --input member_id=100234 \
  --fault supervisor_hold@/mbr/result=1 --on-failure escalate --headed
#   open the ">> operator needed" link the terminal prints (it carries the session token)
#   -> Claim (operator ops.jdoe; roster in config/policy/default.yaml) -> try "resume": rejected,
#   the hold is still there and you keep control -> click "Release Hold" on the live screenshot
#   -> "resume": accepted; automation continues from a checkpoint proven on the live app.

# 4) a business invariant at work: right page, right URL, wrong member -> never "success"
python -m cua replay corelink.member.savings_balance --tenant acme --input member_id=100234 \
  --fault wrong_record@/mbr/result=1                         # failed: invariant_violated / RECORD_MISMATCH
```

**One command for all evidence.** This runs one real discovery plus every replay
scenario, copied into `evidence/` with an index:

```bash
python scripts/make_evidence.py                # needs ANTHROPIC_API_KEY
python scripts/make_evidence.py --with-share   # + the irreversible open-share flow
python scripts/console_e2e.py                  # (run automatically at the end) a real browser operates
                                               # the console: evidence/17-operator-console-e2e/ with
                                               # console.webm, screenshots and assertions.json
```

### Other commands

| command | what it does |
|---|---|
| `cua show <id>` | human review card of an artifact |
| `cua approve <id> --by you` | approve; refused if the capability violates its workflow contract (`config/contracts/`); pins the content hash |
| `cua list` / `cua schema` | saved capabilities / artifact JSON Schema |
| `cua catalog` | artifacts as agent tool definitions (typed in/out) |
| `cua serve` | capability API: `GET /capabilities`, `POST /capabilities/{name}/invoke` |
| `python examples/agent_invoke.py "…"` | an agent discovers and calls capabilities via tool use |

Replay flags:

- `--irreversible deny|allow|ask_operator`: irreversible steps need an *approved*
  artifact **and** caller authorisation.
- `--on-failure fail|escalate`. Without an operator the run is *unattended*, which
  the contract must permit and which is refused for capabilities with unverified steps.
- `--no-overrides`: replay on another tenant without its overlay, to see drift
- `--fault name[@route]=n`: inject a mock-app fault. Available faults are
  `interstitial`, `slow_count` (with `slow_ms`), `error500`, `session_expire`,
  `supervisor_hold`, `lost_response` and `wrong_record`.

Replay exit codes: `0` for success or business outcome, `3` for blocked, `4` for failed.

## Running without live services

```bash
pytest -q             # or: python -m tests.run
```

The suite (89 tests, about 2 minutes) starts the mock app itself and drives a real
headless Chromium. It covers:

- every result class, recovery, tenant overlay and drift
- irreversible gating and in-doubt handling
- verified discovery: rejected unverifiable steps, and the proof recorded on each step
- record-identity and confirmation invariants
- the human takeover
- ownership concurrency and the claim/lease/epoch contract (`tests/test_control.py`)
- the console's HTTP contract: tokens, 401/403/409s, escaping (`tests/test_console_http.py`)
- sandbox boundaries (`tests/test_sandbox.py`)
- that parameter values and sensitive fields never reach the model

Discovery in tests uses `cua/discovery/scripted.py`. That is a test double that
answers through the same tool interface as the model, so everything downstream of
the model runs the production code path. It is labelled `scripted-test-double`
everywhere it appears, and **it is not evidence of discovery**.
`python -m cua discover --scripted savings_balance …` runs it from the CLI.

## Layout

```
cua/
  schema/artifact.py   the capability artifact (the contract)       ← start here
  schema/results.py    the replay result contract
  schema/config.py     policy + sandbox / vendor app profile / tenant overlay / workflow contracts
  surface/             the perception/action seam; web.py + scan.js = Playwright adapter
  discovery/           agent.py (observe→decide→validate→execute→verify→record), verify.py,
                       recorder.py, prompts.py, llm.py, scripted.py
  replay/engine.py     deterministic replay + error taxonomy + escalation
  replay/resume.py     where automation may safely resume after a hand-back (pure)
  handoff/             ownership state machine (control.py), operator console, scripted operator
  guard.py redact.py   allowlist, request sandbox & risk / redaction + symbolizer
  values.py            one parser/normaliser shared by discovery, replay and conditions
  contracts.py         workflow-contract checks (approval, replay, unattended)
  tenancy.py           cross-tenant specialisation + overlay preservation check
  catalog.py           agent-facing capability API
config/                apps/ (vendor profile), tenants/, policy/, contracts/
mockbank/app.py        the hostile target app, with fault injection
capabilities/          saved artifacts (YAML + review card)
evidence/              demonstration runs
```
