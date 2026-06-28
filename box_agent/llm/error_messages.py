"""Translate raw LLM provider exceptions into friendly, user-facing messages.

Providers surface failures as opaque exceptions whose ``str()`` is often a raw
JSON blob, e.g.::

    Error code: 400 - {'error': {'code': 'content_filter', 'message': ...}}

Dumping that straight to the user is unfriendly. ``humanize_llm_error`` maps the
common, actionable failure classes (content moderation, auth, rate limit, quota,
context-length, endpoint 404, server errors) to a short Chinese sentence, and
falls back to a trimmed raw message for anything unrecognized.

Detection is best-effort and SDK-agnostic: we never hard-import openai/anthropic
(they're optional providers), so we inspect attributes if present and otherwise
pattern-match on the lowercased string form.
"""

from __future__ import annotations

from typing import NamedTuple


class FriendlyError(NamedTuple):
    """A humanized error.

    ``message``  – short user-facing text.
    ``category`` – stable tag for logging/metrics.
    ``is_soft``  – True when this is really a *model refusal* (e.g. content
        moderation), not a system failure. Soft errors should be shown as a
        normal assistant reply (no "Error:" prefix, no red), because to the user
        the model simply declined to answer — the turn ended normally.
    """

    message: str
    category: str
    is_soft: bool = False


# Categories that are model refusals rather than system failures.
_SOFT_CATEGORIES: frozenset[str] = frozenset({"content_filter"})


# Ordered list of (category, substring-tokens, friendly-message). First match wins,
# so put more specific categories before generic ones.
_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "content_filter",
        ("content_filter", "content filter", "content management policy",
         "data_inspection_failed", "risk_control", "inappropriate", "flagged"),
        "抱歉，这个问题我不了解相关信息。请换个问题吧，我将继续努力为您解答。",
    ),
    (
        "auth",
        ("invalid api key", "invalid_api_key", "incorrect api key", "authentication",
         "unauthorized", "401"),
        "API 密钥无效或未通过鉴权。请运行 `box-agent setup` 检查 api_key 与 api_base 配置。",
    ),
    (
        "permission",
        ("permission_denied", "permissiondenied", "403", "access denied"),
        "当前账号无权访问该模型或接口（403）。请确认账号权限或所选模型是否开通。",
    ),
    (
        "rate_limit",
        ("rate limit", "rate_limit", "too many requests", "429", "tpm", "rpm"),
        "请求过于频繁，已触发服务商限流（429）。请稍候片刻再重试。",
    ),
    (
        "quota",
        ("insufficient_quota", "insufficient quota", "exceeded your current quota",
         "billing", "arrearage", "balance", "余额", "欠费"),
        "账户额度不足或欠费，模型服务商已拒绝请求。请充值或检查账单后重试。",
    ),
    (
        "context_length",
        ("context_length_exceeded", "context length", "maximum context",
         "too long", "reduce the length", "max_tokens"),
        "对话内容超出模型上下文长度上限。请使用 /clear 清空历史或精简输入后重试。",
    ),
    (
        "model_not_found",
        ("model_not_found", "model not found", "does not exist", "no such model",
         "unknown model"),
        "指定的模型不存在或当前账号不可用。请在配置中确认 model 名称是否正确。",
    ),
    (
        "endpoint_not_found",
        ("404 page not found", "404 not found", "page not found", "not found: /"),
        "模型接口返回 404。通常是 api_base 路径错误，或 provider 协议与接口不匹配；"
        "如果 api_base 是小浣熊默认接口 xiaohuanxiong.com/api/web/llm/v2，"
        "请使用 provider: openai，不要使用 anthropic。",
    ),
    (
        "server_error",
        ("internal server error", "internal_error", "500", "502", "503", "504",
         "bad gateway", "service unavailable", "overloaded", "server_error"),
        "模型服务商暂时不可用（服务端错误）。这是对方临时故障，请稍后重试。",
    ),
    (
        "timeout",
        ("timeout", "timed out", "deadline"),
        "请求模型服务超时。可能是网络或服务商响应缓慢，请重试。",
    ),
    (
        "connection",
        ("connection error", "connection refused", "connection reset",
         "failed to establish", "name resolution", "网络"),
        "无法连接到模型服务。请检查网络或 api_base 地址是否可达。",
    ),
)

# Max length for the raw-string fallback so we never dump a huge blob.
_RAW_FALLBACK_LIMIT = 300

# Last-resort message when even our own parsing/inspection blows up. The whole
# point of this module is to never let error-handling code raise, so this is the
# floor we always fall back to.
_GENERIC_FALLBACK = "模型调用失败，请稍后重试。"


def classify_llm_error(exc: BaseException) -> FriendlyError:
    """Return a :class:`FriendlyError` for ``exc``.

    Unwraps wrapper exceptions (``RetryExhaustedError`` / ``StreamInterrupted``)
    that carry a ``last_exception`` so the underlying provider error is inspected.

    Bulletproof by contract: provider exceptions come in unpredictable shapes and
    this runs *inside* an error-handling path, so any failure here would mask the
    original error with a confusing traceback. Every step is defensive and the
    function never raises — worst case it returns the generic fallback.
    """
    try:
        root = _unwrap(exc)
    except Exception:
        root = exc

    try:
        haystack = _build_haystack(root).lower()
    except Exception:
        haystack = ""

    try:
        for category, tokens, message in _RULES:
            if any(tok in haystack for tok in tokens):
                return FriendlyError(
                    message=message,
                    category=category,
                    is_soft=category in _SOFT_CATEGORIES,
                )
    except Exception:
        return FriendlyError(message=_GENERIC_FALLBACK, category="unknown")

    # Fallback: trimmed raw message, no JSON-dict noise if we can help it.
    try:
        raw = (_safe_str(root) or type(root).__name__).strip()
        if not raw:
            return FriendlyError(message=_GENERIC_FALLBACK, category="unknown")
        if len(raw) > _RAW_FALLBACK_LIMIT:
            raw = raw[:_RAW_FALLBACK_LIMIT].rstrip() + "…"
        return FriendlyError(message=f"模型调用失败：{raw}", category="unknown")
    except Exception:
        return FriendlyError(message=_GENERIC_FALLBACK, category="unknown")


def humanize_llm_error(exc: BaseException) -> str:
    """Return just the friendly user-facing message string for ``exc``.

    Never raises — falls back to a generic message if anything goes wrong.
    """
    try:
        return classify_llm_error(exc).message
    except Exception:
        return _GENERIC_FALLBACK


# Error categories that a retry cannot fix — deterministic client-side faults.
# (Mirrors the classifier categories in ``_RULES``.)
_NON_RETRYABLE_CATEGORIES: frozenset[str] = frozenset({
    "content_filter",
    "auth",
    "permission",
    "quota",
    "context_length",
    "model_not_found",
    "endpoint_not_found",
})

# Categories that represent transient server/network conditions worth retrying.
_RETRYABLE_CATEGORIES: frozenset[str] = frozenset({
    "rate_limit",
    "server_error",
    "timeout",
    "connection",
})


def _http_status_code(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from a provider exception."""
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int):
            return val
    return None


def is_retryable_llm_error(exc: BaseException) -> bool:
    """Whether a (non-streaming) LLM call error is worth retrying.

    Retries transient conditions — 429 rate limits, 5xx server errors, request
    timeouts, and network/connection drops — but fails fast on deterministic
    client errors (400/401/403/404, content filter, quota, context length,
    unknown model, wrong endpoint) that retrying cannot fix.

    Distinct from :func:`is_retryable_stream_error`, which only recognizes
    network/stream-transport faults: that one is too narrow for the non-stream
    path, where the provider returns a structured 429/5xx HTTP error rather than
    dropping the connection. Never raises.
    """
    try:
        root = _unwrap(exc)
    except Exception:
        root = exc

    # HTTP status is the most precise signal, so check it FIRST. A structured
    # 4xx must fail fast even when its message happens to contain a
    # transient-looking substring (e.g. a 400 whose body mentions "connection
    # reset") — otherwise the string-matching predicates below would wrongly
    # flag it as retryable.
    try:
        status = _http_status_code(root)
    except Exception:
        status = None
    if status is not None:
        if status == 429 or status == 408 or 500 <= status <= 599:
            return True
        if 400 <= status < 500:
            return False

    # No usable status: fall back to transport-level transients (httpx/openai
    # network classes that drop the connection without exposing a status code).
    try:
        from ..retry import is_retryable_stream_error

        if is_retryable_stream_error(exc):
            return True
    except Exception:
        pass

    # ...then message-token classification.
    try:
        category = classify_llm_error(exc).category
    except Exception:
        category = "unknown"
    if category in _NON_RETRYABLE_CATEGORIES:
        return False
    if category in _RETRYABLE_CATEGORIES:
        return True
    # Unknown / unclassified: assume transient and allow the retry budget to
    # apply (restores pre-fail-fast behavior for errors we can't categorize).
    return True


def _safe_str(obj: object) -> str:
    """``str(obj)`` that never raises (some exception objects have broken ``__str__``)."""
    try:
        return str(obj)
    except Exception:
        try:
            return repr(obj)
        except Exception:
            return ""


def _unwrap(exc: BaseException) -> BaseException:
    """Follow ``last_exception`` chains down to the underlying provider error."""
    seen: set[int] = set()
    cur = exc
    while True:
        inner = getattr(cur, "last_exception", None)
        if inner is None or id(inner) in seen or inner is cur:
            return cur
        seen.add(id(cur))
        cur = inner


def _build_haystack(exc: BaseException) -> str:
    """Collect searchable text from common SDK exception attributes + str().

    Defensive: any attribute may be a property that raises, or an object whose
    ``str()`` raises. Each piece is collected independently so one bad attribute
    never sinks the whole classification.
    """
    parts: list[str] = [_safe_str(exc)]
    try:
        parts.append(type(exc).__name__)
    except Exception:
        pass
    for attr in ("code", "type", "param"):
        try:
            val = getattr(exc, attr, None)
        except Exception:
            continue
        if isinstance(val, str):
            parts.append(val)
    try:
        status = getattr(exc, "status_code", None)
        if status is not None:
            parts.append(_safe_str(status))
    except Exception:
        pass
    try:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            parts.append(_safe_str(body))
    except Exception:
        pass
    return " ".join(p for p in parts if p)
