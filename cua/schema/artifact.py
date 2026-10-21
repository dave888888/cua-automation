"""The capability artifact: a typed, versioned, reviewable, surface-agnostic flow.

Design rules (see REPORT.md §2):

* **Surface-agnostic.** Nothing here mentions Playwright, CSS-only selectors or
  pixels as the primary identity of a control. A target is a *bundle* of
  strategies ordered from most semantic (role + accessible name) to most
  structural (CSS/XPath) to most physical (point). A desktop adapter resolves
  the same bundle against a UI Automation tree.
* **No values, only references.** Inputs are ``{{param}}`` references; secrets
  are ``secret://`` references resolved at run time; example values are kept
  only for params marked ``public``.
* **Outcomes are first-class.** A capability declares which business outcomes
  it can return (NOT_FOUND, ACCESS_DENIED...) next to its success checkpoint,
  so a calling agent can branch on them without parsing errors.
* **Risk is per step.** Every step carries a risk class; the capability's
  ``side_effects`` is derived from them and drives the approval gate.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "cua.capability/v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- contract

class Sensitivity(str, Enum):
    """Drives redaction. Anything above ``internal`` never reaches logs in clear."""
    public = "public"        # safe to log and to store examples of
    internal = "internal"    # logged, never shown to third parties
    pii = "pii"              # redacted in logs/screenshots, returned only to caller
    financial = "financial"  # balances etc.: same handling as pii
    secret = "secret"        # credentials/tokens: never logged, never returned


REDACTED_SENSITIVITIES = {Sensitivity.pii, Sensitivity.financial, Sensitivity.secret}


class ValueType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"   # serialised as a string to avoid float rounding on money
    boolean = "boolean"
    enum = "enum"


class ParamSpec(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str = ""
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.internal
    pattern: str | None = Field(None, description="Regex the value must fully match (validated pre-flight).")
    enum: list[str] | None = None
    example: str | None = Field(None, description="Only kept when sensitivity == public.")
    identifies_record: bool = Field(False, description="Selects the business record (member, account). "
                                    "Success must then prove the record on screen is this one.")

    @model_validator(mode="after")
    def _no_sensitive_examples(self) -> "ParamSpec":
        if self.example is not None and self.sensitivity != Sensitivity.public:
            self.example = None
        return self


class OutputSpec(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ValueType = ValueType.string
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.internal
    from_step: str = Field(description="Id of the extract step that produces it.")


# --------------------------------------------------------------------------- targeting

class FrameRef(Strict):
    """One hop in a frame path. Legacy apps live in framesets; desktop adapters
    map this to a window/pane path."""
    name: str | None = None
    route: str | None = Field(None, description="Route template the frame URL should match, e.g. /mbr/find")


class RoleStrategy(Strict):
    kind: Literal["role"] = "role"
    role: str
    name: str
    exact: bool = True


class LabelStrategy(Strict):
    """Anchor on visible text near the control — the workhorse for table layouts
    where <label for> is missing. ``relation`` says where the control sits."""
    kind: Literal["label"] = "label"
    anchor: str
    relation: Literal["next_control", "row_cell"] = "next_control"
    role: str | None = Field(None, description="Role the resolved control must have (next_control).")
    cell_offset: int = Field(1, description="row_cell: how many cells to the right of the anchor.")


class TextStrategy(Strict):
    kind: Literal["text"] = "text"
    text: str
    role: str | None = None


class CssStrategy(Strict):
    kind: Literal["css"] = "css"
    selector: str


class XPathStrategy(Strict):
    kind: Literal["xpath"] = "xpath"
    xpath: str


class PointStrategy(Strict):
    """Last resort: coordinates relative to the frame viewport. Recorded only when
    nothing else verified; flagged for review and disabled unless policy allows."""
    kind: Literal["point"] = "point"
    x: float
    y: float


Strategy = Annotated[
    Union[RoleStrategy, LabelStrategy, TextStrategy, CssStrategy, XPathStrategy, PointStrategy],
    Field(discriminator="kind"),
]

STRATEGY_STABILITY = {"role": 5, "label": 4, "text": 3, "css": 2, "xpath": 1, "point": 0}


class Fingerprint(Strict):
    """What the control looked like at record time. Used to *explain* drift
    ("primary failed, matched by css; name was 'Member ID', now 'Member Number'")."""
    role: str | None = None
    name: str | None = None
    tag: str | None = None
    neighbor_text: str | None = None


class Target(Strict):
    frame: list[FrameRef] = Field(default_factory=list)
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)
    sensitive: bool = Field(False, description="Mask this control in saved screenshots.")


# --------------------------------------------------------------------------- value references

class ParamRef(Strict):
    param: str


class LiteralValue(Strict):
    literal: str


class SecretRef(Strict):
    secret: str = Field(pattern=r"^[a-z0-9_.-]+$")


ValueRef = Union[ParamRef, SecretRef, LiteralValue]


# --------------------------------------------------------------------------- conditions

class UrlCondition(Strict):
    kind: Literal["url"] = "url"
    frame: list[FrameRef] = Field(default_factory=list)
    route: str = Field(description="Route template relative to the tenant base, e.g. /mbr/{{member_id}}/share/new")


class TextCondition(Strict):
    kind: Literal["text"] = "text"
    frame: list[FrameRef] | Literal["*"] = "*"
    pattern: str = Field(description="Case-insensitive regex searched in visible text.")


class TargetCondition(Strict):
    kind: Literal["target"] = "target"
    target: Target


class ValueCondition(Strict):
    """A business assertion about what a control *displays*.

    ``equals`` compares the displayed value with an invocation parameter (or a
    literal) after normalisation; ``pattern`` is a full-match regex. This is how
    a capability proves *which record* it is on ("Account # shows
    {{member_id}}") or that a confirmation reflects the submitted operation
    ("Share Type shows {{share_type}}"). Deliberately not an expression
    language: equality or a regex over one displayed value."""
    kind: Literal["value"] = "value"
    target: Target
    equals: ParamRef | LiteralValue | None = None
    pattern: str | None = None
    normalize: Literal["text", "digits", "decimal"] = "text"

    @model_validator(mode="after")
    def _one_of(self) -> "ValueCondition":
        if self.equals is None and self.pattern is None:
            raise ValueError("value condition needs equals or pattern")
        return self


class OutputCondition(Strict):
    """An assertion about an extracted output (evaluated by the engine, not the page)."""
    kind: Literal["output"] = "output"
    output: str
    equals: ParamRef | LiteralValue | None = None
    pattern: str | None = None
    normalize: Literal["text", "digits", "decimal"] = "text"

    @model_validator(mode="after")
    def _one_of(self) -> "OutputCondition":
        if self.equals is None and self.pattern is None:
            raise ValueError("output condition needs equals or pattern")
        return self


class AllCondition(Strict):
    kind: Literal["all"] = "all"
    conditions: list["Condition"]


class AnyCondition(Strict):
    kind: Literal["any"] = "any"
    conditions: list["Condition"]


Condition = Annotated[
    Union[UrlCondition, TextCondition, TargetCondition, ValueCondition, OutputCondition, AllCondition, AnyCondition],
    Field(discriminator="kind"),
]
AllCondition.model_rebuild()
AnyCondition.model_rebuild()


def iter_conditions(c: Any) -> "list[Any]":
    """Leaf conditions of a (possibly nested) condition."""
    if c is None:
        return []
    if isinstance(c, (AllCondition, AnyCondition)):
        return [leaf for sub in c.conditions for leaf in iter_conditions(sub)]
    return [c]


def must_hold(c: Any) -> "list[Any]":
    """Leaves that are *required* to hold (an Any branch proves nothing on its own)."""
    if c is None or isinstance(c, AnyCondition):
        return []
    if isinstance(c, AllCondition):
        return [leaf for sub in c.conditions for leaf in must_hold(sub)]
    return [c]


_PH = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


def bound_params(c: Any) -> tuple[set[str], set[str]]:
    """(params proven by *displayed* values, params bound by the *route* only)."""
    displayed, routed = set(), set()
    for leaf in must_hold(c):
        if isinstance(leaf, (ValueCondition, OutputCondition)) and isinstance(leaf.equals, ParamRef):
            displayed.add(leaf.equals.param)
        elif isinstance(leaf, UrlCondition):
            routed.update(_PH.findall(leaf.route))
    return displayed, routed


class Invariant(Strict):
    """A named business assertion checked together with the success checkpoint."""
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = ""
    when: Condition


# --------------------------------------------------------------------------- outcomes / recovery

class OutcomeClass(str, Enum):
    business_outcome = "business_outcome"  # a legitimate answer for the caller
    recoverable = "recoverable"            # handle and continue
    hard_failure = "hard_failure"          # stop, surface a debuggable error
    requires_human = "requires_human"      # a known state only a person may clear (supervisor hold, ...)


class DismissRecovery(Strict):
    kind: Literal["dismiss"] = "dismiss"
    target: Target


class RestartFlowRecovery(Strict):
    """Back off, return to the entry point and replay from the first step.

    Why restart instead of 'retry this step': after a transient error page the
    form state the step depended on is gone (server-rendered POST flows). Our
    flows are read-only until their commit step, so re-running from the top is
    the only retry that is both deterministic and safe. Illegal once an
    irreversible step has been committed (the engine enforces this)."""
    kind: Literal["restart_flow"] = "restart_flow"
    max_attempts: int = 2
    backoff_ms: int = 1500


class RestartSessionRecovery(Strict):
    """Sign on again (app profile login recipe), then restart the flow.
    Same irreversibility rule as restart_flow."""
    kind: Literal["restart_session"] = "restart_session"
    max_attempts: int = 1
    backoff_ms: int = 0


Recovery = Annotated[Union[DismissRecovery, RestartFlowRecovery, RestartSessionRecovery],
                     Field(discriminator="kind")]


class Detector(Strict):
    """A known runtime state, recognised by a condition and classified."""
    id: str
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str = ""
    when: Condition
    classification: OutcomeClass
    recovery: Recovery | None = None
    message_pattern: str | None = Field(None, description="Regex whose first group becomes the result message.")
    steps: list[str] | None = Field(None, description="Restrict to these step ids (None = every step).")


# --------------------------------------------------------------------------- steps

class RiskClass(str, Enum):
    read = "read"                  # navigate, read, type into a form (nothing committed)
    reversible = "reversible"      # commits something that can be undone
    irreversible = "irreversible"  # posts a transaction / creates a record


RISK_ORDER = {RiskClass.read: 0, RiskClass.reversible: 1, RiskClass.irreversible: 2}


class DialogExpectation(Strict):
    """A native dialog the step is expected to raise (e.g. 'Post this share?')."""
    type: Literal["confirm", "alert", "prompt"] = "confirm"
    message_pattern: str
    accept: bool = True


class Verification(Strict):
    """How discovery proved this step did what it was meant to (reviewable)."""
    method: Literal["readback", "selected", "route", "value_parse", "condition", "unverified"]
    review_required: bool = False
    note: str = ""


class StepBase(Strict):
    id: str = Field(pattern=r"^s[0-9]+$")
    intent: str = Field(description="Human-readable why, from the discovery run.")
    risk: RiskClass = RiskClass.read
    require: Condition | None = Field(None, description="Precondition checked before acting, e.g. the "
                                      "record on screen is the requested one (record identity).")
    expect: Condition | None = Field(None, description="Postcondition proving the step took effect.")
    timeout_ms: int = 10_000
    provenance: Literal["agent", "human", "authored"] = "agent"
    verification: Verification | None = None


class NavigateStep(StepBase):
    action: Literal["navigate"] = "navigate"
    route: str = Field(description="Route template relative to the tenant base URL.")


class ClickStep(StepBase):
    action: Literal["click"] = "click"
    target: Target
    dialog: DialogExpectation | None = None


class FillStep(StepBase):
    action: Literal["fill"] = "fill"
    target: Target
    value: ValueRef


class SelectStep(StepBase):
    action: Literal["select"] = "select"
    target: Target
    value: ValueRef


class ExtractStep(StepBase):
    action: Literal["extract"] = "extract"
    target: Target
    output: str
    parse: ValueType = ValueType.string
    pattern: str | None = Field(None, description="Optional regex; group 1 is the value.")


Step = Annotated[
    Union[NavigateStep, ClickStep, FillStep, SelectStep, ExtractStep],
    Field(discriminator="action"),
]


# --------------------------------------------------------------------------- capability

class Status(str, Enum):
    draft = "draft"
    approved = "approved"
    deprecated = "deprecated"


class AppBinding(Strict):
    product: str = Field(description="Vendor product family, e.g. corelink-teller. Tenants bind to this.")
    versions: str = Field(">=0", description="PEP 440 specifier of product versions this was verified on.")
    surface: Literal["web", "legacy_web", "desktop"] = "web"


class Provenance(Strict):
    discovery_run_id: str | None = None
    recorded_on_tenant: str | None = None
    recorded_on_version: str | None = None
    recorded_at: datetime | None = None
    model: str | None = None
    goal_template: str | None = None
    human_steps: int = 0


class Review(Strict):
    approved_by: str | None = None
    approved_at: datetime | None = None
    approved_hash: str | None = Field(None, description="Approval is pinned to content; edits revert to draft.")
    notes: str = ""


class Capability(Strict):
    schema_version: Literal["cua.capability/v1"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9]+(\.[a-z0-9_]+)+$", description="e.g. corelink.member.savings_balance")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str = Field(description="What it does, written for a calling agent.")
    status: Status = Status.draft
    app: AppBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    preconditions: list[Literal["authenticated_session"]] = Field(default_factory=lambda: ["authenticated_session"])
    entry_route: str = Field(description="Where replay starts, relative to the tenant base URL.")
    steps: list[Step] = Field(min_length=1)
    success: Condition = Field(description="Checkpoint asserted at the end. No success without it.")
    invariants: list[Invariant] = Field(default_factory=list,
                                        description="Business assertions that must also hold at the end.")
    outcomes: list[Detector] = Field(default_factory=list, description="Capability-specific detectors; "
                                     "merged with the app profile's shared ones.")
    side_effects: RiskClass = RiskClass.read
    provenance: Provenance = Field(default_factory=Provenance)
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def _consistency(self) -> "Capability":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        params = {p.name for p in self.inputs}
        for s in self.steps:
            v = getattr(s, "value", None)
            if isinstance(v, ParamRef) and v.param not in params:
                raise ValueError(f"step {s.id} references undeclared param {v.param!r}")
        extracts = {s.id: s for s in self.steps if isinstance(s, ExtractStep)}
        for o in self.outputs:
            if o.from_step not in extracts or extracts[o.from_step].output != o.name:
                raise ValueError(f"output {o.name!r} is not produced by step {o.from_step!r}")
        worst = max((s.risk for s in self.steps), key=RISK_ORDER.__getitem__, default=RiskClass.read)
        self.side_effects = worst
        for s in self.steps:
            if s.risk == RiskClass.irreversible and s.expect is None:
                raise ValueError(f"irreversible step {s.id} must declare a verifiable postcondition (expect)")
        for leaf in self.all_conditions():
            for ref in (getattr(leaf, "equals", None),):
                if isinstance(ref, ParamRef) and ref.param not in params:
                    raise ValueError(f"condition references undeclared param {ref.param!r}")
            if isinstance(leaf, OutputCondition) and leaf.output not in {o.name for o in self.outputs}:
                raise ValueError(f"condition references undeclared output {leaf.output!r}")
        missing = {p.name for p in self.inputs if p.identifies_record} - self.identity_binding()[1]
        if missing:
            raise ValueError(f"record-identity input(s) {sorted(missing)} are not asserted by the success "
                             "checkpoint, an invariant, or an extract precondition; a matching page title is "
                             "not proof of the right record")
        return self

    # ---- verification helpers

    def all_conditions(self) -> list[Any]:
        out = iter_conditions(self.success)
        for inv in self.invariants:
            out += iter_conditions(inv.when)
        for s in self.steps:
            out += iter_conditions(s.require) + iter_conditions(s.expect)
        return out

    def identity_binding(self) -> tuple[set[str], set[str]]:
        """(params whose record identity is proven by displayed values, params proven at all).

        Counted: the success checkpoint, invariants, and the preconditions of
        extract steps (an output is only trustworthy if the record was checked
        when it was read)."""
        displayed, routed = bound_params(self.success)
        for inv in self.invariants:
            d, r = bound_params(inv.when)
            displayed |= d
            routed |= r
        for s in self.steps:
            if isinstance(s, ExtractStep):
                d, r = bound_params(s.require)
                displayed |= d
                routed |= r
        return displayed, displayed | routed

    @property
    def review_required_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.verification and s.verification.review_required]

    # ---- identity

    def content_hash(self) -> str:
        """Hash of everything that affects behaviour (not status/review/provenance)."""
        body = self.model_dump(mode="json", exclude={"status", "review", "provenance"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]

    @property
    def is_approved(self) -> bool:
        return self.status == Status.approved and self.review.approved_hash == self.content_hash()

    def input_spec(self, name: str) -> ParamSpec:
        return next(p for p in self.inputs if p.name == name)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def capability_json_schema() -> dict[str, Any]:
    return Capability.model_json_schema()
