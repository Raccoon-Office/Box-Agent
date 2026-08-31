#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midu 拼音注音工具

调用拼音注音接口，对中文文本逐字注音，返回三种展示形态：
  - rubyHtml：HTML ruby 注音（拼音在上、汉字在下），可直接渲染
  - annotatedText：汉字与拼音对照，如 春(chūn) 天(tiān)
  - pinyinText：纯拼音，空格分隔

展示顺序固定为 rubyHtml → annotatedText → pinyinText（见 SKILL.md 展示规则）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile

from dataclasses import dataclass
from html import escape as _escape
from html.parser import HTMLParser
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


API_URL = "https://api.midu.com/ability/skill/jdt/phon/annot"
DEFAULT_HTML_FILENAME = "pinyin_preview.html"
SKILL_CODE = "JDT_PHON_ANNOT"

# 缺凭证 / 鉴权失效时统一引导走短信授权，禁止再引导去官网取 Key
AUTH_HINT = build_auth_hint()

class MiduPinyinError(RuntimeError):
    pass


def load_business_credentials() -> tuple[str, str, str]:
    """成对读取宿主注入或独立 Box-Agent 保存的凭证。"""
    api_key, user_id, source = load_credentials()
    if api_key and user_id:
        return api_key, user_id, source
    raise MiduPinyinError(AUTH_HINT)


def _http_post_json(
    url: str,
    body: Dict[str, Any],
    headers: Dict[str, str],
    credential_source: str,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout_s)
    except requests.RequestException as exc:
        raise MiduPinyinError(f"网络请求失败：{exc}") from exc
    if r.status_code in (401, 403):
        if looks_like_no_entitlement(r.text):
            raise MiduPinyinError(RECHARGE_HINT)
        raise MiduPinyinError(
            f"鉴权失败或凭证失效（HTTP {r.status_code}）。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )
    if not r.ok:
        if looks_like_no_entitlement(r.text):
            raise MiduPinyinError(RECHARGE_HINT)
        raise MiduPinyinError(f"接口 HTTP 错误: {r.status_code} {r.text}".strip())
    try:
        return r.json()
    except ValueError as exc:
        raise MiduPinyinError(f"响应不是合法 JSON：{r.text}") from exc


@dataclass(frozen=True)
class PinyinResult:
    ruby_html: str
    annotated_text: str
    pinyin_text: str
    transaction_id: str
    char_count: Optional[int]


def call_pinyin_api(
    text: str,
    api_key: str,
    user_id: str,
    credential_source: str,
) -> PinyinResult:
    headers = {
        "Content-Type": "application/json",
        "X-Skill-Code": SKILL_CODE,
        "Authorization": f"Bearer {api_key}",
        "X-User-Id": user_id,
    }
    params: Dict[str, Any] = {
        "text": text,
    }
    resp = _http_post_json(
        API_URL,
        params,
        headers=headers,
        credential_source=credential_source,
        timeout_s=60,
    )

    code = resp.get("code")
    msg = resp.get("msg", "")
    txid = resp.get("transactionId", "")
    char_count = resp.get("charCount")
    if str(code).strip() != "0000":
        if looks_like_no_entitlement(msg) or looks_like_no_entitlement(code):
            raise MiduPinyinError(RECHARGE_HINT)
        raw = json.dumps(resp, ensure_ascii=False)
        raise MiduPinyinError(f"接口调用失败: code={code}, msg={msg}\n接口返回: {raw}")

    data = resp.get("data") or {}
    ruby_html = data.get("rubyHtml")
    annotated_text = data.get("annotatedText")
    pinyin_text = data.get("pinyinText")
    if ruby_html is None or annotated_text is None or pinyin_text is None:
        raise MiduPinyinError(f"响应缺少必要字段(rubyHtml/annotatedText/pinyinText): {json.dumps(resp, ensure_ascii=False)}")

    return PinyinResult(
        ruby_html=str(ruby_html),
        annotated_text=str(annotated_text),
        pinyin_text=str(pinyin_text),
        transaction_id=str(txid),
        char_count=int(char_count) if isinstance(char_count, (int, float)) else None,
    )


def annotate_text(text: str) -> PinyinResult:
    if not text.strip():
        raise MiduPinyinError("text 不能为空。")
    key, uid, credential_source = load_business_credentials()
    return call_pinyin_api(
        text=text,
        api_key=key,
        user_id=uid,
        credential_source=credential_source,
    )


# 预览页样式：每个 <ruby> 包成 inline-block 并居中，相邻字之间留白，
# 避免拼音比汉字宽时挤成一片（这是 ruby 默认排版最常见的“糊在一起”问题）。
_PREVIEW_CSS = """
 body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      max-width:900px;margin:40px auto;padding:0 24px;color:#222;background:#fff}
 h2{color:#444;font-size:18px;border-left:4px solid #c0392b;padding-left:10px;margin:34px 0 14px}
 .meta{color:#999;font-size:13px;margin-bottom:8px}
 .ruby-box{font-size:26px;line-height:2.7}
 .ruby-box ruby{margin:0 3px;ruby-align:center}
 .ruby-box rt{font-size:14px;color:#c0392b;font-weight:500;text-align:center}
 .plain{font-size:17px;line-height:2.2;color:#333;white-space:pre-wrap;word-break:break-word}
 .pinyin{font-size:15px;line-height:2.2;color:#16a085;word-break:break-all}
"""

_ALLOWED_RUBY_TAGS = {"ruby", "rt", "rp"}


class _RubyHtmlSanitizer(HTMLParser):
    """Rebuild provider HTML with only ruby annotation tags and plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in _ALLOWED_RUBY_TAGS:
            self.parts.append(f"<{tag.lower()}>")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() in _ALLOWED_RUBY_TAGS:
            self.parts.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _ALLOWED_RUBY_TAGS:
            self.parts.append(f"</{tag.lower()}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(_escape(data))


def sanitize_ruby_html(value: str) -> str:
    sanitizer = _RubyHtmlSanitizer()
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.parts)


def render_preview_html(result: PinyinResult) -> str:
    """生成统一样式的注音预览页：①ruby 效果 ②汉字拼音对照 ③纯拼音。

    rubyHtml 仅保留 ruby/rt/rp 标签和纯文本；对照/纯拼音转义后嵌入。
    """
    meta = f"transactionId: {result.transaction_id} ｜ charCount: {result.char_count}"
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<title>拼音注音预览</title><style>" + _PREVIEW_CSS + "</style></head><body>"
        f"<div class=\"meta\">{_escape(meta)}</div>"
        "<h2>① 注音效果（拼音在上·汉字在下）</h2>"
        f"<div class=\"ruby-box\">{sanitize_ruby_html(result.ruby_html)}</div>"
        "<h2>② 汉字拼音对照</h2>"
        f"<div class=\"plain\">{_escape(result.annotated_text)}</div>"
        "<h2>③ 纯拼音</h2>"
        f"<div class=\"pinyin\">{_escape(result.pinyin_text)}</div>"
        "</body></html>"
    )


def _default_temp_dir() -> Path:
    """默认临时目录：mac 用 /tmp，其它系统（Windows 无 /tmp）用 tempfile.gettempdir()。"""
    if sys.platform == "darwin":
        return Path("/tmp")
    return Path(tempfile.gettempdir())


def _default_preview_path(result: PinyinResult) -> Path:
    """默认预览页路径：临时目录下，文件名带 transactionId 避免多次调用互相覆盖。"""
    stem = Path(DEFAULT_HTML_FILENAME).stem
    suffix = Path(DEFAULT_HTML_FILENAME).suffix
    tid = result.transaction_id
    name = f"{stem}_{tid}{suffix}" if tid else DEFAULT_HTML_FILENAME
    return _default_temp_dir() / name


def write_preview_html(result: PinyinResult, out_path: Optional[str] = None) -> str:
    """写出预览页，返回绝对路径。out_path 为空时写入系统临时目录（跨平台兼容）。"""
    path = Path(out_path).expanduser() if out_path else _default_preview_path(result)
    path.write_text(render_preview_html(result), encoding="utf-8")
    return str(path.resolve())


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Midu 拼音注音：调用接口并按 rubyHtml→对照→纯拼音 顺序展示")
    p.add_argument("--text", required=True, help="待注音文本（直接传入）")
    p.add_argument(
        "--html-out",
        dest="html_out",
        default=None,
        help="注音预览页输出路径；默认写入系统临时目录（mac/Windows 通用），文件名带 transactionId",
    )
    p.add_argument("--no-html", dest="no_html", action="store_true", help="不生成预览页")
    return p


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)

    try:
        result = annotate_text(args.text)

        print("✅ 拼音注音完成")
        print(f"- transactionId: {result.transaction_id}")
        if result.char_count is not None:
            print(f"- charCount: {result.char_count}")

        if not args.no_html:
            preview_path = write_preview_html(result, args.html_out)
            print("\n========== 注音预览页（请展示此路径给用户） ==========")
            print(f"预览页绝对路径: {preview_path}")
            print("用浏览器打开即可查看「拼音在上·汉字在下」的真实渲染效果。")
            print("=====================================================")

        # 注意：不再打印 rubyHtml 源码——HTML 标签文本对用户无价值，
        # 真实效果请看上面的预览页路径，避免智能体把源码贴给用户。
        print("\n## 汉字拼音对照\n")
        print(result.annotated_text)
        print("\n## 纯拼音\n")
        print(result.pinyin_text)
        return 0
    except MiduPinyinError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
