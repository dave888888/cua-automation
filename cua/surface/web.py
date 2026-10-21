"""Playwright implementation of the Surface seam, built for hostile legacy web.

Key choices:

* **Own role/name/label computation** (scan.js) used identically at record and
  replay time. Framesets, layout tables and label-less inputs are the norm, so
  we compute "the text next to this box" ourselves instead of trusting ARIA.
* **Single-shot resolution.** ``try_resolve`` makes one pass over the strategy
  bundle; the engine loops, so detectors are checked between polls.
* **Uniqueness or nothing.** A strategy that matches 0 or >1 elements is a
  miss, never "click the first one".
* **Grounding is verified.** Every candidate strategy is re-resolved at record
  time and kept only if it lands on exactly the element the model chose.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import BrowserContext, Dialog, Frame, Page, Route

from .. import routes, values
from ..guard import Guard
from ..redact import Redactor, mask_png
from ..schema.artifact import (AllCondition, AnyCondition, Condition, CssStrategy, DialogExpectation,
                               Fingerprint, FrameRef, LabelStrategy, LiteralValue, OutputCondition, ParamRef,
                               PointStrategy, RoleStrategy, Target, TargetCondition, TextCondition, TextStrategy,
                               UrlCondition, ValueCondition, XPathStrategy)
from .base import Actor, Element, FrameInfo, Observation, Resolution, TargetUnresolved

LIB = (Path(__file__).parent / "scan.js").read_text()

# Extra in-page finders sharing scan.js internals via re-declaration.
FINDERS = r"""
const __find = (() => {
  const INTERACTIVE = "a[href],button,input:not([type=hidden]),select,textarea,[onclick],[role=button],[role=link]";
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const key = (s) => norm(s).replace(/[:\s]+$/, "").toLowerCase();
  function ref(el, prefix) {
    if (!el.hasAttribute("data-cua-ref")) {
      const r = document.documentElement, n = Number(r.getAttribute("data-cua-counter") || 0) + 1;
      r.setAttribute("data-cua-counter", String(n)); el.setAttribute("data-cua-ref", `${prefix}-e${n}`);
    }
    return el.getAttribute("data-cua-ref");
  }
  function visible(el) {
    if (!el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  }
  function byRole({ role, name, prefix }) {
    const out = [];
    for (const el of document.querySelectorAll(INTERACTIVE + ",td,th,h1,h2,h3,h4,h5,h6")) {
      if (!visible(el)) continue;
      const d = __cua.describe(ref(el, prefix));
      if (d && d.role === role && key(d.name) === key(name)) out.push(d.ref);
    }
    return out;
  }
  function byText({ text, role, prefix }) {
    const out = [];
    for (const el of document.querySelectorAll(INTERACTIVE + ",td,th,b,span,font,p,div,label,li")) {
      if (!visible(el)) continue;
      const t = el.tagName === "INPUT" ? el.value : el.innerText;
      if (key(t) !== key(text)) continue;
      if (el.querySelector(INTERACTIVE) && !el.matches(INTERACTIVE)) continue;
      if (role) { const d = __cua.describe(ref(el, prefix)); if (!d || d.role !== role) continue; }
      out.push(ref(el, prefix));
    }
    // Innermost matches only (a <td><b>X</b></td> counts once).
    const els = out.map((r) => document.querySelector(`[data-cua-ref="${r}"]`));
    return out.filter((r, i) => !els.some((o, j) => j !== i && els[i].contains(o)));
  }
  function byLabel(arg) {
    const token = "h" + Math.random().toString(36).slice(2);
    __cua.findByLabel({ ...arg, token });
    return Array.from(document.querySelectorAll(`[data-cua-hit="${token}"]`)).map((e) => ref(e, arg.prefix));
  }
  return { byRole, byText, byLabel };
})();
"""


def _js(op: str) -> str:
    return f"(arg) => {{ {LIB}\n{FINDERS}\n return {op}(arg); }}"


READ_VALUE_JS = """el => {
  const t = el.tagName;
  if (t === 'SELECT') { const o = el.options[el.selectedIndex]; return o ? o.text : ''; }
  if (t === 'TEXTAREA' || (t === 'INPUT' && !['submit','button','reset','image'].includes((el.type||'').toLowerCase())))
    return el.value || '';
  return el.innerText || el.value || '';
}"""

VALUEISH = re.compile(r"^[\s$€£\d.,:\-#/]+$")


class WebSurface:
    def __init__(self, page: Page, context: BrowserContext, *, base_url: str, guard: Guard,
                 redactor: Redactor, allow_point: bool = False,
                 sensitive_labels: dict[str, Any] | None = None) -> None:
        self.page = page
        self.context = context
        self.base_url = base_url
        self.guard = guard
        self.redactor = redactor
        self.allow_point = allow_point
        self.control = None                                  # handoff.SessionControl, set by session
        self.on_policy_block: Callable[[str, str], Any] | None = None
        self.on_human_event: Callable[[dict], Awaitable[None]] | None = None
        self.expected_dialog: DialogExpectation | None = None
        self.dialog_log: list[dict[str, Any]] = []
        self.blocked_requests: list[dict[str, str]] = []
        self.sensitive_labels = {_label_key(k): str(getattr(v, "value", v))
                                 for k, v in (sensitive_labels or {}).items()}
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        page.on("framenavigated", self._on_frame_navigated)

    # ------------------------------------------------------------------ setup

    async def install(self) -> None:
        """The sandbox: one network filter + popup/download/dialog handling for every actor."""
        await self.context.route("**/*", self._filter)
        self.context.on("page", self._on_new_page)
        await self.context.expose_binding("__cuaHuman", self._human_binding)
        await self.context.add_init_script(HUMAN_LISTENER)

    def _blocked(self, kind: str, url: str, reason: str) -> None:
        self.blocked_requests.append({"kind": kind, "url": url, "reason": reason})
        if self.on_policy_block:
            self.on_policy_block(url, f"{kind}: {reason}")

    async def _filter(self, route: Route) -> None:
        req = route.request
        ok, why = self.guard.request_allowed(req.method, req.url, req.resource_type, req.is_navigation_request())
        if ok:
            await route.continue_()
            return
        self._blocked("request", req.url, f"{req.method} {why}")
        await route.abort("blockedbyclient")

    async def _on_new_page(self, page: Page) -> None:
        if page is self.page or self.guard.policy.sandbox.allow_popups:
            return
        self._blocked("popup", page.url, "new windows are not allowed")
        try:
            await page.close()
        except Exception:
            pass

    async def _on_frame_navigated(self, frame: Frame) -> None:
        """Navigations the network filter cannot see (data:, javascript:, file:) are undone."""
        url = frame.url
        if url.startswith(("http://", "https://")) or url in ("about:blank", "about:srcdoc", ""):
            return
        self._blocked("navigation", url[:80], "frame left the allowed surface; reset to about:blank")
        try:
            await frame.evaluate("location.replace('about:blank')")
        except Exception:
            pass

    async def _on_download(self, download: Any) -> None:
        if self.guard.policy.sandbox.allow_downloads:
            return
        self._blocked("download", download.url, "downloads are not allowed")
        try:
            await download.cancel()
        except Exception:
            pass

    async def _human_binding(self, source: dict, event: dict) -> None:
        if self.control is not None and self.control.state.value == "human" and self.on_human_event:
            frame = source.get("frame")
            event["frame"] = (frame.name or "top") if frame else "?"
            await self.on_human_event(event)

    async def _on_dialog(self, dialog: Dialog) -> None:
        rec = {"type": dialog.type, "message": dialog.message}
        exp = self.expected_dialog
        if exp and dialog.type == exp.type and re.search(exp.message_pattern, dialog.message, re.I):
            rec["handled"] = "accepted" if exp.accept else "dismissed"
            await (dialog.accept() if exp.accept else dialog.dismiss())
        elif dialog.type == "alert":
            rec["handled"] = "acknowledged"
            await dialog.accept()
        else:   # unexpected confirm, any prompt, beforeunload: never answer on the user's behalf
            rec["handled"] = "unexpected_dismissed"
            await dialog.dismiss()
            self._blocked("dialog", self.page.url, f"unexpected {dialog.type} dismissed")
        self.dialog_log.append(rec)

    def _lease(self, actor: Actor) -> Any:
        """Every browser action runs inside the session lease (in-flight barrier)."""
        if self.control is None:
            return contextlib.nullcontext()
        return self.control.acting(actor)

    # ------------------------------------------------------------------ frames

    @staticmethod
    def frame_label(frame: Frame) -> str:
        return "top" if frame.parent_frame is None else (frame.name or f"frame{abs(hash(frame.url)) % 1000}")

    def frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def frame_path(self, frame: Frame) -> list[FrameRef]:
        chain: list[FrameRef] = []
        f = frame
        while f.parent_frame is not None:
            if f.name:
                chain.insert(0, FrameRef(name=f.name))
            else:
                chain.insert(0, FrameRef(route=routes.canonicalize(f.url, self.base_url, {})))
            f = f.parent_frame
        return chain

    def find_frame(self, path: list[FrameRef]) -> Frame | None:
        cur = self.page.main_frame
        for ref in path:
            nxt = None
            for c in cur.child_frames:
                if c.is_detached():
                    continue
                if ref.name and c.name == ref.name:
                    nxt = c
                    break
                if not ref.name and ref.route and routes.matches(ref.route, c.url, self.base_url, {}):
                    nxt = c
                    break
            if nxt is None:
                return None
            cur = nxt
        return cur

    async def _offset(self, frame: Frame) -> tuple[float, float]:
        if frame.parent_frame is None:
            return 0.0, 0.0
        try:
            bb = await (await frame.frame_element()).bounding_box()
            return (bb["x"], bb["y"]) if bb else (0.0, 0.0)
        except Exception:
            return 0.0, 0.0

    async def _eval(self, frame: Frame, op: str, arg: Any = None) -> Any:
        return await frame.evaluate(_js(op), arg)

    async def _frame_of_ref(self, ref: str) -> Frame | None:
        for f in self.frames():
            try:
                if await self._eval(f, "__cua.hasRef", {"ref": ref}):
                    return f
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ observe

    async def observe(self, *, screenshot: bool = True) -> Observation:
        frames: list[FrameInfo] = []
        elements: list[Element] = []
        texts: dict[str, str] = {}
        for f in self.frames():
            label = self.frame_label(f)
            frames.append(FrameInfo(label, f.name or None, f.url))
            try:
                data = await self._eval(f, "__cua.scan", label)
            except Exception:
                continue  # frame navigating; next observation will see it
            ox, oy = await self._offset(f)
            for e in data["elements"]:   # field-level sensitivity: register before anything is persisted
                sens = self.sensitive_labels.get(_label_key(e.get("label") or ""))
                if sens and e.get("text") and e["role"] in ("cell", "text"):
                    self.redactor.register(e["text"], f"field:{_label_key(e['label'])}")
            texts[label] = data["text"]
            for e in data["elements"]:
                b = e["box"]
                elements.append(Element(
                    ref=e["ref"], frame=label, role=e["role"], name=e["name"] or "", tag=e["tag"],
                    text=e["text"] or "", label=e["label"] or "", value=e["value"],
                    box={"x": b["x"] + ox, "y": b["y"] + oy, "w": b["w"], "h": b["h"]},
                    options=e.get("options"), password=e.get("password", False), enabled=e.get("enabled", True)))
        h = hashlib.sha1()
        for fi in frames:
            h.update(fi.url.encode())
        for e in elements:
            h.update(f"{e.role}|{e.name}|{e.value}|{e.text}".encode())
        shot = await self._masked_screenshot(elements) if screenshot else None
        return Observation(frames, elements, texts, shot, h.hexdigest()[:16], list(self.dialog_log))

    def _sensitive_boxes(self, elements: list[Element]) -> list[tuple[float, float, float, float]]:
        boxes = []
        for e in elements:
            if e.password or self.redactor.is_sensitive_text(e.text) or \
                    (e.value and self.redactor.is_sensitive_text(e.value)):
                boxes.append((e.box["x"], e.box["y"], e.box["w"], e.box["h"]))
        return boxes

    async def _masked_screenshot(self, elements: list[Element] | None = None,
                                 extra: list[tuple[float, float, float, float]] | None = None) -> bytes:
        if elements is None:
            elements = (await self.observe(screenshot=False)).elements
        png = await self.page.screenshot()
        return mask_png(png, self._sensitive_boxes(elements) + (extra or []))

    async def screenshot(self, extra_mask: list[Target] | None = None) -> bytes:
        extra: list[tuple[float, float, float, float]] = []
        for t in extra_mask or []:
            try:
                res = await self.try_resolve(t)
                bb = await res.handle.bounding_box()
                if bb:
                    extra.append((bb["x"], bb["y"], bb["width"], bb["height"]))
            except Exception:
                pass
        return await self._masked_screenshot(None, extra)

    async def visible_text(self, frame_path: Any = "*") -> str:
        frames = self.frames() if frame_path == "*" else [self.find_frame(frame_path)]
        out = []
        for f in frames:
            if f is None:
                continue
            try:
                out.append(await self._eval(f, "__cua.visibleText"))
            except Exception:
                pass
        return "\n".join(out)

    async def text_by_frame(self) -> dict[str, str]:
        out = {}
        for f in self.frames():
            try:
                out[self.frame_label(f)] = await self._eval(f, "__cua.visibleText")
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------ resolve

    async def _locate(self, frame: Frame, s: Any) -> tuple[Any, str]:
        prefix = self.frame_label(frame)
        try:
            if isinstance(s, (RoleStrategy, TextStrategy, LabelStrategy)):
                if isinstance(s, RoleStrategy):
                    refs = await self._eval(frame, "__find.byRole", {"role": s.role, "name": s.name, "prefix": prefix})
                elif isinstance(s, TextStrategy):
                    refs = await self._eval(frame, "__find.byText", {"text": s.text, "role": s.role, "prefix": prefix})
                else:
                    refs = await self._eval(frame, "__find.byLabel", {
                        "anchor": s.anchor, "relation": s.relation, "role": s.role,
                        "offset": s.cell_offset, "prefix": prefix})
                if len(refs) != 1:
                    return None, f"{len(refs)} matches"
                return frame.locator(f'[data-cua-ref="{refs[0]}"]'), ""
            if isinstance(s, (CssStrategy, XPathStrategy)):
                loc = frame.locator(s.selector if isinstance(s, CssStrategy) else "xpath=" + s.xpath)
                n = await loc.count()
                if n != 1:
                    return None, f"{n} matches"
                if not await loc.is_visible():
                    return None, "not visible"
                return loc, ""
            if isinstance(s, PointStrategy):
                if not self.allow_point:
                    return None, "point fallback disabled by policy"
                ref = await self._eval(frame, "__cua.elementAt", {"x": s.x, "y": s.y, "prefix": prefix})
                return (frame.locator(f'[data-cua-ref="{ref}"]'), "") if ref else (None, "nothing at point")
        except Exception as e:  # frame navigated mid-evaluation
            return None, f"error: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
        return None, "unsupported strategy"

    async def try_resolve(self, target: Target) -> Resolution:
        frame = self.find_frame(target.frame)
        if frame is None:
            raise TargetUnresolved([f"frame {[f.model_dump(exclude_none=True) for f in target.frame]} not found"])
        attempts: list[str] = []
        for i, s in enumerate(target.strategies):
            loc, why = await self._locate(frame, s)
            if loc is not None:
                return Resolution(i, s.kind, loc, frame, attempts)
            attempts.append(f"{s.kind}({_strategy_brief(s)}): {why}")
        raise TargetUnresolved(attempts)

    async def resolve_ref(self, ref: str) -> Resolution:
        frame = await self._frame_of_ref(ref)
        if frame is None:
            raise TargetUnresolved([f"ref {ref} not on page (stale observation?)"])
        return Resolution(0, "ref", frame.locator(f'[data-cua-ref="{ref}"]'), frame)

    # ------------------------------------------------------------------ act

    async def click(self, res: Resolution, actor: Actor) -> None:
        async with self._lease(actor):
            await res.handle.click(timeout=3000)

    async def fill(self, res: Resolution, value: str, actor: Actor) -> None:
        async with self._lease(actor):
            await res.handle.fill(value, timeout=3000)

    async def select(self, res: Resolution, value: str, actor: Actor) -> None:
        async with self._lease(actor):
            try:
                await res.handle.select_option(label=value, timeout=3000)
            except Exception:
                await res.handle.select_option(value=value, timeout=3000)

    async def read_text(self, res: Resolution) -> str:
        return (await res.handle.inner_text(timeout=3000)).strip()

    async def read_value(self, res: Resolution) -> str:
        """What the control *shows*: an input's value, a select's chosen option text, else its text."""
        return (await res.handle.evaluate(READ_VALUE_JS)).strip()

    async def goto(self, url: str, actor: Actor) -> None:
        async with self._lease(actor):
            await self.page.goto(url, wait_until="load")

    async def press(self, key: str, actor: Actor) -> None:
        async with self._lease(actor):
            await self.page.keyboard.press(key)

    async def type_text(self, text: str, actor: Actor) -> None:
        async with self._lease(actor):
            await self.page.keyboard.type(text)

    async def click_point(self, x: float, y: float, actor: Actor) -> None:
        async with self._lease(actor):
            await self.page.mouse.click(x, y)

    async def settle(self, timeout_ms: int = 3000) -> None:
        for f in self.frames():
            try:
                await f.wait_for_load_state("load", timeout=timeout_ms)
            except Exception:
                pass

    # ------------------------------------------------------------------ conditions

    async def check(self, cond: Condition, params: dict[str, str]) -> bool:
        if isinstance(cond, UrlCondition):
            f = self.find_frame(cond.frame)
            return f is not None and routes.matches(cond.route, f.url, self.base_url, params)
        if isinstance(cond, TextCondition):
            text = await self.visible_text(cond.frame)
            return re.search(cond.pattern, text, re.I) is not None
        if isinstance(cond, TargetCondition):
            try:
                await self.try_resolve(cond.target)
                return True
            except TargetUnresolved:
                return False
        if isinstance(cond, ValueCondition):
            try:
                shown = await self.read_value(await self.try_resolve(cond.target))
            except Exception:                    # unresolved / ambiguous / navigating
                return False
            want = params.get(cond.equals.param) if isinstance(cond.equals, ParamRef) else (
                cond.equals.literal if isinstance(cond.equals, LiteralValue) else None)
            if isinstance(cond.equals, ParamRef) and want is None:
                return False
            return values.matches(shown, equals=want, pattern=cond.pattern, how=cond.normalize)
        if isinstance(cond, OutputCondition):
            return False                          # evaluated by the engine against outputs
        if isinstance(cond, AllCondition):
            return all([await self.check(c, params) for c in cond.conditions])
        if isinstance(cond, AnyCondition):
            return any([await self.check(c, params) for c in cond.conditions])
        raise TypeError(cond)

    # ------------------------------------------------------------------ grounding

    async def element_at(self, x: float, y: float) -> str | None:
        """Page coordinates -> element ref in the deepest frame under the point."""
        best = None
        for f in self.frames():
            ox, oy = await self._offset(f)
            if f.parent_frame is not None:
                try:
                    bb = await (await f.frame_element()).bounding_box()
                except Exception:
                    continue
                if not bb or not (bb["x"] <= x <= bb["x"] + bb["width"] and bb["y"] <= y <= bb["y"] + bb["height"]):
                    continue
            depth = 0
            p = f
            while p.parent_frame is not None:
                depth += 1
                p = p.parent_frame
            if best is None or depth > best[0]:
                best = (depth, f, ox, oy)
        if best is None:
            return None
        _, f, ox, oy = best
        return await self._eval(f, "__cua.elementAt", {"x": x - ox, "y": y - oy, "prefix": self.frame_label(f)})

    async def describe(self, ref: str) -> tuple[Frame, dict] | None:
        f = await self._frame_of_ref(ref)
        if f is None:
            return None
        return f, await self._eval(f, "__cua.describe", ref)

    async def ground(self, ref: str, purpose: str = "act", avoid: list[str] | None = None) -> Target:
        """Turn the element the model chose into a verified strategy bundle.

        purpose == "extract": the element's own text is *data*, so strategies
        that key on it (role/name, text) are excluded; anchor on the row label.
        ``avoid``: values (params, typed text) that must not appear in a
        strategy, or the locator would only work for this one member.
        """
        got = await self.describe(ref)
        if got is None:
            raise TargetUnresolved([f"ref {ref} not found for grounding"])
        frame, d = got
        avoid = [a for a in (avoid or []) if a]

        def safe(s: str | None) -> bool:
            return bool(s) and not any(a in s for a in avoid) and not self.redactor.is_sensitive_text(s)

        def clean(s: str | None) -> bool:  # human-readable text strategies
            return safe(s) and len(s) <= 80

        cands: list[Any] = []
        is_control = d["role"] in ("button", "link", "textbox", "combobox", "listbox", "checkbox", "radio")
        if purpose != "extract":
            if is_control and clean(d["name"]) and not VALUEISH.match(d["name"]):
                cands.append(RoleStrategy(role=d["role"], name=d["name"]))
            if is_control and clean(d["label"]) and d["label"] != d["name"]:
                cands.append(LabelStrategy(anchor=d["label"], relation="next_control", role=d["role"]))
            if d["role"] in ("button", "link") and clean(d["text"] or d["name"]):
                cands.append(TextStrategy(text=d["text"] or d["name"], role=d["role"]))
            if not is_control and clean(d["text"]) and not VALUEISH.match(d["text"]):
                cands.append(TextStrategy(text=d["text"]))
        else:
            row = d.get("row")
            if row and clean(row["anchor"]):
                cands.append(LabelStrategy(anchor=row["anchor"], relation="row_cell", cell_offset=row["offset"]))
        if d.get("css") and safe(d["css"]):
            cands.append(CssStrategy(selector=d["css"]))
        if d.get("xpath"):
            cands.append(XPathStrategy(xpath=d["xpath"]))

        verified = []
        for s in cands:
            loc, _ = await self._locate(frame, s)
            if loc is not None and await loc.get_attribute("data-cua-ref") == ref:
                verified.append(s)
        if not verified:
            b = d["box"]
            verified.append(PointStrategy(x=b["x"] + b["w"] / 2, y=b["y"] + b["h"] / 2))
        return Target(
            frame=self.frame_path(frame),
            strategies=verified,
            fingerprint=Fingerprint(role=d["role"],
                                    name=d["name"] if purpose != "extract" and clean(d["name"]) else None,
                                    tag=d["tag"], neighbor_text=d["label"] if clean(d["label"]) else None),
            sensitive=bool(d.get("password")),
        )

    def fingerprint_name(self, d: dict) -> str:
        return d.get("name") or d.get("text") or ""


def _label_key(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().rstrip(":").strip().lower()


def _strategy_brief(s: Any) -> str:
    if isinstance(s, RoleStrategy):
        return f"{s.role} '{s.name}'"
    if isinstance(s, LabelStrategy):
        return f"'{s.anchor}' {s.relation}"
    if isinstance(s, TextStrategy):
        return f"'{s.text}'"
    if isinstance(s, CssStrategy):
        return s.selector
    if isinstance(s, XPathStrategy):
        return s.xpath
    return s.kind


# Captures what a human does in a *headed* browser while they hold the lease.
HUMAN_LISTENER = LIB + r"""
;(() => {
  if (window.__cuaHumanInstalled) return; window.__cuaHumanInstalled = true;
  const send = (type, el, extra) => {
    try {
      const prefix = window.name || "top";
      if (!el.hasAttribute("data-cua-ref")) __cua.scan(prefix);
      const ref = el.getAttribute("data-cua-ref");
      const d = ref ? __cua.describe(ref) : null;
      window.__cuaHuman && window.__cuaHuman(Object.assign({
        type, role: d && d.role, name: d && d.name, label: d && d.label, tag: el.tagName.toLowerCase(),
        url: location.pathname }, extra || {}));
    } catch (e) {}
  };
  document.addEventListener("click", (e) => {
    const el = e.target.closest("a,button,input,select,textarea,[onclick]") || e.target; send("click", el);
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target, pw = (el.type || "") === "password";
    send("change", el, { value: pw ? "[password]" : el.value });
  }, true);
})();
"""
