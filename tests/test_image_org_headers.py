"""Organization identity on actual image generation and multipart edit requests."""

import json

import httpx
import pytest

from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.billing import confirmation


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("deployment", ["public", "private", "custom"])
@pytest.mark.parametrize("api_key", ["", "custom-key"])
async def test_image_org_header_tracks_current_identity_on_wire(
    tmp_path, monkeypatch, edit, deployment, api_key,
):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true" if deployment == "private" else "false")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    origin = {
        "public": "https://image.xiaohuanxiong.com",
        "private": "https://tenant.example/office",
        "custom": "https://xiaohuanxiong.com.example.org",
    }[deployment]
    auth_file = tmp_path / "auth.json"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=PNG_BYTES, headers={"Content-Type": "image/png"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), endpoint=f"{origin}/v1/images/generations",
        api_key=api_key, auth_file=str(auth_file),
    )
    (tmp_path / "reference.png").write_bytes(PNG_BYTES)
    # Reuse the tool so identity switches cannot be hidden by cached headers.
    for identity in ("team-one", "team-two", "personal", "", None, "bad\r\nheader", "团队"):
        auth_file.write_text(json.dumps({"access_token": "login-token", "office_identity": identity}))
        result = await tool.execute(
            prompt="draw", output_path="result.png", watermark=False,
            image_mode="image_to_image" if edit else "text_to_image",
            reference_images=["reference.png"] if edit else None,
        )
        assert result.success, result.error
        request = requests[-1]
        expected = identity if identity in ("team-one", "team-two") and not api_key else None
        assert request.headers.get("X-Org-Code") == expected
        assert request.url.path.endswith("/images/edits" if edit else "/images/generations")
        if edit:
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert PNG_BYTES in request.content
    assert len(requests) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("status", [307, 308])
async def test_image_redirect_preserves_org_code_without_forwarding_bearer(
    tmp_path, monkeypatch, edit, status,
):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "false")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"access_token": "login-token", "office_identity": "team-one"}))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "image.xiaohuanxiong.com":
            return httpx.Response(status, headers={"Location": "https://cdn.example.org/image"})
        return httpx.Response(200, content=PNG_BYTES, headers={"Content-Type": "image/png"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), endpoint="https://image.xiaohuanxiong.com/v1/images/generations",
        api_key="", auth_file=str(auth_file),
    )
    (tmp_path / "reference.png").write_bytes(PNG_BYTES)
    result = await tool.execute(
        prompt="draw", output_path="result.png", watermark=False,
        image_mode="image_to_image" if edit else "text_to_image",
        reference_images=["reference.png"] if edit else None,
    )
    assert result.success, result.error
    assert len(requests) == 2
    assert requests[0].headers["X-Org-Code"] == "team-one"
    assert requests[0].headers["Authorization"] == "Bearer login-token"
    assert requests[1].headers["X-Org-Code"] == "team-one"
    assert "Authorization" not in requests[1].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["approval", "401", "refresh"])
@pytest.mark.parametrize("new_org", ["team-one", "team-two"])
async def test_identity_change_never_reuses_image_billing_approval(tmp_path, monkeypatch, phase, new_org):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "false")
    auth_file = tmp_path / "auth.json"
    original_auth = {"access_token": "original", "refresh_token": "original-refresh", "office_identity": "team-one"}
    new_auth = {"access_token": "other-account", "refresh_token": "other-refresh", "office_identity": new_org}
    auth_file.write_text(json.dumps(original_auth))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/auth/v1/refresh"):
            auth_file.write_text(json.dumps(new_auth))
            return httpx.Response(200, json={"data": {"access_token": "rotated-original"}})
        if phase == "401":
            auth_file.write_text(json.dumps(new_auth))
        return httpx.Response(401)

    async def approve(_capability):
        if phase == "approval":
            auth_file.write_text(json.dumps(new_auth))
        return True

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    tool = GenerateImageTool(
        workspace_dir=str(tmp_path), endpoint="https://image.xiaohuanxiong.com/v1/images/generations",
        api_key="", auth_file=str(auth_file),
    )
    token = confirmation.set(approve)
    try:
        result = await tool.execute(prompt="draw", output_path="result.png")
    finally:
        confirmation.reset(token)
    assert not result.success
    assert "登录账号或组织已变更" in result.error
    assert json.loads(auth_file.read_text()) == new_auth
    assert len(requests) == {"approval": 0, "401": 1, "refresh": 2}[phase]
    if requests:
        assert requests[0].headers["Authorization"] == "Bearer original"
        assert requests[0].headers["X-Org-Code"] == "team-one"
