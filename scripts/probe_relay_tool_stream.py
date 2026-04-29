"""Probe a tool-using LLM stream and dump raw chunk shapes.

Used to diagnose mid-relay providers that translate Anthropic tool_use ->
OpenAI tool_calls and may drop or mis-shape the arguments stream.

Usage:
    uv run python scripts/probe_relay_tool_stream.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml
from openai import AsyncOpenAI

CONFIG_PATH = Path.home() / ".box-agent" / "config" / "config.yaml"


def _serialize(obj):
    """Best-effort dump of pydantic / dataclass / plain objects."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(exclude_none=False)
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: _serialize(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return repr(obj)


async def main() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    api_key = cfg["api_key"]
    api_base = cfg["api_base"]
    model = cfg["model"]

    print(f"[probe] model={model} base={api_base}", file=sys.stderr)

    client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write text content to a file at the given path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path"},
                        "content": {"type": "string", "description": "File contents"},
                    },
                    "required": ["path", "content"],
                },
            },
        }
    ]

    messages = [
        {
            "role": "system",
            "content": "You are a test agent. When asked to create a file, you MUST call the write_file tool with both `path` and `content` arguments populated.",
        },
        {
            "role": "user",
            "content": "Please create a file at /tmp/probe_hello.txt containing exactly the text: hello relay",
        },
    ]

    print("\n=== Test 1: streaming tool call ===", file=sys.stderr)
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        stream=True,
        stream_options={"include_usage": True},
    )

    chunk_idx = 0
    tool_acc: dict[int, dict] = {}
    async for chunk in stream:
        chunk_idx += 1
        choices = getattr(chunk, "choices", None) or []
        usage = getattr(chunk, "usage", None)
        choice = choices[0] if choices else None
        delta = getattr(choice, "delta", None) if choice else None
        finish = getattr(choice, "finish_reason", None) if choice else None
        tcs = getattr(delta, "tool_calls", None) if delta else None

        line = {
            "n": chunk_idx,
            "finish": finish,
            "content": getattr(delta, "content", None) if delta else None,
            "reasoning": getattr(delta, "reasoning_content", None) if delta else None,
            "tool_calls": [_serialize(tc) for tc in tcs] if tcs else None,
            "usage": _serialize(usage) if usage else None,
        }
        print(f"CHUNK {json.dumps(line, ensure_ascii=False, default=str)}", file=sys.stderr)

        if tcs:
            for tc in tcs:
                idx = getattr(tc, "index", 0)
                fn = getattr(tc, "function", None)
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    acc["id"] = tc.id
                if fn and getattr(fn, "name", None):
                    acc["name"] = fn.name
                if fn and getattr(fn, "arguments", None) is not None:
                    args = fn.arguments
                    if isinstance(args, str):
                        acc["arguments"] += args
                    else:
                        acc["arguments_nonstr"] = args

    print("\n=== Accumulated tool calls (streaming) ===", file=sys.stderr)
    print(json.dumps(tool_acc, indent=2, default=str), file=sys.stderr)
    for idx, acc in tool_acc.items():
        raw = acc.get("arguments", "")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError as e:
            parsed = f"<JSON decode error: {e}>"
        print(f"  tool[{idx}] name={acc.get('name')!r} parsed={parsed!r}", file=sys.stderr)

    print("\n=== Test 2: non-streaming tool call ===", file=sys.stderr)
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        stream=False,
    )
    print(json.dumps(_serialize(resp), indent=2, default=str), file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
