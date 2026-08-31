"""Shared classification for Midu authentication and entitlement failures."""

from __future__ import annotations

from typing import Any


RECHARGE_HINT = (
    "账号权益不足（余额/次数已用尽）。\n"
    "请告知用户前往 https://ai.mdata.net/pro 充值或开通对应能力后再重试。\n"
    "注意：这不是登录问题，不要重新走登录流程，也不要重复调用本命令。"
)

_NO_ENTITLEMENT_WORDS = (
    "权益不足",
    "余额不足",
    "次数用尽",
    "额度不足",
    "配额",
    "未开通",
    "欠费",
    "insufficient",
    "quota",
    "entitlement",
    "balance",
)

_AUTH_WORDS = (
    "api_key",
    "unauthor",
    "token",
    "credential",
    "鉴权",
    "凭证失效",
    "登录失效",
)


def looks_like_no_entitlement(value: Any) -> bool:
    if not value:
        return False
    text = str(value)
    lower = text.lower()
    return any(word in text or word.lower() in lower for word in _NO_ENTITLEMENT_WORDS)


def looks_like_auth_failure(code: Any, message: Any) -> bool:
    if str(code).strip() in {"401", "403"}:
        return True
    lower = str(message or "").lower()
    return any(word.lower() in lower for word in _AUTH_WORDS)
