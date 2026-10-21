"""Policy enforcement: allowlist + risk classification.

The guard is consulted *before* every action by both the discovery agent and
the replay engine, and installed as a network filter on the browser context so
navigation outside the allowlist is impossible even if a click leads there
(a link, a redirect, a script). It knows nothing about LLMs.

Risk is decided deterministically from policy (regex on the control's
accessible name) and can only be *raised* by the agent's own declaration
(``commits=true``), never lowered. At replay time the recorded risk is
re-checked against the *current* policy, so a stricter policy applies to old
artifacts.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .schema.artifact import RISK_ORDER, RiskClass
from .schema.config import Policy


@dataclass
class Decision:
    allowed: bool
    risk: RiskClass
    reason: str = ""


class Guard:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._denied = [re.compile(p) for p in policy.denied_targets]
        self._irrev = [re.compile(p) for p in policy.irreversible_targets]
        self._rev = [re.compile(p) for p in policy.reversible_targets]

    # ---- network / navigation

    def url_allowed(self, url: str) -> tuple[bool, str]:
        if url in ("about:blank", "about:srcdoc"):
            return True, ""
        if url.startswith(("data:", "blob:")):
            return True, ""     # inline resources only; documents are refused in request_allowed
        if not url.startswith(("http://", "https://")):
            return False, f"scheme of {url.split(':', 1)[0]}: is not allowed"
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.policy.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = p.path or "/"
        for pat in self.policy.denied_routes:
            if fnmatch.fnmatch(path, pat):
                return False, f"route {path} matches denied pattern {pat}"
        if not any(fnmatch.fnmatch(path, pat) for pat in self.policy.allowed_routes):
            return False, f"route {path} not in allowed routes"
        return True, ""

    def request_allowed(self, method: str, url: str, resource_type: str,
                        is_navigation: bool) -> tuple[bool, str]:
        """Network filter for every request the browser makes (any actor, any frame)."""
        sb = self.policy.sandbox
        if url.startswith(("data:", "blob:")) and (is_navigation or resource_type == "document"):
            return False, "navigation to data:/blob: documents is not allowed"
        ok, why = self.url_allowed(url)
        if not ok:
            return False, why
        m = method.upper()
        if m not in sb.allowed_methods:
            return False, f"HTTP method {m} not allowed"
        if m not in ("GET", "HEAD") and not url.startswith(("data:", "blob:")):
            path = urlsplit(url).path or "/"
            if not any(fnmatch.fnmatch(path, pat) for pat in sb.mutation_routes):
                return False, f"{m} to {path} is not a permitted mutation endpoint"
        if sb.allowed_resource_types is not None and resource_type not in sb.allowed_resource_types:
            return False, f"resource type {resource_type} not allowed"
        return True, ""

    # ---- actions

    def classify(self, action: str, target_name: str | None, declared: RiskClass | None = None) -> RiskClass:
        risk = RiskClass.read
        name = target_name or ""
        if action == "click":
            if any(r.search(name) for r in self._irrev):
                risk = RiskClass.irreversible
            elif any(r.search(name) for r in self._rev):
                risk = RiskClass.reversible
        if declared and RISK_ORDER[declared] > RISK_ORDER[risk]:
            risk = declared
        return risk

    def check(self, action: str, target_name: str | None, *, declared: RiskClass | None = None,
              url: str | None = None) -> Decision:
        risk = self.classify(action, target_name, declared)
        if action not in self.policy.allowed_actions:
            return Decision(False, risk, f"action {action!r} not allowed by policy {self.policy.id}")
        name = target_name or ""
        for r in self._denied:
            if r.search(name):
                return Decision(False, risk, f"target {name!r} matches denied pattern {r.pattern!r}")
        if url is not None:
            ok, why = self.url_allowed(url)
            if not ok:
                return Decision(False, risk, why)
        return Decision(True, risk)
