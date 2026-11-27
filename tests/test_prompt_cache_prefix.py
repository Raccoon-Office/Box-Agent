"""Anthropic prompt-cache prefix stays byte-identical across a session.

The cached prefix is ``tools -> system`` with one breakpoint on the system
block (``apply_cache_breakpoints``). Two features must keep that prefix
stable within a day, otherwise every turn re-pays the whole prompt:

- Automatic memory is a per-day system snapshot (``MemoryHistory``). Edits to
  the backend later that day, or reopening the session, must not change it.
- Tool interface v2 freezes provider tool definitions. Tools discovered
  mid-session are delivered as runtime messages at the tail and invoked
  through ``call_tool``; they must not change the ``tools`` array.

These tests compare the request body the real ``AnthropicClient`` would send.
"""
from __future__ import annotations

from datetime import date, timedelta
import json

import httpx
import pytest

from box_agent.config import MemoryHttpOperation
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.session_log import SessionLog
from tests.test_mcp_tool_search import FakeMCPTool
from tests.test_memory_backends import (  # noqa: F401  (memory_http is a fixture)
    backend_settings, managed_session, memory_http, run, set_memory_date,
)
from tests.test_stable_tool_exposure import make


_EPHEMERAL = {"type": "ephemeral"}


def _request_body(messages: list[Message], tools=None) -> dict:
    client = AnthropicClient(api_key="test")
    system, api_messages = client._convert_messages(messages)
    return client._request_body_params(system, api_messages, tools, thinking_enabled=False)


def _cached_prefix(params: dict) -> str:
    """The bytes the system breakpoint caches: tool definitions, then system."""
    return json.dumps({"tools": params.get("tools"), "system": params.get("system")},
                      ensure_ascii=False, sort_keys=True, default=str)


def test_prefix_comparison_detects_a_one_character_system_change():
    """Control: the comparison below is not vacuous."""
    first = _request_body([Message(role="system", content="rules\npreference A"), Message(role="user", content="hi")])
    second = _request_body([Message(role="system", content="rules\npreference B"), Message(role="user", content="hi")])
    assert _cached_prefix(first) != _cached_prefix(second)
    assert first["system"][-1]["cache_control"] == _EPHEMERAL


@pytest.mark.parametrize("backend_type", ["local", "memsense", "generic"])
async def test_memory_snapshot_keeps_cached_prefix_for_the_whole_day(
    tmp_path, memory_http, monkeypatch, backend_type,
):
    today = date(2026, 11, 28)
    set_memory_date(monkeypatch, today)
    settings = backend_settings(tmp_path)
    settings.memory_backend_type = backend_type
    if backend_type == "generic":
        settings.memory_external.recall = MemoryHttpOperation(path="/context", response_path="text")
        memory_http.handler = lambda request, payload: httpx.Response(200, json={"text": memory_http.profile})

    def change_memory(session, text):
        if backend_type == "local":
            session.memory_manager.write_core(text)
        else:
            memory_http.profile = text

    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="session-1", cwd=tmp_path)
    session, llm = await managed_session(tmp_path, settings, session_log=log)
    try:
        change_memory(session, "姓名：缓存测试用户，偏好 A")
        await run(session, "第一轮")
        await run(session, "第二轮", "run-2")
        change_memory(session, "姓名：缓存测试用户，偏好 B")
        await run(session, "当天改记忆后", "run-3")
        await run(session, "第四轮", "run-4")
    finally:
        await session.aclose()
        log.close()

    # Reopen the same session on the same day (host restart, switching back).
    log = SessionLog.open(root, session_id="session-1", cwd=tmp_path)
    reopened, reopened_llm = await managed_session(tmp_path, settings, session_log=log, resume=True)
    try:
        await run(reopened, "同日重开", "run-5")
        set_memory_date(monkeypatch, today + timedelta(days=1))
        await run(reopened, "第二天", "run-6")
        await run(reopened, "第二天再问", "run-7")
    finally:
        await reopened.aclose()
        log.close()

    bodies = [_request_body(request["messages"], request.get("tools"))
              for request in [*llm.requests, *reopened_llm.requests]]
    assert len(bodies) == 7
    prefixes = [_cached_prefix(body) for body in bodies]
    same_day, next_day = prefixes[:5], prefixes[5:]

    assert len(set(same_day)) == 1
    assert "偏好 A" in same_day[0] and "偏好 B" not in same_day[0]
    assert bodies[0]["system"][-1]["cache_control"] == _EPHEMERAL
    # The conversation after the prefix only grows; earlier turns are not rewritten.
    for before, after in zip(bodies[:4], bodies[1:5]):
        assert after["messages"][:len(before["messages"]) - 1] == before["messages"][:-1]
    # The next day refreshes once, then stays stable again.
    assert next_day[0] != same_day[0] and "偏好 B" in next_day[0]
    assert len(set(next_day)) == 1


async def test_mid_session_mcp_discovery_keeps_cached_tools_prefix():
    catalog, manager, search, engine = make()

    def body() -> dict:
        return _request_body([Message(role="system", content="S"), Message(role="user", content="u")],
                             list(engine.prepare_tools().definitions))

    initial = body()
    catalog.replace_server("crm", [FakeMCPTool("lookup", "crm", "Look up a synthetic record"),
                                   FakeMCPTool("update", "crm", "Update a synthetic record")])
    after_connect = body()
    result = await search.execute(tool_names=["lookup", "update"])
    manager.record_search_result("search-1", result.content)
    manager.acknowledge_search_results([
        Message(role="tool", name="tool_search", tool_call_id="search-1", content=result.content),
    ])
    activated = json.loads(result.content)["activated"]
    after_activation = body()

    assert len(activated) == 2
    call = ToolCall(id="call-1", type="function", function=FunctionCall(
        name="call_tool", arguments={"tool_ref": activated[0]["tool_ref"], "arguments_json": "{}"},
    ))
    assert engine.prepare_tools().resolve_call(call)[2] is None
    assert _cached_prefix(initial) == _cached_prefix(after_connect) == _cached_prefix(after_activation)
    tool_names = [tool["name"] for tool in initial["tools"]]
    assert "call_tool" in tool_names and "tool_search" in tool_names
    assert not {"lookup", "update"} & set(tool_names)
