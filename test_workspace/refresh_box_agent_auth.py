"""Safely refresh the Box-Agent desktop authentication file.

The desktop application owns interactive login.  This helper only uses the
existing refresh token from ``auth.json`` so unattended ACP evaluation can
recover from an expired (or nearly expired) access token.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_AUTH_FILE = Path("~/.box-agent/config/auth.json")
DEFAULT_REFRESH_URL = "https://xiaohuanxiong.com/api/web/auth/v1/refresh"
DEFAULT_REFRESH_WINDOW_SECONDS = 300
MINIMUM_NEW_TOKEN_LIFETIME_SECONDS = 60
MAX_AUTH_FILE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
ALLOWED_REFRESH_HOSTS = frozenset({"xiaohuanxiong.com", "10.158.136.99"})

_PROCESS_REFRESH_LOCK = threading.Lock()


class AuthRefreshError(RuntimeError):
    """A safe authentication refresh error that never contains credentials."""


@dataclass(frozen=True)
class AuthRefreshResult:
    refreshed: bool
    access_expires_at: int | None
    refresh_token_rotated: bool = False

    def to_safe_dict(self) -> dict[str, bool | int | None | str]:
        return {
            "status": "refreshed" if self.refreshed else "ready",
            "refreshed": self.refreshed,
            "access_expires_at": self.access_expires_at,
            "refresh_token_rotated": self.refresh_token_rotated,
        }


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _decode_jwt_expiry(token: str) -> int | None:
    parts = token.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        expiry = claims["exp"]
        if isinstance(expiry, bool):
            return None
        return int(expiry)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return None


def _read_auth_document(auth_file: Path) -> dict[str, Any]:
    if auth_file.is_symlink():
        raise AuthRefreshError("认证文件不能是符号链接")
    try:
        if auth_file.stat().st_size > MAX_AUTH_FILE_BYTES:
            raise AuthRefreshError("认证文件大小异常")
        document = json.loads(auth_file.read_text(encoding="utf-8"))
    except AuthRefreshError:
        raise
    except FileNotFoundError as error:
        raise AuthRefreshError("未找到办公小浣熊登录状态") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AuthRefreshError("办公小浣熊登录状态不可读") from error
    if not isinstance(document, dict):
        raise AuthRefreshError("办公小浣熊登录状态格式无效")
    return document


def _validate_refresh_url(refresh_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(refresh_url)
        port = parsed.port
    except ValueError as error:
        raise AuthRefreshError("认证刷新地址无效") from error
    hostname = (parsed.hostname or "").casefold()
    host_allowed = hostname in ALLOWED_REFRESH_HOSTS or hostname.endswith(
        ".xiaohuanxiong.com"
    )
    if (
        parsed.scheme.casefold() != "https"
        or not host_allowed
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/api/web/auth/v1/refresh"
        or parsed.query
        or parsed.fragment
    ):
        raise AuthRefreshError("认证刷新地址不在允许的官方 HTTPS 范围内")
    return urllib.parse.urlunsplit(parsed)


def _atomic_write_auth(auth_file: Path, document: Mapping[str, Any]) -> None:
    auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{auth_file.name}.",
        suffix=".tmp",
        dir=auth_file.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, auth_file)
        os.chmod(auth_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as error:
        raise AuthRefreshError("无法安全写入办公小浣熊登录状态") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _default_urlopen(
    request: urllib.request.Request,
    timeout: float,
):
    opener = urllib.request.build_opener(_RejectRedirects)
    return opener.open(request, timeout=timeout)


def _request_new_tokens(
    refresh_token: str,
    *,
    refresh_url: str,
    timeout_seconds: float,
    urlopen: Callable[..., Any],
) -> tuple[str, str | None]:
    safe_url = _validate_refresh_url(refresh_url)
    payload = json.dumps(
        {"refresh_token": refresh_token}, separators=(",", ":")
    ).encode("utf-8")
    request = urllib.request.Request(
        safe_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise AuthRefreshError("认证刷新服务返回了不安全的重定向") from error
        raise AuthRefreshError(f"认证刷新服务返回 HTTP {error.code}") from error
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise AuthRefreshError("无法连接认证刷新服务") from error
    if len(raw_response) > MAX_RESPONSE_BYTES:
        raise AuthRefreshError("认证刷新服务响应大小异常")
    try:
        response_document = json.loads(raw_response.decode("utf-8"))
        data = response_document["data"]
        access_token = data["access_token"]
        rotated_refresh_token = data.get("refresh_token")
    except (
        KeyError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise AuthRefreshError("认证刷新服务响应格式无效") from error
    if not isinstance(access_token, str) or not access_token.strip():
        raise AuthRefreshError("认证刷新服务未返回有效 access_token")
    if rotated_refresh_token is not None and (
        not isinstance(rotated_refresh_token, str)
        or not rotated_refresh_token.strip()
    ):
        raise AuthRefreshError("认证刷新服务返回的 refresh_token 无效")
    return access_token.strip(), (
        rotated_refresh_token.strip()
        if isinstance(rotated_refresh_token, str)
        else None
    )


def ensure_fresh_auth(
    *,
    auth_file: Path | None = None,
    refresh_url: str | None = None,
    refresh_window_seconds: int = DEFAULT_REFRESH_WINDOW_SECONDS,
    timeout_seconds: float = 15.0,
    now: int | None = None,
    urlopen: Callable[..., Any] | None = None,
) -> AuthRefreshResult:
    """Return a usable access token state, refreshing the file when necessary."""
    if refresh_window_seconds < 0:
        raise ValueError("refresh_window_seconds must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    configured_auth_file = os.environ.get("BOX_AGENT_EVAL_AUTH_FILE", "").strip()
    resolved_auth_file = (
        auth_file
        if auth_file is not None
        else Path(configured_auth_file) if configured_auth_file else DEFAULT_AUTH_FILE
    ).expanduser()
    configured_refresh_url = (
        refresh_url
        or os.environ.get("BOX_AGENT_AUTH_REFRESH_URL", "").strip()
        or DEFAULT_REFRESH_URL
    )
    current_time = int(time.time()) if now is None else int(now)

    with _PROCESS_REFRESH_LOCK:
        document = _read_auth_document(resolved_auth_file)
        access_token = document.get("access_token")
        expiry = (
            _decode_jwt_expiry(access_token)
            if isinstance(access_token, str) and access_token.strip()
            else None
        )
        if isinstance(access_token, str) and access_token.strip() and (
            expiry is None or expiry > current_time + refresh_window_seconds
        ):
            return AuthRefreshResult(False, expiry)

        refresh_token = document.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise AuthRefreshError("登录已过期且本地没有可用的 refresh_token")
        new_access_token, new_refresh_token = _request_new_tokens(
            refresh_token.strip(),
            refresh_url=configured_refresh_url,
            timeout_seconds=timeout_seconds,
            urlopen=urlopen or _default_urlopen,
        )
        new_expiry = _decode_jwt_expiry(new_access_token)
        if (
            new_expiry is None
            or new_expiry <= current_time + MINIMUM_NEW_TOKEN_LIFETIME_SECONDS
        ):
            raise AuthRefreshError("认证刷新服务返回的 access_token 时效无效")

        updated_document = dict(document)
        updated_document["access_token"] = new_access_token
        if new_refresh_token is not None:
            updated_document["refresh_token"] = new_refresh_token
        _atomic_write_auth(resolved_auth_file, updated_document)
        return AuthRefreshResult(
            True,
            new_expiry,
            refresh_token_rotated=(
                new_refresh_token is not None
                and new_refresh_token != refresh_token.strip()
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用本地 refresh_token 安全刷新 Box-Agent 登录状态"
    )
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--refresh-url")
    parser.add_argument(
        "--refresh-window-seconds",
        type=int,
        default=DEFAULT_REFRESH_WINDOW_SECONDS,
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = ensure_fresh_auth(
            auth_file=args.auth_file,
            refresh_url=args.refresh_url,
            refresh_window_seconds=args.refresh_window_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except (AuthRefreshError, ValueError) as error:
        print(f"认证刷新失败: {error}", file=os.sys.stderr)
        return 2
    print(json.dumps(result.to_safe_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
