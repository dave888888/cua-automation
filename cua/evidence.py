"""Run evidence: a redacted JSONL event log plus masked screenshots/DOM snapshots.

Every run (discovery or replay) gets ``runs/<run_id>/``:

    events.jsonl        structured log — what happened and why (redacted)
    shots/NNN-*.png     masked screenshots (every discovery turn; replay on failure)
    dom/NNN-*.html      redacted visible-text snapshot of each frame on failure
    result.json         final result (replay) or summary (discovery)
    interventions/      intervention requests and resolutions (handoff)

Playwright traces are deliberately *off* by default: a trace stores raw DOM
and network bodies, i.e. unredacted PII. Enable with ``--trace`` only against
fake data.
"""
from __future__ import annotations

import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import Redactor


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


class RunLog:
    def __init__(self, run_dir: Path, redactor: Redactor, *, echo: bool = True) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "shots").mkdir(exist_ok=True)
        self.redactor = redactor
        self.echo = echo
        self._seq = 0
        self._t0 = time.monotonic()
        self._fh = open(self.dir / "events.jsonl", "a", buffering=1)

    @property
    def run_id(self) -> str:
        return self.dir.name

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        rec = {
            "seq": self._seq,
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "type": type_,
            **self.redactor.obj(_jsonable(data)),
        }
        self._fh.write(json.dumps(rec, default=str) + "\n")
        if self.echo:
            brief = {k: v for k, v in rec.items() if k not in ("seq", "ts", "t_ms")}
            line = json.dumps(brief, default=str)
            print(f"  [{rec['t_ms']:>6}ms] {line[:220]}", file=sys.stderr)

    def save_png(self, name: str, png: bytes) -> str:
        """Caller must pass an already-masked screenshot."""
        rel = f"shots/{self._seq:03d}-{name}.png"
        (self.dir / rel).write_bytes(png)
        return rel

    def save_text(self, name: str, text: str, sub: str = "dom") -> str:
        (self.dir / sub).mkdir(exist_ok=True)
        rel = f"{sub}/{self._seq:03d}-{name}.txt"
        (self.dir / rel).write_text(self.redactor.text(text))
        return rel

    def write_json(self, name: str, obj: Any) -> str:
        (self.dir / name).write_text(json.dumps(self.redactor.obj(_jsonable(obj)), indent=2, default=str))
        return name

    def close(self) -> None:
        self._fh.close()


def _jsonable(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o
