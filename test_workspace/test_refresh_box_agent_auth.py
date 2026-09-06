import base64
import json
import stat
import urllib.error
from pathlib import Path

import pytest

from test_workspace.refresh_box_agent_auth import (
    AuthRefreshError,
    ensure_fresh_auth,
)


def _unsigned_jwt(expiry: int) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expiry})}.signature"


class _Response:
    def __init__(self, document):
        self.payload = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def test_refreshes_expired_auth_rotates_refresh_token_and_preserves_metadata(
    tmp_path: Path,
):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "access_token": _unsigned_jwt(now - 1),
                "refresh_token": "old-secret-refresh-token",
                "office_identity": {"user_id": "user-one"},
            }
        ),
        encoding="utf-8",
    )
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response(
            {
                "data": {
                    "access_token": _unsigned_jwt(now + 3600),
                    "refresh_token": "new-secret-refresh-token",
                }
            }
        )

    result = ensure_fresh_auth(
        auth_file=auth_file,
        now=now,
        urlopen=fake_urlopen,
    )

    assert result.refreshed is True
    assert result.access_expires_at == now + 3600
    assert result.refresh_token_rotated is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "https://xiaohuanxiong.com/api/web/auth/v1/refresh"
    assert timeout == 15.0
    assert json.loads(request.data) == {
        "refresh_token": "old-secret-refresh-token"
    }
    document = json.loads(auth_file.read_text(encoding="utf-8"))
    assert document["access_token"] == _unsigned_jwt(now + 3600)
    assert document["refresh_token"] == "new-secret-refresh-token"
    assert document["office_identity"] == {"user_id": "user-one"}
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
    safe_result = json.dumps(result.to_safe_dict())
    assert "old-secret-refresh-token" not in safe_result
    assert "new-secret-refresh-token" not in safe_result


def test_does_not_call_refresh_service_for_a_fresh_access_token(tmp_path: Path):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "access_token": _unsigned_jwt(now + 301),
                "refresh_token": "secret-refresh-token",
            }
        ),
        encoding="utf-8",
    )

    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("refresh endpoint should not be called")

    result = ensure_fresh_auth(
        auth_file=auth_file,
        now=now,
        urlopen=unexpected_urlopen,
    )

    assert result.refreshed is False
    assert result.access_expires_at == now + 301


def test_rejects_non_official_refresh_url_before_sending_credentials(tmp_path: Path):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    original = json.dumps(
        {
            "access_token": _unsigned_jwt(now - 1),
            "refresh_token": "secret-refresh-token",
        }
    )
    auth_file.write_text(original, encoding="utf-8")

    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("credentials must not be sent")

    with pytest.raises(AuthRefreshError, match="官方 HTTPS"):
        ensure_fresh_auth(
            auth_file=auth_file,
            refresh_url="https://attacker.example/api/web/auth/v1/refresh",
            now=now,
            urlopen=unexpected_urlopen,
        )

    assert auth_file.read_text(encoding="utf-8") == original


def test_http_failure_does_not_modify_auth_file(tmp_path: Path):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    original = json.dumps(
        {
            "access_token": _unsigned_jwt(now - 1),
            "refresh_token": "secret-refresh-token",
        }
    )
    auth_file.write_text(original, encoding="utf-8")

    def failing_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    with pytest.raises(AuthRefreshError, match="HTTP 401"):
        ensure_fresh_auth(
            auth_file=auth_file,
            now=now,
            urlopen=failing_urlopen,
        )

    assert auth_file.read_text(encoding="utf-8") == original


def test_rejects_redirect_without_following_it(tmp_path: Path):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    original = json.dumps(
        {
            "access_token": _unsigned_jwt(now - 1),
            "refresh_token": "secret-refresh-token",
        }
    )
    auth_file.write_text(original, encoding="utf-8")

    def redirecting_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

    with pytest.raises(AuthRefreshError, match="不安全的重定向"):
        ensure_fresh_auth(
            auth_file=auth_file,
            now=now,
            urlopen=redirecting_urlopen,
        )

    assert auth_file.read_text(encoding="utf-8") == original


def test_rejects_refresh_response_with_an_expired_access_token(tmp_path: Path):
    now = 1_000_000
    auth_file = tmp_path / "auth.json"
    original = json.dumps(
        {
            "access_token": _unsigned_jwt(now - 1),
            "refresh_token": "secret-refresh-token",
        }
    )
    auth_file.write_text(original, encoding="utf-8")

    with pytest.raises(AuthRefreshError, match="时效无效"):
        ensure_fresh_auth(
            auth_file=auth_file,
            now=now,
            urlopen=lambda *_args, **_kwargs: _Response(
                {"data": {"access_token": _unsigned_jwt(now)}}
            ),
        )

    assert auth_file.read_text(encoding="utf-8") == original
