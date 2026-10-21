"""Configuration models: policy, vendor app profile, tenant overlay.

The three layers are deliberately separate:

* ``AppProfile``  — knowledge about a *vendor product* (CoreLink Teller): how to
  sign on, what its error banners look like, its interstitials. Written once,
  shared by every capability and every tenant running that product.
* ``TenantConfig`` — one institution's instance: base URL, product version,
  where its secrets live, and *overrides* (label aliases, per-step targets).
* ``Policy``       — what automation may do there. Owned by security, not by
  whoever recorded the capability.
* ``WorkflowContract`` — reviewed constraints on what a family of capabilities
  may *be* (operations, inputs/outputs, checkpoint strength, risk, approval,
  unattended use). Trusted configuration; never produced by the model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .artifact import Condition, Detector, ClickStep, FillStep, RiskClass, Sensitivity, Target, ValueType


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryLimits(Strict):
    max_steps: int = 30
    max_no_progress: int = 3          # identical observations in a row → stuck
    max_wall_seconds: int = 600


class SandboxPolicy(Strict):
    """Browser behaviour allowed beyond origin/route allowlists. Applied by
    LiveSession, so discovery, replay and the operator share one sandbox."""
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET", "HEAD", "POST"])
    mutation_routes: list[str] = Field(default_factory=list,
                                       description="Globs: the only paths a non-GET/HEAD request may target "
                                                   "(legacy POST forms are listed explicitly).")
    allowed_resource_types: list[str] | None = Field(None, description="None = any; e.g. [document, stylesheet, "
                                                                       "image, script]")
    allow_popups: bool = False          # new windows/tabs are closed on sight
    allow_downloads: bool = False       # downloads are cancelled
    block_service_workers: bool = True
    # Native dialogs: alert -> acknowledged, an *expected* confirm (declared on the step) -> as
    # declared, anything else (unexpected confirm, any prompt) -> dismissed and the step fails.


class ModelExposure(Strict):
    """What the discovery model may see. Values are symbolic ({{name}}) by default."""
    allow_sensitive_values: bool = Field(False, description="Permit exposing pii/financial/secret parameter "
                                                            "values to the model when a caller asks for it.")


class Policy(Strict):
    id: str
    allowed_origins: list[str]
    allowed_routes: list[str] = Field(description="Glob patterns on URL path, e.g. /*/mbr/**")
    denied_routes: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["navigate", "click", "fill", "select", "extract"])
    denied_targets: list[str] = Field(default_factory=list, description="Regex on control name: never touch.")
    irreversible_targets: list[str] = Field(default_factory=list,
                                            description="Regex on control name: commits a transaction.")
    reversible_targets: list[str] = Field(default_factory=list)
    allow_point_fallback: bool = False
    discovery: DiscoveryLimits = Field(default_factory=DiscoveryLimits)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    operators: list[str] = Field(default_factory=list,
                                 description="Operator ids allowed to claim interventions on this tenant. "
                                             "Empty = nobody (fail closed). Stand-in for SSO group membership.")
    model_exposure: ModelExposure = Field(default_factory=ModelExposure)


class LoginRecipe(Strict):
    route: str
    steps: list[FillStep | ClickStep]
    success: Condition


class VersionProbe(Strict):
    frame: list = Field(default_factory=list)
    pattern: str = Field(description="Regex over visible text; group 1 is the version.")


class AppProfile(Strict):
    product: str
    description: str = ""
    entry_route: str
    login: LoginRecipe
    authenticated: Condition
    version_probe: VersionProbe | None = None
    detectors: list[Detector] = Field(default_factory=list)
    sensitive_patterns: dict[str, str] = Field(default_factory=dict)
    sensitive_labels: dict[str, Sensitivity] = Field(
        default_factory=dict, description="Visible label -> sensitivity of the value next to it. Such values are "
                                          "redacted from prompts, logs, screenshots and diagnostics on sight.")


class TenantConfig(Strict):
    id: str
    name: str
    product: str
    version: str
    base_url: str
    policy: str = "default"
    secrets: dict[str, str] = Field(default_factory=dict, description="secret ref -> env var name")
    label_aliases: dict[str, str] = Field(default_factory=dict,
                                          description="Recorded visible text -> this tenant's text")
    step_overrides: dict[str, dict[str, Target]] = Field(default_factory=dict,
                                                         description="capability id -> step id -> target")
    detector_overrides: dict[str, str] = Field(default_factory=dict,
                                               description="detector id -> replacement text pattern")


class OutputRule(Strict):
    type: ValueType
    allow_pattern: bool = Field(False, description="May the extract step post-process with a regex?")


class WorkflowContract(Strict):
    """Constraints a capability must satisfy to be approved and replayed.

    Matched by capability id (first match wins, most specific first), so a
    capability cannot opt out by omitting a field. Checked on the recorded
    artifact *and* on each tenant's effective artifact."""
    id: str
    applies_to: str = Field(description="fnmatch glob on capability id")
    description: str = ""
    max_side_effects: RiskClass = RiskClass.read
    max_irreversible_steps: int = 0
    allowed_actions: list[str] = Field(default_factory=lambda: ["navigate", "click", "fill", "select", "extract"])
    route_scope: list[str] = Field(default_factory=lambda: ["/**"],
                                   description="Every route the capability visits or asserts must match")
    required_inputs: list[str] = Field(default_factory=list)
    identity_inputs: list[str] = Field(default_factory=list, description="Must be declared identifies_record")
    outputs: dict[str, OutputRule] = Field(default_factory=dict, description="Required outputs and how they may "
                                                                            "be produced")
    checkpoint: Literal["screen", "identity_bound", "identity_displayed"] = "identity_bound"
    must_confirm: list[str] = Field(default_factory=list,
                                    description="Params the final screen must be asserted to show (invariants)")
    irreversible_routes: list[str] = Field(default_factory=list,
                                           description="A step whose postcondition reaches these routes must be "
                                                       "classed irreversible")
    approval_required: bool = False
    unattended: bool = Field(True, description="May replay run with no operator attached?")
    allow_review_required: bool = Field(False, description="May steps that discovery could not verify be "
                                                           "approved/replayed?")


class ContractSet(Strict):
    product: str
    contracts: list[WorkflowContract]
