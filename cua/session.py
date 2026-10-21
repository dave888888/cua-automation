"""A live, guarded browser session for one tenant: browser + surface + lease.

The same object is used by discovery, replay and the operator console, which
is what makes human takeover operate on *the same* session (same cookies, same
page, same app state) rather than a fresh one.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from playwright.async_api import async_playwright

from . import routes
from .config import Environment
from .evidence import RunLog
from .guard import Guard
from .handoff.control import SessionControl
from .schema.artifact import ClickStep, FillStep, SecretRef, Target
from .surface.base import Actor, TargetUnresolved
from .surface.web import WebSurface


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LiveSession:
    def __init__(self, env: Environment, log: RunLog, *, headed: bool = False, trace: bool = False,
                 slow_mo: int = 0) -> None:
        self.env = env
        self.log = log
        self.headed = headed
        self.trace = trace
        self.slow_mo = slow_mo
        self.guard = Guard(env.policy)
        self.surface: WebSurface
        self.control: SessionControl

    async def __aenter__(self) -> "LiveSession":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=not self.headed, slow_mo=self.slow_mo)
        sb = self.env.policy.sandbox
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800}, accept_downloads=sb.allow_downloads,
            service_workers="block" if sb.block_service_workers else "allow", permissions=[])
        if self.trace:
            await self._context.tracing.start(screenshots=True, snapshots=True)
        page = await self._context.new_page()
        self.surface = WebSurface(page, self._context, base_url=self.env.tenant.base_url, guard=self.guard,
                                  redactor=self.log.redactor, allow_point=self.env.policy.allow_point_fallback,
                                  sensitive_labels=self.env.profile.sensitive_labels)
        await self.surface.install()
        self.control = SessionControl(self.log.run_id, self.log.dir, self.log, operators=self.env.policy.operators)
        self.surface.control = self.control
        self.control.surface = self.surface
        self.surface.on_policy_block = lambda url, why: self.log.event("sandbox_blocked", url=url, reason=why)

        async def human_event(ev: dict) -> None:
            self.control.record_human_action({"source": "browser", **ev})
        self.surface.on_human_event = human_event
        self.log.event("session_started", tenant=self.env.tenant.id, headed=self.headed,
                       policy=self.env.policy.id)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self.trace:
                await self._context.tracing.stop(path=str(self.log.dir / "trace.zip"))
            await self._browser.close()
        finally:
            await self._pw.stop()
            self.control.close()

    # ------------------------------------------------------------------ secrets

    def secret(self, ref: str) -> str:
        value = self.env.secrets.get(ref)
        self.log.redactor.register(value, "secret")
        return value

    # ------------------------------------------------------------------ session establishment

    def url(self, route: str, params: dict[str, str] | None = None) -> str:
        return routes.join(self.env.tenant.base_url, routes.render(route, params or {}))

    async def login(self) -> None:
        prof = self.env.profile
        self.log.event("login_started", route=prof.login.route)
        await self.surface.goto(self.url(prof.login.route), Actor.agent)
        for step in prof.login.steps:
            res = await self._resolve_wait(step.target, 8000)
            if isinstance(step, FillStep):
                v = step.value
                value = self.secret(v.secret) if isinstance(v, SecretRef) else getattr(v, "literal", "")
                await self.surface.fill(res, value, Actor.agent)
            elif isinstance(step, ClickStep):
                await self.surface.click(res, Actor.agent)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await self.surface.check(prof.login.success, {}):
                self.log.event("login_ok")
                return
            text = await self.surface.visible_text()
            for d in prof.detectors:
                if d.code == "AUTH_REJECTED" and await self.surface.check(d.when, {}):
                    raise SessionError("AUTH_REJECTED", "credentials rejected by the application")
            await asyncio.sleep(0.25)
        raise SessionError("LOGIN_FAILED", f"login did not reach {prof.login.success}")

    async def ensure_authenticated(self) -> None:
        await self.surface.goto(self.url(self.env.profile.entry_route), Actor.agent)
        await self.surface.settle()
        if not await self.surface.check(self.env.profile.authenticated, {}):
            await self.login()
            await self.surface.goto(self.url(self.env.profile.entry_route), Actor.agent)
            await self.surface.settle()

    async def probe_version(self) -> str | None:
        vp = self.env.profile.version_probe
        if vp is None:
            return None
        from .schema.artifact import FrameRef
        frame = [FrameRef.model_validate(f) for f in vp.frame]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            m = re.search(vp.pattern, await self.surface.visible_text(frame))
            if m:
                return m.group(1)
            await asyncio.sleep(0.25)
        return None

    async def _resolve_wait(self, target: Target, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        last: list[str] = []
        while time.monotonic() < deadline:
            try:
                return await self.surface.try_resolve(target)
            except TargetUnresolved as e:
                last = e.attempts
            await asyncio.sleep(0.2)
        raise TargetUnresolved(last)
