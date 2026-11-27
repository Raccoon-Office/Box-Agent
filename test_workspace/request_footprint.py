"""Deterministic size of the first main-model request, per session scenario.

Drives the real ACP adapter offline (``httpx.MockTransport`` behind the
OpenAI-protocol client, no network, no model) and measures what the provider
would receive on the first turn: system message, tool schemas and the rest of
the messages. This is the fixed-input metric for prompt/tool trimming batches;
it is exact for a given source tree and needs no paid evaluation.

Sizes are UTF-8 bytes of compact JSON (``ensure_ascii=False``). Provider token
counts differ per tokenizer and local tokenizers need a network download, so
bytes are the offline gate. Real provider ``prompt_tokens`` come from live
runs, not from here.

Scope: POSIX tool descriptions (bash describes PowerShell on Windows), MCP and
Skills disabled (their catalogs depend on the machine), memory disabled, no
image service configured, ripgrep pinned as available (code_agent sessions get
grep/glob, as with OfficeV3's bundled rg), OpenAI wire format.

Usage::

    uv run python -m test_workspace.request_footprint                  # table
    uv run python -m test_workspace.request_footprint --output a.json  # save
    uv run python -m test_workspace.request_footprint --compare a.json b.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "box_agent" / "config"
SALES_CSV = ROOT / "test_workspace" / "inputs" / "prompt_regression" / "sales.csv"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Scenario:
    name: str
    text: str
    session_mode: str | None = None
    files: dict[str, Path] = field(default_factory=dict)


# Plain chat is the baseline every user pays for; the CSV case is the
# "explicit table input" path that batch 2B must keep fully equipped.
SCENARIOS = (
    Scenario("acp_general_chat", "你好"),
    Scenario("acp_code_agent_chat", "你好", session_mode="code_agent"),
    Scenario("acp_data_analysis_chat", "你好", session_mode="data_analysis"),
    Scenario("acp_general_csv", "统计 sales.csv 里各地区的销售额合计",
             files={"sales.csv": SALES_CSV}),
)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _size(value: Any) -> int:
    text = value if isinstance(value, str) else _compact(value)
    return len(text.encode("utf-8"))


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else _compact(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize(payload: dict, workspace: Path) -> dict:
    """Remove the per-run workspace path and current date."""
    text = _compact(payload)
    for path in sorted({str(workspace.resolve()), str(workspace)}, key=len, reverse=True):
        text = text.replace(path, "<WORKSPACE>")
    for path in sorted({str(workspace.parent.resolve()), str(workspace.parent)}, key=len, reverse=True):
        text = text.replace(path, "<ROOT>")
    text = re.sub(r"<current_date>\d{4}-\d{2}-\d{2}</current_date>",
                  "<current_date><DATE></current_date>", text)
    return json.loads(text)


class _Conn:
    async def session_update(self, *args, **kwargs):
        return None


def _stream_response(request) -> Any:
    import httpx

    payload = json.loads(request.content)
    if not payload.get("stream"):
        return httpx.Response(200, json={
            "id": "footprint", "object": "chat.completion", "created": 0, "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"},
                         "finish_reason": "stop"}],
        })
    chunk = lambda delta, reason: "data: " + json.dumps({  # noqa: E731
        "id": "footprint", "object": "chat.completion.chunk", "created": 0, "model": payload["model"],
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }) + "\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text=chunk({"content": "ok"}, None) + chunk({}, "stop") + "data: [DONE]\n\n")


async def capture(scenario: Scenario, workspace: Path) -> dict:
    """Run one ACP turn offline and return the first main (streaming) request."""
    import httpx
    from openai import AsyncOpenAI

    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.llm import LLMClient
    from box_agent.retry import RetryConfig
    from box_agent.schema import LLMProvider
    from box_agent.tools.image_generation_tool import _API_KEY_ENV, _ENDPOINT_ENV
    from box_agent.tools.setup import initialize_base_tools

    import os
    for name in (*_ENDPOINT_ENV, *_API_KEY_ENV):
        os.environ.pop(name, None)
    ripgrep = workspace.parent / "bin" / "rg"
    ripgrep.parent.mkdir(exist_ok=True)
    ripgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    ripgrep.chmod(0o755)
    previous_rg = os.environ.get("BOX_AGENT_RG")
    os.environ["BOX_AGENT_RG"] = str(ripgrep)

    for name, source in scenario.files.items():
        shutil.copyfile(source, workspace / name)
    requests: list[dict] = []

    def handle(request):
        requests.append(json.loads(request.content))
        return _stream_response(request)

    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(
            workspace_dir=str(workspace), max_steps=1, goal_autopilot_enabled=False,
            enable_memory=False, enable_memory_extraction=False,
            system_prompt_path=str(CONFIG_DIR / "system_prompt.md"),
            code_prompt_path=str(CONFIG_DIR / "code_prompt.md"),
            analysis_prompt_path=str(CONFIG_DIR / "analysis_prompt.md"),
        ),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    llm = LLMClient(api_key="test", provider=LLMProvider.OPENAI, api_base="https://footprint.invalid/v1",
                    model="footprint-model", max_output_tokens=8_192, context_window=256_000,
                    retry_config=RetryConfig(enabled=False))
    await llm._client.client.close()
    llm._client.client = AsyncOpenAI(api_key="test", base_url=llm.api_base, max_retries=0,
                                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    adapter = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            tools, loader, mcp_task, skill_task = await initialize_base_tools(
                config, output=lambda *a, **k: None, llm=llm, defer_skills=True)
            template = (CONFIG_DIR / "system_prompt.md").read_text(encoding="utf-8").replace("{SKILLS_METADATA}", "")
            adapter = BoxACPAgent(_Conn(), config, llm, tools, template, skill_loader=loader,
                                  mcp_task=mcp_task, skill_task=skill_task)
            meta = {"session_mode": scenario.session_mode} if scenario.session_mode else {}
            session = await adapter.new_session(str(workspace), **meta)
            await adapter.prompt(session.session_id, [{"type": "text", "text": scenario.text}])
    finally:
        if adapter is not None:
            await adapter.aclose()
        await llm.aclose()
        if previous_rg is None:
            os.environ.pop("BOX_AGENT_RG", None)
        else:
            os.environ["BOX_AGENT_RG"] = previous_rg
    main = [request for request in requests if request.get("stream")]
    if not main:
        raise RuntimeError(f"{scenario.name}: no main request captured")
    return normalize(main[0], workspace)


def footprint(payload: dict) -> dict:
    messages = payload.get("messages", [])
    system = [message for message in messages if message.get("role") == "system"]
    rest = [message for message in messages if message.get("role") != "system"]
    tools = {}
    for tool in payload.get("tools", []):
        function = tool.get("function", tool)
        tools[function["name"]] = {
            "bytes": _size(tool),
            "description_bytes": _size(function.get("description", "")),
            "parameters_bytes": _size(function.get("parameters", {})),
            "sha256": _digest(tool),
        }
    return {
        "system_bytes": sum(_size(message.get("content", "")) for message in system),
        "system_sha256": _digest(system),
        "tools_bytes": _size(payload.get("tools", [])),
        "tool_count": len(tools),
        "messages_bytes": _size(rest),
        "request_bytes": _size({key: payload[key] for key in ("messages", "tools") if key in payload}),
        "tools": dict(sorted(tools.items())),
    }


@contextlib.contextmanager
def pinned_runtimes():
    """Render Skill Runtime Context as on a machine without managed runtimes.

    Discovery reads the sandbox venv and the managed Node manifest, so the
    system prompt (and this snapshot) would otherwise differ between machines.
    The section's real text is kept; only the discovered state is fixed.
    """
    from unittest import mock

    from box_agent.tools import runtime

    def python(sandbox_mode, *_args, **_kwargs):
        if sandbox_mode:
            return runtime.SkillRuntime("python", "missing", "box_agent", notes=(
                "Sandbox venv Python executable does not exist yet; use execute_code for Python code execution.",))
        return runtime.SkillRuntime("python", "missing", "missing",
                                    notes=("No Python runtime is configured for shell skills.",))

    def node(*_args, **_kwargs):
        return runtime.SkillRuntime("node", "missing", "missing",
                                    notes=("No Box-Agent managed Node runtime manifest was found.",))

    with mock.patch.object(runtime, "_build_python_runtime", python), \
            mock.patch.object(runtime, "_build_node_runtime", node):
        yield


async def measure(scenarios=SCENARIOS) -> dict:
    results = {}
    with pinned_runtimes():
        for scenario in scenarios:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "workspace"
                workspace.mkdir()
                results[scenario.name] = footprint(await capture(scenario, workspace))
    return {"schema_version": SCHEMA_VERSION, "unit": "utf8_bytes_compact_json", "scenarios": results}


def compare(before: dict, after: dict) -> dict:
    rows = {}
    for name in sorted(set(before["scenarios"]) | set(after["scenarios"])):
        a, b = before["scenarios"].get(name), after["scenarios"].get(name)
        if a is None or b is None:
            rows[name] = {"status": "missing_in_" + ("before" if a is None else "after")}
            continue
        keys = ("system_bytes", "tools_bytes", "messages_bytes", "request_bytes", "tool_count")
        tool_names = sorted(set(a["tools"]) | set(b["tools"]))
        rows[name] = {
            **{key: {"before": a[key], "after": b[key], "delta": b[key] - a[key]} for key in keys},
            "tools": {tool: (b["tools"].get(tool, {}).get("bytes", 0) - a["tools"].get(tool, {}).get("bytes", 0))
                      for tool in tool_names
                      if a["tools"].get(tool, {}).get("sha256") != b["tools"].get(tool, {}).get("sha256")},
        }
    return {"schema_version": SCHEMA_VERSION, "unit": before.get("unit"), "scenarios": rows}


def _print_report(report: dict) -> None:
    print(f"{'scenario':<26}{'system':>9}{'tools':>9}{'#':>4}{'messages':>10}{'request':>10}")
    for name, row in report["scenarios"].items():
        print(f"{name:<26}{row['system_bytes']:>9}{row['tools_bytes']:>9}{row['tool_count']:>4}"
              f"{row['messages_bytes']:>10}{row['request_bytes']:>10}")
    first = next(iter(report["scenarios"].values()))
    print("\nlargest tools (first scenario):")
    for tool, info in sorted(first["tools"].items(), key=lambda item: -item[1]["bytes"])[:8]:
        print(f"  {tool:<26}{info['bytes']:>7}  (description {info['description_bytes']}, "
              f"parameters {info['parameters_bytes']})")


def _print_comparison(result: dict) -> None:
    print(f"{'scenario':<26}{'Δsystem':>10}{'Δtools':>10}{'Δmessages':>11}{'Δrequest':>11}")
    for name, row in result["scenarios"].items():
        if "status" in row:
            print(f"{name:<26}{row['status']}")
            continue
        print(f"{name:<26}{row['system_bytes']['delta']:>+10}{row['tools_bytes']['delta']:>+10}"
              f"{row['messages_bytes']['delta']:>+11}{row['request_bytes']['delta']:>+11}")
        for tool, delta in row["tools"].items():
            print(f"  {tool:<24}{delta:>+10}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, help="write the footprint JSON here")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"),
                        help="compare two saved footprints instead of measuring")
    args = parser.parse_args(argv)
    if args.compare:
        before, after = (json.loads(path.read_text(encoding="utf-8")) for path in args.compare)
        _print_comparison(compare(before, after))
        return 0
    report = asyncio.run(measure())
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
