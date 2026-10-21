"""Artifact storage: versioned YAML files + a generated human review card.

    capabilities/<capability id>/<semver>.yaml    the artifact (source of truth)
    capabilities/<capability id>/<semver>.md      review card (generated, for humans)

Saving lints the artifact for sensitive data first and refuses on findings —
the last line of defence if a recorded literal contains PII.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from packaging.version import Version

from .config import CAPABILITIES_DIR
from .redact import Redactor
from .schema.artifact import (Capability, ClickStep, ExtractStep, FillStep, LiteralValue, NavigateStep, ParamRef,
                              Review, SecretRef, SelectStep, Status, utcnow)


class UnsafeArtifact(ValueError):
    pass


def _dir(cap_id: str, root: Path | None = None) -> Path:
    return (root or CAPABILITIES_DIR) / cap_id


def versions(cap_id: str, root: Path | None = None) -> list[str]:
    d = _dir(cap_id, root)
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.yaml")), key=Version)


def next_version(cap_id: str, root: Path | None = None) -> str:
    vs = versions(cap_id, root)
    if not vs:
        return "0.1.0"
    v = Version(vs[-1])
    return f"{v.major}.{v.minor + 1}.0"


def lint(cap: Capability, redactor: Redactor) -> list[str]:
    text = yaml.safe_dump(cap.model_dump(mode="json", exclude={"provenance", "review"}))
    return sorted(set(redactor.findings(text)))


def save(cap: Capability, redactor: Redactor, root: Path | None = None) -> Path:
    findings = lint(cap, redactor)
    if findings:
        raise UnsafeArtifact(f"artifact contains sensitive data ({', '.join(findings)}); refusing to save")
    d = _dir(cap.id, root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{cap.version}.yaml"
    header = (f"# Capability artifact {cap.id}@{cap.version} ({cap.status.value})\n"
              f"# content hash: {cap.content_hash()}\n# Review card: {cap.version}.md\n")
    path.write_text(header + yaml.safe_dump(cap.model_dump(mode="json", exclude_none=True), sort_keys=False,
                                            width=110, allow_unicode=True))
    (d / f"{cap.version}.md").write_text(review_card(cap))
    return path


def load(ref: str, root: Path | None = None) -> Capability:
    """ref: a path to a .yaml, 'capability.id' (latest) or 'capability.id@1.2.0'."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return Capability.model_validate(yaml.safe_load(p.read_text()))
    cap_id, _, ver = ref.partition("@")
    vs = versions(cap_id, root)
    if not vs:
        raise FileNotFoundError(f"no capability {cap_id!r} under {root or CAPABILITIES_DIR}")
    ver = ver or vs[-1]
    return Capability.model_validate(yaml.safe_load((_dir(cap_id, root) / f"{ver}.yaml").read_text()))


def path_of(cap: Capability, root: Path | None = None) -> Path:
    return _dir(cap.id, root) / f"{cap.version}.yaml"


def list_all(root: Path | None = None) -> list[Capability]:
    base = root or CAPABILITIES_DIR
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []:
        vs = versions(d.name, root)
        if vs:
            out.append(load(f"{d.name}@{vs[-1]}", root))
    return out


class ContractViolation(ValueError):
    pass


def approve(cap: Capability, by: str, notes: str = "") -> Capability:
    """Approval pins the content hash: any later edit silently reverts the
    capability to draft behaviour (``is_approved`` is False). A capability that
    violates its workflow contract (including unverified steps) cannot be approved."""
    from . import contracts
    contract = contracts.contract_for(cap)
    violations = contracts.check(cap, contract, approved=True)
    if violations:
        raise ContractViolation(f"{cap.id} violates contract {contract.id}: " + "; ".join(violations))
    cap.status = Status.approved
    cap.review = Review(approved_by=by, approved_at=utcnow(), approved_hash=cap.content_hash(), notes=notes)
    return cap


# --------------------------------------------------------------------------- review card

def _val(v: object) -> str:
    if isinstance(v, ParamRef):
        return f"`{{{{{v.param}}}}}`"
    if isinstance(v, SecretRef):
        return f"`secret://{v.secret}`"
    if isinstance(v, LiteralValue):
        return f'"{v.literal}"'
    return str(v)


def _tgt(t: object) -> str:
    strategies = getattr(t, "strategies", [])
    first = strategies[0].model_dump(exclude={"kind"}, exclude_none=True) if strategies else {}
    frame = "/".join(f.name or f.route or "?" for f in getattr(t, "frame", [])) or "top"
    more = f" (+{len(strategies) - 1} fallbacks: {', '.join(s.kind for s in strategies[1:])})" if len(strategies) > 1 else ""
    return f"[{frame}] {strategies[0].kind} {first}{more}"


def _cond(c: object) -> str:
    """Human-readable condition: targets shown by their primary anchor, values as {{param}}."""
    from .detect import describe as _d
    k = getattr(c, "kind", "")
    if k in ("all", "any"):
        return f" {k.upper()} ".join(f"({_cond(x)})" for x in c.conditions)  # type: ignore[attr-defined]
    if k == "value":
        return _d(c).replace("does not show", "shows")
    if k == "output":
        return f"output `{c.output}` " + (f"matches `{c.pattern}`" if c.pattern else f"equals {c.equals}")
    if k == "url":
        return f"[{'/'.join(f.name or '?' for f in c.frame) or 'top'}] at `{c.route}`"
    if k == "text":
        return f"text /{c.pattern}/ visible"
    return k


def review_card(cap: Capability) -> str:
    L = [f"# {cap.title}", "", f"`{cap.id}` v{cap.version} — **{cap.status.value}**"
         f"{' (approved by ' + cap.review.approved_by + ')' if cap.is_approved and cap.review.approved_by else ''}",
         "", cap.description, "",
         f"- App: `{cap.app.product}` versions `{cap.app.versions}` ({cap.app.surface})",
         f"- Side effects: **{cap.side_effects.value}**",
         f"- Recorded: run `{cap.provenance.discovery_run_id}` on tenant `{cap.provenance.recorded_on_tenant}` "
         f"v{cap.provenance.recorded_on_version} with `{cap.provenance.model}`; human steps: {cap.provenance.human_steps}",
         f"- Content hash: `{cap.content_hash()}`", "", "## Inputs", "",
         "| name | type | sensitivity | required | constraint | description |", "|---|---|---|---|---|---|"]
    for p in cap.inputs:
        c = p.pattern or (", ".join(p.enum) if p.enum else "")
        L.append(f"| {p.name} | {p.type.value} | {p.sensitivity.value} | {p.required} | {c} | {p.description} |")
    L += ["", "## Outputs", "", "| name | type | sensitivity | from step | description |", "|---|---|---|---|---|"]
    for o in cap.outputs:
        L.append(f"| {o.name} | {o.type.value} | {o.sensitivity.value} | {o.from_step} | {o.description} |")
    L += ["", "## Steps", ""]
    for s in cap.steps:
        risk = f" ⚠ **{s.risk.value}**" if s.risk.value != "read" else ""
        if isinstance(s, NavigateStep):
            what = f"navigate `{s.route}`"
        elif isinstance(s, (FillStep, SelectStep)):
            what = f"{s.action} {_val(s.value)} into {_tgt(s.target)}"
        elif isinstance(s, ExtractStep):
            what = f"extract → `{s.output}` ({s.parse.value}) from {_tgt(s.target)}"
        elif isinstance(s, ClickStep):
            what = f"click {_tgt(s.target)}" + (f" — expects {s.dialog.type} dialog" if s.dialog else "")
        else:
            what = s.action
        req = f"  \n   require: {_cond(s.require)}" if s.require else ""
        exp = f"  \n   expect: {_cond(s.expect)}" if s.expect else ""
        v = s.verification
        ver = (f"  \n   verified by: `{v.method}`" + (f" — **REVIEW REQUIRED**: {v.note}" if v.review_required
                                                       else (f" ({v.note})" if v.note else ""))) if v else ""
        L.append(f"{s.id}. **{s.intent}**{risk} ({s.provenance})  \n   {what}{req}{exp}{ver}")
    L += ["", "## Success checkpoint", "", _cond(cap.success), "", "## Business invariants", ""]
    L += [f"- `{i.id}`: {_cond(i.when)}" for i in cap.invariants] or ["- (none)"]
    shown, bound = cap.identity_binding()
    ids = [p.name for p in cap.inputs if p.identifies_record]
    if ids:
        L += ["", "Record identity: " + ", ".join(
            f"`{n}` {'displayed on screen' if n in shown else 'bound by route only'}" for n in ids)]
    if cap.review_required_steps:
        L += ["", f"**Steps requiring review before approval:** {', '.join(cap.review_required_steps)}"]
    L += ["", "## Business outcomes the caller may receive", ""]
    L += [f"- `{d.code}` — {d.description}" for d in cap.outcomes] or \
         ["- (capability-specific: none; product-level detectors from the app profile also apply: "
          "NOT_FOUND, ACCESS_DENIED, VALIDATION_ERROR)"]
    return "\n".join(L) + "\n"
