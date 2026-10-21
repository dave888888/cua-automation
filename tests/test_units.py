"""Fast unit tests: schema contract, routes, redaction, guard, tenant overlay."""
from tests import _harness  # noqa: F401

from tests._harness import raises

from cua import routes
from cua.guard import Guard
from cua.redact import Redactor
from cua.config import load_policy, load_tenant
from cua.schema.artifact import (AppBinding, Capability, ClickStep, ExtractStep, FillStep, OutputSpec, ParamRef,
                                 ParamSpec, RiskClass, RoleStrategy, Sensitivity, Status, Target, TextCondition)
from cua.store import approve, lint
from cua.tenancy import specialize


def _cap(**over) -> Capability:
    base = dict(
        id="corelink.test.cap", version="0.1.0", title="t", description="d",
        app=AppBinding(product="corelink-teller", versions=">=4.2,<5"),
        inputs=[ParamSpec(name="member_id", sensitivity=Sensitivity.pii)],
        outputs=[OutputSpec(name="bal", from_step="s3")],
        entry_route="/desk",
        steps=[
            FillStep(id="s1", intent="enter", target=Target(strategies=[RoleStrategy(role="textbox", name="Member ID")]),
                     value=ParamRef(param="member_id")),
            ClickStep(id="s2", intent="go", target=Target(strategies=[RoleStrategy(role="button", name="Search")])),
            ExtractStep(id="s3", intent="read", output="bal",
                        target=Target(strategies=[RoleStrategy(role="cell", name="x")])),
        ],
        success=TextCondition(pattern="Member Detail"),
    )
    base.update(over)
    return Capability(**base)


# ---- schema contract

def test_undeclared_param_rejected():
    with raises(Exception):
        _cap(inputs=[])


def test_output_must_come_from_extract_step():
    with raises(Exception):
        _cap(outputs=[OutputSpec(name="bal", from_step="s2")])


def test_sensitive_examples_are_dropped():
    p = ParamSpec(name="ssn", sensitivity=Sensitivity.pii, example="900-12-3456")
    assert p.example is None
    assert ParamSpec(name="kind", sensitivity=Sensitivity.public, example="Savings").example == "Savings"


def test_side_effects_derived_from_steps():
    c = _cap()
    assert c.side_effects == RiskClass.read
    steps = list(c.steps)
    steps[1] = steps[1].model_copy(update={"risk": RiskClass.irreversible})
    with raises(Exception):                      # an irreversible step must prove its effect
        _cap(steps=steps)
    steps[1] = steps[1].model_copy(update={"expect": TextCondition(pattern="Done")})
    assert _cap(steps=steps).side_effects == RiskClass.irreversible


def test_approval_is_pinned_to_content():
    c = approve(_cap(), "reviewer")
    assert c.is_approved and c.status == Status.approved
    c.steps[0].intent = "edited after approval"
    assert not c.is_approved


def test_hash_ignores_review_metadata():
    a, b = _cap(), _cap()
    approve(b, "x")
    assert a.content_hash() == b.content_hash()


# ---- routes

def test_canonicalize_params_and_ids():
    base = "http://h/acme"
    assert routes.canonicalize("http://h/acme/mbr/100234/share/new", base, {"member_id": "100234"}) == \
        "/mbr/{{member_id}}/share/new"
    assert routes.canonicalize("http://h/acme/mbr/result?q1=100234", base, {"member_id": "100234"}) == \
        "/mbr/result?q1={{member_id}}"
    assert routes.canonicalize("http://h/acme/mbr/555555/x", base, {}) == "/mbr/:id/x"


def test_route_matching_across_tenants():
    t = "/mbr/result?q1={{member_id}}"
    assert routes.matches(t, "http://h/blueriver/mbr/result?q1=100456", "http://h/blueriver", {"member_id": "100456"})
    assert not routes.matches(t, "http://h/blueriver/mbr/result?q1=1", "http://h/blueriver", {"member_id": "100456"})
    assert routes.matches("/mbr/:id/x", "http://h/a/mbr/42abc/x", "http://h/a", {})


# ---- redaction

def test_redaction_patterns_and_known_values():
    r = Redactor()
    r.register("100234", "input:member_id")
    out = r.text("member 100234 ssn 900-12-3456 card 4111 1111 1111 1111 mail a@b.com ver x@1.2.0")
    assert "100234" not in out and "900-12-3456" not in out and "4111" not in out and "a@b.com" not in out
    assert "x@1.2.0" in out  # versions are not emails
    assert r.text("order 4111111111111112") == "order 4111111111111112"  # fails Luhn: not a card


def test_money_values_redacted_in_display_format():
    r = Redactor()
    r.register("2345.67", "output:bal")
    assert "2,345.67" not in r.text("Balance $2,345.67")


def test_artifact_lint_flags_pii_literals():
    r = Redactor()
    r.register("100234", "input:member_id")
    c = _cap(success=TextCondition(pattern="Member 100234"))
    assert lint(c, r)


# ---- guard

def test_guard_allowlist_and_risk():
    g = Guard(load_policy("default"))
    assert g.url_allowed("http://127.0.0.1:8800/acme/mbr/find")[0]
    assert not g.url_allowed("http://127.0.0.1:8800/__faults")[0]
    assert not g.url_allowed("http://127.0.0.1:8800/acme/admin")[0]
    assert not g.url_allowed("https://evil.example/acme/mbr/find")[0]
    assert g.classify("click", "Submit") == RiskClass.irreversible
    assert g.classify("click", "Search") == RiskClass.read
    assert not g.check("click", "System Admin").allowed
    assert not g.check("click", "Release Hold").allowed
    # declared risk can raise, never lower
    assert g.classify("click", "Search", RiskClass.irreversible) == RiskClass.irreversible
    assert g.classify("click", "Submit", RiskClass.read) == RiskClass.irreversible


# ---- tenant overlay

def test_tenant_aliases_rewrite_semantic_strategies_only():
    from cua.schema.artifact import CssStrategy, LabelStrategy
    t = Target(strategies=[LabelStrategy(anchor="Member ID:"), CssStrategy(selector="input[name=q1]")])
    c = _cap(steps=[FillStep(id="s1", intent="x", target=t, value=ParamRef(param="member_id")),
                    ExtractStep(id="s3", intent="read", output="bal",
                                target=Target(strategies=[LabelStrategy(anchor="Share Savings", relation="row_cell")]))])
    eff, notes = specialize(c, load_tenant("blueriver"))
    assert eff.steps[0].target.strategies[0].anchor == "Member Number:"
    assert eff.steps[0].target.strategies[1].selector == "input[name=q1]"
    assert eff.steps[1].target.strategies[0].anchor == "Primary Savings"
    assert len(notes) == 2
    assert c.steps[0].target.strategies[0].anchor == "Member ID:"  # base untouched


# ================================================================ invariants & identity (P0-3)

from cua.schema.artifact import (Invariant, LabelStrategy, OutputCondition, UrlCondition, ValueCondition,  # noqa: E402
                                 AllCondition, LiteralValue, ValueType, Verification)


def _acct_cond(param="member_id"):
    return ValueCondition(target=Target(strategies=[LabelStrategy(anchor="Account #:", relation="row_cell")]),
                          equals=ParamRef(param=param))


def _id_cap(**over):
    return _cap(inputs=[ParamSpec(name="member_id", sensitivity=Sensitivity.pii, identifies_record=True)], **over)


def test_identity_input_requires_a_record_assertion():
    with raises(Exception):                                   # title-only success is not enough
        _id_cap()
    by_route = _id_cap(success=AllCondition(conditions=[UrlCondition(route="/mbr/result?q1={{member_id}}"),
                                                         TextCondition(pattern="Member Detail")]))
    assert by_route.identity_binding() == (set(), {"member_id"})
    shown = _id_cap(invariants=[Invariant(id="shows_member", when=_acct_cond())])
    assert shown.identity_binding()[0] == {"member_id"}


def test_any_branch_does_not_count_as_proof():
    from cua.schema.artifact import AnyCondition
    with raises(Exception):
        _id_cap(success=AnyCondition(conditions=[_acct_cond(), TextCondition(pattern="Member Detail")]))


def test_conditions_must_reference_declared_params_and_outputs():
    with raises(Exception):
        _cap(invariants=[Invariant(id="x", when=_acct_cond("nope"))])
    with raises(Exception):
        _cap(invariants=[Invariant(id="x", when=OutputCondition(output="nope", pattern=".*"))])


def test_value_parsing_and_comparison():
    from cua import values
    from cua.schema.artifact import ValueType
    assert values.parse_value("$2,345.67", ValueType.decimal) == "2345.67"
    assert values.parse_value("($12.00)", ValueType.decimal) == "-12.00"
    for bad in ("2011-04-02", "", "12.3.4", "abc"):
        with raises(values.ParseError):
            values.parse_value(bad, ValueType.decimal)
    with raises(values.ParseError):
        values.parse_value("1.5", ValueType.integer)
    assert values.matches(" 100234 ", equals="100234", pattern=None, how="text")
    assert values.matches("$25.00", equals="25", pattern=None, how="decimal")
    assert not values.matches("100456", equals="100234", pattern=None, how="text")
    assert values.matches("SH-48213", equals=None, pattern=r"^[A-Z]+\-\d+$", how="text")


def test_output_shape_generalises_ids_only():
    from cua.discovery.verify import output_shape
    assert output_shape("SH-48213") == r"^[A-Z]+\-\d+$"
    import re
    assert re.fullmatch(output_shape("SH-48213"), "SH-1") and not re.fullmatch(output_shape("SH-48213"), "sh-1")


# ================================================================ overlays & contracts (P1-6)

def test_overlay_may_relocate_controls_but_not_change_semantics():
    from cua.tenancy import OverlayViolation, check_overlay_preserves
    base = _cap()

    def with_step(i, **upd):
        steps = list(base.steps)
        steps[i] = steps[i].model_copy(update=upd)
        return base.model_copy(update={"steps": steps})

    # how a control is found may change
    check_overlay_preserves(base, with_step(0, target=Target(strategies=[RoleStrategy(role="textbox",
                                                                                      name="Member Number")])))
    # what the workflow does or asserts may not
    for bad in (with_step(1, risk=RiskClass.irreversible), with_step(0, value=LiteralValue(literal="1")),
                with_step(1, expect=TextCondition(pattern="anything")),
                base.model_copy(update={"success": TextCondition(pattern="anything")})):
        with raises(OverlayViolation):
            check_overlay_preserves(base, bad)
    with raises(OverlayViolation):                            # dropping an invariant is a semantic change
        check_overlay_preserves(_cap(invariants=[Invariant(id="i", when=TextCondition(pattern="x"))]), base)


def test_contract_violations_are_specific():
    from cua import contracts
    from cua.schema.config import OutputRule, WorkflowContract
    c = WorkflowContract(id="t", applies_to="*", route_scope=["/desk", "/mbr/*"], identity_inputs=["member_id"],
                         outputs={"bal": OutputRule(type=ValueType.decimal)}, checkpoint="identity_displayed",
                         must_confirm=["member_id"])
    cap = _id_cap(success=AllCondition(conditions=[UrlCondition(route="/admin/x?q={{member_id}}"),
                                                    TextCondition(pattern="Detail")]))
    v = " | ".join(contracts.check(cap, c))
    assert "route /admin/x outside contract scope" in v
    assert "output 'bal' is string" in v
    assert "must be proven by a displayed value" in v
    assert "not asserted to show 'member_id'" in v
    ok = _id_cap(success=AllCondition(conditions=[UrlCondition(route="/mbr/result?q1={{member_id}}"),
                                                   TextCondition(pattern="Detail")]),
                 invariants=[Invariant(id="shows_member", when=_acct_cond())],
                 outputs=[OutputSpec(name="bal", type=ValueType.decimal, from_step="s3")],
                 steps=[*_cap().steps[:2], ExtractStep(id="s3", intent="read", output="bal", parse=ValueType.decimal,
                                                       target=Target(strategies=[RoleStrategy(role="cell", name="x")]))])
    assert contracts.check(ok, c) == []


def test_unverified_steps_block_approval_and_unattended_replay():
    from cua import contracts
    from cua.store import ContractViolation, approve
    steps = list(_cap().steps)
    steps[1] = steps[1].model_copy(update={"verification": Verification(method="unverified", review_required=True)})
    cap = _cap(steps=steps)
    assert cap.review_required_steps == ["s2"]
    assert not contracts.unattended_ok(cap)[0]
    with raises(ContractViolation):
        approve(cap, "reviewer")


def test_irreversible_route_must_be_classed_irreversible():
    from cua import contracts
    from cua.schema.config import WorkflowContract
    c = WorkflowContract(id="t", applies_to="*", max_side_effects=RiskClass.irreversible, max_irreversible_steps=1,
                         irreversible_routes=["/mbr/*/share/commit"], checkpoint="screen")
    steps = list(_cap().steps)
    steps[1] = steps[1].model_copy(update={"expect": UrlCondition(route="/mbr/{{member_id}}/share/commit")})
    assert any("not classed irreversible" in x for x in contracts.check(_cap(steps=steps), c))


# ================================================================ sandbox & exposure (P1-4, P1-5)

def test_guard_request_rules():
    g = Guard(load_policy("default"))
    ok = lambda *a: g.request_allowed(*a)[0]   # noqa: E731
    assert ok("POST", "http://127.0.0.1:8800/acme/login", "document", True)
    assert ok("POST", "http://127.0.0.1:8800/acme/mbr/100234/share/commit", "document", True)
    assert not ok("POST", "http://127.0.0.1:8800/acme/mbr/find", "document", True)
    assert not ok("PUT", "http://127.0.0.1:8800/acme/mbr/100234/share/commit", "fetch", False)
    assert not ok("GET", "file:///etc/passwd", "document", True)
    assert not ok("GET", "data:text/html,x", "document", True)
    assert ok("GET", "data:image/png;base64,AA", "image", False)


def test_param_flags_and_symbolizer():
    from cua.discovery.agent import parse_param
    p = parse_param("member_id:string:pii:id+expose=100234")
    assert p.identifies_record and p.expose and p.sensitivity == Sensitivity.pii
    with raises(ValueError):
        parse_param("x:string:pii:bogus=1")
    sym = Redactor(use_patterns=False, template="{{{{{label}}}}}")
    sym.register("100234", "member_id")
    assert sym.text("Account #: 100234") == "Account #: {{member_id}}"


# ================================================================ drift vs page-load races (regression)

class _FakeSurface:
    """Plays back try_resolve results; records the order of calls."""
    def __init__(self, indexes):
        self.indexes, self.calls = list(indexes), []

    async def try_resolve(self, target):
        from cua.surface.base import Resolution
        self.calls.append("resolve")
        i = self.indexes.pop(0)
        kind = target.strategies[i].kind
        return Resolution(i, kind, None, None, [] if i == 0 else ["label(...): 0 matches"])

    async def settle(self, timeout_ms=3000):
        self.calls.append("settle")

    async def text_by_frame(self):
        return {}

    async def check(self, cond, params):
        self.calls.append("check")
        return True


def _engine(surface):
    import types
    from cua.replay.engine import ReplayEngine, _RunState
    e = ReplayEngine.__new__(ReplayEngine)
    e.s = types.SimpleNamespace(surface=surface)
    e.log = types.SimpleNamespace(event=lambda *a, **k: None)
    e.drift, e.state, e.params, e.outputs = [], _RunState(), {}, {}
    return e


def _two_strategy_target():
    from cua.schema.artifact import CssStrategy
    return Target(strategies=[LabelStrategy(anchor="Share Type:"), CssStrategy(selector="select[name=f_tp]")])


def test_fallback_hit_during_page_load_is_not_drift():
    import asyncio
    sf = _FakeSurface([1, 0])           # first pass: page half-parsed, css hit; after settle: label hits
    e = _engine(sf)
    step = FillStep(id="s5", intent="x", target=_two_strategy_target(), value=ParamRef(param="member_id"))
    res = asyncio.run(e._resolve(step, step.target))
    assert res.strategy_index == 0 and e.drift == []
    assert sf.calls == ["resolve", "settle", "resolve"]


def test_real_drift_is_still_reported_after_settling():
    import asyncio
    sf = _FakeSurface([1, 1])           # the label really changed: primary misses even when settled
    e = _engine(sf)
    step = FillStep(id="s5", intent="x", target=_two_strategy_target(), value=ParamRef(param="member_id"))
    res = asyncio.run(e._resolve(step, step.target))
    assert res.strategy_index == 1 and [d.step_id for d in e.drift] == ["s5"]


def test_postcondition_waits_for_the_page_to_load():
    import asyncio
    sf = _FakeSurface([])
    e = _engine(sf)
    step = ClickStep(id="s4", intent="x", target=_two_strategy_target(),
                     expect=UrlCondition(route="/mbr/{{member_id}}/share/new"))
    asyncio.run(e._await_postcondition(step))
    assert sf.calls == ["check", "settle"]   # URL committed -> wait for the document before the next step


# ================================================================ model client (regression)

def test_llm_falls_back_when_model_rejects_forced_tool_use_and_keeps_thinking():
    import asyncio
    import types
    from cua.discovery.llm import AnthropicLLM

    class Rejected(Exception):
        status_code = 400

    calls = []

    class Messages:
        async def create(self, **kw):
            calls.append(kw["tool_choice"]["type"])
            if kw["tool_choice"]["type"] == "any":
                raise Rejected('tool_choice: type "tool" and "any" are not supported for this model.')
            blk = types.SimpleNamespace
            return blk(content=[blk(type="thinking", thinking="...", signature="sig"),
                                blk(type="tool_use", id="t1", name="click", input={"ref": "nav-e1"})],
                       usage=blk(input_tokens=10, output_tokens=5))

    llm = AnthropicLLM(model="m", client=types.SimpleNamespace(messages=Messages()))
    t1 = asyncio.run(llm.decide("sys", [], []))
    t2 = asyncio.run(llm.decide("sys", [], []))
    assert calls == ["any", "auto", "auto"]          # falls back once, then stays on auto
    assert [c.name for c in t1.tool_calls] == ["click"]
    assert t1.assistant_content[0].type == "thinking"   # reasoning block preserved verbatim
    assert t2.usage == {"input_tokens": 10, "output_tokens": 5}


def test_llm_does_not_swallow_other_errors():
    import asyncio
    import types
    from cua.discovery.llm import AnthropicLLM

    class Auth(Exception):
        status_code = 401

    class Messages:
        async def create(self, **kw):
            raise Auth("invalid x-api-key")

    llm = AnthropicLLM(model="m", client=types.SimpleNamespace(messages=Messages()))
    with raises(Auth):
        asyncio.run(llm.decide("sys", [], []))
