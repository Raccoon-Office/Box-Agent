"""Organization headers on the actual OpenAI SDK request path."""

import json

import httpx
import pytest

from box_agent.auth import HostedAuthRefreshError
from box_agent.llm import OpenAIClient
from box_agent.schema import Message


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("api_base,api_key,expected", [
    ("https://code-test.xiaohuanxiong.com/api/web/llm/v2", "box-agent-no-auth", "team-one"),
    ("https://xiaohuanxiong.com/api/web/llm/v2", "box-agent-auth-json", "team-one"),
    ("https://CODE-TEST.XIAOHUANXIONG.COM./v1", "box-agent-no-auth", "team-one"),
    ("https://code-test.xiaohuanxiong.com/v1", "custom-provider-key", None),
    ("https://api.openai.com/v1", "box-agent-no-auth", None),
    ("http://10.158.136.99/v1", "box-agent-no-auth", None),
    ("https://xiaohuanxiong.com.example.org/v1", "box-agent-no-auth", None),
    ("https://notxiaohuanxiong.com/v1", "box-agent-no-auth", None),
])
async def test_llm_org_header_domain_and_custom_key_boundary(
    tmp_path, api_base, api_key, expected, stream,
):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "access_token": "login-token", "office_identity": "team-one",
    }), encoding="utf-8")
    requests = []

    def handler(request):
        requests.append(request)
        if stream:
            chunk = {
                "id": "test", "object": "chat.completion.chunk", "created": 0,
                "model": "test", "choices": [
                    {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"},
                ],
            }
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"},
            ],
        })

    client = OpenAIClient(api_key=api_key, api_base=api_base, model="test",
                          auth_file=str(auth_file))
    await client.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client.client = client.client.with_options(http_client=http_client)
        messages = [Message(role="user", content="hi")]
        if stream:
            _ = [event async for event in client.generate_stream(messages)]
        else:
            await client.generate(messages)
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/chat/completions")
    assert requests[0].headers.get("X-Org-Code") == expected


@pytest.mark.asyncio
async def test_llm_org_header_tracks_team_switch_and_personal_identity(tmp_path):
    auth_file = tmp_path / "auth.json"
    client = OpenAIClient(
        api_key="box-agent-no-auth",
        api_base="https://code-test.xiaohuanxiong.com/api/web/llm/v2",
        model="test", auth_file=str(auth_file),
    )
    try:
        for identity, expected in [
            ("team-one", "team-one"), ("team-two", "team-two"),
            ("personal", None), ("", None), (None, None),
            ("bad\r\nx-injected: true", None), ("团队", None),
        ]:
            auth_file.write_text(json.dumps({
                "access_token": "login-token", "office_identity": identity,
            }), encoding="utf-8")
            headers = await client._auth_headers()
            assert headers.get("X-Org-Code") == expected
            assert headers["Authorization"] == "Bearer login-token"
        auth_file.unlink()
        with pytest.raises(
            HostedAuthRefreshError,
            match="^未登录，请通过客户端登录后再试$",
        ):
            await client._auth_headers()
    finally:
        await client.client.close()
