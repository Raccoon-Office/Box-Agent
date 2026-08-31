#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midu 智能校对工具（prod · 短信授权版）

调用现网校对接口对文本进行校对，展示校对状态和结果链接，不自动下载服务端返回的 URL。
鉴权用短信登录得到的 appSecret（MIDU_APP_SECRET）+ userId（MIDU_USER_ID）；请求头必须带 X-User-Id。
缺凭证时不引导去官网取 Key，而是引导走共享短信认证脚本。
用 requests 直连（TLS 默认校验）。
"""

from __future__ import annotations

import argparse
import os
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


API_URL = "https://api.midu.com/ability/skill/jdt/proof/read"
SKILL_CODE = "JDT_PROOF"

# 缺凭证 / 鉴权失效时统一引导走短信授权，禁止再引导去官网取 Key
AUTH_HINT = build_auth_hint()

class MiduProofreadError(RuntimeError):
    pass


def load_business_credentials() -> tuple[str, str, str]:
    """成对读取宿主注入或独立 Box-Agent 保存的凭证。"""
    api_key, user_id, source = load_credentials()
    if api_key and user_id:
        return api_key, user_id, source
    raise MiduProofreadError(AUTH_HINT)


@dataclass(frozen=True)
class ProofreadUrls:
    proof_json_url: str
    erratum_excel_url: str
    erratum_md_url: str
    transaction_id: str
    char_count: Optional[int]


@dataclass(frozen=True)
class ProofreadOutputs:
    urls: ProofreadUrls
    erratum_md: str


def call_proofread_api(
    proof_text: str,
    api_key: str,
    user_id: str,
    credential_source: str,
    file_name: Optional[str] = None,
) -> ProofreadUrls:
    headers = {
        "Content-Type": "application/json",
        "X-Skill-Code": SKILL_CODE,
        "Authorization": f"Bearer {api_key}",
        "X-User-Id": user_id,
    }
    params: Dict[str, Any] = {"proofText": proof_text}
    if file_name and file_name.strip():
        params["fileName"] = os.path.basename(file_name.strip())

    try:
        resp = requests.post(API_URL, json=params, headers=headers, timeout=60)
    except requests.RequestException as exc:
        raise MiduProofreadError(f"网络请求失败：{exc}") from exc

    if resp.status_code in (401, 403):
        if looks_like_no_entitlement(resp.text):
            raise MiduProofreadError(RECHARGE_HINT)
        raise MiduProofreadError(
            f"鉴权失败或凭证失效（HTTP {resp.status_code}）。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )
    if not resp.ok:
        if looks_like_no_entitlement(resp.text):
            raise MiduProofreadError(RECHARGE_HINT)
        raise MiduProofreadError(f"接口 HTTP 错误: {resp.status_code} {resp.text}".strip())

    try:
        data_all = resp.json()
    except ValueError as exc:
        raise MiduProofreadError(f"响应不是合法 JSON：{resp.text}") from exc

    code = data_all.get("code")
    msg = data_all.get("msg", "")
    txid = data_all.get("transactionId", "")
    char_count = data_all.get("charCount")
    if str(code).strip() != "0000":
        if looks_like_no_entitlement(msg) or looks_like_no_entitlement(code):
            raise MiduProofreadError(RECHARGE_HINT)
        raise MiduProofreadError(
            f"接口调用失败: code={code}, msg={msg}" + (f", transactionId={txid}" if txid else "")
        )

    data = data_all.get("data") or {}
    proof_json_url = data.get("proofResultJsonUrl")
    excel_url = data.get("erratumExcelUrl")
    md_url = data.get("erratumMdUrl")
    if not (proof_json_url and excel_url and md_url):
        raise MiduProofreadError(f"响应缺少必要 URL 字段: {data_all}")

    return ProofreadUrls(
        proof_json_url=str(proof_json_url),
        erratum_excel_url=str(excel_url),
        erratum_md_url=str(md_url),
        transaction_id=str(txid),
        char_count=int(char_count) if isinstance(char_count, (int, float)) else None,
    )


def proofread_text(
    proof_text: str,
    file_name: Optional[str] = None,
) -> ProofreadOutputs:
    if not proof_text.strip():
        raise MiduProofreadError("proofText 不能为空。")

    key, uid, credential_source = load_business_credentials()
    urls = call_proofread_api(
        proof_text=proof_text,
        api_key=key,
        user_id=uid,
        credential_source=credential_source,
        file_name=file_name,
    )

    return ProofreadOutputs(urls=urls, erratum_md="")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Midu 智能校对（prod·短信授权版）：调用接口并展示 Markdown（不落盘下载）")
    p.add_argument("--text", required=True, help="待校对文本（直接传入）")
    p.add_argument(
        "--file-name",
        dest="file_name",
        default=None,
        help="来源文件名（仅 basename，可选）；当正文来自图片/PDF/Word 等文件抽取时建议传入",
    )
    return p


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)

    try:
        outputs = proofread_text(
            args.text,
            file_name=args.file_name,
        )

        print("✅ 校对完成")
        print(f"- transactionId: {outputs.urls.transaction_id}")
        if outputs.urls.char_count is not None:
            print(f"- charCount: {outputs.urls.char_count}")
        print("\n## 勘误结果\n")
        print(
            "> 校对已完成。为避免自动访问服务端返回的任意地址，"
            "脚本未下载勘误 Markdown，请使用下方结果链接。"
        )
        print("\n## 下载链接\n")
        print(f"- 校对结果 JSON: {outputs.urls.proof_json_url}")
        print(f"- 勘误表 Excel: {outputs.urls.erratum_excel_url}")
        print(f"- 勘误 Markdown: {outputs.urls.erratum_md_url}")
        return 0
    except MiduProofreadError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
