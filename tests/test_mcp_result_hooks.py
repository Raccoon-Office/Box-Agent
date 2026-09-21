"""Tests for the generic run-scoped MCP result adapter seam.

These tests intentionally use a non-CUA adapter.  The MCP loader should only
know how to expose inline content to the adapters active in the current
ContextVar scope; the adapter owns the decision to create transient model
input.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.mcp_result_hooks import (
    bind_mcp_result_adapter,
    current_mcp_result_adapters,
)


class _TextItem:
    def __init__(self, text: str) -> None:
        self.text = text


class _ImageItem:
    def __init__(self, data: str = "aW1hZ2U=", mime_type: str = "image/png") -> None:
        self.data = data
        self.mimeType = mime_type


class _Result:
    def __init__(self, content: list[Any], *, is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class _FakeSession:
    def __init__(self, result: _Result) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> _Result:
        del name, arguments
        return self.result


class _Adapter:
    """A non-CUA adapter with observable eligibility and conversion calls."""

    def __init__(
        self,
        label: str,
        *,
        allowed: bool = True,
        raise_on_allow: bool = False,
        raise_on_convert: bool = False,
    ) -> None:
        self.label = label
        self.allowed = allowed
        self.raise_on_allow = raise_on_allow
        self.raise_on_convert = raise_on_convert
        self.allow_calls: list[tuple[str, str]] = []
        self.convert_calls: list[tuple[str, str, list[dict[str, str]]]] = []

    def allows_transient_followup(self, *, server_name: str, remote_name: str) -> bool:
        self.allow_calls.append((server_name, remote_name))
        if self.raise_on_allow:
            raise RuntimeError(f"{self.label}: eligibility failed")
        return self.allowed

    def transient_followup_content(
        self,
        *,
        server_name: str,
        remote_name: str,
        inline_images: list[dict[str, str]],
    ) -> list[dict[str, Any]] | None:
        self.convert_calls.append(
            (server_name, remote_name, [dict(item) for item in inline_images])
        )
        if self.raise_on_convert:
            raise RuntimeError(f"{self.label}: conversion failed")
        return [
            {
                "type": "input_image",
                "media_type": "image/png",
                "data": inline_images[0]["data"],
                "adapter": self.label,
            }
        ]


def _make_tool(result: _Result | None = None) -> MCPTool:
    return MCPTool(
        name="capture",
        description="capture an image",
        parameters={"type": "object", "properties": {}},
        session=_FakeSession(
            result
            or _Result([_TextItem("captured"), _ImageItem()])
        ),
        server_name="desktop-server",
        remote_name="capture_screen",
    )


async def test_no_adapter_keeps_result_and_context_empty() -> None:
    tool = _make_tool()

    assert current_mcp_result_adapters() == ()
    result = await tool.execute()

    assert result.success is True
    assert result.content == "captured"
    assert result.transient_followup_content is None
    assert result.raw_output == {
        "mcp_inline_images": [{"data": "aW1hZ2U=", "mime_type": "image/png"}]
    }
    assert tool.transient_followup_allowed is False


async def test_allowed_non_cua_adapter_converts_inline_images_and_preserves_result() -> None:
    adapter = _Adapter("fake-vision")
    tool = _make_tool()

    with bind_mcp_result_adapter(adapter):
        assert current_mcp_result_adapters() == (adapter,)
        result = await tool.execute()

    assert result.success is True
    assert result.content == "captured"
    assert result.raw_output == {
        "mcp_inline_images": [{"data": "aW1hZ2U=", "mime_type": "image/png"}]
    }
    assert result.transient_followup_content == [
        {
            "type": "input_image",
            "media_type": "image/png",
            "data": "aW1hZ2U=",
            "adapter": "fake-vision",
        }
    ]
    assert adapter.allow_calls == [("desktop-server", "capture_screen")]
    assert adapter.convert_calls == [
        (
            "desktop-server",
            "capture_screen",
            [{"data": "aW1hZ2U=", "mime_type": "image/png"}],
        )
    ]


async def test_disallowed_adapter_is_not_called_for_conversion() -> None:
    adapter = _Adapter("text-only", allowed=False)
    tool = _make_tool()

    with bind_mcp_result_adapter(adapter):
        result = await tool.execute()
        assert tool.transient_followup_allowed is False

    assert result.success is True
    assert result.transient_followup_content is None
    assert result.raw_output is not None
    assert adapter.allow_calls
    assert all(
        call == ("desktop-server", "capture_screen")
        for call in adapter.allow_calls
    )
    assert adapter.convert_calls == []


def test_nested_bindings_append_and_restore() -> None:
    outer = _Adapter("outer")
    inner = _Adapter("inner")

    assert current_mcp_result_adapters() == ()
    with bind_mcp_result_adapter(outer):
        assert current_mcp_result_adapters() == (outer,)
        with bind_mcp_result_adapter(inner):
            assert current_mcp_result_adapters() == (outer, inner)
        assert current_mcp_result_adapters() == (outer,)
    assert current_mcp_result_adapters() == ()


async def test_multiple_allowed_adapters_contribute_blocks_in_binding_order() -> None:
    first = _Adapter("first")
    second = _Adapter("second")
    tool = _make_tool()

    with bind_mcp_result_adapter(first), bind_mcp_result_adapter(second):
        result = await tool.execute()

    assert result.transient_followup_content is not None
    assert [block["adapter"] for block in result.transient_followup_content] == [
        "first",
        "second",
    ]
    assert len(first.convert_calls) == 1
    assert len(second.convert_calls) == 1


async def test_adapter_eligibility_error_fails_open_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _Adapter("broken-eligibility", raise_on_allow=True)
    tool = _make_tool()
    caplog.set_level(logging.DEBUG, logger="box_agent.tools.mcp_result_hooks")

    with bind_mcp_result_adapter(adapter):
        result = await tool.execute()

    assert result.success is True
    assert result.content == "captured"
    assert result.transient_followup_content is None
    assert result.raw_output is not None
    assert "eligibility failed" in caplog.text


async def test_adapter_conversion_error_fails_open_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _Adapter("broken-conversion", raise_on_convert=True)
    tool = _make_tool()
    caplog.set_level(logging.DEBUG, logger="box_agent.tools.mcp_result_hooks")

    with bind_mcp_result_adapter(adapter):
        result = await tool.execute()

    assert result.success is True
    assert result.content == "captured"
    assert result.transient_followup_content is None
    assert result.raw_output is not None
    assert "conversion failed" in caplog.text


async def test_shared_tool_isolated_between_concurrent_adapter_contexts() -> None:
    tool = _make_tool()
    left = _Adapter("left")
    right = _Adapter("right")

    async def invoke(adapter: _Adapter) -> Any:
        with bind_mcp_result_adapter(adapter):
            await asyncio.sleep(0)
            result = await tool.execute()
            await asyncio.sleep(0)
            return result

    left_result, right_result = await asyncio.gather(invoke(left), invoke(right))

    assert [block["adapter"] for block in left_result.transient_followup_content or []] == ["left"]
    assert [
        block["adapter"]
        for block in right_result.transient_followup_content or []
    ] == ["right"]
    assert current_mcp_result_adapters() == ()
