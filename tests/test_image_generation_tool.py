import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from box_agent.config import ImageGenerationConfig, ToolsConfig
from box_agent.llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.setup import (
    add_workspace_tools,
    build_image_generation_prompt,
    image_generation_service_configured,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage-bytes"
JPEG_BYTES = b"\xff\xd8\xff\xe0jpeg-bytes"


def test_generate_image_is_parallel_safe() -> None:
    assert GenerateImageTool.parallel_safe is True


def patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_: original(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_generate_image_requires_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", raising=False)
    monkeypatch.delenv("BOX_AGENT_IMAGE_GEN_ENDPOINT", raising=False)

    tool = GenerateImageTool(workspace_dir=str(tmp_path), allow_full_access=False)
    result = await tool.execute(prompt="test", output_path="assets/generated/test.png")

    assert not result.success
    assert "BOX_AGENT_IMAGE_GENERATION_ENDPOINT" in (result.error or "")


@pytest.mark.asyncio
async def test_generate_image_saves_base64_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer secret"
        assert payload == {
            "model": "gpt-image-1",
            "prompt": "editorial hero\n\nStyle: magazine illustration\n\nAvoid: text",
            "size": "1536x1024",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
                    }
                ]
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(
        prompt="editorial hero",
        output_path="assets/generated/hero.png",
        width=1600,
        height=900,
        style="magazine illustration",
        negative_prompt="text",
        metadata={"slide": "03"},
    )

    assert result.success
    assert (tmp_path / "assets/generated/hero.png").read_bytes() == PNG_BYTES
    assert result.raw_output
    assert result.raw_output["type"] == "artifact"
    assert result.raw_output["kind"] == "image"
    assert result.raw_output["filename"] == "hero.png"
    assert result.raw_output["rel_path"] == "assets/generated/hero.png"
    assert result.raw_output["abs_path"] == str(tmp_path / "assets/generated/hero.png")
    assert result.raw_output["uri"] == (tmp_path / "assets/generated/hero.png").as_uri()
    assert result.raw_output["mime"] == "image/png"
    assert result.raw_output["size_bytes"] == len(PNG_BYTES)
    assert result.raw_output["path"] == "assets/generated/hero.png"
    assert result.raw_output["mime_type"] == "image/png"
    assert result.raw_output["width"] == 1536
    assert result.raw_output["height"] == 1024
    assert result.raw_output["size"] == "1536x1024"
    assert result.raw_output["requested_height"] == 900
    assert "assets/generated/hero.png" in result.content


@pytest.mark.asyncio
async def test_generate_image_logs_full_json_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict] = []
    long_prompt = "deep brand scene " + ("with detailed lighting " * 80)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    token = set_llm_debug_sink(records.append)
    try:
        result = await tool.execute(
            prompt=long_prompt,
            output_path="assets/generated/full-log.png",
            style="cinematic",
            negative_prompt="text",
        )
    finally:
        reset_llm_debug_sink(token)

    assert result.success
    request_record = next(record for record in records if record["event"] == "image_generation/request")
    response_record = next(record for record in records if record["event"] == "image_generation/response_meta")
    assert request_record["mode"] == "text_to_image"
    assert request_record["method"] == "POST"
    assert request_record["endpoint"] == "https://image.example.test/v1/images/generations"
    assert request_record["configured_model"] == "gpt-image-1"
    assert request_record["payload"]["json"]["model"] == "gpt-image-1"
    assert request_record["payload"]["json"]["prompt"] == (
        f"{long_prompt.strip()}\n\nStyle: cinematic\n\nAvoid: text"
    )
    assert request_record["payload"]["json"]["size"] == "1024x1024"
    assert "<redacted>" in json.dumps(request_record, ensure_ascii=False)
    assert "secret" not in json.dumps(request_record, ensure_ascii=False)
    assert response_record["status_code"] == 200


@pytest.mark.asyncio
async def test_generate_image_saves_relative_paths_under_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    artifact_root = tmp_path / "session-a" / "output"
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        output_dir=str(artifact_root),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(prompt="hero", output_path="assets/generated/hero.png")

    target = artifact_root / "assets/generated/hero.png"
    assert result.success
    assert target.read_bytes() == PNG_BYTES
    assert not (tmp_path / "assets/generated/hero.png").exists()
    assert result.raw_output["rel_path"] == "session-a/output/assets/generated/hero.png"
    assert result.raw_output["artifact_rel_path"] == "assets/generated/hero.png"
    assert result.raw_output["path"] == "assets/generated/hero.png"
    assert result.raw_output["abs_path"] == str(target)
    assert "[assets/generated/hero.png]" in result.content


@pytest.mark.asyncio
async def test_generate_image_does_not_duplicate_workspace_relative_output_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    workspace = tmp_path / "session-a"
    artifact_root = workspace / "output"
    tool = GenerateImageTool(
        workspace_dir=str(workspace),
        output_dir=str(artifact_root),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(
        prompt="hero",
        output_path="output/assets/generated/legacy-path.png",
    )

    assert result.success
    assert (artifact_root / "assets/generated/legacy-path.png").read_bytes() == PNG_BYTES
    assert not (artifact_root / "output/assets/generated/legacy-path.png").exists()


@pytest.mark.asyncio
async def test_generate_image_accepts_explicit_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "2048x2048"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
                    }
                ]
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(
        prompt="seasonal sequence",
        output_path="assets/generated/seasonal.png",
        size="2048x2048",
    )

    assert result.success
    assert (tmp_path / "assets/generated/seasonal.png").read_bytes() == PNG_BYTES
    assert result.raw_output
    assert result.raw_output["path"] == "assets/generated/seasonal.png"
    assert result.raw_output["size"] == "2048x2048"
    assert result.raw_output["width"] == 2048
    assert result.raw_output["height"] == 2048


def test_image_generation_config_default_max_dimension() -> None:
    assert ImageGenerationConfig().max_dimension == 1024


@pytest.mark.asyncio
async def test_generate_image_clamps_oversized_size_for_generic_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1024x1024"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://custom.example.test/v1/generate",
        max_dimension=1024,
    )

    result = await tool.execute(
        prompt="oversized hero",
        output_path="assets/generated/hero.png",
        size="1400x1400",
    )

    assert result.success
    assert result.raw_output["size"] == "1024x1024"
    assert result.raw_output["width"] == 1024
    assert result.raw_output["height"] == 1024


@pytest.mark.asyncio
async def test_generate_image_generic_clamp_preserves_aspect_ratio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1024x512"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://custom.example.test/v1/generate",
        max_dimension=1024,
    )

    result = await tool.execute(
        prompt="wide banner",
        output_path="assets/generated/banner.png",
        size="2000x1000",
    )

    assert result.success
    assert result.raw_output["width"] == 1024
    assert result.raw_output["height"] == 512


@pytest.mark.asyncio
async def test_generate_image_generic_clamp_disabled_forwards_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1400x1400"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://custom.example.test/v1/generate",
        max_dimension=0,  # clamp disabled
    )

    result = await tool.execute(
        prompt="oversized on purpose",
        output_path="assets/generated/big.png",
        size="1400x1400",
    )

    assert result.success
    assert result.raw_output["size"] == "1400x1400"


@pytest.mark.asyncio
async def test_generate_image_max_dimension_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOX_AGENT_IMAGE_MAX_DIM", "768")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "768x768"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://custom.example.test/v1/generate",
        max_dimension=1024,  # env override wins over this
    )

    result = await tool.execute(
        prompt="env clamp",
        output_path="assets/generated/env.png",
        size="1400x1400",
    )

    assert result.success
    assert result.raw_output["width"] == 768


@pytest.mark.asyncio
async def test_generate_image_seedream_endpoint_not_clamped_by_max_dimension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Doubao/Seedream service reached through a generic (non /images/gen)
    # endpoint must keep its own size mapping — the generic max-dimension clamp
    # must not shrink the explicit size before _seedream_size_for_ratio runs.
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "4096x4096"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://custom.example.test/v1/generate",
        model="Doubao-Seedream-5.0-lite",
        max_dimension=1024,
    )

    result = await tool.execute(
        prompt="wide courtyard",
        output_path="assets/generated/ds.png",
        size="4096x4096",
    )

    assert result.success
    assert result.raw_output["width"] == 4096
    assert result.raw_output["height"] == 4096


@pytest.mark.asyncio
async def test_generate_image_openai_endpoint_size_not_clamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # OpenAI-style endpoints keep their own normalization; the generic
        # max-dimension clamp must not shrink them.
        assert payload["size"] == "2048x2048"
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        max_dimension=1024,
    )

    result = await tool.execute(
        prompt="high-res",
        output_path="assets/generated/openai.png",
        size="2048x2048",
    )

    assert result.success
    assert result.raw_output["width"] == 2048
    assert result.raw_output["height"] == 2048


@pytest.mark.asyncio
async def test_generate_image_upscales_too_small_explicit_size_for_openai_style_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1366x768"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
                    }
                ]
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(
        prompt="wide edit frame",
        output_path="assets/generated/wide-safe.png",
        size="1024x576",
    )

    assert result.success
    assert result.raw_output
    assert result.raw_output["size"] == "1366x768"
    assert result.raw_output["width"] == 1366
    assert result.raw_output["height"] == 768


@pytest.mark.asyncio
async def test_generate_image_preserves_host_16_9_explicit_size_with_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1536x864"
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="wide infographic",
        output_path="assets/generated/wide-host.png",
        size="1280x720",
    )

    assert result.success, result.error
    assert result.raw_output
    assert result.raw_output["size"] == "1536x864"
    assert result.raw_output["width"] % 16 == 0
    assert result.raw_output["height"] % 16 == 0


@pytest.mark.asyncio
async def test_generate_image_preserves_host_16_9_legacy_dimensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1536x864"
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="wide infographic",
        output_path="assets/generated/wide-legacy.png",
        width=1024,
        height=576,
    )

    assert result.success, result.error
    assert result.raw_output
    assert result.raw_output["size"] == "1536x864"


@pytest.mark.asyncio
async def test_generate_image_saves_direct_image_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(prompt="direct image", output_path="assets/generated/direct")

    assert result.success
    assert (tmp_path / "assets/generated/direct.png").read_bytes() == PNG_BYTES
    assert result.raw_output
    assert result.raw_output["path"] == "assets/generated/direct.png"


@pytest.mark.asyncio
async def test_generate_image_downloads_url_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://image.example.test/v1/images/generations":
            return httpx.Response(200, json={"url": "https://cdn.example.test/image.webp"})
        return httpx.Response(200, content=b"webp", headers={"content-type": "image/webp"})

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(prompt="url image", output_path="assets/generated/from-url")

    assert result.success
    assert (tmp_path / "assets/generated/from-url.webp").read_bytes() == b"webp"
    assert result.raw_output
    assert result.raw_output["mime_type"] == "image/webp"


@pytest.mark.asyncio
async def test_generate_image_accepts_minimax_image_base64_list_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "image_base64": [
                        base64.b64encode(JPEG_BYTES).decode("ascii"),
                    ],
                },
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://api.minimaxi.com/v1/image_generation",
    )

    result = await tool.execute(prompt="minimax image", output_path="assets/generated/minimax")

    assert result.success
    assert (tmp_path / "assets/generated/minimax.jpg").read_bytes() == JPEG_BYTES
    assert result.raw_output
    assert result.raw_output["path"] == "assets/generated/minimax.jpg"
    assert result.raw_output["mime_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_generate_image_accepts_nested_image_url_list_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://image.example.test/v1/images/generations":
            return httpx.Response(
                200,
                json={"result": {"image_urls": ["https://cdn.example.test/nested.webp"]}},
            )
        return httpx.Response(200, content=b"webp", headers={"content-type": "image/webp"})

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(prompt="nested url", output_path="assets/generated/nested")

    assert result.success
    assert (tmp_path / "assets/generated/nested.webp").read_bytes() == b"webp"


@pytest.mark.asyncio
async def test_generate_image_uses_auth_file_for_hosted_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"access_token": "login-token"}', encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer login-token"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-1"
        assert payload["size"] == "1536x1024"
        assert "wide image" in payload["prompt"]
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.xiaohuanxiong.com/v1/images/generations",
        auth_file=str(auth_file),
    )

    result = await tool.execute(prompt="wide image", output_path="assets/generated/wide.png", width=4096, height=900)

    assert result.success
    assert result.raw_output
    assert result.raw_output["width"] == 1536
    assert result.raw_output["height"] == 1024
    assert result.raw_output["size"] == "1536x1024"


@pytest.mark.asyncio
async def test_generate_image_uses_default_size_for_seedream_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "2048x2048"
        return httpx.Response(
            200,
            json={
                "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://code-dev.xiaohuanxiong.com/api/web/llm/v2/images/gen",
        model="Doubao-Seedream-5.0-lite",
    )

    result = await tool.execute(
        prompt="seasonal courtyard",
        output_path="assets/generated/ds.png",
        width=1024,
        height=1024,
    )

    assert result.success
    assert (tmp_path / "assets/generated/ds.png").read_bytes() == PNG_BYTES
    assert result.raw_output
    assert result.raw_output["size"] == "2048x2048"


@pytest.mark.asyncio
async def test_generate_image_passes_explicit_size_through_for_remote_passthrough_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "1024x1024"
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="http://10.158.136.99:9000/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="海边的落日",
        output_path="assets/generated/sunset.png",
        size="1024x1024",
    )

    assert result.success, result.error
    assert result.raw_output
    assert result.raw_output["size"] == "1024x1024"
    assert result.raw_output["image_mode"] == "text_to_image"


@pytest.mark.asyncio
async def test_generate_image_maps_seedream_explicit_size_to_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "2848x1600"
        return httpx.Response(
            200,
            json={
                "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://code-dev.xiaohuanxiong.com/api/web/llm/v2/images/gen",
        model="Doubao-Seedream-5.0-lite",
    )

    result = await tool.execute(
        prompt="unsupported ratio",
        output_path="assets/generated/ds-unsupported.png",
        size="2048x1024",
    )

    assert result.success
    assert result.raw_output
    assert result.raw_output["size"] == "2848x1600"


@pytest.mark.asyncio
async def test_generate_image_respects_seedream_exact_ratio_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["size"] == "4096x2304"
        return httpx.Response(
            200,
            json={
                "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
            },
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://code-dev.xiaohuanxiong.com/api/web/llm/v2/images/gen",
        model="Doubao-Seedream-5.0-lite",
    )

    result = await tool.execute(
        prompt="wide season",
        output_path="assets/generated/ds-wide.png",
        width=4096,
        height=2304,
    )

    assert result.success
    assert (tmp_path / "assets/generated/ds-wide.png").read_bytes() == PNG_BYTES
    assert result.raw_output
    assert result.raw_output["size"] == "4096x2304"


@pytest.mark.asyncio
async def test_generate_image_edits_reference_image_with_multipart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://image.example.test/api/web/llm/v2/images/edits"
        assert request.headers["authorization"] == "Bearer secret"
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = request.read()
        assert b'name="image"; filename="reference.png"' in body
        assert PNG_BYTES in body
        assert b'name="prompt"' in body
        assert "图片里添加水印".encode("utf-8") in body
        assert b'name="size"' in body
        assert b"1024x1024" in body
        assert b"gpt-image-1" not in body
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(JPEG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
        api_key="secret",
    )

    result = await tool.execute(
        prompt="图片里添加水印: 网宿科技",
        output_path="assets/generated/edited.jpg",
        size="1024x1024",
        image_mode="image_to_image",
        reference_images=["reference.png"],
    )

    assert result.success, result.error
    assert (tmp_path / "assets/generated/edited.jpg").read_bytes() == JPEG_BYTES
    assert result.raw_output
    assert result.raw_output["image_mode"] == "image_to_image"
    assert result.raw_output["reference_images"] == ["reference.png"]


@pytest.mark.asyncio
async def test_generate_image_edit_logs_full_multipart_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict] = []
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG_BYTES)
    prompt = "把参考图改成深色版本，保留构图和主体。" + (" 背景降低亮度但保留细节。" * 50)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(JPEG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
        api_key="secret",
    )

    token = set_llm_debug_sink(records.append)
    try:
        result = await tool.execute(
            prompt=prompt,
            output_path="assets/generated/edited.jpg",
            size="1366x768",
            image_mode="image_to_image",
            reference_images=["reference.png"],
        )
    finally:
        reset_llm_debug_sink(token)

    assert result.success
    request_record = next(record for record in records if record["event"] == "image_generation/request")
    assert request_record["mode"] == "image_to_image"
    assert request_record["endpoint"] == "https://image.example.test/api/web/llm/v2/images/edits"
    assert request_record["configured_model"] == "gpt-image-1"
    assert "multipart/form-data" in request_record["headers"]["content-type"]
    assert request_record["payload"]["multipart_fields"] == {
        "prompt": prompt,
        "size": "1376x768",
    }
    assert "model" not in request_record["payload"]["multipart_fields"]
    assert request_record["payload"]["files"] == [
        {
            "field": "image",
            "filename": "reference.png",
            "path": "reference.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG_BYTES),
            "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "content": "<binary omitted>",
        }
    ]
    serialized = json.dumps(request_record, ensure_ascii=False)
    assert "secret" not in serialized
    assert str(PNG_BYTES) not in serialized


@pytest.mark.asyncio
async def test_generate_image_error_log_keeps_full_response_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict] = []
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG_BYTES)
    error_body = {
        "error": {
            "message": "/images/edits: Invalid model name passed in model=openai/gpt-image-1-full-tail"
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=error_body)

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
        api_key="secret",
    )

    token = set_llm_debug_sink(records.append)
    try:
        result = await tool.execute(
            prompt="深色版本",
            output_path="assets/generated/edited.jpg",
            image_mode="image_to_image",
            reference_images=["reference.png"],
        )
    finally:
        reset_llm_debug_sink(token)

    assert not result.success
    error_record = next(record for record in records if record["event"] == "image_generation/error_meta")
    assert error_record["status_code"] == 400
    assert "openai/gpt-image-1-full-tail" in error_record["response_body"]
    assert "secret" not in json.dumps(error_record, ensure_ascii=False)


@pytest.mark.asyncio
async def test_generate_image_edit_upscales_too_small_explicit_size_for_openai_style_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG_BYTES)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b'name="size"' in body
        assert b"1536x864" in body
        return httpx.Response(
            200,
            json={"b64_json": base64.b64encode(JPEG_BYTES).decode("ascii")},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="给图右下角加一个角标",
        output_path="assets/generated/edit-safe.jpg",
        size="1024x576",
        image_mode="image_to_image",
        reference_images=["reference.png"],
    )

    assert result.success, result.error
    assert result.raw_output
    assert result.raw_output["size"] == "1536x864"
    assert result.raw_output["width"] == 1536
    assert result.raw_output["height"] == 864


@pytest.mark.asyncio
async def test_generate_image_edit_requires_reference_image(tmp_path: Path) -> None:
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="edit image",
        output_path="assets/generated/edit.png",
        image_mode="image_to_image",
    )

    assert not result.success
    assert "requires at least one reference image" in (result.error or "")


@pytest.mark.asyncio
async def test_generate_image_edit_rejects_reference_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-reference.png"
    outside.write_bytes(PNG_BYTES)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
    )

    result = await tool.execute(
        prompt="edit image",
        output_path="assets/generated/edit.png",
        reference_images=[str(outside)],
    )

    assert not result.success
    assert "outside the workspace" in (result.error or "")


@pytest.mark.asyncio
async def test_generate_image_rejects_output_outside_workspace(tmp_path: Path) -> None:
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
    )

    result = await tool.execute(prompt="bad path", output_path="../outside.png")

    assert not result.success
    assert "outside the workspace" in (result.error or "")


def test_add_workspace_tools_registers_generate_image(tmp_path: Path) -> None:
    tools = []

    class Config:
        tools = ToolsConfig(enable_bash=False, enable_file_tools=False, enable_todo=False, enable_sub_agent=False)
        image_generation = ImageGenerationConfig(
            endpoint="https://image.example.test/v1/images/generations"
        )

    add_workspace_tools(tools, Config(), tmp_path, allow_full_access=False, output=lambda *_: None)

    assert any(tool.name == "generate_image" for tool in tools)


def test_add_workspace_tools_skips_generate_image_without_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", raising=False)
    monkeypatch.delenv("BOX_AGENT_IMAGE_GEN_ENDPOINT", raising=False)
    tools = []

    class Config:
        tools = ToolsConfig(enable_bash=False, enable_file_tools=False, enable_todo=False, enable_sub_agent=False)
        image_generation = ImageGenerationConfig()  # endpoint unset

    add_workspace_tools(tools, Config(), tmp_path, allow_full_access=False, output=lambda *_: None)

    assert not any(tool.name == "generate_image" for tool in tools)


def test_image_generation_service_is_box_agent_configured_for_cli_and_acp() -> None:
    class Config:
        image_generation = ImageGenerationConfig(
            endpoint="https://image.example.test/api/web/llm/v2/images/gen"
        )

    assert image_generation_service_configured(Config(), {}) is True
    prompt = build_image_generation_prompt(Config(), {})
    assert "Box-Agent 的标准工具" in prompt
    assert "CLI 与 ACP 共用" in prompt
    assert "不由宿主 `env_context` 控制" in prompt
    assert "优先调用 `generate_image`" in prompt


def test_image_generation_service_accepts_cli_environment_override() -> None:
    class Config:
        image_generation = ImageGenerationConfig()

    env = {
        "BOX_AGENT_IMAGE_GENERATION_ENDPOINT": (
            "https://image.example.test/api/web/llm/v2/images/gen"
        )
    }
    assert image_generation_service_configured(Config(), env) is True
    assert "已配置" in build_image_generation_prompt(Config(), env)


def test_image_generation_prompt_reports_unconfigured_service() -> None:
    class Config:
        image_generation = ImageGenerationConfig()

    assert image_generation_service_configured(Config(), {}) is False
    prompt = build_image_generation_prompt(Config(), {})
    assert "未配置" in prompt
    assert "不得假装已生成图片" in prompt


def test_add_workspace_tools_passes_image_generation_config(tmp_path: Path) -> None:
    tools = []

    class LLM:
        auth_file = str(tmp_path / "auth.json")

    class Config:
        llm = LLM()
        tools = ToolsConfig(enable_bash=False, enable_file_tools=False, enable_todo=False, enable_sub_agent=False)
        image_generation = ImageGenerationConfig(
            endpoint="https://image.example.test/v1/images/generations",
            api_key="image-token",
            model="chatgpt-image-latest",
            timeout=45.0,
        )

    add_workspace_tools(tools, Config(), tmp_path, allow_full_access=False, output=lambda *_: None)

    tool = next(tool for tool in tools if tool.name == "generate_image")
    assert isinstance(tool, GenerateImageTool)
    assert tool.endpoint == "https://image.example.test/v1/images/generations"
    assert tool.api_key == "image-token"
    assert tool.model == "chatgpt-image-latest"
    assert tool.auth_file == str(tmp_path / "auth.json")
    assert tool.timeout == 45.0


@pytest.mark.asyncio
async def test_output_mode_tools_share_artifact_relative_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    patch_async_client(monkeypatch, handler)
    workspace = tmp_path / "session-a"
    workspace.mkdir()
    (workspace / "source.txt").write_text("uploaded", encoding="utf-8")
    artifact_root = workspace / "output"
    tools = []

    class Config:
        tools = ToolsConfig(
            enable_bash=True,
            enable_file_tools=True,
            enable_todo=False,
            enable_sub_agent=False,
        )
        image_generation = ImageGenerationConfig(
            endpoint="https://image.example.test/v1/images/generations",
        )

    add_workspace_tools(
        tools,
        Config(),
        workspace,
        allow_full_access=False,
        output=lambda *_: None,
        use_output_dir=True,
        artifact_root_dir=artifact_root,
    )
    by_name = {tool.name: tool for tool in tools}

    write_result = await by_name["write_file"].execute(
        path="assets/generated/manifest.json",
        content=json.dumps(
            {
                "mode": "creative_image_mode",
                "image_plan": [
                    {
                        "slide": 1,
                        "decision": "generate",
                        "status": "generated",
                        "output_path": "assets/generated/hero.png",
                    }
                ],
            }
        ),
    )
    image_result = await by_name["generate_image"].execute(
        prompt="hero",
        output_path="assets/generated/hero.png",
    )
    read_result = await by_name["read_file"].execute(path="../source.txt")
    legacy_write_result = await by_name["write_file"].execute(
        path="output/legacy.txt",
        content="legacy-compatible",
    )

    assert write_result.success
    assert image_result.success
    assert read_result.success
    assert legacy_write_result.success
    assert "uploaded" in read_result.content
    assert (artifact_root / "assets/generated/manifest.json").is_file()
    assert (artifact_root / "assets/generated/hero.png").is_file()
    assert (artifact_root / "legacy.txt").read_text(encoding="utf-8") == "legacy-compatible"
    assert not (artifact_root / "output/legacy.txt").exists()
    assert by_name["bash"].workspace_dir == str(artifact_root)
    assert by_name["bash"].scope_root_dir == str(workspace)
    assert by_name["bash"]._subprocess_env["BOX_AGENT_OUTPUT_DIR"] == str(artifact_root)
    assert by_name["bash"]._subprocess_env["BOX_AGENT_SCRATCH_DIR"] == str(
        workspace / ".box-agent-scratch"
    )
    assert (workspace / ".box-agent-scratch").is_dir()

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the PPT image-manifest validator")
    validator = (
        Path(__file__).parents[1]
        / "box_agent/skills/document-skills/pptx/scripts/validate_image_manifest.js"
    )
    completed = subprocess.run(
        [
            node,
            str(validator),
            "assets/generated/manifest.json",
            "--mode",
            "creative_image_mode",
            "--min-generated",
            "1",
            "--report",
            "qa/image_manifest.json",
        ],
        cwd=artifact_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_project_mode_tools_keep_workspace_relative_root(tmp_path: Path) -> None:
    tools = []

    class Config:
        tools = ToolsConfig(
            enable_bash=True,
            enable_file_tools=True,
            enable_todo=False,
            enable_sub_agent=False,
        )
        image_generation = ImageGenerationConfig(
            endpoint="https://image.example.test/v1/images/generations"
        )

    add_workspace_tools(
        tools,
        Config(),
        tmp_path,
        allow_full_access=False,
        output=lambda *_: None,
        use_output_dir=False,
    )
    by_name = {tool.name: tool for tool in tools}

    assert by_name["bash"].workspace_dir == str(tmp_path)
    assert by_name["write_file"].relative_root_dir == tmp_path
    assert by_name["generate_image"].output_dir == tmp_path
    assert not (tmp_path / "output").exists()


def _real_png(size=(256, 256), color=(40, 40, 40)) -> bytes:
    import io as _io

    from PIL import Image as _Image

    buf = _io.BytesIO()
    _Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _b64_handler(payload_bytes: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(payload_bytes).decode("ascii")}]},
        )

    return handler


@pytest.mark.asyncio
async def test_generate_image_watermarks_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _real_png()
    patch_async_client(monkeypatch, _b64_handler(source))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(prompt="hero", output_path="assets/generated/hero.png")

    assert result.success, result.error
    saved = (tmp_path / "assets/generated/hero.png").read_bytes()
    assert saved != source  # watermark changed the bytes
    assert result.raw_output["watermark"]["applied"] is True
    assert "watermark: applied" in result.content


@pytest.mark.asyncio
async def test_generate_image_watermark_disabled_keeps_original_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _real_png()
    patch_async_client(monkeypatch, _b64_handler(source))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(
        prompt="hero",
        output_path="assets/generated/plain.png",
        watermark=False,
    )

    assert result.success, result.error
    saved = (tmp_path / "assets/generated/plain.png").read_bytes()
    assert saved == source  # untouched
    assert result.raw_output["watermark"]["applied"] is False
    assert result.raw_output["watermark"]["reason"] == "disabled by caller"


@pytest.mark.asyncio
async def test_generate_image_custom_watermark_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _real_png()
    patch_async_client(monkeypatch, _b64_handler(source))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(
        prompt="hero",
        output_path="assets/generated/branded.png",
        watermark_text="网宿科技",
    )

    assert result.success, result.error
    saved = (tmp_path / "assets/generated/branded.png").read_bytes()
    assert saved != source
    assert result.raw_output["watermark"]["applied"] is True


@pytest.mark.asyncio
async def test_generate_image_edit_path_is_also_watermarked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _real_png()
    (tmp_path / "reference.png").write_bytes(_real_png(color=(10, 10, 10)))
    patch_async_client(monkeypatch, _b64_handler(source))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/api/web/llm/v2/images/gen",
        api_key="secret",
    )

    result = await tool.execute(
        prompt="restyle",
        output_path="assets/generated/edited.png",
        image_mode="image_to_image",
        reference_images=["reference.png"],
    )

    assert result.success, result.error
    saved = (tmp_path / "assets/generated/edited.png").read_bytes()
    assert saved != source
    assert result.raw_output["image_mode"] == "image_to_image"
    assert result.raw_output["watermark"]["applied"] is True


@pytest.mark.asyncio
async def test_generate_image_svg_response_skips_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svg_bytes = b"<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=svg_bytes,
            headers={"content-type": "image/svg+xml"},
        )

    patch_async_client(monkeypatch, handler)
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path),
        allow_full_access=False,
        endpoint="https://image.example.test/v1/images/generations",
        api_key="secret",
    )

    result = await tool.execute(prompt="logo", output_path="assets/generated/logo.svg")

    assert result.success, result.error
    saved = (tmp_path / "assets/generated/logo.svg").read_bytes()
    assert saved == svg_bytes  # vector untouched
    assert result.raw_output["watermark"]["applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_artifact", [True, False])
async def test_intermediate_image_stays_available_without_artifact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish_artifact: bool
) -> None:
    from box_agent.core import _detect_tool_artifacts, _snapshot_workspace_signatures

    patch_async_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        ),
    )
    output = tmp_path / "output"
    output.mkdir()
    before = _snapshot_workspace_signatures(str(tmp_path))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), output_dir=str(output),
        endpoint="https://image.example.test/v1/images/generations",
    )
    result = await tool.execute(
        prompt="slide illustration", output_path="assets/generated/hero.png",
        watermark=False, publish_artifact=publish_artifact,
    )
    assert result.success
    target = output / "assets/generated/hero.png"
    assert target.read_bytes() == PNG_BYTES
    assert json.loads(result.model_context)["absolute_path"] == str(target)
    assert result.raw_output["type"] == ("artifact" if publish_artifact else "intermediate_asset")
    # An unrelated deliverable must still be discovered in the same batch.
    other = output / "deck.html"
    other.write_text("<html></html>")
    events = _detect_tool_artifacts(
        "image-call", "generate_image",
        result.content + "\n[deck.html]\n[assets/generated/hero.png]",
        result.raw_output, before, _snapshot_workspace_signatures(str(tmp_path)),
        str(tmp_path), str(output),
    )
    paths = {event.abs_path for event in events}
    assert str(other) in paths
    assert (str(target) in paths) == publish_artifact


@pytest.mark.asyncio
async def test_intermediate_image_failure_does_not_publish_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_async_client(monkeypatch, lambda request: httpx.Response(500, text="failed"))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), endpoint="https://image.example.test/v1/images/generations",
    )
    result = await tool.execute(prompt="slide", output_path="hero.png", publish_artifact=False)
    assert not result.success
    assert not result.raw_output
    assert not (tmp_path / "hero.png").exists()
