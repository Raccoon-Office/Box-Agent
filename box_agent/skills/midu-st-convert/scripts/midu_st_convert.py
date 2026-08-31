#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midu 简繁转换工具（prod）

调用接口对文本做简体/繁体互转，直接返回并展示转换后的文本。

convertType:
  1 = 繁体转简体
  2 = 简体转繁体
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests


_SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from _midu_shared.auth_hint import build_auth_hint  # noqa: E402
from _midu_shared.midu_auth import load_credentials  # noqa: E402
from _midu_shared.service_errors import (  # noqa: E402
    RECHARGE_HINT,
    looks_like_no_entitlement,
)


API_URL = "https://api.midu.com/ability/skill/jdt/tradSimp/convert"
SKILL_CODE = "JDT_TRAD_SIMP_CONVERT"

# convertType 取值与方向说明
CONVERT_TRAD_TO_SIMP = 1  # 繁体 -> 简体
CONVERT_SIMP_TO_TRAD = 2  # 简体 -> 繁体
CONVERT_TYPE_DESC = {
    CONVERT_TRAD_TO_SIMP: "繁体转简体",
    CONVERT_SIMP_TO_TRAD: "简体转繁体",
}

AUTH_HINT = build_auth_hint()

class MiduStConvertError(RuntimeError):
    pass


def load_business_credentials() -> tuple[str, str, str]:
    """成对读取宿主注入或独立 Box-Agent 保存的凭证。"""
    api_key, user_id, source = load_credentials()
    if api_key and user_id:
        return api_key, user_id, source
    raise MiduStConvertError(AUTH_HINT)


@dataclass(frozen=True)
class StConvertResult:
    converted_text: str
    convert_type: int
    transaction_id: str
    char_count: Optional[int]


def call_st_convert_api(
    text: str,
    convert_type: int,
    api_key: str,
    user_id: str,
    credential_source: str,
) -> StConvertResult:
    headers = {
        "Content-Type": "application/json",
        "X-Skill-Code": SKILL_CODE,
        "Authorization": f"Bearer {api_key}",
        "X-User-Id": user_id,
    }
    params: Dict[str, Any] = {
        "convertType": convert_type,
        "text": text,
    }

    try:
        r = requests.post(API_URL, json=params, headers=headers, timeout=60)
    except requests.RequestException as exc:
        raise MiduStConvertError(f"网络请求失败：{exc}") from exc

    if r.status_code in (401, 403):
        if looks_like_no_entitlement(r.text):
            raise MiduStConvertError(RECHARGE_HINT)
        raise MiduStConvertError(
            f"鉴权失败或凭证失效（HTTP {r.status_code}）。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )
    if not r.ok:
        if looks_like_no_entitlement(r.text):
            raise MiduStConvertError(RECHARGE_HINT)
        raise MiduStConvertError(f"接口 HTTP 错误: {r.status_code} {r.text}".strip())

    try:
        resp = r.json()
    except ValueError as exc:
        raise MiduStConvertError(f"响应不是合法 JSON：{r.text}") from exc

    code = resp.get("code")
    msg = resp.get("msg", "")
    txid = resp.get("transactionId", "")
    char_count = resp.get("charCount")
    if str(code).strip() != "0000":
        if looks_like_no_entitlement(msg) or looks_like_no_entitlement(code):
            raise MiduStConvertError(RECHARGE_HINT)
        raise MiduStConvertError(
            f"接口调用失败: code={code}, msg={msg}" + (f", transactionId={txid}" if txid else "")
        )

    data = resp.get("data") or {}
    converted = data.get("convertedText")
    if converted is None:
        raise MiduStConvertError(f"响应缺少 convertedText 字段: {resp}")

    return StConvertResult(
        converted_text=str(converted),
        convert_type=convert_type,
        transaction_id=str(txid),
        char_count=int(char_count) if isinstance(char_count, (int, float)) else None,
    )


def convert_text(
    text: str,
    convert_type: int,
) -> StConvertResult:
    if not text.strip():
        raise MiduStConvertError("text 不能为空。")
    if convert_type not in CONVERT_TYPE_DESC:
        raise MiduStConvertError(
            f"convertType 非法: {convert_type}（仅支持 1=繁转简 / 2=简转繁）。"
        )

    key, uid, credential_source = load_business_credentials()
    return call_st_convert_api(
        text=text,
        convert_type=convert_type,
        api_key=key,
        user_id=uid,
        credential_source=credential_source,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Midu 简繁转换：调用接口并展示转换后的文本")
    p.add_argument("--text", required=True, help="待转换文本（直接传入）")
    p.add_argument(
        "--convert-type",
        dest="convert_type",
        type=int,
        required=True,
        choices=[CONVERT_TRAD_TO_SIMP, CONVERT_SIMP_TO_TRAD],
        help="转换方向：1=繁体转简体，2=简体转繁体",
    )
    return p


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)

    try:
        result = convert_text(
            args.text,
            convert_type=args.convert_type,
        )

        print("✅ 转换完成")
        print(f"- 方向: {CONVERT_TYPE_DESC[result.convert_type]}（convertType={result.convert_type}）")
        print(f"- transactionId: {result.transaction_id}")
        if result.char_count is not None:
            print(f"- charCount: {result.char_count}")
        print("\n## 转换结果\n")
        print(result.converted_text)
        return 0
    except MiduStConvertError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
