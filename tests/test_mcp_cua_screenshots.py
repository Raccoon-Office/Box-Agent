"""CUA adaptation through generic MCP hooks and the managed agent loop."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.wiring import build_cua_bindings
from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.mcp_result_hooks import (
    bind_mcp_result_adapter,
    current_mcp_result_adapters,
)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_CUA_VISION", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def _image_b64(format="PNG") -> str:
    img = Image.new("RGB", (64, 48), (1, 2, 3))
    buf = io.BytesIO()
    img.save(buf, format=format)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _result(*, is_error=False):
    return SimpleNamespace(
        content=[
            SimpleNamespace(text="captured desktop"),
            SimpleNamespace(data=_image_b64(), mimeType="image/png"),
        ],
        isError=is_error,
    )


class _FakeSession:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, arguments=None):
        return self.result


def _make_tool(result, *, server_name="desktop") -> MCPTool:
    return MCPTool(
        name="screenshot",
        description="take a screenshot",
        parameters={"type": "object", "properties": {}},
        session=_FakeSession(result),
        server_name=server_name,
        remote_name="screenshot",
    )


def _binding(capability=True):
    return build_cua_bindings(
        llm=SimpleNamespace(capabilities={"image_input": capability}),
        config=CuaConfig(server_name="desktop"),
    )


async def test_matching_server_normalizes_only_supported_images():
    result = _result()
    result.content.append(SimpleNamespace(data=_image_b64("GIF"), mimeType="image/gif"))
    tool = _make_tool(result)
    with bind_mcp_result_adapter(_binding()):
        out = await tool.execute()
        assert tool.transient_followup_allowed

    assert out.success
    assert out.content == "captured desktop"
    assert len([b for b in out.transient_followup_content if b["type"] == "input_image"]) == 1
    assert any(b["type"] == "text" and "tool invocation" in b["text"]
               for b in out.transient_followup_content)
    assert out.transient_followup_content[0]["type"] == "input_image"
    assert out.transient_followup_content[0]["media_type"] == "image/png"
    assert out.raw_output["mcp_inline_images"] == [
        {"data": item.data, "mime_type": item.mimeType}
        for item in result.content if hasattr(item, "data")
    ]


async def test_no_plugin_preserves_the_original_mcp_result():
    tool = _make_tool(_result())
    out = await tool.execute()
    assert out.success
    assert out.content == "captured desktop"
    assert len(out.raw_output["mcp_inline_images"]) == 1
    assert out.transient_followup_content is None
    assert not tool.transient_followup_allowed


async def test_other_servers_do_not_enter_the_cua_adapter():
    tool = _make_tool(_result(), server_name="other")
    with bind_mcp_result_adapter(_binding()):
        out = await tool.execute()
        assert not tool.transient_followup_allowed
    assert out.success
    assert out.transient_followup_content is None
    assert len(out.raw_output["mcp_inline_images"]) == 1


@pytest.mark.parametrize("capability", [False, None])
async def test_text_and_unknown_models_skip_image_encoding(capability, monkeypatch):
    from box_agent.plugins.cua import wiring

    def forbidden_encoding(*args):
        pytest.fail("image encoding must not run for unsupported models")

    monkeypatch.setattr(wiring, "encode_canonical_cua_image", forbidden_encoding)
    tool = _make_tool(_result())
    with bind_mcp_result_adapter(_binding(capability)):
        out = await tool.execute()
        assert not tool.transient_followup_allowed
    assert out.success
    assert out.content == "captured desktop"
    assert out.transient_followup_content is None
    assert len(out.raw_output["mcp_inline_images"]) == 1


async def test_failed_mcp_call_does_not_inject_images():
    with bind_mcp_result_adapter(_binding()):
        out = await _make_tool(_result(is_error=True)).execute()
    assert not out.success
    assert out.transient_followup_content is None


async def test_invalid_image_does_not_fail_the_desktop_action():
    result = _result()
    result.content[1].data = "not base64"
    with bind_mcp_result_adapter(_binding()):
        out = await _make_tool(result).execute()
    assert out.success
    assert out.content == "captured desktop"
    assert out.transient_followup_content is None
    assert out.raw_output["mcp_inline_images"][0]["data"] == "not base64"


def _image_messages(messages):
    return [
        message for message in messages
        if isinstance(message.content, list)
        and any(block.get("type") == "input_image" for block in message.content)
    ]


@pytest.mark.parametrize("capability", [True, False, None])
async def test_managed_mcp_loop_persists_sidecar_and_sends_latest_image(
    tmp_path, capability,
):
    from box_agent.agent_session import AgentSession
    from box_agent.events import DoneEvent, StopReason
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.session_context import HostBindings, SessionOptions
    from box_agent.session_log import SessionLog
    from tests.test_agent_session import session_config

    class ScreenshotThenGoalLLM:
        def __init__(self):
            self.capabilities = {"image_input": capability}
            self.requests = []

        async def generate_stream(self, messages, **kwargs):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            if len(self.requests) <= 2:
                name = "screenshot" if len(self.requests) == 1 else "goal_read"
                yield StreamEvent(
                    type="finish", finish_reason="tool_use",
                    tool_calls=[ToolCall(
                        id=f"call-{len(self.requests)}", type="function",
                        function=FunctionCall(name=name, arguments={}),
                    )],
                )
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    config = session_config(tmp_path, max_steps=5)
    config.plugins["cua"] = {"server_name": "desktop"}
    llm = ScreenshotThenGoalLLM()
    session_log = SessionLog.create(tmp_path / "logs", session_id="vision", cwd=tmp_path)
    session = await AgentSession.open(
        config=config,
        host=HostBindings(
            llm_client=llm, system_prompt="system", tools=[_make_tool(_result())],
            session_log=session_log,
        ),
        options=SessionOptions(workspace_dir=tmp_path),
    )
    try:
        session.agent.add_user_message("Capture the desktop, read the goal, and finish.")
        events = [event async for event in session.run_events(
            options=session.build_run_options(logger=None),
        )]
        assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.END_TURN]
        assert len(llm.requests) == 3
        assert [len(_image_messages(request)) for request in llm.requests] == (
            [0, 1, 0] if capability is True else [0, 0, 0]
        )
        if capability is True:
            overlay = _image_messages(llm.requests[1])[0]
            assert overlay.role == "user"
            assert overlay.trace_redact_content
            assert overlay.content[0].get("data")
            assert not _image_messages(llm.requests[2])
            assert not _image_messages(session.agent.messages)
        else:
            assert not _image_messages(session.agent.messages)
        session_log.flush()
        replay_text = [str(message.content) for message in session_log.replay().messages]
        assert any("images/" in content for content in replay_text)
        assert list((session_log.path.parent / "images").glob("*.png"))
        assert current_mcp_result_adapters() == ()
        assert "run_events" not in vars(session.agent)
    finally:
        await session.aclose()
        session_log.close()


@pytest.mark.parametrize("is_error", [False, True])
async def test_structured_driver_state_reaches_context_and_persistence(is_error):
    result = _result(is_error=is_error)
    result.structuredContent = {"snapshot_id": "s12345678", "pid": 17, "window_id": 22}
    with bind_mcp_result_adapter(_binding()):
        out = await _make_tool(result).execute()
    assert out.success is (not is_error)
    assert out.raw_output["mcp_structured_content"] == result.structuredContent
    assert '"snapshot_id":"s12345678"' in out.content
    if is_error:
        assert out.transient_followup_content is None


@pytest.mark.parametrize("state", [{"screenshot_frame_valid": False}, {"screenshot_error": "capture_failed"}])
async def test_invalid_driver_screenshot_is_not_injected(state):
    result = _result()
    result.structuredContent = state
    with bind_mcp_result_adapter(_binding()):
        out = await _make_tool(result).execute()
    assert out.success
    assert out.transient_followup_content is None
    assert out.raw_output["mcp_structured_content"] == state


async def test_observation_context_distinguishes_image_pixels_and_screen_coordinates():
    result = _result()
    result.structuredContent = {
        "capture_id": "capture-live", "snapshot_id": "snapshot-live",
        "window_bounds": {"x": 138, "y": 439, "width": 230, "height": 408},
        "screenshot_scale": 2.0,
        "elements": [{
            "element_token": "snapshot-live:2", "label": "Clear",
            "frame": {"x": 202, "y": 572, "w": 48, "h": 48},
            "screenshot_frame": {"x": 128, "y": 266, "w": 96, "h": 96},
        }],
    }
    with bind_mcp_result_adapter(_binding()):
        out = await _make_tool(result).execute()
    assert out.success
    assert out.raw_output["mcp_structured_content"] == result.structuredContent
    assert "image pixels and screen points are different spaces" in out.content
    assert "move_cursor" in out.content
    assert "never both" in out.content
    assert "capture_id is consumed" in out.content
    assert "inspect fresh state" in out.content
