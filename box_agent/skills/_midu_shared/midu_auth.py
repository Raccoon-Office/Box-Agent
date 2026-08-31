#!/usr/bin/env python3
"""Shared Midu SMS authentication for the builtin Midu skill suite.

The Officev3 host can inject ``MIDU_APP_SECRET`` and ``MIDU_USER_ID``. When
those values are present this helper never starts SMS authentication. The
``~/.midu_keys`` fallback is retained for the standalone first-stage rollout;
the Officev3 connector will replace that persistence path in the next stage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://api.midu.com"
SEND_SMS_URL = f"{BASE_URL}/ability/auth/send/sms"
VERIFY_URL = f"{BASE_URL}/ability/auth/mobile/verify"
PRODUCT_TYPE = 40
SMS_TYPE = "1"
DEFAULT_TIMEOUT = 30
KEYS_FILE = Path.home() / ".midu_keys"
MOBILE_RE = re.compile(r"^1\d{10}$")
APP_SECRET_ENV = "MIDU_APP_SECRET"
USER_ID_ENV = "MIDU_USER_ID"


class MiduAuthError(RuntimeError):
    pass


def _print_json(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def _load_keys_file(path: Path = KEYS_FILE) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            result[key.strip()] = value.strip()
    except OSError:
        return {}
    return result


def load_credentials() -> tuple[str, str, str]:
    """Return secret, user id and source without exposing the secret."""

    env_secret = os.environ.get(APP_SECRET_ENV, "").strip()
    env_user_id = os.environ.get(USER_ID_ENV, "").strip()
    if env_secret and env_user_id and env_user_id != "0":
        return env_secret, env_user_id, "environment"

    stored = _load_keys_file()
    secret = stored.get(APP_SECRET_ENV, "").strip()
    user_id = stored.get(USER_ID_ENV, "").strip()
    if secret and user_id and user_id != "0":
        return secret, user_id, "legacy_file"
    return "", "", "none"


def save_credentials(app_secret: str, user_id: Any, path: Path = KEYS_FILE) -> None:
    """Persist the standalone fallback with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"MIDU_APP_SECRET={app_secret}\nMIDU_USER_ID={user_id}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _safe_service_message(payload: dict[str, Any], fallback: str) -> str:
    value = payload.get("message") or payload.get("msg")
    return str(value).strip() if value else fallback


def _post_form(url: str, data: dict[str, Any], timeout: int) -> dict[str, Any]:
    try:
        response = requests.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MiduAuthError(f"网络请求失败：{exc}") from exc

    try:
        parsed = response.json()
    except ValueError as exc:
        raise MiduAuthError(f"认证服务返回了无效响应（HTTP {response.status_code}）") from exc
    if not isinstance(parsed, dict):
        raise MiduAuthError("认证服务返回格式错误")
    if not response.ok:
        raise MiduAuthError(
            f"认证服务请求失败（HTTP {response.status_code}）："
            f"{_safe_service_message(parsed, '请稍后重试')}"
        )

    code = str(parsed.get("code", ""))
    if code not in ("0000", "0", "200") and parsed.get("success") is not True:
        raise MiduAuthError(_safe_service_message(parsed, "认证失败"))
    return parsed


def send_sms(mobile: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    return _post_form(
        SEND_SMS_URL,
        {"productType": PRODUCT_TYPE, "mobile": mobile, "smsType": SMS_TYPE},
        timeout,
    )


def verify_sms(mobile: str, sms_code: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    return _post_form(
        VERIFY_URL,
        {"productType": PRODUCT_TYPE, "mobile": mobile, "smsCode": sms_code},
        timeout,
    )


def extract_credentials(result: dict[str, Any]) -> tuple[str, str]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    user_vo = result.get("userVo") or data.get("userVo") or {}
    if not isinstance(user_vo, dict):
        user_vo = {}
    app_secret = (
        user_vo.get("appSecret")
        or data.get("appSecret")
        or result.get("appSecret")
        or ""
    )
    user_id = (
        user_vo.get("id")
        or data.get("userId")
        or data.get("id")
        or result.get("userId")
        or result.get("id")
        or ""
    )
    return str(app_secret).strip(), str(user_id).strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蜜度内置 Skill 共用认证")
    parser.add_argument("--action", required=True, choices=["status", "send", "verify"])
    parser.add_argument("--mobile", default="", help="中国大陆手机号；send/verify 时必填")
    parser.add_argument("--sms-code", "--sms_code", dest="sms_code", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.action == "status":
        secret, user_id, source = load_credentials()
        _print_json(
            {
                "configured": bool(secret and user_id),
                "source": source,
                "userId": user_id or None,
            },
            args.pretty,
        )
        return 0

    mobile = args.mobile.strip()
    if not MOBILE_RE.fullmatch(mobile):
        _print_json({"success": False, "error": "请输入有效的 11 位手机号"}, args.pretty)
        return 2
    if args.action == "verify" and not args.sms_code.strip():
        _print_json({"success": False, "error": "verify 时必须提供短信验证码"}, args.pretty)
        return 2

    try:
        if args.action == "send":
            result = send_sms(mobile, args.timeout)
            _print_json(
                {
                    "success": True,
                    "message": _safe_service_message(result, "验证码已发送"),
                },
                args.pretty,
            )
            return 0

        result = verify_sms(mobile, args.sms_code.strip(), args.timeout)
        app_secret, user_id = extract_credentials(result)
        if not app_secret or not user_id or user_id == "0":
            raise MiduAuthError("认证成功，但服务端未返回有效凭证")
        save_credentials(app_secret, user_id)
        _print_json(
            {
                "success": True,
                "configured": True,
                "userId": user_id,
                "message": "登录成功，可以继续执行蜜度能力。",
            },
            args.pretty,
        )
        return 0
    except MiduAuthError as exc:
        _print_json({"success": False, "error": str(exc)}, args.pretty)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
