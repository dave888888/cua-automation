"""High-level entry points shared by the CLI, the capability API and tests."""
from __future__ import annotations

import contextlib
import json
import urllib.request
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from . import store
from .config import RUNS_DIR, Environment
from .discovery.agent import DiscoveryAgent, DiscoveryResult
from .discovery.llm import LLM
from .discovery.recorder import DeclaredParam
from .evidence import RunLog, new_run_id
from .handoff.operator import OperatorConsole
from .redact import Redactor
from .replay.engine import ReplayEngine, ReplayOptions
from .schema.artifact import Capability
from .schema.results import ReplayResult
from .session import LiveSession


@contextlib.asynccontextmanager
async def _maybe_console(port: int | None) -> AsyncIterator[None]:
    if port is None:
        yield
        return
    async with OperatorConsole(port):
        yield


async def discover(*, tenant: str, goal: str, params: list[DeclaredParam], cap_id: str, llm: LLM,
                   title: str | None = None, headed: bool = False, operator_port: int | None = 8900,
                   echo: bool = True) -> tuple[DiscoveryResult, str]:
    env = Environment.load(tenant)
    log = RunLog(RUNS_DIR / new_run_id("discovery"), Redactor(env.profile.sensitive_patterns), echo=echo)
    async with _maybe_console(operator_port):
        async with LiveSession(env, log, headed=headed) as s:
            agent = DiscoveryAgent(s, llm, goal=goal, params=params, cap_id=cap_id, title=title,
                                   escalate_when_stuck=operator_port is not None)
            try:
                result = await agent.run()
            except Exception as e:
                log.event("discovery_crashed", error=f"{type(e).__name__}: {e}")
                raise
            finally:
                log.event("discovery_finished", usage=agent.usage)
    log.write_json("result.json", result.__dict__)
    log.close()
    return result, str(log.dir)


async def replay(cap: Capability | str, *, tenant: str, inputs: dict[str, Any], opts: ReplayOptions | None = None,
                 headed: bool = False, operator_port: int | None = None, echo: bool = True,
                 faults: dict[str, int] | None = None) -> ReplayResult:
    if isinstance(cap, str):
        cap = store.load(cap)
    env = Environment.load(tenant)
    log = RunLog(RUNS_DIR / new_run_id("replay"), Redactor(env.profile.sensitive_patterns), echo=echo)
    if faults:
        inject_faults(env.tenant.base_url, faults)
        log.event("faults_injected_by_harness", faults=faults)
    try:
        async with _maybe_console(operator_port):
            async with LiveSession(env, log, headed=headed) as s:
                return await ReplayEngine(s, cap, inputs, opts).run()
    finally:
        if faults:
            inject_faults(env.tenant.base_url, None)
        log.close()


def inject_faults(base_url: str, faults: dict[str, int] | None) -> None:
    """Test-harness control plane of the mock app (never reachable by agents)."""
    p = urlsplit(base_url)
    url = f"{p.scheme}://{p.netloc}/__faults"
    req = urllib.request.Request(url, method="POST" if faults else "DELETE",
                                 data=json.dumps(faults).encode() if faults else None,
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
