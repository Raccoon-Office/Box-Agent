"""Stress-test: ask the relay model to issue MANY parallel tool_calls in one stream
with large argument bodies. This mimics the PPT per-page parallel generation case
where Box-Agent saw empty-arguments tool calls.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from openai import AsyncOpenAI

CONFIG_PATH = Path.home() / ".box-agent" / "config" / "config.yaml"


def _ser(o):
    if hasattr(o, "model_dump"):
        try:
            return o.model_dump(exclude_none=False)
        except Exception:
            pass
    return getattr(o, "__dict__", repr(o))


async def main() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    client = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"])
    model = cfg["model"]
    print(f"[probe] model={model}", file=sys.stderr)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "ppt_emit_html",
                "description": "Emit one PPT page's full HTML content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_index": {"type": "integer"},
                        "title": {"type": "string"},
                        "html": {"type": "string", "description": "Full <section>...</section> HTML, ~3000-8000 chars"},
                    },
                    "required": ["page_index", "title", "html"],
                },
            },
        }
    ]

    sys_prompt = (
        "You are a PPT generator. When asked, you MUST call ppt_emit_html ONCE PER PAGE "
        "in a SINGLE assistant turn (parallel tool_calls). Each call must include a "
        "complete <section> HTML body of at least 1500 characters. Do not write any "
        "natural-language text — only emit tool calls."
    )
    user_prompt = (
        "Generate a 5-page HTML deck about 'Brazilian football legends'. "
        "Pages: 1) Title, 2) Pelé, 3) Garrincha, 4) Romário, 5) Ronaldo. "
        "Emit ALL FIVE pages in this turn as parallel ppt_emit_html calls. "
        "Each <section> must have a heading, body paragraphs, and inline styles."
    )

    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
        tools=tools,
        max_tokens=16384,
        stream=True,
        stream_options={"include_usage": True},
    )

    chunks_total = 0
    tc_chunks_per_idx: dict[int, int] = defaultdict(int)
    tool_acc: dict[int, dict] = {}
    finish = None
    usage = None

    async for chunk in stream:
        chunks_total += 1
        choices = getattr(chunk, "choices", None) or []
        u = getattr(chunk, "usage", None)
        if u:
            usage = _ser(u)
        if not choices:
            continue
        c = choices[0]
        if c.finish_reason:
            finish = c.finish_reason
        d = c.delta
        if d is None:
            continue
        if d.tool_calls:
            for tc in d.tool_calls:
                idx = tc.index
                tc_chunks_per_idx[idx] += 1
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": "", "args_chunks": 0})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function and tc.function.name:
                    acc["name"] = tc.function.name
                if tc.function and tc.function.arguments is not None:
                    a = tc.function.arguments
                    if isinstance(a, str):
                        acc["arguments"] += a
                        if a:
                            acc["args_chunks"] += 1
                    else:
                        acc["nonstr"] = a

    print(f"\n=== summary ===", file=sys.stderr)
    print(f"total stream chunks: {chunks_total}", file=sys.stderr)
    print(f"finish_reason: {finish}", file=sys.stderr)
    print(f"usage: {usage}", file=sys.stderr)
    print(f"tool_call indices seen: {sorted(tool_acc)}", file=sys.stderr)
    print(f"chunks-per-tool-index: {dict(tc_chunks_per_idx)}", file=sys.stderr)

    for idx in sorted(tool_acc):
        a = tool_acc[idx]
        raw = a["arguments"]
        try:
            parsed = json.loads(raw) if raw else None
            ok = parsed is not None and parsed.get("html") and len(parsed.get("html", "")) > 100
            html_len = len(parsed.get("html", "")) if parsed else 0
        except json.JSONDecodeError as e:
            parsed = f"<decode error: {e}>"
            ok = False
            html_len = -1
        print(
            f"  idx={idx} id={a['id'][:30]!r} name={a['name']!r} "
            f"args_chunks={a['args_chunks']} args_len={len(raw)} html_len={html_len} ok={ok}",
            file=sys.stderr,
        )
        if not ok:
            print(f"    >>> RAW (first 400): {raw[:400]!r}", file=sys.stderr)
            print(f"    >>> RAW (last 200):  {raw[-200:]!r}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
