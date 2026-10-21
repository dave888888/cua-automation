"""Fallback runner for environments without pytest:  python -m tests.run [-k substring]"""
from __future__ import annotations

import importlib
import sys
import time
import traceback
from pathlib import Path

from tests import _harness  # noqa: F401


def main() -> int:
    k = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
    failed = passed = 0
    for f in sorted(Path(__file__).parent.glob("test_*.py")):
        mod = importlib.import_module(f"tests.{f.stem}")
        for name in [n for n in dir(mod) if n.startswith("test_") and k in n]:
            t0 = time.monotonic()
            try:
                getattr(mod, name)()
                passed += 1
                print(f"PASS {f.stem}::{name} ({time.monotonic() - t0:.1f}s)")
            except Exception:
                failed += 1
                print(f"FAIL {f.stem}::{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
