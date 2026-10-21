"""An AI agent discovering and invoking capabilities from the catalog.

    python examples/agent_invoke.py "What is the savings balance of member 100234 at Blue River?"

The agent only sees the generated tool definitions (typed inputs, outputs,
business outcomes). It never sees the UI. Each tool call is a deterministic
replay; irreversible capabilities are invoked with irreversible="deny", so the
agent can *prepare* but never commit on its own in this demo.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cua.catalog import catalog, invoke  # noqa: E402
from cua.discovery.llm import DEFAULT_MODEL  # noqa: E402


async def main(question: str) -> None:
    import anthropic

    tools = [{k: v for k, v in t.items() if k != "x-capability"} for t in catalog()]
    print("tools offered:", [t["name"] for t in tools], file=sys.stderr)
    client = anthropic.AsyncAnthropic()
    messages: list[dict] = [{"role": "user", "content": question}]
    for _ in range(4):
        resp = await client.messages.create(model=DEFAULT_MODEL, max_tokens=4096, tools=tools, messages=messages,
                                            system="You help bank staff. Use the tools to look things up.")
        messages.append({"role": "assistant", "content": resp.content})   # verbatim (keeps thinking blocks)
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            print("".join(b.text for b in resp.content if b.type == "text"))
            return
        results = []
        for c in calls:
            print(f"-> invoking {c.name} {json.dumps(c.input)}", file=sys.stderr)
            r = await invoke(c.name, dict(c.input), irreversible="deny")
            payload = {k: v for k, v in r.model_dump(mode="json").items()
                       if k in ("status", "outputs", "outcome", "blocked", "failure")}
            print(f"<- {r.status.value} (run {r.run_id})", file=sys.stderr)
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(payload)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "What is the savings balance of member 100234 at Blue River?"))
