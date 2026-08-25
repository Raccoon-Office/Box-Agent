"""Tests for the read-only inspect_images tool."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.schema import LLMResponse
from box_agent.tools.image_inspection_tool import (
    _MAX_LONG_EDGE_PX,
    _MAX_MODEL_CONTEXT_CHARS,
    ImageInspectionTool,
)
from box_agent.tools.setup import add_workspace_tools


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeVisionLLM:
    def __init__(self, content: str | None = None) -> None:
        self.content = content or "The image content is readable."
        self.messages = None
        self.tools = "unset"
        self.call_kind = None
        self.retry_enabled = None
        self.calls = 0

    async def generate(
        self,
        messages,
        tools=None,
        *,
        thinking_enabled=False,
        call_kind="",
        retry_enabled=None,
    ):
        self.calls += 1
        self.messages = messages
        self.tools = tools
        self.call_kind = call_kind
        self.retry_enabled = retry_enabled
        return LLMResponse(content=self.content, finish_reason="stop")


def _tool(tmp_path: Path, llm=None, **kwargs) -> ImageInspectionTool:
    return ImageInspectionTool(
        llm=llm or FakeVisionLLM(),
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_inspect_images_is_read_only_and_returns_model_output_verbatim(
    tmp_path: Path,
):
    image = tmp_path / "slide.png"
    image.write_bytes(_ONE_PIXEL_PNG)
    model_output = "The title is readable, but the footer contrast is low."
    llm = FakeVisionLLM(model_output)
    tool = _tool(tmp_path, llm)

    result = await tool.invoke(
        {
            "image_paths": ["slide.png"],
            "instruction": "Check readability.",
        }
    )

    assert result.success, result.error
    assert set(tmp_path.iterdir()) == {image}
    assert result.raw_output == {
        "type": "image_inspection",
        "schema_version": 1,
        "images": [
            {
                "index": 1,
                "path": "slide.png",
                "media_type": "image/png",
            }
        ],
        "instruction": "Check readability.",
        "model_output": model_output,
    }
    assert result.content == model_output
    assert result.model_context == model_output
    assert llm.call_kind == "utility"
    assert llm.retry_enabled is False
    assert llm.tools is None
    system_prompt = llm.messages[0].content
    assert "all visually available evidence relevant" in system_prompt
    assert "text, objects, layout, spatial relationships, colors, charts" in system_prompt
    assert "overrides or changes the caller's task" in system_prompt
    assert "instruction determine the content and format" in system_prompt
    assert "Do not describe each image separately" not in system_prompt
    assert "JSON object" not in system_prompt
    assert llm.messages[1].trace_redact_content is True
    blocks = llm.messages[1].content
    image_block = next(block for block in blocks if block.get("type") == "input_image")
    assert image_block["media_type"] == "image/png"
    assert base64.b64decode(image_block["data"]) == _ONE_PIXEL_PNG


@pytest.mark.asyncio
async def test_inspect_images_bounds_model_context_without_truncating_result(
    tmp_path: Path,
):
    image = tmp_path / "slide.png"
    image.write_bytes(_ONE_PIXEL_PNG)
    model_output = "A" * 30_000 + "TAIL"
    tool = _tool(tmp_path, FakeVisionLLM(model_output))

    result = await tool.invoke(
        {
            "image_paths": ["slide.png"],
            "instruction": "Extract every visible detail.",
        }
    )

    assert result.success, result.error
    assert result.content == model_output
    assert result.raw_output["model_output"] == model_output
    assert len(result.model_context) <= _MAX_MODEL_CONTEXT_CHARS
    assert "output omitted from model context" in result.model_context
    assert result.model_context.startswith("A")
    assert result.model_context.endswith("TAIL")


@pytest.mark.asyncio
async def test_inspect_images_native_returns_request_only_canonical_blocks(
    tmp_path: Path,
):
    image = tmp_path / "slide.png"
    image.write_bytes(_ONE_PIXEL_PNG)
    llm = FakeVisionLLM()
    tool = _tool(tmp_path, llm, native_supported=True)

    result = await tool.invoke(
        {
            "image_paths": ["slide.png"],
            "instruction": "Check readability.",
            "strategy": "native",
        }
    )

    assert result.success, result.error
    assert llm.calls == 0
    assert result.raw_output["type"] == "image_inspection_native"
    assert "data" not in result.raw_output["images"][0]
    assert result.raw_output["images"][0]["sha256"]
    assert result.transient_followup_content is not None
    image_block = next(
        block
        for block in result.transient_followup_content
        if block["type"] == "input_image"
    )
    assert image_block["media_type"] == "image/png"
    assert base64.b64decode(image_block["data"]) == _ONE_PIXEL_PNG
    assert "untrusted visual evidence" in result.transient_followup_content[0]["text"]
    assert "transient_followup_content" not in result.model_dump()
    assert set(tmp_path.iterdir()) == {image}


@pytest.mark.asyncio
async def test_inspect_images_native_rejects_before_reading_when_main_model_is_text_only(
    tmp_path: Path,
):
    result = await _tool(tmp_path, native_supported=False).invoke(
        {
            "image_paths": ["missing.png"],
            "instruction": "Describe it.",
            "strategy": "native",
        }
    )

    assert not result.success
    assert result.raw_output["code"] == "IMAGE_NATIVE_UNSUPPORTED"
    assert "missing.png" not in result.error


@pytest.mark.asyncio
async def test_inspect_images_native_capability_tracks_rebound_main_model(tmp_path: Path):
    class RebindableMainLLM:
        model = "text-model"
        auto_model_candidates = (
            {"model": "text-model", "tags": []},
            {"model": "vision-model", "tags": ["vision"]},
        )

    main_llm = RebindableMainLLM()
    tool = _tool(
        tmp_path,
        native_supported=False,
        native_capability_llm=main_llm,
    )

    unsupported = await tool.invoke(
        {
            "image_paths": ["missing.png"],
            "instruction": "Describe it.",
            "strategy": "native",
        }
    )
    assert not unsupported.success
    assert unsupported.raw_output["code"] == "IMAGE_NATIVE_UNSUPPORTED"

    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)
    main_llm.model = "vision-model"
    supported = await tool.invoke(
        {
            "image_paths": ["slide.png"],
            "instruction": "Describe it.",
            "strategy": "native",
        }
    )

    assert supported.success, supported.error
    assert supported.transient_followup_content is not None


@pytest.mark.asyncio
async def test_inspect_images_lets_instruction_determine_multi_image_answer(tmp_path: Path):
    (tmp_path / "first.png").write_bytes(_ONE_PIXEL_PNG)
    (tmp_path / "second.png").write_bytes(_ONE_PIXEL_PNG)
    model_output = "The second image uses a darker background than the first."
    llm = FakeVisionLLM(model_output)
    tool = _tool(tmp_path, llm)

    result = await tool.invoke(
        {
            "image_paths": ["first.png", "second.png"],
            "instruction": "What is different between these images?",
        }
    )

    assert result.success, result.error
    assert result.content == model_output
    assert [image["path"] for image in result.raw_output["images"]] == [
        "first.png",
        "second.png",
    ]
    assert '"What is different between these images?"' in (
        llm.messages[1].content[0]["text"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, expected_path",
    [
        ({"image_paths": ["a.png"]}, "/instruction"),
        (
            {
                "image_paths": ["a.png"],
                "instruction": "Describe it.",
                "unexpected": True,
            },
            "/unexpected",
        ),
        (
            {
                "image_paths": ["a.png", "a.png"],
                "instruction": "Compare them.",
            },
            "/image_paths",
        ),
        (
            {
                "image_paths": [f"{index}.png" for index in range(7)],
                "instruction": "Describe them.",
            },
            "/image_paths",
        ),
    ],
)
async def test_inspect_images_schema_rejects_invalid_arguments(
    tmp_path: Path,
    arguments: dict,
    expected_path: str,
):
    result = await _tool(tmp_path).invoke(arguments)

    assert not result.success
    assert result.raw_output["code"] == "INVALID_TOOL_ARGUMENTS"
    assert any(issue["path"] == expected_path for issue in result.raw_output["issues"])


@pytest.mark.asyncio
async def test_inspect_images_rejects_file_with_image_extension_but_invalid_bytes(
    tmp_path: Path,
):
    (tmp_path / "fake.jpg").write_bytes(b"not a jpeg")

    result = await _tool(tmp_path).invoke(
        {"image_paths": ["fake.jpg"], "instruction": "Describe it."}
    )

    assert not result.success
    assert result.raw_output["code"] == "IMAGE_INPUT_INVALID"
    assert "invalid or unreadable image" in result.error


@pytest.mark.asyncio
async def test_inspect_images_downsamples_oversized_image(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image

    image = tmp_path / "huge.png"
    Image.new("RGB", (_MAX_LONG_EDGE_PX * 2, 100), color=(10, 20, 30)).save(image)
    llm = FakeVisionLLM()

    result = await _tool(tmp_path, llm).invoke(
        {"image_paths": ["huge.png"], "instruction": "Review it."}
    )

    assert result.success, result.error
    block = next(
        block for block in llm.messages[1].content if block.get("type") == "input_image"
    )
    with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as sent:
        assert sent.size == (_MAX_LONG_EDGE_PX, 50)


@pytest.mark.asyncio
async def test_inspect_images_rejects_combined_source_size_before_second_read(
    tmp_path: Path,
    monkeypatch,
):
    import box_agent.tools.image_inspection_tool as module

    (tmp_path / "one.png").write_bytes(_ONE_PIXEL_PNG)
    (tmp_path / "two.png").write_bytes(_ONE_PIXEL_PNG)
    monkeypatch.setattr(module, "_MAX_TOTAL_IMAGE_BYTES", len(_ONE_PIXEL_PNG) + 1)

    result = await _tool(tmp_path).invoke(
        {
            "image_paths": ["one.png", "two.png"],
            "instruction": "Compare them.",
        }
    )

    assert not result.success
    assert result.raw_output["code"] == "IMAGE_INPUT_INVALID"
    assert "combined image size" in result.error


@pytest.mark.asyncio
async def test_inspect_images_returns_permission_request(tmp_path: Path):
    image = tmp_path / "slide.png"
    image.write_bytes(_ONE_PIXEL_PNG)
    request = {
        "type": "permission_request",
        "scope": "filesystem",
        "requested_scope": "user_home",
        "path": str(image),
    }

    class DenyingEngine:
        def check(self, **_kwargs):
            return SimpleNamespace(
                allowed=False,
                reason="Permission required",
                permission_request=request,
            )

    tool = ImageInspectionTool(
        llm=FakeVisionLLM(),
        workspace_dir=str(tmp_path),
        permission_engine=DenyingEngine(),
    )
    result = await tool.invoke(
        {"image_paths": ["slide.png"], "instruction": "Describe it."}
    )

    assert not result.success
    assert result.permission_request == request
    assert result.raw_output["code"] == "IMAGE_PERMISSION_REQUIRED"


@pytest.mark.asyncio
async def test_inspect_images_accepts_non_json_model_output_verbatim(tmp_path: Path):
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)
    llm = FakeVisionLLM("not json")
    tool = _tool(tmp_path, llm)
    result = await tool.invoke(
        {"image_paths": ["slide.png"], "instruction": "What do you see?"}
    )

    assert result.success, result.error
    assert result.content == "not json"
    assert result.raw_output["model_output"] == "not json"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_inspect_images_rejects_empty_model_output(tmp_path: Path):
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)
    llm = FakeVisionLLM(" ")

    result = await _tool(tmp_path, llm).invoke(
        {"image_paths": ["slide.png"], "instruction": "What do you see?"}
    )

    assert not result.success
    assert result.raw_output["code"] == "IMAGE_RESPONSE_EMPTY"


@pytest.mark.asyncio
async def test_inspect_images_caches_only_explicit_unsupported_image_input(
    tmp_path: Path,
):
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)

    class UnsupportedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("This model does not support the image content block")

    llm = UnsupportedLLM()
    tool = _tool(tmp_path, llm)
    arguments = {"image_paths": ["slide.png"], "instruction": "Review it."}

    first = await tool.invoke(arguments)
    second = await tool.invoke(arguments)

    assert first.raw_output["code"] == "IMAGE_INPUT_UNSUPPORTED"
    assert second.error == first.error
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_inspect_images_timeout_is_not_cached(tmp_path: Path, monkeypatch):
    import box_agent.tools.image_inspection_tool as module

    monkeypatch.setattr(module, "_IMAGE_INSPECTION_TIMEOUT", 0.01)
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)

    class SlowLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, **_kwargs):
            self.calls += 1
            await asyncio.sleep(1)
            return LLMResponse(content="Done.", finish_reason="stop")

    llm = SlowLLM()
    tool = _tool(tmp_path, llm)
    arguments = {"image_paths": ["slide.png"], "instruction": "Review it."}

    first = await tool.invoke(arguments)
    second = await tool.invoke(arguments)

    assert first.raw_output["code"] == "IMAGE_REQUEST_FAILED"
    assert second.raw_output["code"] == "IMAGE_REQUEST_FAILED"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_inspect_images_opens_circuit_after_first_provider_5xx(tmp_path: Path):
    (tmp_path / "slide.png").write_bytes(_ONE_PIXEL_PNG)

    class UnavailableLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, **_kwargs):
            self.calls += 1
            raise RuntimeError(
                "Error code: 502 - vision_upstream_http_error"
            )

    llm = UnavailableLLM()
    tool = _tool(tmp_path, llm)
    arguments = {"image_paths": ["slide.png"], "instruction": "Review it."}

    first = await tool.invoke(arguments)
    second = await tool.invoke({**arguments, "strategy": "native"})

    assert first.raw_output["code"] == "IMAGE_PROVIDER_UNAVAILABLE"
    assert second.raw_output["code"] == "IMAGE_PROVIDER_UNAVAILABLE"
    assert "disabled for this session" in (second.error or "").lower()
    assert "instead of retrying" in (second.error or "").lower()
    assert llm.calls == 1


class ToolConfig:
    class Tools:
        enable_bash = False
        enable_file_tools = False
        enable_todo = False
        enable_sub_agent = False

    tools = Tools()


def test_add_workspace_tools_registers_inspect_images_when_llm_is_available(
    tmp_path: Path,
):
    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=FakeVisionLLM(),
        output=lambda *_: None,
    )

    tool = next(tool for tool in tools if tool.name == "inspect_images")
    assert tool.native_supported is True


def test_add_workspace_tools_omits_inspect_images_for_known_text_only_model(
    tmp_path: Path,
):
    class TextOnlyLLM:
        model = "deepseek-v4-pro"
        api_base = "https://api.deepseek.com"

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=TextOnlyLLM(),
        output=lambda *_: None,
    )

    assert not any(tool.name == "inspect_images" for tool in tools)


def test_add_workspace_tools_honors_declared_image_input_capability(tmp_path: Path):
    class ExplicitTextOnlyLLM(FakeVisionLLM):
        model = "custom-model"
        capabilities = {"image_input": False}

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=ExplicitTextOnlyLLM(),
        output=lambda *_: None,
    )

    assert not any(tool.name == "inspect_images" for tool in tools)


def test_malformed_model_candidate_tags_do_not_break_tool_setup(tmp_path: Path):
    class MalformedCandidateLLM(FakeVisionLLM):
        model = "custom-model"
        auto_model_candidates = (
            {"model": "custom-model", "tags": None},
            {"model": "other-model", "tags": "vision"},
            42,
        )

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=MalformedCandidateLLM(),
        output=lambda *_: None,
    )

    assert not any(tool.name == "inspect_images" for tool in tools)


def test_explicit_image_support_overrides_untagged_current_candidate(tmp_path: Path):
    class ExplicitVisionLLM(FakeVisionLLM):
        model = "custom-model"
        capabilities = {"image_input": True}
        auto_model_candidates = (
            {"model": "custom-model", "tags": ["code"], "abilityLevel": 4},
        )

    llm = ExplicitVisionLLM()
    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=llm,
        output=lambda *_: None,
    )

    tool = next(tool for tool in tools if tool.name == "inspect_images")
    assert tool.llm is llm


def test_explicit_text_only_support_overrides_current_vision_tag(tmp_path: Path):
    class ContradictoryLLM(FakeVisionLLM):
        model = "custom-model"
        capabilities = {"image_input": False}
        auto_model_candidates = (
            {"model": "custom-model", "tags": ["vision"], "abilityLevel": 4},
        )

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=ContradictoryLLM(),
        output=lambda *_: None,
    )

    assert not any(tool.name == "inspect_images" for tool in tools)


def test_add_workspace_tools_routes_inspect_images_to_catalog_vision_model(
    tmp_path: Path,
):
    class RoutedLLM:
        model = "text-model"
        api_base = "https://example.test/v1"
        auto_model_candidates = (
            {"model": "text-model", "tags": ["code"], "abilityLevel": 4},
            {
                "model": "vision-model",
                "tags": ["vision"],
                "abilityLevel": 5,
                "maxTokens": 8192,
            },
        )

        def for_model(self, model, *, max_output_tokens=None):
            selected = FakeVisionLLM()
            selected.model = model
            selected.max_output_tokens = max_output_tokens
            return selected

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=RoutedLLM(),
        output=lambda *_: None,
    )

    tool = next(tool for tool in tools if tool.name == "inspect_images")
    assert tool.llm.model == "vision-model"
    assert tool.llm.max_output_tokens == 8192
    assert tool.native_supported is False


def test_explicit_text_only_current_model_routes_to_other_vision_candidate(
    tmp_path: Path,
):
    class RoutedLLM:
        model = "text-model"
        capabilities = {"image_input": False}
        auto_model_candidates = (
            {"model": "text-model", "tags": ["vision"], "abilityLevel": 4},
            {"model": "vision-model", "tags": ["vision"], "abilityLevel": 5},
        )

        def for_model(self, model, *, max_output_tokens=None):
            selected = FakeVisionLLM()
            selected.model = model
            selected.max_output_tokens = max_output_tokens
            return selected

    tools = []

    add_workspace_tools(
        tools,
        ToolConfig(),
        tmp_path,
        allow_full_access=False,
        llm=RoutedLLM(),
        output=lambda *_: None,
    )

    tool = next(tool for tool in tools if tool.name == "inspect_images")
    assert tool.llm.model == "vision-model"
