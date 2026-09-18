"""Shared request-auth helpers for hosted Box-Agent integrations."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from box_agent.user_paths import configured_box_agent_home, state_path

AUTH_TOKEN_ENV_VARS = (
    "BOX_AGENT_AUTH_TOKEN",
    "OFFICEV3_AUTH_TOKEN",
    "RACCOON_ACCESS_TOKEN",
    "RACCOON_TOKEN",
)

AUTH_TOKEN_FILE_KEYS = ("token", "access_token", "auth_token")
AUTH_HEADER_HOST_SUFFIXES = ("xiaohuanxiong.com", "10.158.136.99")
AUTH_REFRESH_WINDOW_SECONDS = 300
AUTH_REFRESH_TIMEOUT_SECONDS = 15.0


class HostedAuthRefreshError(RuntimeError):
    """Raised when an expired hosted login cannot be refreshed."""


class HostedAuthRequiredError(HostedAuthRefreshError):
    """Hosted authentication requires user login rather than an automatic retry."""


_refresh_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _coerce_token(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _read_auth_state(auth_file: str | Path | None) -> tuple[Path | None, dict[str, Any]]:
    if not auth_file:
        return None, {}

    path = state_path("config/auth.json", auth_file)
    if not path.exists():
        return path, {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, {}
    return path, data if isinstance(data, dict) else {}


def _auth_token_from_state(data: Mapping[str, Any]) -> str:
    for key in AUTH_TOKEN_FILE_KEYS:
        token = _coerce_token(data.get(key))
        if token:
            return token
    return ""


def _jwt_expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    expiry = decoded.get("exp") if isinstance(decoded, dict) else None
    return int(expiry) if isinstance(expiry, (int, float)) else None


def _xiaohuanxiong_refresh_url(api_base: str) -> str:
    try:
        parsed = urlparse(api_base)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not (
        hostname == "xiaohuanxiong.com"
        or hostname.endswith(".xiaohuanxiong.com")
    ):
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/api/web/auth/v1/refresh"


def _refresh_lock(auth_file: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _refresh_locks.setdefault(loop, {})
    return locks.setdefault(str(auth_file.resolve()), asyncio.Lock())


def _write_auth_state(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            f"{json.dumps(dict(data), ensure_ascii=False, indent=2)}\n",
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def read_auth_token_file(auth_file: str | Path | None) -> str:
    """Read a bearer token from auth.json.

    The file is intentionally separate from config.yaml because login tokens
    rotate independently from model/provider settings. Supported shapes:
    ``{"access_token": "..."}``, ``{"token": "..."}``, or
    ``{"auth_token": "..."}``.
    """
    _, data = _read_auth_state(auth_file)
    return _auth_token_from_state(data)


def read_auth_org_code(auth_file: str | Path | None) -> str:
    """Read the current desktop team identity; personal sessions have no org."""
    _, data = _read_auth_state(auth_file)
    identity = _coerce_token(data.get("office_identity"))
    if (
        not identity
        or identity == "personal"
        or not identity.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in identity)
    ):
        return ""
    return identity


async def ensure_hosted_auth_ready(
    api_base: str,
    auth_file: str | Path | None,
    *,
    explicit_token: str = "",
    now: int | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Fail fast when a hosted xiaohuanxiong request has nothing to authenticate with.

    Non-xiaohuanxiong bases are left unchanged. An explicit in-memory token is
    accepted as-is. When auth.json has neither a readable access token nor a
    refresh_token, preserve the profile-aware environment fallback or raise
    ``HostedAuthRequiredError`` so callers never send a bare upstream request.
    Otherwise delegate to
    ``refresh_hosted_auth_token_if_needed`` (including refresh-401「登录态已过期…」).
    """
    token = _coerce_token(explicit_token)
    if token:
        return token

    if not _xiaohuanxiong_refresh_url(api_base):
        _, state = _read_auth_state(auth_file)
        return _auth_token_from_state(state)

    _, state = _read_auth_state(auth_file)
    current_token = _auth_token_from_state(state)
    refresh_token = _coerce_token(state.get("refresh_token"))
    if not current_token and not refresh_token:
        token = resolve_auth_token(auth_file=auth_file)
        if token:
            return token
        raise HostedAuthRequiredError("未登录，请通过客户端登录后再试")

    return await refresh_hosted_auth_token_if_needed(
        api_base,
        auth_file,
        now=now,
        http_client=http_client,
    )


async def refresh_hosted_auth_token_if_needed(
    api_base: str,
    auth_file: str | Path | None,
    *,
    now: int | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Refresh a near-expiry xiaohuanxiong.com login token and return it.

    Opaque tokens remain backward-compatible: only a missing token or a JWT with
    an ``exp`` inside the five-minute refresh window activates the refresh flow.
    """
    refresh_url = _xiaohuanxiong_refresh_url(api_base)
    path, state = _read_auth_state(auth_file)
    current_token = _auth_token_from_state(state)
    if not refresh_url or path is None:
        return current_token

    current_time = int(time.time()) if now is None else int(now)
    expiry = _jwt_expiry(current_token)
    if current_token and (
        expiry is None or current_time + AUTH_REFRESH_WINDOW_SECONDS < expiry
    ):
        return current_token

    async with _refresh_lock(path):
        # Another request may have refreshed the shared auth file while waiting.
        _, state = _read_auth_state(path)
        current_token = _auth_token_from_state(state)
        expiry = _jwt_expiry(current_token)
        current_time = int(time.time()) if now is None else int(now)
        if current_token and (
            expiry is None or current_time + AUTH_REFRESH_WINDOW_SECONDS < expiry
        ):
            return current_token

        refresh_token = _coerce_token(state.get("refresh_token"))
        if not refresh_token:
            return current_token

        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=AUTH_REFRESH_TIMEOUT_SECONDS)
        try:
            response = await client.post(
                refresh_url,
                headers={"Content-Type": "application/json"},
                json={"refresh_token": refresh_token},
            )
        except httpx.HTTPError as exc:
            if expiry is not None and current_time < expiry:
                return current_token
            raise HostedAuthRefreshError(f"登录态刷新失败：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 401:
            raise HostedAuthRequiredError("登录态已过期，请重新登录")
        if not response.is_success:
            if expiry is not None and current_time < expiry:
                return current_token
            raise HostedAuthRefreshError(
                f"登录态刷新失败（HTTP {response.status_code}）"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HostedAuthRefreshError("登录态刷新失败：服务器响应不是有效 JSON") from exc
        result = payload.get("data") if isinstance(payload, dict) else None
        next_access_token = _coerce_token(
            result.get("access_token") if isinstance(result, dict) else None
        )
        next_refresh_token = _coerce_token(
            result.get("refresh_token") if isinstance(result, dict) else None
        )
        next_expiry = _jwt_expiry(next_access_token)
        if (
            not next_access_token
            or next_expiry is None
            or current_time + AUTH_REFRESH_WINDOW_SECONDS >= next_expiry
        ):
            raise HostedAuthRefreshError("登录态刷新失败：服务器返回的 token 不可用")

        next_state = dict(state)
        next_state.pop("token", None)
        next_state.pop("auth_token", None)
        next_state["access_token"] = next_access_token
        if next_refresh_token:
            next_state["refresh_token"] = next_refresh_token
        _write_auth_state(path, next_state)
        return next_access_token


def resolve_auth_token(
    explicit: str | None = None,
    auth_file: str | Path | None = None,
) -> str:
    """Return an explicit, auth-file, or environment login token."""
    if explicit is not None and explicit.strip():
        return explicit.strip()

    file_token = read_auth_token_file(auth_file)
    if file_token:
        return file_token

    if configured_box_agent_home() is not None:
        return ""  # An isolated profile must not inherit the desktop user's login token.

    for name in AUTH_TOKEN_ENV_VARS:
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return ""


def bearer_auth_headers(
    token: str | None,
    existing: Mapping[str, str | bytes] | None = None,
    url: str | None = None,
) -> dict[str, str | bytes]:
    """Return headers with ``Authorization: Bearer <token>`` when safe.

    Existing Authorization headers are preserved so user-configured provider
    or MCP credentials are not overwritten by the officev3 login token.
    """
    headers = dict(existing or {})
    if not token:
        return headers

    if url and not should_attach_auth_header(url):
        return headers

    for key in headers:
        if key.lower() == "authorization":
            return headers

    headers["Authorization"] = f"Bearer {token}"
    return headers


def should_attach_auth_header(url: str) -> bool:
    """True for hosted officev3 gateway URLs that expect the login token."""
    try:
        hostname = urlparse(url).hostname or ""
    except ValueError:
        return False

    hostname = hostname.lower().rstrip(".")
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in AUTH_HEADER_HOST_SUFFIXES
    )


def request_auth_headers(
    auth_file: str | Path | None = None,
    explicit_token: str | None = None,
    existing: Mapping[str, str | bytes] | None = None,
    url: str | None = None,
) -> dict[str, str | bytes]:
    """Return request headers after reading the current auth token."""
    return bearer_auth_headers(resolve_auth_token(explicit_token, auth_file), existing, url=url)
