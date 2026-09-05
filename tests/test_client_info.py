from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.client_info import ClientInfo, current_client_headers, scoped_client_info
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import StreamEvent


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "http://localhost:8000/v1",
    "http://10.158.136.99/v1",
    "https://xiaohuanxiong.com.example.org/v1",
    "https://notxiaohuanxiong.com/v1",
    "https://xiaohuanxiong.com@evil.example/v1",
    "https://[invalid",
    "file://xiaohuanxiong.com/v1",
])
def test_client_metadata_is_not_sent_outside_raccoon_domain(url):
    assert ClientInfo(device_id="device-1").headers_for_url(url) == {}


@pytest.mark.parametrize("version,expected", [
    ("1.2.3", "v1.2.3"), ("v1.2.3", "v1.2.3"),
    ("git-abc123", None), ("", None), ("v01.2.3", None),
])
def test_product_version_is_normalized_without_inventing_missing_version(version, expected):
    headers = ClientInfo(version=version).headers_for_url("https://PRE.XIAOHUANXIONG.COM./v1")
    assert headers["x-client-name"] == "raccoon"
    assert headers["x-client-platform"] == "unknown"
    assert headers.get("x-client-version") == expected


def test_optional_host_metadata_is_forwarded_without_collecting_device_identifiers():
    headers = ClientInfo(
        os_version="15.6", channel="official", device_id="host-issued-id"
    ).headers_for_url("https://xiaohuanxiong.com/v1")
    assert headers == {
        "x-client-name": "raccoon", "x-client-platform": "unknown",
        "x-client-os-version": "15.6", "x-client-channel": "official",
        "x-client-device-id": "host-issued-id",
    }


def test_client_info_parses_supported_host_metadata() -> None:
    client_info = ClientInfo.from_meta(
        {
            "name": " raccoon-ai ",
            "platform": "desktop-macos-arm64",
            "version": "v0.21.1",
            "osVersion": "15.6",
            "channel": "official",
            "deviceId": "device-1",
        }
    )

    assert client_info == ClientInfo(
        name="raccoon-ai",
        platform="desktop-macos-arm64",
        version="v0.21.1",
        os_version="15.6",
        channel="official",
        device_id="device-1",
    )


def test_client_info_rejects_non_ascii_and_control_character_values() -> None:
    client_info = ClientInfo.from_meta(
        {
            "name": "raccoon-ai\r\nx-injected: true",
            "platform": "桌面端",
            "version": "v0.21.1",
        }
    )

    assert client_info == ClientInfo(version="v0.21.1")


def test_client_headers_are_limited_to_raccoon_owned_backends() -> None:
    client_info = ClientInfo(
        name="raccoon-ai",
        platform="desktop-windows-x64",
        version="v0.21.1",
    )

    assert client_info.headers_for_url("https://xiaohuanxiong.com/api/web/llm/v2") == {
        "x-client-name": "raccoon",
        "x-client-platform": "desktop-windows-x64",
        "x-client-version": "v0.21.1",
    }
    assert client_info.headers_for_url("https://api.openai.com/v1") == {}
    assert client_info.headers_for_url("https://api.anthropic.com") == {}


def test_scoped_client_info_does_not_leak_after_request_scope() -> None:
    client_info = ClientInfo(name="raccoon-ai", device_id="scoped-device")

    with scoped_client_info(client_info):
        assert current_client_headers("https://xiaohuanxiong.com/api/web/llm/v2") == {
            "x-client-name": "raccoon",
            "x-client-platform": "unknown",
            "x-client-device-id": "scoped-device",
        }

    assert current_client_headers("https://xiaohuanxiong.com/api/web/llm/v2") == {
        "x-client-name": "raccoon",
        "x-client-platform": "unknown",
    }


class _DummyConn:
    async def sessionUpdate(self, _payload) -> None:
        return None


class _ClientHeaderCaptureLLM:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    async def generate_stream(self, _messages, _tools=None, **_kwargs):
        self.headers = current_client_headers(
            "https://xiaohuanxiong.com/api/web/llm/v2"
        )
        yield StreamEvent(type="text", delta="ok")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_acp_session_inherits_client_info_from_initialize(tmp_path) -> None:
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(tmp_path),
            enable_memory_extraction=False,
        ),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    llm = _ClientHeaderCaptureLLM()
    agent = BoxACPAgent(_DummyConn(), config, llm, [], "system")
    await agent.initialize(
        SimpleNamespace(
            field_meta={
                "client_info": {
                    "name": "raccoon-ai",
                    "platform": "desktop-linux-arm64",
                    "version": "v0.21.1",
                }
            }
        )
    )
    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={}))

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "hello"}],
            field_meta={},
        )
    )

    assert llm.headers == {
        "x-client-name": "raccoon",
        "x-client-platform": "desktop-linux-arm64",
        "x-client-version": "v0.21.1",
    }
