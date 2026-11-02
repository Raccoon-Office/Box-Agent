from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator
from mcp.server.fastmcp.exceptions import ToolError

from box_agent.llm import AnthropicClient, OpenAIClient
from box_agent.mcp_servers import web_extract_server
from box_agent.tools.base import ToolResult
from box_agent.tools.mcp_loader import MCPTool


@pytest.mark.asyncio
async def test_advertised_parameters_have_explicit_types_in_both_provider_formats():
    listed = await web_extract_server.mcp.list_tools()
    assert len(listed) == 1
    tool = listed[0]
    assert tool.name == "web_extract"
    assert tool.description == web_extract_server.WebExtractTool(llm=None).description
    assert "open a visible browser" in tool.description
    wrapper = MCPTool(
        name=tool.name,
        description=tool.description or "",
        parameters=tool.inputSchema,
        session=SimpleNamespace(),
    )
    openai = OpenAIClient._convert_tools(None, [wrapper])[0]
    anthropic = AnthropicClient._convert_tools(None, [wrapper])[0]

    for schema in (
        tool.inputSchema,
        openai["function"]["parameters"],
        anthropic["input_schema"],
    ):
        Draft202012Validator.check_schema(schema)
        assert schema["required"] == ["url"]
        for name, expected_type in (
            ("url", "string"),
            ("model", "string"),
            ("max_output_tokens", "integer"),
        ):
            parameter = schema["properties"][name]
            assert parameter["type"] == expected_type
            assert "anyOf" not in parameter
            assert "default" not in parameter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "optional_arguments",
    [{}, {"model": "summary-model", "max_output_tokens": 8_000}],
)
async def test_advertised_tool_accepts_omitted_or_typed_optional_arguments(
    optional_arguments,
):
    tool = (await web_extract_server.mcp.list_tools())[0]
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(content=[], isError=False),
        ),
    )
    wrapper = MCPTool(
        name=tool.name,
        description=tool.description or "",
        parameters=tool.inputSchema,
        session=session,
    )
    arguments = {"url": "https://example.com", **optional_arguments}
    result = await wrapper.invoke(arguments)
    assert result.success
    session.call_tool.assert_awaited_once_with("web_extract", arguments=arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"url": "https://example.com", "model": 12},
        {"url": "https://example.com", "max_output_tokens": "invalid"},
        {"url": "https://example.com", "max_output_tokens": 0},
        {"url": "https://example.com", "model": None},
        {"url": "https://example.com", "max_output_tokens": None},
        {"url": "https://example.com", "unknown": True},
    ],
)
async def test_advertised_tool_rejects_invalid_arguments_before_dispatch(arguments):
    tool = (await web_extract_server.mcp.list_tools())[0]
    session = SimpleNamespace(call_tool=AsyncMock())
    wrapper = MCPTool(
        name=tool.name,
        description=tool.description or "",
        parameters=tool.inputSchema,
        session=session,
    )
    result = await wrapper.invoke(arguments)
    assert not result.success
    assert result.error.startswith("INVALID_TOOL_ARGUMENTS:")
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "optional_arguments",
    [
        {},
        {"model": "summary-model", "max_output_tokens": 8_000},
        {"model": None, "max_output_tokens": None},
    ],
)
async def test_mcp_execution_preserves_defaults_values_and_legacy_nulls(
    monkeypatch, optional_arguments,
):
    extractor = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(success=True, content="extracted")),
    )
    monkeypatch.setattr(web_extract_server, "_extractor", lambda: extractor)
    content, structured = await web_extract_server.mcp.call_tool(
        "web_extract", {"url": "https://example.com", **optional_arguments},
    )
    assert content[0].text == "extracted"
    assert structured == {"result": "extracted"}
    extractor.execute.assert_awaited_once_with(
        url="https://example.com",
        model=optional_arguments.get("model"),
        max_output_tokens=optional_arguments.get("max_output_tokens"),
    )


@pytest.mark.asyncio
async def test_mcp_execution_reports_extraction_failure(monkeypatch):
    extractor = SimpleNamespace(
        execute=AsyncMock(
            return_value=ToolResult(success=False, error="Web extraction timed out"),
        ),
    )
    monkeypatch.setattr(web_extract_server, "_extractor", lambda: extractor)
    with pytest.raises(ToolError, match="Web extraction timed out"):
        await web_extract_server.mcp.call_tool(
            "web_extract", {"url": "https://example.com"},
        )


@pytest.mark.asyncio
async def test_summary_requests_keep_isolated_parent_sessions(monkeypatch):
    import asyncio
    from box_agent.llm.base import LLMClientBase
    from mcp.types import RequestParams

    headers = []

    class Delegate:
        model = "summary-model"
        max_output_tokens = 1000

        def for_model(self, model, **kwargs):
            return self

        async def generate(self, messages, tools=None, **kwargs):
            await asyncio.sleep(0)
            headers.append(LLMClientBase._agent_headers(
                session_id=kwargs.get("session_id", ""),
            ))
            return SimpleNamespace(content="summary", finish_reason="stop")

    class Extractor(web_extract_server.WebExtractTool):
        async def execute(self, **kwargs):
            summary, error = await self._summarize(
                url="https://example.com", content="page", requested_model=None,
                requested_max_output_tokens=None,
            )
            return ToolResult(success=error is None, content=summary or "", error=error)

    extractor = Extractor(Delegate())
    monkeypatch.setattr(web_extract_server, "_extractor", lambda: extractor)
    def context(session_id):
        return SimpleNamespace(request_context=SimpleNamespace(
            meta=RequestParams.Meta.model_validate({"box_agent_session_id": session_id}),
        ))

    await asyncio.gather(*(
        web_extract_server.web_extract("https://example.com", ctx=context(sid))
        for sid in ("session-a", "session-b")
    ))
    await web_extract_server.web_extract("https://example.com")
    assert headers == [
        {"X-RACCOON-Session-ID": "session-a"},
        {"X-RACCOON-Session-ID": "session-b"},
        {},
    ]
    assert isinstance(extractor.llm, Delegate)


@pytest.mark.asyncio
async def test_runtime_scopes_session_id_to_tool_execution(monkeypatch):
    from box_agent import runtime
    from box_agent.tools.model_tool_context import current_model_tool_context

    async def loop(**kwargs):
        assert current_model_tool_context().session_id == "host-session"
        yield "event"

    monkeypatch.setattr(runtime, "_run_agent_loop", loop)
    events = runtime.run_agent_loop(
        llm=SimpleNamespace(model="model", max_output_tokens=1000),
        session_id="host-session",
    )
    assert await anext(events) == "event"
    assert current_model_tool_context() is None
    await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("override,expected", [({}, "parent"), ({"session_id": "other"}, "other"), ({"session_id": ""}, "")])
async def test_nested_runtime_inherits_session_unless_explicitly_overridden(monkeypatch, override, expected):
    from box_agent import runtime
    from box_agent.tools.model_tool_context import (
        current_model_tool_context, set_model_tool_context, reset_model_tool_context,
    )

    async def loop(**kwargs):
        assert current_model_tool_context().session_id == expected
        yield "event"

    monkeypatch.setattr(runtime, "_run_agent_loop", loop)
    token = set_model_tool_context(model="parent-model", max_output_tokens=1000, session_id="parent")
    try:
        events = runtime.run_agent_loop(
            llm=SimpleNamespace(model="child-model", max_output_tokens=100), **override,
        )
        assert await anext(events) == "event"
        assert current_model_tool_context().session_id == "parent"
        await events.aclose()
    finally:
        reset_model_tool_context(token)
    assert current_model_tool_context() is None
