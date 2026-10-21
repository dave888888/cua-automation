"""Concurrency and regression tests for session ownership (no browser needed).

They drive SessionControl with real asyncio tasks to pin down the guarantees:
drain before transfer, no admission in the wrong state, explicit operator
ownership (claim → lease → resolve), stale/duplicate/foreign requests refused,
verification decided before control moves (accept / keep with operator /
terminate), and the resume-point rule that never re-executes an attempted
irreversible step.
"""
from tests import _harness  # noqa: F401

import asyncio
import tempfile
from pathlib import Path

from cua.handoff.control import (ControlLost, ControlState, Decision, HandbackVerdict, InterventionKind,
                                 NotAuthorized, SessionControl, StaleRequest)
from cua.redact import Redactor
from cua.replay.resume import StepFacts, resume_point
from cua.surface.base import Actor
from tests._harness import raises

OPS = ("ops.a", "ops.b")


class _Log:
    def __init__(self) -> None:
        self.redactor = Redactor()
        self.events: list[tuple[str, dict]] = []

    def event(self, type_: str, **kw) -> None:
        self.events.append((type_, kw))


def _control(**kw) -> tuple[SessionControl, _Log]:
    log = _Log()
    kw.setdefault("operators", OPS)
    return SessionControl(f"t-{id(log)}", Path(tempfile.mkdtemp()), log, **kw), log


async def _escalate(c: SessionControl, verify=None, timeout_s: float = 5, kind=InterventionKind.failure):
    return await c.escalate(run_id="r", kind=kind, subject="s", code="TEST", reason="test",
                            timeout_s=timeout_s, verify=verify)


async def _wait_state(c: SessionControl, state: ControlState) -> None:
    for _ in range(400):
        if c.state == state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"never reached {state}, at {c.state}")


async def _claim(c: SessionControl, op: str = "ops.a") -> str:
    await _wait_state(c, ControlState.awaiting_operator)
    lease, _ = c.claim(c.current.id, op, c.epoch)
    return lease


async def _resolve(c: SessionControl, lease: str, decision=Decision.resume, op: str = "ops.a"):
    return await c.resolve(c.current.id, decision, operator=op, epoch=c.epoch, lease=lease)


async def _ok(res=None):
    return HandbackVerdict(True, "test")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- barrier & admission

def test_escalation_waits_for_in_flight_agent_action():
    async def go():
        c, _ = _control()
        order = []

        async def slow_agent_action():
            async with c.acting(Actor.agent):
                order.append("agent-start")
                await asyncio.sleep(0.2)
                order.append("agent-end")

        t = asyncio.create_task(slow_agent_action())
        await asyncio.sleep(0.01)
        esc = asyncio.create_task(_escalate(c))
        await asyncio.sleep(0.05)
        assert c.state == ControlState.pausing          # no new agent actions, nobody may claim yet
        with raises(StaleRequest):
            c.claim(c.current.id if c.current else "x", "ops.a", c.epoch)
        await _wait_state(c, ControlState.awaiting_operator)
        order.append("awaiting")
        await t
        lease = await _claim(c)
        await _resolve(c, lease, Decision.abort)
        await esc
        return order
    assert run(go()) == ["agent-start", "agent-end", "awaiting"]


def test_nobody_acts_until_an_operator_claims():
    async def go():
        c, _ = _control()
        esc = asyncio.create_task(_escalate(c))
        await _wait_state(c, ControlState.awaiting_operator)
        for actor in (Actor.agent, Actor.human):         # paused: neither automation nor an unclaimed human
            with raises(ControlLost):
                async with c.acting(actor):
                    pass
        lease = await _claim(c)
        async with c.acting(Actor.human):                # the owner may act
            pass
        with raises(ControlLost):
            async with c.acting(Actor.agent):
                pass
        await _resolve(c, lease, Decision.abort)
        await esc
    run(go())


# ---------------------------------------------------------------- explicit ownership

def test_resume_before_claim_is_refused():
    async def go():
        c, _ = _control()
        esc = asyncio.create_task(_escalate(c, _ok))
        await _wait_state(c, ControlState.awaiting_operator)
        with raises(StaleRequest):                       # 409: claim it first
            await c.resolve(c.current.id, Decision.resume, operator="ops.a", epoch=c.epoch, lease="x")
        with raises(StaleRequest):
            c.check_operator(c.current.id, "ops.a", c.epoch, "x")
        lease = await _claim(c)
        assert (await _resolve(c, lease))["accepted"]
        await esc
    run(go())


def test_claim_rules_roster_double_claim_and_epochs():
    async def go():
        c, _ = _control()
        esc = asyncio.create_task(_escalate(c, _ok))
        await _wait_state(c, ControlState.awaiting_operator)
        iid, ep = c.current.id, c.epoch
        with raises(NotAuthorized):
            c.claim(iid, "mallory", ep)                  # not on the roster
        with raises(StaleRequest):
            c.claim(iid, "ops.a", None)                  # epoch is mandatory
        with raises(StaleRequest):
            c.claim(iid, "ops.a", ep - 1)                # stale epoch
        lease, ep2 = c.claim(iid, "ops.a", ep)
        assert ep2 == ep + 1 and c.owner == "ops.a" and c.current.claimed_by == "ops.a"
        with raises(StaleRequest):
            c.claim(iid, "ops.b", ep2)                   # double claim
        with raises(StaleRequest):
            c.claim(iid, "ops.a", ep2)                   # even by the owner
        await _resolve(c, lease)
        await esc
    run(go())


def test_only_the_owner_with_its_lease_and_epoch_may_act_or_resolve():
    async def go():
        c, _ = _control()
        esc = asyncio.create_task(_escalate(c, _ok))
        lease = await _claim(c, "ops.a")
        iid, ep = c.current.id, c.epoch
        with raises(NotAuthorized):
            c.check_operator(iid, "ops.b", ep, lease)    # another operator, even with the token
        with raises(NotAuthorized):
            c.check_operator(iid, "ops.a", ep, "forged")
        with raises(StaleRequest):
            c.check_operator(iid, "ops.a", ep - 1, lease)
        with raises(NotAuthorized):
            await c.resolve(iid, Decision.resume, operator="ops.b", epoch=ep, lease=lease)
        assert c.state == ControlState.human             # refused requests change nothing
        res = await _resolve(c, lease)
        assert res["accepted"]
        await esc
        with raises(StaleRequest):                       # old id/lease/epoch are dead after hand-back
            c.check_operator(iid, "ops.a", ep, lease)
    run(go())


def test_epoch_increments_on_every_transfer():
    async def go():
        c, _ = _control()
        e0 = c.epoch
        esc = asyncio.create_task(_escalate(c, _ok))
        await _wait_state(c, ControlState.awaiting_operator)
        e_wait = c.epoch
        lease = await _claim(c)
        e_claim = c.epoch
        await _resolve(c, lease)
        await esc
        return [e0, e_wait, e_claim, c.epoch]
    eps = run(go())
    assert eps == sorted(set(eps))


# ---------------------------------------------------------------- verification decides before control moves

def test_resolve_waits_for_in_flight_human_action_before_verifying():
    async def go():
        c, _ = _control()
        order = []

        async def verify(res):
            order.append("verify")
            return HandbackVerdict(True, "test")

        esc = asyncio.create_task(_escalate(c, verify))
        lease = await _claim(c)

        async def slow_human_action():
            async with c.acting(Actor.human):
                order.append("human-start")
                await asyncio.sleep(0.2)
                order.append("human-end")

        h = asyncio.create_task(slow_human_action())
        await asyncio.sleep(0.01)
        r = asyncio.create_task(_resolve(c, lease))
        await asyncio.sleep(0.01)
        assert c.state == ControlState.resuming
        with raises(ControlLost):                        # no new human actions once resolving
            async with c.acting(Actor.human):
                pass
        out = await r
        res = await esc
        await h
        return order, out, res, c.state
    order, out, res, state = run(go())
    assert order == ["human-start", "human-end", "verify"]
    assert out["accepted"] and res.resumed and state == ControlState.automation


def test_recoverable_rejection_keeps_the_operator_in_control():
    async def go():
        c, log = _control()
        cleared = {"v": False}

        async def verify(res):
            if not cleared["v"]:
                return HandbackVerdict(False, "blocked", "supervisor hold still present", recoverable=True)
            return HandbackVerdict(True, "continue@3")

        esc = asyncio.create_task(_escalate(c, verify))
        lease = await _claim(c, "ops.a")
        first = await _resolve(c, lease)
        assert first == {"accepted": False, "state": "human", "epoch": c.epoch, "owner": "ops.a",
                         "reason": "supervisor hold still present"}
        assert not esc.done()                            # automation is still paused
        with raises(ControlLost):
            async with c.acting(Actor.agent):
                pass
        async with c.acting(Actor.human):                # owner can keep working, same lease
            cleared["v"] = True
        second = await _resolve(c, lease)                # with the *new* epoch
        res = await esc
        return second, res, c.current, log.events
    second, res, cur, events = run(go())
    assert second["accepted"] and res.resumed
    assert any(t == "handback_rejected" for t, _ in events)


def test_failed_verification_terminates_instead_of_resuming():
    async def go():
        c, _ = _control()

        async def verify(res):
            return HandbackVerdict(False, "none", "nothing provable")   # not recoverable

        esc = asyncio.create_task(_escalate(c, verify))
        lease = await _claim(c)
        out = await _resolve(c, lease)
        res = await esc
        assert not out["accepted"] and not res.resumed and c.state == ControlState.terminated
        for actor in (Actor.agent, Actor.human):
            with raises(ControlLost):
                async with c.acting(actor):
                    pass
        with raises(ControlLost):                        # and nobody can escalate a dead session
            await _escalate(c)
    run(go())


def test_timeout_while_awaiting_claim_terminates_and_blocks_late_operator():
    async def go():
        c, _ = _control()
        task = asyncio.create_task(_escalate(c, _ok, timeout_s=0.2))
        await _wait_state(c, ControlState.awaiting_operator)
        iid, ep = c.current.id, c.epoch
        res = await task
        assert res.timed_out and not res.resumed and c.state == ControlState.terminated
        with raises(StaleRequest):
            c.claim(iid, "ops.a", ep)
    run(go())


def test_abort_never_restores_automation():
    async def go():
        calls = []

        async def verify(res):
            calls.append(1)
            return HandbackVerdict(True, "x")
        c, _ = _control()
        esc = asyncio.create_task(_escalate(c, verify))
        lease = await _claim(c)
        await _resolve(c, lease, Decision.abort)
        res = await esc
        return res, c.state, calls
    res, state, calls = run(go())
    assert not res.resumed and state == ControlState.terminated and calls == []


def test_human_action_outliving_drain_is_never_trusted():
    async def go():
        c, _ = _control(drain_timeout_s=0.1)
        verified = []

        async def verify(res):
            verified.append(1)
            return HandbackVerdict(True, "x")

        esc = asyncio.create_task(_escalate(c, verify))
        lease = await _claim(c)

        async def stuck():
            async with c.acting(Actor.human):
                await asyncio.sleep(1)
        h = asyncio.create_task(stuck())
        await asyncio.sleep(0.01)
        out = await _resolve(c, lease)
        res = await esc
        h.cancel()
        return out, res, c.state, verified
    out, res, state, verified = run(go())
    assert not out["accepted"] and not res.resumed and state == ControlState.terminated and verified == []
    assert "in flight" in res.verification_detail


def test_empty_roster_fails_closed():
    async def go():
        c, _ = _control(operators=())
        esc = asyncio.create_task(_escalate(c, _ok, timeout_s=0.3))
        await _wait_state(c, ControlState.awaiting_operator)
        with raises(NotAuthorized):
            c.claim(c.current.id, "ops.a", c.epoch)
        res = await esc
        assert res.timed_out
    run(go())


# ---------------------------------------------------------------- resume-point rule

def _f(sid, expect=True, holds=False, extract=False, irrev=False):
    return StepFacts(sid, expect, holds, extract, irrev)


def test_resume_after_last_proven_postcondition():
    steps = [_f("s1", holds=False), _f("s2", expect=False), _f("s3", holds=True), _f("s4", expect=False, extract=True)]
    rp = resume_point(3, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "continue" and rp.index == 3


def test_step_done_by_human_is_skipped():
    steps = [_f("s1", holds=True), _f("s2", holds=True)]
    rp = resume_point(1, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "continue" and rp.index == 2


def test_attempted_irreversible_step_is_never_retried():
    # s3 = Submit (attempted, its postcondition does NOT hold), s2 = review page (holds)
    steps = [_f("s1", holds=False), _f("s2", holds=True), _f("s3", holds=False, irrev=True)]
    rp = resume_point(2, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "unsafe" and "s3" in rp.reason


def test_no_restart_after_irreversible_attempt():
    steps = [_f("s1"), _f("s2", irrev=True), _f("s3", expect=False, extract=True)]
    rp = resume_point(2, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "unsafe"


def test_irreversible_step_whose_effect_is_proven_is_not_repeated():
    steps = [_f("s1", holds=False), _f("s2", holds=True, irrev=True), _f("s3", expect=False, extract=True)]
    rp = resume_point(2, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "continue" and rp.index == 2       # only the read after the commit is re-run


def test_read_only_flow_can_restart_from_entry():
    steps = [_f("s1"), _f("s2", expect=False)]
    rp = resume_point(1, steps, success_holds=False, outputs_complete=False)
    assert rp.kind == "restart" and rp.index == 0


def test_success_needs_outputs_too():
    steps = [_f("s1", holds=True), _f("s2", expect=False, extract=True)]
    assert resume_point(1, steps, success_holds=True, outputs_complete=False).kind == "continue"
    assert resume_point(1, steps, success_holds=True, outputs_complete=True).kind == "done"
