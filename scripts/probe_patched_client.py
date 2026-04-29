"""Verify the patched OpenAIClient: surface finish_reason='length' and emit
no broken tool_calls when arguments are truncated mid-stream.

Forces truncation by passing max_tokens=300 (small) on a request that needs
to emit a long arguments string.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import Message
from box_agent.retry import RetryConfig

CONFIG_PATH = Path.home() / ".box-agent" / "config" / "config.yaml"


async def main() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = OpenAIClient(
        api_key=cfg["api_key"],
        api_base=cfg["api_base"],
        model=cfg["model"],
        retry_config=RetryConfig(enabled=False),
    )

    tools = [
        type("T", (), dict(
            to_openai_schema=lambda self: {
                "type": "function",
                "function": {
                    "name": "ppt_emit_html",
                    "description": "Emit one PPT page",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "page_index": {"type": "integer"},
                            "title": {"type": "string"},
                            "html": {"type": "string"},
                        },
                        "required": ["page_index", "title", "html"],
                    },
                },
            }
        ))()
    ]

    messages = [
        Message(role="system", content="Emit a single ppt_emit_html call with html >= 2000 chars."),
        Message(role="user", content="Brazilian football legends, page 1 title slide. Long body."),
    ]

    # First, sanity: with our patched default (16384), it should succeed
    print("=== Test A: default max_tokens (patched 16384) ===", file=sys.stderr)
    finish = None
    tool_calls = None
    async for ev in client.generate_stream(messages, tools):
        if ev.type == "finish":
            finish = ev.finish_reason
            tool_calls = ev.tool_calls
    print(f"finish_reason={finish}", file=sys.stderr)
    print(f"tool_calls={[(t.function.name, list((t.function.arguments or {}).keys())) for t in (tool_calls or [])]}", file=sys.stderr)
    assert finish in {"tool_calls", "stop", "end_turn"}, f"unexpected finish_reason: {finish}"
    assert tool_calls and len(tool_calls) == 1, "expected exactly one tool_call"
    assert "html" in tool_calls[0].function.arguments, "expected complete arguments"
    print("OK: complete tool_call with arguments", file=sys.stderr)

    # Test B: monkeypatch _DEFAULT_MAX_TOKENS to force truncation, confirm our
    # surface-truncated-tool_call branch fires.
    print("\n=== Test B: force truncation, expect finish_reason=length ===", file=sys.stderr)
    from box_agent.llm import openai_client as oc_mod
    oc_mod._DEFAULT_MAX_TOKENS = 200  # type: ignore[attr-defined]

    finish = None
    tool_calls = None
    async for ev in client.generate_stream(messages, tools):
        if ev.type == "finish":
            finish = ev.finish_reason
            tool_calls = ev.tool_calls
    print(f"finish_reason={finish}", file=sys.stderr)
    print(f"tool_calls={tool_calls}", file=sys.stderr)
    assert finish == "length", f"expected length, got {finish!r}"
    # The truncated tool_call should be dropped (no broken JSON fed back)
    assert not tool_calls, f"truncated tool_call should be dropped, got {tool_calls}"
    print("OK: finish=length, broken tool_call dropped", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
