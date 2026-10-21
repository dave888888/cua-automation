"""End-to-end against the real mock app in a real (headless) browser.

Discovery here uses the scripted test double instead of a model so it runs
offline/in CI; everything downstream of the model (guard, grounding, recorder,
artifact, replay, detectors, handoff) is the production code path.
"""
from tests import _harness

import asyncio
import functools
import json
import urllib.request

from cua import runner, store
from cua.discovery.agent import parse_param
from cua.discovery.scripted import ScriptedLLM, open_share_script, savings_balance_script
from cua.handoff.scripted_operator import Plan, run_operator
from cua.redact import Redactor
from cua.replay.engine import ReplayOptions

BAL = "corelink.member.savings_balance"
SHARE = "corelink.member.open_share"
SHARE_INPUTS = {"member_id": "100456", "share_type": "Savings Club", "nickname": "Holiday fund",
                "opening_deposit": "25.00"}


@functools.lru_cache(maxsize=None)
def recorded_balance() -> str:
    _harness.ensure_mockbank()
    r, _ = asyncio.run(runner.discover(
        tenant="acme", goal="Look up member {member_id} and read their current savings balance",
        params=[parse_param("member_id:string:pii:id=100234")], cap_id=BAL,
        llm=ScriptedLLM(savings_balance_script()), operator_port=None, echo=False))
    assert r.status == "recorded", r
    return r.artifact_path


@functools.lru_cache(maxsize=None)
def recorded_share() -> str:
    _harness.ensure_mockbank()

    async def go():
        op = asyncio.create_task(run_operator(8911, [Plan("approve", "IRREVERSIBLE_STEP")]))
        r, _ = await runner.discover(
            tenant="acme", goal="Open a {share_type} share named {nickname} with {opening_deposit} for member "
                                "{member_id} and return the confirmation number",
            params=[parse_param("member_id:string:pii:id=100456"), parse_param("share_type:string:public=Savings Club"),
                    parse_param("nickname=Holiday fund"), parse_param("opening_deposit:decimal=25.00")],
            cap_id=SHARE, llm=ScriptedLLM(open_share_script()), operator_port=8911, echo=False)
        await op
        return r
    r = asyncio.run(go())
    assert r.status == "recorded", r
    cap = store.load(r.artifact_path)
    store.save(store.approve(cap, "test-reviewer"), Redactor())
    return r.artifact_path


def replay(cap: str, inputs: dict, tenant: str = "acme", **kw):
    return asyncio.run(runner.replay(cap, tenant=tenant, inputs=inputs, echo=False, **kw))


# ---------------------------------------------------------------- artifact

def test_artifact_is_parameterised_and_pii_free():
    path = recorded_balance()
    text = open(path).read()
    cap = store.load(path)
    assert "100234" not in text and "900-12-3456" not in text
    assert [p.name for p in cap.inputs] == ["member_id"]
    assert cap.outputs[0].name == "savings_balance" and cap.outputs[0].sensitivity.value == "financial"
    s3 = cap.steps[2]
    assert s3.expect.route == "/mbr/result?q1={{member_id}}"
    # the label-less input is anchored on its neighbouring cell, not on markup
    assert cap.steps[1].target.strategies[0].kind == "label"
    # the extracted cell is anchored on its row header, never on its value
    assert cap.steps[3].target.strategies[0].model_dump()["anchor"] == "Share Savings"


# ---------------------------------------------------------------- result taxonomy

def test_success_returns_outputs():
    r = replay(recorded_balance(), {"member_id": "100234"})
    assert r.status.value == "success" and r.outputs == {"savings_balance": "2345.67"}


def test_not_found_is_a_business_outcome():
    r = replay(recorded_balance(), {"member_id": "100999"})
    assert r.status.value == "business_outcome" and r.outcome.code == "NOT_FOUND"


def test_access_denied_is_a_business_outcome():
    r = replay(recorded_balance(), {"member_id": "100777"})
    assert r.status.value == "business_outcome" and r.outcome.code == "ACCESS_DENIED"


def test_bad_input_blocked_before_touching_the_app():
    r = replay(recorded_balance(), {"member_id": "12ab"})
    assert r.status.value == "blocked" and r.blocked.code == "INVALID_INPUT" and r.steps == []


def test_interstitial_dismissed():
    r = replay(recorded_balance(), {"member_id": "100234"}, faults={"interstitial@/mbr/result": 1})
    assert r.status.value == "success"
    assert [x.action for x in r.recoveries] == ["dismiss"]


def test_transient_error_restarts_flow():
    r = replay(recorded_balance(), {"member_id": "100234"}, faults={"error500@/mbr/result": 1})
    assert r.status.value == "success" and [x.action for x in r.recoveries] == ["restart_flow"]


def test_persistent_error_exhausts_recovery():
    r = replay(recorded_balance(), {"member_id": "100234"}, faults={"error500@/mbr/find": 5})
    assert r.status.value == "failed" and r.failure.kind.value == "recovery_exhausted"
    assert r.failure.evidence  # masked screenshot + redacted text snapshot


def test_session_expiry_relogs_and_restarts():
    r = replay(recorded_balance(), {"member_id": "100456"}, faults={"session_expire@/mbr/result": 1})
    assert r.status.value == "success" and r.outputs["savings_balance"] == "15.00"
    assert [x.action for x in r.recoveries] == ["restart_session"] and r.drift == []


def test_supervisor_hold_requires_a_human():
    """A known state only a person may clear: recognised at once, never auto-'recovered'."""
    r = replay(recorded_balance(), {"member_id": "100234"}, faults={"supervisor_hold@/mbr/result": 1})
    assert r.status.value == "failed" and r.failure.kind.value == "requires_human"
    assert r.failure.step_id == "s4" and r.failure.code == "SUPERVISOR_HOLD" and r.failure.evidence
    assert r.duration_ms < 6000


# ---------------------------------------------------------------- multi-tenant

def test_other_tenant_with_overlay_has_no_drift():
    r = replay(recorded_balance(), {"member_id": "100234"}, tenant="blueriver")
    assert r.status.value == "success" and r.drift == []


def test_other_tenant_without_overlay_succeeds_but_reports_drift():
    r = replay(recorded_balance(), {"member_id": "100234"}, tenant="blueriver",
               opts=ReplayOptions(use_tenant_overrides=False))
    assert r.status.value == "success"
    assert {d.step_id for d in r.drift} == {"s1", "s2", "s3", "s4"}


# ---------------------------------------------------------------- irreversible + handoff

def _shares() -> list:
    recorded_share()  # make sure discovery (which itself posts one share) has happened first
    return json.loads(urllib.request.urlopen("http://127.0.0.1:8800/__state").read())["shares_created"]


def test_irreversible_needs_caller_authorisation():
    r = replay(recorded_share(), SHARE_INPUTS)
    assert r.status.value == "blocked" and r.blocked.code == "IRREVERSIBLE_NOT_AUTHORIZED"


def test_irreversible_with_authorisation_commits_once():
    before = len(_shares())
    r = replay(recorded_share(), SHARE_INPUTS, opts=ReplayOptions(irreversible="allow"))
    assert r.status.value == "success" and r.outputs["confirmation_number"].startswith("SH-")
    assert r.drift == []                # same tenant it was recorded on: any drift would be a false alarm
    assert len(_shares()) == before + 1


def test_irreversible_on_other_tenant_via_overlay():
    r = replay(recorded_share(), SHARE_INPUTS, tenant="blueriver", opts=ReplayOptions(irreversible="allow"))
    assert r.status.value == "success" and r.drift == []


def test_validation_error_is_business_outcome_before_commit():
    before = len(_shares())
    r = replay(recorded_share(), {**SHARE_INPUTS, "opening_deposit": "9999"},
               opts=ReplayOptions(irreversible="allow"))
    assert r.status.value == "business_outcome" and r.outcome.code == "VALIDATION_ERROR"
    assert len(_shares()) == before


def test_error_after_commit_is_in_doubt_and_not_retried():
    before = len(_shares())
    r = replay(recorded_share(), SHARE_INPUTS, opts=ReplayOptions(irreversible="allow"),
               faults={"lost_response": 1})
    assert r.status.value == "failed" and r.failure.kind.value == "in_doubt"
    assert len(_shares()) == before + 1   # posted exactly once; no blind retry


def test_edited_artifact_loses_approval():
    cap = store.load(recorded_share())
    cap.steps[0].intent = "tampered"
    r = asyncio.run(runner.replay(cap, tenant="acme", inputs=SHARE_INPUTS, echo=False,
                                  opts=ReplayOptions(irreversible="allow")))
    assert r.status.value == "blocked" and r.blocked.code == "APPROVAL_REQUIRED"


def test_operator_can_deny_irreversible_step():
    async def go():
        op = asyncio.create_task(run_operator(8912, [Plan("deny", "IRREVERSIBLE_STEP", note="hold")]))
        r = await runner.replay(recorded_share(), tenant="acme", inputs=SHARE_INPUTS, echo=False,
                                opts=ReplayOptions(irreversible="ask_operator"), operator_port=8912)
        await op
        return r
    before = len(_shares())
    r = asyncio.run(go())
    assert r.status.value == "blocked" and r.blocked.code == "APPROVAL_DENIED" and len(r.interventions) == 1
    assert len(_shares()) == before


def test_human_takes_over_live_session_and_hands_back():
    cap_path = recorded_balance()                 # (discovery must not run inside the event loop below)

    async def go():
        op = asyncio.create_task(run_operator(8913, [Plan(
            "resume", "SUPERVISOR_HOLD", actions=[{"op": "click", "ref_finder": '"Release Hold"'}],
            note="released supervisor hold", resume_first=True)]))
        r = await runner.replay(cap_path, tenant="acme", inputs={"member_id": "100234"}, echo=False,
                                opts=ReplayOptions(on_failure="escalate"), operator_port=8913,
                                faults={"supervisor_hold@/mbr/result": 1})
        return r, await op
    r, ops = asyncio.run(go())
    assert r.status.value == "success" and r.outputs == {"savings_balance": "2345.67"}
    assert len(r.interventions) == 1 and ops[0]["code"] == "SUPERVISOR_HOLD"
    early, final = ops[0]["steps"][0], ops[0]["steps"][-1]
    assert early["status"] == 409 and "SUPERVISOR_HOLD is still present" in early["result"]["reason"]
    assert final["status"] == 200 and final["result"]["resumed"]
    rec = json.loads(open(f"{r.run_dir}/interventions/{r.interventions[0]}.json").read())
    assert rec["claimed_by"] == "ops.jdoe" and len(rec["rejected_resumes"]) == 1
    acts = rec["resolution"]["human_actions"]
    assert [a["target"] for a in acts if a.get("source") == "console"] == ["Release Hold"]
    assert all(a["by"] == "ops.jdoe" for a in acts)


def test_discovery_escalation_records_human_steps_in_artifact():
    """Model asks for help; the operator performs a step on the live session; it lands
    in the artifact as provenance=human and the model finishes the task."""
    _harness.ensure_mockbank()
    script = [
        lambda o: ("ask_human", {"reason": "I cannot find the member search function", "intent": "need help"}),
    ] + savings_balance_script()[1:]

    async def go():
        op = asyncio.create_task(run_operator(8914, [Plan(
            "resume", "MODEL_ASKED", actions=[{"op": "click", "ref_finder": 'link "Member Search"'}],
            note="it is under Functions")]))
        r, _ = await runner.discover(
            tenant="acme", goal="Look up member {member_id} and read their current savings balance",
            params=[parse_param("member_id:string:pii:id=100234")], cap_id="corelink.member.savings_balance_assisted",
            llm=ScriptedLLM(script), operator_port=8914, echo=False)
        await op
        return r
    r = asyncio.run(go())
    assert r.status == "recorded", r
    cap = store.load(r.artifact_path)
    assert cap.steps[0].provenance == "human" and cap.provenance.human_steps == 1
    assert cap.steps[0].expect.route == "/mbr/find"
    rr = replay(r.artifact_path, {"member_id": "100456"})
    assert rr.status.value == "success" and rr.outputs == {"savings_balance": "15.00"}


# ================================================================ verified discovery (P0-1)

def test_every_recorded_step_carries_its_proof():
    cap = store.load(recorded_balance())
    assert [s.verification.method for s in cap.steps] == ["route", "readback", "route", "value_parse"]
    assert cap.review_required_steps == []
    share = store.load(recorded_share())
    commit = next(s for s in share.steps if s.risk.value == "irreversible")
    assert commit.verification.method == "route" and commit.expect.route == "/mbr/{{member_id}}/share/commit"
    assert commit.dialog is not None   # the app's own confirm() is part of the contract


def test_discovery_rejects_unverifiable_steps():
    """Validate/Verify stages: bad params, sensitive literals, no-effect clicks and values that
    don't parse never reach the artifact; the model is told why."""
    _harness.ensure_mockbank()
    from cua.discovery.scripted import find
    script = [
        lambda o: ("click", {"ref": find(o, role="link", name="Member Search"), "intent": "open search"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Member ID", frame="main"), "param": "nope",
                            "intent": "undeclared param"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Member ID", frame="main"),
                            "value": "900-12-3456", "intent": "sensitive literal"}),
        lambda o: ("fill", {"ref": find(o, role="textbox", label="Member ID", frame="main"), "param": "member_id",
                            "intent": "enter member"}),
        lambda o: ("click", {"ref": find(o, role="cell", name="Member ID", frame="main"), "intent": "click a label"}),
        lambda o: ("click", {"ref": find(o, role="button", name="Search", frame="main"), "intent": "search"}),
        lambda o: ("extract", {"ref": find(o, role="cell", label="Member Since", frame="main"), "output": "bal",
                               "type": "decimal", "description": "wrong cell", "intent": "wrong cell"}),
        lambda o: ("fill", {"ref": find(o, role="cell", label="Share Savings", frame="main"), "param": "member_id",
                            "intent": "fill a cell"}),
    ] + savings_balance_script()[3:]
    llm = ScriptedLLM(script)
    r, _ = asyncio.run(runner.discover(
        tenant="acme", goal="Read member {member_id}'s savings balance",
        params=[parse_param("member_id:string:pii:id=100234")], cap_id="corelink.member.savings_balance_strict",
        llm=llm, operator_port=None, echo=False))
    assert r.status == "recorded", r
    cap = store.load(r.artifact_path)
    assert [s.action for s in cap.steps] == ["click", "fill", "click", "extract"]
    results = [c["content"] for msgs in llm.seen for m in msgs if m["role"] == "user"
               for c in m["content"] if c.get("type") == "tool_result"]
    joined = "\n".join(dict.fromkeys(results))
    assert "unknown parameter 'nope'" in joined
    assert "looks sensitive" in joined
    assert "no observable effect" in joined
    assert "not a valid decimal" in joined
    assert "fill needs a textbox" in joined


# ================================================================ business invariants (P0-3)

def test_artifact_asserts_record_identity():
    cap = store.load(recorded_balance())
    shown, bound = cap.identity_binding()
    assert "member_id" in shown                      # proven by a displayed value, not just the URL
    extract = cap.steps[3]
    assert extract.require.kind == "value" and extract.require.equals.param == "member_id"
    assert [i.id for i in cap.invariants] == ["shows_member_id"]


def test_wrong_record_is_never_success():
    """Right page title, right URL, different member: must fail, quickly, without leaking it."""
    r = replay(recorded_balance(), {"member_id": "100234"}, faults={"wrong_record@/mbr/result": 1})
    assert r.status.value == "failed" and r.outputs == {}
    assert r.failure.kind.value == "invariant_violated" and r.failure.code == "RECORD_MISMATCH"
    assert r.duration_ms < 6000
    import glob
    for f in [f"{r.run_dir}/events.jsonl", *glob.glob(f"{r.run_dir}/dom/*.txt")]:
        text = open(f).read()
        assert "100456" not in text and "Robert Testcase" not in text, f   # the other member's data stays out


def test_share_confirmation_reflects_the_operation():
    cap = store.load(recorded_share())
    ids = {i.id for i in cap.invariants}
    assert {"shows_share_type", "confirmation_number_shape"} <= ids
    r = replay(recorded_share(), {**SHARE_INPUTS, "share_type": "Money Market"},
               opts=ReplayOptions(irreversible="allow"))
    assert r.status.value == "success"               # invariants are parameterised, not recorded values


# ================================================================ ownership + handback (P0-2)

def test_approval_is_void_if_operator_changes_the_screen():
    async def go():
        op = asyncio.create_task(run_operator(8915, [Plan(
            "approve", "IRREVERSIBLE_STEP", actions=[{"op": "click", "ref_finder": '"Cancel"'}])]))
        r = await runner.replay(recorded_share(), tenant="acme", inputs=SHARE_INPUTS, echo=False,
                                opts=ReplayOptions(irreversible="ask_operator", escalation_timeout_s=60),
                                operator_port=8915)
        await op
        return r
    before = len(_shares())
    r = asyncio.run(go())
    assert r.status.value == "blocked" and r.blocked.code == "APPROVAL_STATE_CHANGED"
    assert len(_shares()) == before


def test_handback_after_lost_commit_never_reposts():
    async def go():
        op = asyncio.create_task(run_operator(8916, [Plan("resume", "APP_ERROR_TRANSIENT"),
                                                     Plan("abort", "APP_ERROR_TRANSIENT")]))
        r = await runner.replay(recorded_share(), tenant="acme", inputs=SHARE_INPUTS, echo=False,
                                opts=ReplayOptions(irreversible="allow", on_failure="escalate",
                                                   escalation_timeout_s=60),
                                operator_port=8916, faults={"lost_response": 1})
        await op
        return r
    before = len(_shares())
    r = asyncio.run(go())
    assert r.status.value == "failed" and r.failure.kind.value == "in_doubt"
    assert len(r.interventions) == 1                # resume rejected (error page still showing), then abort
    rec = json.loads(open(f"{r.run_dir}/interventions/{r.interventions[0]}.json").read())
    assert len(rec["rejected_resumes"]) == 1 and rec["resolution"]["decision"] == "abort"
    assert len(_shares()) == before + 1             # Submit was never clicked again


# ================================================================ model context (P1-5)

def test_model_never_sees_parameter_values_or_sensitive_fields():
    _harness.ensure_mockbank()
    llm = ScriptedLLM(savings_balance_script())
    r, run_dir = asyncio.run(runner.discover(
        tenant="acme", goal="Look up member {member_id} and read their current savings balance",
        params=[parse_param("member_id:string:pii:id+expose=100234")],   # expose asked, policy refuses
        cap_id="corelink.member.savings_balance_leakcheck", llm=llm, operator_port=None, echo=False))
    assert r.status == "recorded"
    text = json.dumps([[c for m in msgs for c in m["content"] if c.get("type") != "image"] for msgs in llm.seen])
    for secret in ("100234", "900-12-3456", "Jane Q. Sample", "2,345.67", "demo-only-pass", "teller01"):
        assert secret not in text, secret
    assert "{{member_id}}" in text and "[REDACTED:field:member]" in text
    events = open(f"{run_dir}/events.jsonl").read()
    assert '"exposure_denied"' in events and "Jane Q. Sample" not in events


# ================================================================ contracts (P1-6)

def test_contract_gates_unattended_and_tenant_specialisation():
    from cua import contracts
    for path in (recorded_balance(), recorded_share()):
        cap = store.load(path)
        assert contracts.check(cap) == [] and contracts.unattended_ok(cap)[0]
    # a capability with an unverified step: replay without an operator is refused up front
    cap = store.load(recorded_balance())
    steps = list(cap.steps)
    steps[0] = steps[0].model_copy(update={"verification": steps[0].verification.model_copy(
        update={"review_required": True, "note": "test"})})
    flagged = cap.model_copy(update={"steps": steps, "id": "corelink.member.savings_balance_flagged"})
    r = asyncio.run(runner.replay(flagged, tenant="acme", inputs={"member_id": "100234"}, echo=False))
    assert r.status.value == "blocked" and r.blocked.code == "CONTRACT_VIOLATION" and r.steps == []
