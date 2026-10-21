"""LLM seam. The discovery agent depends only on ``LLM.decide``.

``AnthropicLLM`` is the real implementation (Claude, tool use, screenshot +
element list). ``ScriptedLLM`` (scripted.py) is a deterministic stand-in used by
tests — it is *not* used to produce the evidence of a real discovery run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    text: str
    tool_calls: list[ToolCall]
    assistant_content: list[Any]              # appended to history verbatim (SDK blocks or dicts)
    usage: dict[str, int] = field(default_factory=dict)


class LLM(Protocol):
    model: str

    async def decide(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     observation: Any = None) -> LLMTurn: ...


DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-5-5")


class AnthropicLLM:
    """Claude via the Messages API, one tool call per turn.

    * Forced tool use (``tool_choice: any``) is preferred, but models that reason before
      answering reject it; on that specific 400 we fall back to ``auto`` for the rest of
      the run. The agent loop already handles a turn without a tool call.
    * The assistant turn is kept *verbatim* (including thinking blocks and their
      signatures), which the API requires when a tool-use conversation continues.
    """

    def __init__(self, model: str | None = None, max_tokens: int = 4096, client: Any = None) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("pip install anthropic  (and set ANTHROPIC_API_KEY)") from e
            client = anthropic.AsyncAnthropic()
        self.client = client
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.force_tool = True

    async def _create(self, **kw: Any) -> Any:
        if self.force_tool:
            try:
                return await self.client.messages.create(tool_choice={"type": "any"}, **kw)
            except Exception as e:     # anthropic.BadRequestError, matched without importing the SDK
                if getattr(e, "status_code", None) == 400 and "tool_choice" in str(e):
                    self.force_tool = False
                else:
                    raise
        return await self.client.messages.create(tool_choice={"type": "auto"}, **kw)

    async def decide(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     observation: Any = None) -> LLMTurn:
        resp = await self._create(model=self.model, max_tokens=self.max_tokens, system=system, tools=tools,
                                  messages=messages)
        text, calls, content = "", [], []
        for b in resp.content:
            content.append(b)          # verbatim: thinking/redacted_thinking blocks must be sent back as-is
            if b.type == "text":
                text += b.text
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input)))
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return LLMTurn(text, calls, content, usage)
