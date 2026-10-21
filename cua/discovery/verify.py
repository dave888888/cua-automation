"""The Verify stage of discovery: prove an action's effect before recording it.

Every function returns a ``Verdict``. ``accept=False`` means the step is NOT
added to the capability (the model is told why). ``accept=True`` carries the
postcondition that was *observed to hold* — never one inferred without
evidence — and a reviewable ``Verification`` record:

    fill     read the control back; it must show the bound value
    select   the chosen option must be the bound value
    click    a frame navigated → canonical route postcondition, re-checked live;
             the page changed without navigating → accepted, flagged for review
             (no postcondition is invented); nothing changed → rejected
    extract  the displayed value must parse as the declared output type
    any      a product detector firing afterwards (error page, NOT_FOUND,
             session expiry) means the step did not achieve its intent → rejected
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import detect, routes, values
from ..schema.artifact import (Condition, OutcomeClass, RestartFlowRecovery, RestartSessionRecovery, UrlCondition,
                               ValueType, Verification)


@dataclass
class Verdict:
    accept: bool
    message: str
    expect: Condition | None = None
    verification: Verification | None = None


def reject(msg: str) -> Verdict:
    return Verdict(False, msg)


async def readback(surface: Any, res: Any, intended: str, *, method: str) -> Verdict:
    try:
        shown = await surface.read_value(res)
    except Exception as e:
        return reject(f"could not read the control back ({type(e).__name__})")
    if not values.matches(shown, equals=intended, pattern=None, how="text"):
        return reject("the control does not show the value that was entered (field may be read-only, "
                      "masked, or reformatted)")
    return Verdict(True, "verified by read-back", None, Verification(method=method))


def parse_check(raw: str, vtype: ValueType) -> Verdict:
    try:
        values.parse_value(raw, vtype)
    except values.ParseError as e:
        return reject(f"the element's text is not a valid {vtype.value} ({e}); choose the element that "
                      "holds the value itself")
    return Verdict(True, f"parses as {vtype.value}", None, Verification(method="value_parse"))


async def transition(surface: Any, *, base_url: str, params: dict[str, str], before_urls: dict[str, str],
                     after_urls: dict[str, str], before_state: str, after_state: str,
                     frame_paths: dict[str, list], dialog: dict | None, human: bool = False) -> Verdict:
    """Verify that a click produced an observable state transition."""
    if dialog and dialog.get("handled") == "unexpected_dismissed":
        return reject(f"an unexpected {dialog['type']} dialog appeared and was dismissed by the sandbox; "
                      "the click had no effect")
    changed = [k for k in after_urls if before_urls.get(k) != after_urls[k] and k != "top"]
    if not changed and before_urls.get("top") != after_urls.get("top"):
        changed = ["top"]
    for k in changed:
        cond = UrlCondition(frame=frame_paths.get(k, []), route=routes.canonicalize(after_urls[k], base_url, params))
        if await surface.check(cond, params):       # observed to hold, in its parameterised form
            return Verdict(True, f"navigated: frame {k} is at {cond.route}", cond,
                           Verification(method="route", note=f"frame {k}"))
    if changed:
        return Verdict(True, "a frame navigated but its route could not be re-verified", None,
                       Verification(method="unverified", review_required=True,
                                    note="navigation observed; canonical route did not re-match"))
    if before_state != after_state:
        return Verdict(True, "the page changed without navigating; no postcondition can be proven", None,
                       Verification(method="unverified", review_required=True,
                                    note="in-page change only; reviewer must add an explicit condition"))
    if human:
        return Verdict(True, "operator action without navigation", None,
                       Verification(method="unverified", review_required=True,
                                    note="operator action without navigation; effect not verifiable"))
    return reject("the click had no observable effect (no navigation, no page change)")


async def detectors_after(surface: Any, profile: Any) -> Verdict | None:
    """A product detector firing right after an action means the action did not do what it was meant to."""
    hit = await detect.first_hit(profile.detectors, surface, {}, None)
    if hit is None:
        return None
    d = hit.detector
    if d.classification == OutcomeClass.recoverable and not isinstance(
            d.recovery, (RestartFlowRecovery, RestartSessionRecovery)):
        return None                       # e.g. an interstitial over the new page: the transition happened
    what = {OutcomeClass.business_outcome: "the app returned a business result",
            OutcomeClass.hard_failure: "the app reported an error",
            OutcomeClass.requires_human: "a state only a human may clear appeared (call ask_human)"
            }.get(d.classification, "a transient error occurred")
    return reject(f"{what} ({d.code}{': ' + hit.message if hit.message else ''}); the step was not recorded. "
                  "If this result is the answer, call report_outcome.")


def output_shape(value: str) -> str | None:
    """A reviewable regex for a string output's *shape*, e.g. 'SH-48213' -> ^[A-Z]+-\\d+$."""
    if not value or len(value) > 40:
        return None
    out, prev = [], None
    for ch in value:
        cls = r"[A-Z]+" if ch.isupper() else r"[a-z]+" if ch.islower() else r"\d+" if ch.isdigit() else re.escape(ch)
        if cls != prev or cls not in (r"[A-Z]+", r"[a-z]+", r"\d+"):
            out.append(cls)
        prev = cls
    return "^" + "".join(out) + "$"
