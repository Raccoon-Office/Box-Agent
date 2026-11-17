"""Screenshot bytes, local paths and preview guidance agree at the tool boundary."""

import base64
import io
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from PIL import Image

from box_agent.tools.base import ToolResult
from box_agent.tools.browser_result_adapter import (
    _persist_browser_screenshot_output,
    _prepare_browser_screenshot_output,
)
from box_agent.tools.browser_tool_names import public_browser_tool_description
from box_agent.tools.engine.call_contracts import ToolCallRecord
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.image_inspection_tool import ImageInspectionTool
from box_agent.tools.mcp_loader import MCPTool


def screenshot_result(*, original=True, kind="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (1720, 1680), "white").save(stream, format=kind)
    payload = stream.getvalue()
    raw = {"mcp_inline_images": [{"data": base64.b64encode(payload).decode(),
                                   "mime_type": "image/jpeg" if kind == "JPEG" else "image/png"}]}
    if original:
        raw["mcp_structured_content"] = {
            "raccoonScreenshot": {"version": 1, "inline": "original"},
        }
    return ToolResult(success=True, content="[Screenshot](.playwright-mcp/cache.png)", raw_output=raw), payload


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [None, "质检 图片/页面.png"])
@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
async def test_saved_original_is_readable_at_reported_session_path(tmp_path, filename, kind):
    arguments = {"type": "jpeg" if kind == "JPEG" else "png"}
    if filename is not None:
        arguments["filename"] = filename
    target, error = _prepare_browser_screenshot_output(
        "managed_browser_take_screenshot", arguments, str(tmp_path),
    )
    assert error is None and target.is_relative_to(tmp_path)
    assert "filename" not in arguments
    result, payload = screenshot_result(kind=kind)
    saved = _persist_browser_screenshot_output(result, target)
    assert target.read_bytes() == payload
    assert str(target) in saved.content and ".playwright-mcp/cache.png" not in saved.content
    assert "Do not resize or redesign" in saved.content
    assert saved.raw_output["browser_screenshot"] == {
        "path": str(target), "width": 1720, "height": 1680, "quality": "original",
    }
    inspection = await ImageInspectionTool(
        None, workspace_dir=str(tmp_path), allow_full_access=False, native_supported=True,
    ).execute([str(target)], "Inspect the saved screenshot")
    assert inspection.success, inspection.error
    assert Image.open(target).size == (1720, 1680)


def test_legacy_preview_is_never_claimed_to_be_an_original(tmp_path):
    result, _ = screenshot_result(original=False)
    saved = _persist_browser_screenshot_output(result, tmp_path / "preview.png")
    assert saved.raw_output["browser_screenshot"]["quality"] == "preview"
    assert "MCP did not confirm original resolution" in saved.content


def test_invalid_image_does_not_overwrite_a_previous_capture(tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"previous capture")
    result = ToolResult(success=True, raw_output={"mcp_inline_images": [{"data": "aW1hZ2U="}]})
    saved = _persist_browser_screenshot_output(result, target)
    assert "Could not persist" in saved.content
    assert target.read_bytes() == b"previous capture"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_png_crc_error_returns_a_warning_without_changing_the_destination(tmp_path, existing):
    from box_agent.tools.file_change_receipts import persist_file_change

    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
    payload = bytearray(stream.getvalue())
    chunk = payload.index(b"IDAT")
    length = int.from_bytes(payload[chunk - 4:chunk], "big")
    payload[chunk + 4 + length] ^= 1  # Corrupt the IDAT checksum, leaving image headers intact.
    target = tmp_path / "shot.png"
    if existing:
        target.write_bytes(b"previous capture")
    result = ToolResult(success=True, raw_output={"mcp_inline_images": [{
        "data": base64.b64encode(payload).decode(), "mime_type": "image/png",
    }]})

    saved = await persist_file_change(
        str(tmp_path), target, lambda path: _persist_browser_screenshot_output(result, path),
    )

    assert saved.success  # Screenshot persistence failures remain advisory.
    assert "Could not persist browser screenshot" in saved.content
    assert "Screenshot persisted to" not in saved.content
    assert saved._written_file_sha256 is None
    assert "file_changes" not in saved.raw_output
    if existing:
        assert target.read_bytes() == b"previous capture"
    else:
        assert not target.exists()


def test_relative_screenshot_escape_and_symlink_escape_are_rejected(tmp_path):
    for filename in ["../outside.png", "qa/../../outside.png"]:
        target, error = _prepare_browser_screenshot_output(
            "managed_browser_take_screenshot", {"filename": filename}, str(tmp_path),
        )
        assert target is None and "OUTPUT_PATH_INVALID" in error
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    target, error = _prepare_browser_screenshot_output(
        "managed_browser_take_screenshot", {"filename": "escape/outside.png"}, str(tmp_path),
    )
    assert target is None and "OUTPUT_PATH_INVALID" in error


def test_default_destination_survives_hook_recheck_and_is_unique(tmp_path):
    engine = DefaultToolEngine(tools={})
    engine._context = SimpleNamespace(workspace_dir=str(tmp_path))
    first = ToolCallRecord(call_id="a", requested_name="managed_browser_take_screenshot",
                           name="managed_browser_take_screenshot", arguments={}, target=None)
    second = ToolCallRecord(call_id="b", requested_name="managed_browser_take_screenshot",
                            name="managed_browser_take_screenshot", arguments={}, target=None)
    assert engine._prepare_paths(first) is None
    initial = first.screenshot_target
    assert engine._prepare_paths(first) is None and first.screenshot_target == initial
    assert engine._prepare_paths(second) is None and second.screenshot_target != initial
    first.arguments["filename"] = "replacement.png"
    assert engine._prepare_paths(first) is None
    assert first.screenshot_target == tmp_path / "replacement.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", ["browser_take_screenshot", "browser_snapshot"])
async def test_only_screenshots_negotiate_original_bytes(remote):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(arguments)
            return SimpleNamespace(content=[], isError=False)

    tool = MCPTool(name="managed_" + remote, remote_name=remote, description="browser",
                   parameters={"type": "object"}, session=Session(), server_name="playwright")
    assert (await tool.execute(_meta={"box_agent_screenshot_original": False})).success
    assert calls == ([{"_meta": {"box_agent_screenshot_original": True}}]
                     if remote == "browser_take_screenshot" else [{}])


def test_screenshot_schema_guidance_does_not_change_other_browser_descriptions():
    description = public_browser_tool_description("playwright", "Capture", "browser_take_screenshot")
    assert "absolute path" in description and "Never change page dimensions" in description
    assert "pixel limit" not in public_browser_tool_description("playwright", "Navigate", "browser_navigate")


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("BOX_AGENT_TEST_PLAYWRIGHT_MCP_SCRIPT"),
                    reason="Set BOX_AGENT_TEST_PLAYWRIGHT_MCP_SCRIPT for the real bundled MCP contract")
@pytest.mark.parametrize("filename", [None, "质检 图片/原始页面.png"])
async def test_real_mcp_original_is_saved_and_read_from_a_different_session_cwd(tmp_path, filename):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from urllib.parse import quote

    from box_agent.tools.file_change_receipts import persist_file_change

    workspace = tmp_path / "任务 工作区"
    workspace.mkdir()
    server_cwd = tmp_path / "mcp-server"
    server_cwd.mkdir()
    args = [str(Path(os.environ["BOX_AGENT_TEST_PLAYWRIGHT_MCP_SCRIPT"]).resolve()),
            "--headless", "--isolated"]
    browser = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if browser:
        args.extend(["--executable-path", browser])
    env = {name: os.environ[name] for name in ("RACCOON_ELECTRON_EXECUTABLE", "RACCOON_ELECTRON_APP")
           if name in os.environ}
    parameters = StdioServerParameters(command=shutil.which("node"), args=args, cwd=str(server_cwd), env=env)
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        html = '<body style="margin:0;width:1720px;height:2200px">Original layout</body>'
        assert not (await session.call_tool("browser_navigate", {"url": "data:text/html," + quote(html)})).isError
        assert not (await session.call_tool("browser_resize", {"width": 1720, "height": 1680})).isError
        tool = MCPTool(name="managed_browser_take_screenshot", remote_name="browser_take_screenshot",
                       description="screenshot", parameters={"type": "object"},
                       session=session, server_name="playwright")
        arguments = {"fullPage": True}
        if filename is not None:
            arguments["filename"] = filename
        target, error = _prepare_browser_screenshot_output(tool.name, arguments, str(workspace))
        assert error is None
        result = await tool.execute(**arguments)
        assert result.success, result.error
        saved = await persist_file_change(
            str(workspace), target, lambda path: _persist_browser_screenshot_output(result, path),
        )
        assert saved.raw_output["browser_screenshot"]["quality"] == "original"
        assert saved.raw_output["browser_screenshot"]["path"] == str(target)
        assert saved.raw_output["file_changes"][0]["path"] == str(target.relative_to(workspace))
        assert Image.open(target).size == (1720, 2200)
        cached = next((server_cwd / ".playwright-mcp").glob("*.png"))
        assert target.read_bytes() == cached.read_bytes()
        inspection = await ImageInspectionTool(
            None, workspace_dir=str(workspace), allow_full_access=False, native_supported=True,
        ).execute([saved.raw_output["browser_screenshot"]["path"]], "Check original layout")
        assert inspection.success, inspection.error
        after = await session.call_tool("browser_evaluate", {
            "function": "() => [document.body.offsetWidth, document.body.offsetHeight]",
        })
        assert "1720" in str(after.content) and "2200" in str(after.content)
