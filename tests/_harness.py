"""Shared test setup: isolated dirs, fake credentials, a mock bank on :8800."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="cua-tests-"))
os.environ.setdefault("CUA_CAPABILITIES_DIR", str(_TMP / "capabilities"))
os.environ.setdefault("CUA_RUNS_DIR", str(_TMP / "runs"))
os.environ.setdefault("CUA_SECRET_CORELINK_USERNAME", "teller01")
os.environ.setdefault("CUA_SECRET_CORELINK_PASSWORD", "demo-only-pass")

_started = False


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_mockbank(port: int = 8800) -> None:
    """Start the mock app in a background thread unless something already serves :8800."""
    global _started
    if _started or _port_open(port):
        _started = True
        return
    import uvicorn
    from mockbank.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if _port_open(port):
            break
        time.sleep(0.05)
    _started = True


try:
    from pytest import raises  # noqa: F401
except ImportError:  # minimal stand-in so the suite also runs via tests/run.py
    import contextlib

    @contextlib.contextmanager
    def raises(exc):  # type: ignore[no-redef]
        try:
            yield
        except exc:
            return
        raise AssertionError(f"expected {exc.__name__}")
