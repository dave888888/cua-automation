"""Browser sandbox boundary tests (P1-4).

Each probe runs *inside the application's own frame* — the position a
misbehaving page, a wrong click or a confused model would be in — through the
same LiveSession that discovery, replay and the operator console all use.
"""
from tests import _harness

import asyncio
import json
from pathlib import Path

from cua.config import RUNS_DIR, Environment
from cua.evidence import RunLog, new_run_id
from cua.redact import Redactor
from cua.schema.artifact import FrameRef
from cua.session import LiveSession


async def _probe(fn):
    _harness.ensure_mockbank()
    env = Environment.load("acme")
    log = RunLog(RUNS_DIR / new_run_id("sandbox"), Redactor(env.profile.sensitive_patterns), echo=False)
    async with LiveSession(env, log) as s:
        await s.ensure_authenticated()
        main = s.surface.find_frame([FrameRef(name="main")])
        out = await fn(s, main)
    log.close()
    events = [json.loads(line) for line in Path(log.dir, "events.jsonl").read_text().splitlines()]
    return out, [e for e in events if e["type"] == "sandbox_blocked"]


FETCH = "([m, u, b]) => fetch(u, {method: m, body: b, headers: {'content-type': " \
        "'application/x-www-form-urlencoded'}}).then(r => r.status).catch(() => 'blocked')"


def test_post_only_to_declared_mutation_endpoints():
    async def go(s, main):
        return {
            "legit_post": await main.evaluate(FETCH, ["POST", "/acme/mbr/100234/share/review",
                                                      "f_tp=Savings+Club&f_nk=x&f_dp=1"]),
            "post_elsewhere": await main.evaluate(FETCH, ["POST", "/acme/mbr/find", "x=1"]),
            "delete": await main.evaluate(FETCH, ["DELETE", "/acme/mbr/100234/share/commit", ""]),
            "get": await main.evaluate(FETCH, ["GET", "/acme/mbr/find", None]),
            "foreign_origin": await main.evaluate(FETCH, ["GET", "http://localhost:9/x", None]),
        }
    out, blocked = asyncio.run(_probe(go))
    assert out["legit_post"] == 200 and out["get"] == 200          # the legacy POST form still works
    assert out["post_elsewhere"] == "blocked" and out["delete"] == "blocked" and out["foreign_origin"] == "blocked"
    reasons = " ".join(b["reason"] for b in blocked)
    assert "not a permitted mutation endpoint" in reasons and "method DELETE" in reasons


def test_popups_are_closed():
    async def go(s, main):
        await main.evaluate("window.open('/acme/welcome', '_blank')")
        await asyncio.sleep(0.8)
        return len([p for p in s.surface.context.pages if not p.is_closed()])
    pages, blocked = asyncio.run(_probe(go))
    assert pages == 1 and any(b["reason"].startswith("popup") for b in blocked)


def test_downloads_are_cancelled():
    async def go(s, main):
        await main.evaluate("""() => { const a = document.createElement('a');
            a.href = URL.createObjectURL(new Blob(['secret,export'])); a.download = 'export.csv';
            document.body.appendChild(a); a.click(); }""")
        await asyncio.sleep(0.8)
    _, blocked = asyncio.run(_probe(go))
    assert any(b["reason"].startswith("download") for b in blocked)


def test_unexpected_dialogs_are_dismissed_not_answered():
    async def go(s, main):
        confirm = await main.evaluate("() => confirm('Wire $10,000 to account 42?')")
        prompt = await main.evaluate("() => prompt('Enter supervisor PIN')")
        return confirm, prompt
    (confirm, prompt), blocked = asyncio.run(_probe(go))
    assert confirm is False and prompt is None
    assert sum(b["reason"].startswith("dialog") for b in blocked) == 2


def test_frame_cannot_escape_to_data_or_javascript_urls():
    async def go(s, main):
        await main.evaluate("() => { location.href = 'data:text/html,<h1>phish</h1>'; }")
        await asyncio.sleep(0.8)
        return [f.url for f in s.surface.frames()]
    urls, blocked = asyncio.run(_probe(go))
    assert not any(u.startswith("data:") for u in urls)
    assert any(b["reason"].startswith(("navigation", "request")) for b in blocked)
