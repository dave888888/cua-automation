"""The perception/action seam.

Everything above this line (discovery agent, recorder, replay engine, handoff)
talks to a ``Surface``. Everything below it is surface-specific:

    WebSurface      Playwright: frames, in-page scan, DOM locators   (implemented)
    LegacyWeb       same adapter; the scan/label strategies exist for it
    DesktopSurface  UI Automation / AT-SPI tree + screenshots         (design only)

A surface must be able to: observe (elements + screenshot), resolve a
``Target`` bundle (one attempt, no waiting — the engine owns waiting so it can
check detectors between polls), act on a resolution, read text, evaluate a
frame/window URL or title, and *ground* an element (turn "the thing the model
pointed at" into a durable ``Target``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..schema.artifact import Condition, Target


class Actor(str, Enum):
    agent = "agent"      # LLM discovery or deterministic replay
    human = "human"      # operator holding the control lease


@dataclass
class Element:
    ref: str
    frame: str           # frame display path, e.g. "main"
    role: str
    name: str
    tag: str
    text: str
    label: str
    value: str | None
    box: dict[str, float]          # page coordinates
    options: list[str] | None = None
    password: bool = False
    enabled: bool = True

    def brief(self) -> str:
        shown = self.name or (self.text if self.role == "text" else "")
        s = f'{self.ref} {self.role} "{shown[:60]}"'
        if self.label and self.label != self.name:
            s += f' label="{self.label[:40]}"'
        if self.value is not None:
            s += f' value="{self.value[:40]}"'
        if self.options:
            s += f" options={self.options}"
        if self.role in ("cell", "text", "columnheader", "heading") and self.text not in (self.name, shown):
            s += f' text="{self.text[:80]}"'
        if not self.enabled:
            s += " disabled"
        return s


@dataclass
class FrameInfo:
    path: str            # "top", "banner", "main" ...
    name: str | None
    url: str


@dataclass
class Observation:
    frames: list[FrameInfo]
    elements: list[Element]
    text_by_frame: dict[str, str]
    screenshot: bytes | None = None     # already masked
    state_hash: str = ""
    dialogs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Resolution:
    strategy_index: int
    strategy_kind: str
    handle: Any                    # adapter-specific (Playwright Locator)
    frame: Any = None
    attempts: list[str] = field(default_factory=list)


class TargetUnresolved(Exception):
    def __init__(self, attempts: list[str]) -> None:
        super().__init__("; ".join(attempts))
        self.attempts = attempts


class Surface(Protocol):
    async def observe(self, *, screenshot: bool = True) -> Observation: ...
    async def try_resolve(self, target: Target) -> Resolution: ...
    async def click(self, res: Resolution, actor: Actor) -> None: ...
    async def fill(self, res: Resolution, value: str, actor: Actor) -> None: ...
    async def select(self, res: Resolution, value: str, actor: Actor) -> None: ...
    async def read_text(self, res: Resolution) -> str: ...
    async def goto(self, url: str, actor: Actor) -> None: ...
    async def check(self, cond: Condition, params: dict[str, str]) -> bool: ...
    async def visible_text(self, frame_path: Any = "*") -> str: ...
    async def ground(self, ref: str, purpose: str, avoid: list[str]) -> Target: ...
    async def element_at(self, x: float, y: float) -> str | None: ...
    async def screenshot(self, extra_mask: list[Target] | None = None) -> bytes: ...
