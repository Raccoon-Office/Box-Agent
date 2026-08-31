"""Tests for hosted request authentication helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from box_agent.auth import (
    HostedAuthRefreshError,
    bearer_auth_headers,
    read_auth_token_file,
    refresh_hosted_auth_token_if_needed,
    resolve_auth_token,
    should_attach_auth_header,
)
from box_agent.config import Config, LLMConfig, derive_context_token_limit
from box_agent.llm import AnthropicClient, OpenAIClient
from box_agent.tools import mcp_loader


def _jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature"


def test_resolve_auth_token_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOX_AGENT_AUTH_TOKEN", "env-token")
    assert resolve_auth_token(" explicit-token ") == "explicit-token"


def test_resolve_auth_token_reads_supported_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOX_AGENT_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OFFICEV3_AUTH_TOKEN", "office-token")
    assert resolve_auth_token() == "office-token"


def test_context_compaction_limit_uses_original_input_budget_formula() -> None:
    config = LLMConfig(context_window=180_000, max_output_tokens=63_999)

    assert config.context_token_limit == 104_400


def test_context_compaction_limit_uses_selected_model_capabilities() -> None:
    assert derive_context_token_limit(128_000, 16_000) == 100_800


@pytest.mark.parametrize(
    ("context_window", "max_output_tokens"),
    [(0, 1), (128_000, 0), (128_000, 128_000), (128_000, 128_001)],
)
def test_context_compaction_limit_rejects_invalid_capabilities(
    context_window: int,
    max_output_tokens: int,
) -> None:
    with pytest.raises(ValueError):
        derive_context_token_limit(context_window, max_output_tokens)


def test_resolve_auth_token_reads_auth_json_before_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOX_AGENT_AUTH_TOKEN", "env-token")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"access_token": "file-token"}\n', encoding="utf-8")

    assert read_auth_token_file(auth_file) == "file-token"
    assert resolve_auth_token(auth_file=auth_file) == "file-token"


def test_read_auth_token_file_prefers_token_over_access_token(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        '{"access_token": "login-token", "token": "override-token"}\n',
        encoding="utf-8",
    )

    assert read_auth_token_file(auth_file) == "override-token"


@pytest.mark.asyncio
async def test_refresh_hosted_auth_token_skips_fresh_token(tmp_path: Path) -> None:
    now = int(time.time())
    access_token = _jwt_with_exp(now + 3600)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"access_token": access_token, "refresh_token": "refresh-one"}),
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await refresh_hosted_auth_token_if_needed(
            "https://xiaohuanxiong.com/api/web/llm/v2",
            auth_file,
            now=now,
            http_client=client,
        )

    assert resolved == access_token
    assert requests == []


@pytest.mark.asyncio
async def test_refresh_hosted_auth_token_rotates_and_atomically_persists(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    old_access_token = _jwt_with_exp(now + 120)
    next_access_token = _jwt_with_exp(now + 3600)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "access_token": old_access_token,
                "refresh_token": "refresh-one",
                "office_identity": "employee",
            }
        ),
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "access_token": next_access_token,
                    "refresh_token": "refresh-two",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await refresh_hosted_auth_token_if_needed(
            "https://xiaohuanxiong.com/api/web/llm/v2",
            auth_file,
            now=now,
            http_client=client,
        )

    assert resolved == next_access_token
    assert len(requests) == 1
    assert str(requests[0].url) == "https://xiaohuanxiong.com/api/web/auth/v1/refresh"
    assert json.loads(requests[0].content) == {"refresh_token": "refresh-one"}
    assert json.loads(auth_file.read_text(encoding="utf-8")) == {
        "access_token": next_access_token,
        "refresh_token": "refresh-two",
        "office_identity": "employee",
    }
    assert auth_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_refresh_hosted_auth_token_deduplicates_concurrent_requests(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    next_access_token = _jwt_with_exp(now + 3600)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "access_token": _jwt_with_exp(now - 1),
                "refresh_token": "refresh-one",
            }
        ),
        encoding="utf-8",
    )
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"data": {"access_token": next_access_token}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(
            refresh_hosted_auth_token_if_needed(
                "https://code-test.xiaohuanxiong.com/api/web/llm/v2",
                auth_file,
                now=now,
                http_client=client,
            ),
            refresh_hosted_auth_token_if_needed(
                "https://code-test.xiaohuanxiong.com/api/web/llm/v2",
                auth_file,
                now=now,
                http_client=client,
            ),
        )

    assert results == [next_access_token, next_access_token]
    assert request_count == 1


@pytest.mark.asyncio
async def test_refresh_hosted_auth_token_skips_non_xiaohuanxiong_provider(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    access_token = _jwt_with_exp(now - 1)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"access_token": access_token, "refresh_token": "refresh-one"}),
        encoding="utf-8",
    )

    resolved = await refresh_hosted_auth_token_if_needed(
        "https://llm.example.com/v1",
        auth_file,
        now=now,
    )

    assert resolved == access_token


@pytest.mark.asyncio
async def test_refresh_hosted_auth_token_reports_expired_refresh_login(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    old_access_token = _jwt_with_exp(now - 1)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {"access_token": old_access_token, "refresh_token": "refresh-one"}
        ),
        encoding="utf-8",
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HostedAuthRefreshError, match="登录态已过期"):
            await refresh_hosted_auth_token_if_needed(
                "https://xiaohuanxiong.com/api/web/llm/v2",
                auth_file,
                now=now,
                http_client=client,
            )

    assert read_auth_token_file(auth_file) == old_access_token


def test_bearer_auth_headers_preserves_existing_authorization() -> None:
    headers = bearer_auth_headers(
        "login-token",
        {"authorization": "Bearer custom-provider-token", "X-Test": "1"},
    )
    assert headers == {"authorization": "Bearer custom-provider-token", "X-Test": "1"}


def test_auth_header_only_attaches_to_hosted_gateway_hosts() -> None:
    assert should_attach_auth_header("https://api.xiaohuanxiong.com/v1")
    assert should_attach_auth_header("https://llm.internal.xiaohuanxiong.com/v1")
    assert should_attach_auth_header("http://10.158.136.99:9090/api/web/llm/v2")
    assert not should_attach_auth_header("https://llm.example.com/v1")

    assert bearer_auth_headers(
        "login-token",
        url="https://api.xiaohuanxiong.com/v1",
    ) == {"Authorization": "Bearer login-token"}
    assert bearer_auth_headers(
        "login-token",
        url="http://10.158.136.99:9090/api/web/llm/v2",
    ) == {"Authorization": "Bearer login-token"}
    assert bearer_auth_headers("login-token", url="https://llm.example.com/v1") == {}


def test_config_defaults_auth_file_next_to_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_key: "provider-key"\n'
        'api_base: "https://llm.example.com/v1"\n'
        'model: "test-model"\n'
        'provider: "openai"\n',
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.auth_file == str(tmp_path / "auth.json")


def test_config_accepts_custom_auth_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    auth_file = tmp_path / "runtime-auth.json"
    config_path.write_text(
        'api_key: "provider-key"\n'
        'api_base: "https://llm.example.com/v1"\n'
        'model: "test-model"\n'
        'provider: "openai"\n'
        f'auth_file: "{auth_file}"\n',
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.auth_file == str(auth_file)


def test_config_accepts_image_generation_block(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_key: "provider-key"\n'
        'api_base: "https://llm.example.com/v1"\n'
        'model: "test-model"\n'
        'provider: "openai"\n'
        "image_generation:\n"
        '  endpoint: "https://image.example.com/v1/images/generations"\n'
        '  api_key: "image-token"\n'
        '  model: "chatgpt-image-latest"\n'
        "  timeout: 45\n",
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.image_generation.endpoint == "https://image.example.com/v1/images/generations"
    assert config.image_generation.api_key == "image-token"
    assert config.image_generation.model == "chatgpt-image-latest"
    assert config.image_generation.timeout == 45.0
    assert config.image_generation.auth_file == str(tmp_path / "auth.json")


def test_hosted_gateway_allows_missing_api_key_and_model(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_base: "https://code-test.xiaohuanxiong.com/api/web/llm/v2"\n'
        'provider: "openai"\n',
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.api_key == "box-agent-auth-json"
    assert config.llm.model == ""
    assert config.llm.max_output_tokens == 80000


def test_user_configured_endpoint_defaults_main_max_output_tokens_to_common_ceiling(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_key: "user-key"\n'
        'api_base: "https://token.sensenova.cn/v1"\n'
        'model: "deepseek-v4-flash"\n'
        'provider: "openai"\n',
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.max_output_tokens == 63999


def test_explicit_main_max_output_tokens_overrides_domain_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_base: "https://code-test.xiaohuanxiong.com/api/web/llm/v2"\n'
        'provider: "openai"\n'
        "max_output_tokens: 12345\n",
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.max_output_tokens == 12345


def test_hosted_gateway_drops_unconfigured_default_model(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'api_key: "YOUR_API_KEY_HERE"\n'
        'api_base: "https://code-test.xiaohuanxiong.com/api/web/llm/v2"\n'
        'model: "claude-sonnet-4-20250514"\n'
        'provider: "openai"\n',
        encoding="utf-8",
    )

    config = Config.from_yaml(config_path)
    assert config.llm.api_key == "box-agent-auth-json"
    assert config.llm.model == ""


@pytest.mark.asyncio
async def test_openai_client_uses_configured_api_key_without_auth_json(tmp_path: Path) -> None:
    captured: list[dict[str, str] | None] = []

    class FakeCompletions:
        async def create(self, **params):
            captured.append(params.get("extra_headers"))
            return object()

    class FakeChat:
        completions = FakeCompletions()

    client = OpenAIClient(
        api_key="provider-key",
        api_base="https://llm.xiaohuanxiong.com/v1",
        model="test-model",
        auth_file=str(tmp_path / "auth.json"),
    )
    client.client.chat = FakeChat()

    (tmp_path / "auth.json").write_text('{"access_token": "token-one"}\n', encoding="utf-8")
    await client._make_api_request([{"role": "user", "content": "hi"}])

    (tmp_path / "auth.json").write_text('{"access_token": "token-two"}\n', encoding="utf-8")
    await client._make_api_request([{"role": "user", "content": "hi"}])

    assert captured == [None, None]


@pytest.mark.asyncio
async def test_openai_client_omits_empty_model(tmp_path: Path) -> None:
    captured: list[dict[str, object]] = []

    class FakeCompletions:
        async def create(self, **params):
            captured.append(params)
            return object()

    class FakeChat:
        completions = FakeCompletions()

    client = OpenAIClient(
        api_key="box-agent-auth-json",
        api_base="https://llm.xiaohuanxiong.com/v1",
        model="",
        auth_file=str(tmp_path / "auth.json"),
    )
    client.client.chat = FakeChat()

    (tmp_path / "auth.json").write_text('{"access_token": "token-one"}\n', encoding="utf-8")
    await client._make_api_request([{"role": "user", "content": "hi"}])

    assert "model" not in captured[0]
    assert captured[0]["extra_headers"] == {"Authorization": "Bearer token-one"}


@pytest.mark.asyncio
async def test_openai_client_reads_auth_json_for_officev3_placeholder(tmp_path: Path) -> None:
    captured: list[dict[str, str]] = []

    class FakeCompletions:
        async def create(self, **params):
            captured.append(params["extra_headers"])
            return object()

    class FakeChat:
        completions = FakeCompletions()

    client = OpenAIClient(
        api_key="box-agent-no-auth",
        api_base="https://pre.xiaohuanxiong.com/api/web/llm/v2",
        model="test-model",
        auth_file=str(tmp_path / "auth.json"),
    )
    client.client.chat = FakeChat()

    (tmp_path / "auth.json").write_text('{"access_token": "token-one"}\n', encoding="utf-8")
    await client._make_api_request([{"role": "user", "content": "hi"}])

    assert captured == [{"Authorization": "Bearer token-one"}]


@pytest.mark.asyncio
async def test_anthropic_client_uses_configured_api_key_without_auth_json(tmp_path: Path) -> None:
    captured: list[dict[str, str] | None] = []

    class FakeMessages:
        async def create(self, **params):
            captured.append(params.get("extra_headers"))
            return object()

    client = AnthropicClient(
        api_key="provider-key",
        api_base="https://llm.xiaohuanxiong.com",
        model="test-model",
        auth_file=str(tmp_path / "auth.json"),
    )
    client.client.messages = FakeMessages()

    (tmp_path / "auth.json").write_text('{"access_token": "token-one"}\n', encoding="utf-8")
    await client._make_api_request(None, [{"role": "user", "content": "hi"}])

    (tmp_path / "auth.json").write_text('{"access_token": "token-two"}\n', encoding="utf-8")
    await client._make_api_request(None, [{"role": "user", "content": "hi"}])

    assert captured == [None, None]


@pytest.mark.asyncio
async def test_mcp_loader_adds_dynamic_auth_for_hosted_url_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[dict[str, str], object]] = []

    async def fake_connect(self) -> bool:
        captured.append((self.headers, self.auth))
        return False

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", fake_connect)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {
                "mcpServers": {
                    "remote": {
                        "transport": "sse",
                        "url": "https://mcp.xiaohuanxiong.com/sse",
                    }
                }
            },
            f,
        )
        f.flush()

        try:
            auth_file = Path(f.name).with_name("auth.json")
            auth_file.write_text('{"access_token": "login-token"}\n', encoding="utf-8")
            await mcp_loader.load_mcp_tools_async(f.name, auth_file=str(auth_file))
        finally:
            auth_file.unlink(missing_ok=True)
            Path(f.name).unlink()

    assert captured[0][0] == {}
    assert isinstance(captured[0][1], mcp_loader.DynamicBearerAuth)


def test_dynamic_mcp_auth_reads_auth_json_for_each_request(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    auth = mcp_loader.DynamicBearerAuth(auth_file=str(auth_file))

    auth_file.write_text('{"access_token": "token-one"}\n', encoding="utf-8")
    request_one = httpx.Request("POST", "https://mcp.xiaohuanxiong.com/mcp")
    next(auth.sync_auth_flow(request_one))

    auth_file.write_text('{"access_token": "token-two"}\n', encoding="utf-8")
    request_two = httpx.Request("POST", "https://mcp.xiaohuanxiong.com/mcp")
    next(auth.sync_auth_flow(request_two))

    assert request_one.headers["Authorization"] == "Bearer token-one"
    assert request_two.headers["Authorization"] == "Bearer token-two"


@pytest.mark.asyncio
async def test_mcp_loader_skips_auth_header_for_non_xiaohuanxiong_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str]] = []

    async def fake_connect(self) -> bool:
        captured.append(self.headers)
        return False

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", fake_connect)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {
                "mcpServers": {
                    "remote": {
                        "transport": "sse",
                        "url": "https://mcp.example.com/sse",
                    }
                }
            },
            f,
        )
        f.flush()

        try:
            auth_file = Path(f.name).with_name("auth.json")
            auth_file.write_text('{"access_token": "login-token"}\n', encoding="utf-8")
            await mcp_loader.load_mcp_tools_async(f.name, auth_file=str(auth_file))
        finally:
            auth_file.unlink(missing_ok=True)
            Path(f.name).unlink()

    assert captured == [{}]


@pytest.mark.asyncio
async def test_mcp_loader_does_not_override_configured_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str]] = []

    async def fake_connect(self) -> bool:
        captured.append(self.headers)
        return False

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", fake_connect)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "streamable_http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "Bearer mcp-token"},
                    }
                }
            },
            f,
        )
        f.flush()

        try:
            await mcp_loader.load_mcp_tools_async(f.name, auth_token="login-token")
        finally:
            Path(f.name).unlink()

    assert captured == [{"Authorization": "Bearer mcp-token"}]
