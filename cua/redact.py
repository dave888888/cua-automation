"""Redaction of regulated data before anything is persisted or sent to a model.

Two complementary mechanisms:

* **Known values** — every secret we hand out and every input/output value
  whose declared sensitivity is pii/financial/secret is registered here and
  scrubbed verbatim wherever it appears. Declaring sensitivity in the artifact
  contract is what makes this exact rather than heuristic.
* **Patterns** — SSNs, card numbers (Luhn-checked), emails, plus product
  patterns (e.g. money), for values we never declared.
* **Sensitive fields** — the app profile names labels whose neighbouring value
  is sensitive ("Member:", "Account #:"); the surface registers those values
  as known values the moment it observes them, so they are scrubbed from
  prompts, logs and diagnostics like any declared input.

``Redactor(use_patterns=False, template="{{{{{label}}}}}")`` is the *symbolizer*
the discovery agent uses to show the model ``{{member_id}}`` instead of the value.

Applied to: log events (recursively), observation text sent to the LLM,
screenshots (by masking element boxes), and artifacts (lint: refuse to save).
"""
from __future__ import annotations

import io
import re
from typing import Any, Iterable

DEFAULT_PATTERNS: dict[str, str] = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "pan": r"\b(?:\d[ -]?){12,18}\d\b",
    "email": r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
}


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if len(d) < 13:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
    return s % 10 == 0


class Redactor:
    def __init__(self, extra_patterns: dict[str, str] | None = None, *, use_patterns: bool = True,
                 template: str = "[REDACTED:{label}]") -> None:
        pats = {**DEFAULT_PATTERNS, **(extra_patterns or {})} if use_patterns else {}
        self.patterns = {k: re.compile(v) for k, v in pats.items()}
        self.template = template
        self._known: dict[str, str] = {}  # value -> label

    def register(self, value: Any, label: str) -> None:
        v = str(value) if value is not None else ""
        if len(v) >= 3:  # don't scrub 'a' everywhere
            self._known[v] = label
            # Also the formatted variants of money values we might see on screen.
            if re.fullmatch(r"-?\d+(\.\d+)?", v):
                try:
                    self._known[f"{float(v):,.2f}"] = label
                except ValueError:
                    pass

    def register_many(self, values: Iterable[tuple[Any, str]]) -> None:
        for v, label in values:
            self.register(v, label)

    def text(self, s: str) -> str:
        if not s:
            return s
        for v in sorted(self._known, key=len, reverse=True):
            if v in s:
                s = s.replace(v, self.template.format(label=self._known[v]))
        for name, rx in self.patterns.items():
            if name == "pan":
                s = rx.sub(lambda m: "[REDACTED:pan]" if _luhn(m.group(0)) else m.group(0), s)
            else:
                s = rx.sub(f"[REDACTED:{name}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def is_sensitive_text(self, s: str) -> bool:
        return bool(s) and self.text(s) != s

    def findings(self, s: str) -> list[str]:
        """Which pattern/known labels occur in s (for artifact lint)."""
        out = [lbl for v, lbl in self._known.items() if v in s]
        for name, rx in self.patterns.items():
            for m in rx.finditer(s):
                if name != "pan" or _luhn(m.group(0)):
                    out.append(name)
        return out


def mask_png(png: bytes, boxes: list[tuple[float, float, float, float]]) -> bytes:
    """Black out page-coordinate boxes (x, y, w, h) in a screenshot."""
    if not boxes:
        return png
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for x, y, w, h in boxes:
        draw.rectangle([x - 1, y - 1, x + w + 1, y + h + 1], fill=(0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
