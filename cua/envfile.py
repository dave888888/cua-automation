"""Load ``.env`` from the repo root into the environment.

Deliberately imports nothing from ``cua``: scripts call this *before* they set
CUA_CAPABILITIES_DIR / CUA_RUNS_DIR, and ``cua.config`` reads those once, at
import time. Importing config here would freeze the paths too early.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(root: Path | None = None) -> None:
    p = Path(os.environ.get("CUA_HOME", root or Path(__file__).resolve().parent.parent)) / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
