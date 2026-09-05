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

import json
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
        "API 密钥无效或未通过鉴权。请确认服务配置。",
    ),
    (
        "permission",
        ("permission_denied", "permissiondenied", "403", "access denied"),
        "当前账号无权访问该模型或接口（403）。请确认账号权限或所选模型是否开通。",
    ),
    (
        "rate_limit",
        ("rate limit", "rate_limit", "too many requests", "429", "tpm", "rpm"),
        "请求过于频繁，已触发服务限流（429）。请稍候片刻再重试。",
    ),
    (
        "quota",
        ("insufficient_quota", "insufficient quota", "exceeded your current quota",
         "insufficient_points", "1000007", "billing", "arrearage", "balance",
         "积分不足", "余额", "欠费"),
        "账户额度不足或欠费，模型已拒绝请求。请充值或检查账单后重试。",
    ),
    (
        "context_length",
        ("context_length_exceeded", "context length", "maximum context",
         "too long", "reduce the length", "max_tokens", "input prompt token len"),
        "当前对话内容过长，超出所选模型可处理的上限。"
        "请新建会话，或精简历史消息、附件和本次输入后重试。",
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
        "当前服务暂时不可用（服务端错误）。请稍后重试。",
    ),
    (
        "timeout",
        ("timeout", "timed out", "deadline"),
        "请求模型服务超时。可能是网络或服务响应缓慢，请重试。",
    ),
    (
        "connection",
        ("connection error", "connection refused", "connection reset",
         "failed to establish", "name resolution", "网络"),
        "无法连接到模型服务。请检查网络或 api_base 地址是否可达。",
    ),
)

_MODEL_CONFIGURATION_MESSAGE = (
    "当前配置的模型不受支持。请检查 model 名称及其与 provider 的兼容性。"
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
        if _looks_like_unsupported_model(haystack):
            return FriendlyError(
                message=_MODEL_CONFIGURATION_MESSAGE,
                category="model_configuration",
            )
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


def extract_llm_error_code(exc: BaseException) -> int | str | None:
    """Extract a structured provider/business error code without parsing prose."""
    try:
        root = _unwrap(exc)
    except Exception:
        root = exc

    try:
        code = _normalize_error_code(getattr(root, "code", None))
        if code is not None:
            return code
    except Exception:
        pass

    try:
        return _error_code_from_body(getattr(root, "body", None))
    except Exception:
        return None


def structured_llm_error(
    exc: BaseException,
    *,
    provider: object = "",
    model: object = "",
) -> dict[str, object]:
    """Return a stable, JSON-serializable provider error envelope."""
    friendly = classify_llm_error(exc)
    normalized_provider = _normalize_identity(provider)
    normalized_model = _normalize_identity(model)
    message = friendly.message
    reason = friendly.category
    if friendly.category == "model_configuration":
        reason = "model_not_supported"
        model_label = f" `{normalized_model}` " if normalized_model else ""
        provider_label = (
            f"当前 `{normalized_provider}` provider"
            if normalized_provider
            else "当前 provider"
        )
        message = (
            f"当前配置的模型{model_label}不受支持。"
            f"请检查 model 名称及其与{provider_label} 的兼容性。"
        )

    root = _safe_unwrap(exc)
    body = _safe_attr(root, "body")
    error_type = _normalize_error_code(_safe_attr(root, "type"))
    if error_type is None:
        error_type = _normalize_error_code(_field_from_body(body, "type"))
    request_id = _normalize_text(_safe_attr(root, "request_id"), limit=256)
    if request_id is None:
        request_id = _normalize_text(_field_from_body(body, "request_id"), limit=256)
    if request_id is None:
        response = _safe_attr(root, "response")
        headers = _safe_attr(response, "headers") if response is not None else None
        try:
            from .debug_logging import request_id_from_headers

            request_id = _normalize_text(request_id_from_headers(headers), limit=256)
        except Exception:
            request_id = None

    try:
        retryable = is_retryable_llm_error(exc)
    except Exception:
        retryable = False

    return {
        "source": "llm_provider",
        "category": friendly.category,
        "reason": reason,
        "code": extract_llm_error_code(exc),
        "type": error_type,
        "httpStatus": _safe_http_status_code(root),
        "provider": normalized_provider or None,
        "model": normalized_model or None,
        "message": message,
        "retryable": retryable,
        "requestId": request_id,
    }


def _normalize_error_code(value: object) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    if normalized.isdecimal():
        try:
            return int(normalized)
        except ValueError:
            return None
    return normalized


def _error_code_from_body(body: object) -> int | str | None:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            return None
    if not isinstance(body, dict):
        return None

    code = _normalize_error_code(body.get("code"))
    if code is not None:
        return code
    nested = body.get("error")
    if nested is body:
        return None
    return _error_code_from_body(nested)


# Error categories that a retry cannot fix — deterministic client-side faults.
# (Mirrors the classifier categories in ``_RULES``.)
_NON_RETRYABLE_CATEGORIES: frozenset[str] = frozenset({
    "content_filter",
    "auth",
    "permission",
    "quota",
    "context_length",
    "model_not_found",
    "model_configuration",
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


def _safe_http_status_code(exc: BaseException) -> int | None:
    try:
        return _http_status_code(exc)
    except Exception:
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


def _safe_attr(obj: object, name: str) -> object | None:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _safe_unwrap(exc: BaseException) -> BaseException:
    try:
        return _unwrap(exc)
    except Exception:
        return exc


def _normalize_identity(value: object) -> str:
    try:
        enum_value = getattr(value, "value", value)
    except Exception:
        enum_value = value
    return _normalize_text(enum_value, limit=256) or ""


def _normalize_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        return None
    return normalized


def _field_from_body(body: object, field: str) -> object | None:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            return None
    if not isinstance(body, dict):
        return None
    value = body.get(field)
    if value is not None:
        return value
    nested = body.get("error")
    if nested is body:
        return None
    return _field_from_body(nested, field)


def _looks_like_unsupported_model(haystack: str) -> bool:
    normalized = " ".join(haystack.split())
    explicit_unsupported = "unsupported model" in normalized or (
        "model " in normalized and " is not supported" in normalized
    )
    supported_names_mismatch = (
        (
            "supported api model names are" in normalized
            or "supported model names are" in normalized
        )
        and "but you passed" in normalized
    )
    return explicit_unsupported or supported_names_mismatch


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
