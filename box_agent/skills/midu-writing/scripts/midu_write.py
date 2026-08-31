#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midu 写作工具（短信授权版）

调用写作接口，支持多轮对话（thread_id）。
鉴权用短信登录得到的 appSecret（MIDU_APP_SECRET）+ userId（MIDU_USER_ID）；
请求头必须带 Authorization / X-Skill-Code / X-User-Id。
缺凭证时引导走共享短信认证脚本，禁止跳转官网取 Key。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import requests


_SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from _midu_shared.auth_hint import build_auth_hint  # noqa: E402
from _midu_shared.midu_auth import load_credentials  # noqa: E402
from _midu_shared.service_errors import (  # noqa: E402
    RECHARGE_HINT,
    looks_like_auth_failure,
    looks_like_no_entitlement,
)


if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


API_URL = "https://api.midu.com/ability/skill/write/info"
DEFAULT_TIMEOUT = 600
SKILL_CODE = "MIDU-WRITING"

AUTH_HINT = build_auth_hint()


class MiduWriteError(RuntimeError):
    pass


def load_business_credentials() -> tuple[str, str, str]:
    """成对读取宿主注入或独立 Box-Agent 保存的凭证。"""
    api_key, user_id, source = load_credentials()
    if api_key and user_id:
        return api_key, user_id, source
    raise MiduWriteError(AUTH_HINT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="调用蜜度写作 API（短信授权版），支持多轮对话。"
    )
    parser.add_argument(
        "--user_input",
        required=True,
        help="写作指令或补充要求（必填）。",
    )
    parser.add_argument(
        "--thread_id",
        default="",
        help="多轮对话时传入上一次返回的 thread_id；首次可省略，脚本自动生成。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP 超时秒数，默认 {DEFAULT_TIMEOUT}。",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="格式化输出 JSON。",
    )
    return parser.parse_args()


def normalize_thread_id(thread_id: str) -> str:
    """校验并返回合法的 UUID thread_id；为空时自动生成。"""
    if thread_id:
        try:
            return str(uuid.UUID(thread_id))
        except ValueError as exc:
            raise ValueError(f"thread_id 格式不合法（需为 UUID）：{thread_id}") from exc
    return str(uuid.uuid4())


def build_payload(user_input: str, thread_id: str) -> dict[str, str]:
    user_input = user_input.strip().lstrip("\ufeff")
    if not user_input:
        raise ValueError("--user_input 不能为空。")
    return {"thread_id": thread_id, "user_input": user_input}


def build_headers(api_key: str, user_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Skill-Code": SKILL_CODE,
        "X-User-Id": user_id,
    }


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: int,
    api_key: str,
    user_id: str,
    credential_source: str,
) -> dict[str, Any]:
    try:
        response = requests.post(
            url,
            json=payload,
            headers=build_headers(api_key, user_id),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MiduWriteError(f"网络请求失败：{exc}") from exc

    if response.status_code in (401, 403):
        if looks_like_no_entitlement(response.text):
            raise MiduWriteError(RECHARGE_HINT)
        raise MiduWriteError(
            f"鉴权失败或凭证失效（HTTP {response.status_code}）。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )
    if response.status_code == 429:
        raise MiduWriteError("请求过于频繁（HTTP 429）。请稍等片刻后重试。")
    if response.status_code >= 500:
        raise MiduWriteError(
            f"服务端错误（HTTP {response.status_code}）。服务暂时不可用，请稍后重试。"
        )
    if not response.ok:
        if looks_like_no_entitlement(response.text):
            raise MiduWriteError(RECHARGE_HINT)
        raise MiduWriteError(f"HTTP {response.status_code}：{response.text}")

    try:
        parsed = response.json()
    except ValueError as exc:
        raise MiduWriteError(f"响应不是合法 JSON：{response.text}") from exc

    if not isinstance(parsed, dict):
        raise MiduWriteError(f"响应 JSON 格式异常（非 object）：{parsed!r}")

    error_value = parsed.get("error")
    message = error_value or parsed.get("message") or parsed.get("msg") or ""
    code = parsed.get("code")
    if looks_like_no_entitlement(message) or looks_like_no_entitlement(code):
        raise MiduWriteError(RECHARGE_HINT)

    if error_value:
        error_text = str(error_value)
        if looks_like_auth_failure(code, error_text):
            raise MiduWriteError(
                "鉴权失败。\n"
                f"{build_auth_hint(credential_source=credential_source)}"
            )
        raise MiduWriteError(f"API 返回错误：{error_text}")

    if str(code).strip() not in {"", "0000", "0", "200"} and looks_like_auth_failure(
        code, message
    ):
        raise MiduWriteError(
            "鉴权失败。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )

    return parsed


def print_result(data: dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False))


def print_error(message: str, pretty: bool) -> int:
    print_result({"error": message}, pretty)
    return 1


def main() -> int:
    args = parse_args()

    try:
        api_key, user_id, credential_source = load_business_credentials()
        thread_id = normalize_thread_id(args.thread_id)
        payload = build_payload(args.user_input, thread_id)
        response = post_json(
            API_URL,
            payload,
            args.timeout,
            api_key,
            user_id,
            credential_source,
        )
    except Exception as exc:
        return print_error(str(exc), args.pretty)

    output = dict(response)
    output["thread_id"] = thread_id

    print_result(output, args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
