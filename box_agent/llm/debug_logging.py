"""Debug logging helpers for provider-level LLM HTTP requests.

The normal ``AgentLogger`` records the agent's internal message list.  These
helpers record compact SDK request metadata and response ids for vendor support,
while redacting authentication headers and token-like values.  Full request
payload logging is available behind an explicit environment flag.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "box_agent_llm_debug_sink", default=None
)

_TRUTHY = {"1", "true", "yes", "on", "debug"}
_FALSY = {"0", "false", "no", "off", ""}
_REDACTED = "<redacted>"
_SENSITIVE_KEYS = {
    "api-key",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "proxy-authorization",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}
_REQUEST_TEXT_PREVIEW_CHARS = 320
_REQUEST_ARGUMENT_PREVIEW_CHARS = 240
_REQUEST_LIST_PREVIEW_ITEMS = 20


def set_llm_debug_sink(sink: Callable[[dict[str, Any]], None]) -> Token:
    """Route LLM debug records to a caller-owned sink for the current context."""

    return _SINK.set(sink)


def reset_llm_debug_sink(token: Token) -> None:
    """Restore the previous LLM debug sink."""

    _SINK.reset(token)


def llm_debug_enabled() -> bool:
    """Return True when provider request/response debug records should emit."""

    specific = os.environ.get("BOX_AGENT_LLM_DEBUG")
    if specific is not None:
        return specific.strip().lower() in _TRUTHY

    if _SINK.get() is not None:
        return True

    return os.environ.get("BOX_AGENT_LOG_LEVEL", "").strip().lower() == "debug"


def log_llm_request(
    *,
    provider: str,
    mode: str,
    api_base: str,
    params: Mapping[str, Any],
) -> None:
    """Log provider request parameters, summarized by default."""

    if not llm_debug_enabled():
        return

    sanitized_payload = sanitize_for_logging(params)
    if not full_payload_logging_enabled():
        sanitized_payload = summarize_request_payload_for_logging(sanitized_payload)

    _emit(
        {
            "event": "llm/request",
            "provider": provider,
            "mode": mode,
            "api_base": _sanitize_url(api_base),
            "payload": sanitized_payload,
        }
    )


def log_llm_response_meta(
    *,
    provider: str,
    mode: str,
    request_id: str | None = None,
    headers: Any = None,
) -> None:
    """Log provider response metadata, including the vendor request id."""

    if not llm_debug_enabled():
        return

    _emit(
        {
            "event": "llm/response_meta",
            "provider": provider,
            "mode": mode,
            "request_id": request_id or request_id_from_headers(headers),
            "headers": sanitize_for_logging(_mapping_from_headers(headers)),
        }
    )


def log_llm_error_meta(*, provider: str, mode: str, exc: BaseException) -> None:
    """Log request id/header metadata carried by provider exceptions."""

    if not llm_debug_enabled():
        return

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    request_id = getattr(exc, "request_id", None) or request_id_from_headers(headers)

    _emit(
        {
            "event": "llm/error_meta",
            "provider": provider,
            "mode": mode,
            "request_id": request_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "headers": sanitize_for_logging(_mapping_from_headers(headers)),
        }
    )


def log_image_generation_request(
    *,
    mode: str,
    endpoint: str,
    method: str,
    headers: Any = None,
    json_payload: Mapping[str, Any] | None = None,
    multipart_fields: Mapping[str, Any] | None = None,
    files: list[Mapping[str, Any]] | None = None,
    configured_model: str | None = None,
    timeout: float | None = None,
) -> None:
    """Log the exact image-generation request fields sent to the HTTP service.

    Text fields are intentionally kept in full even when normal LLM payload
    logging is summarized; binary file contents are represented by metadata.
    """

    if not llm_debug_enabled():
        return

    payload: dict[str, Any] = {}
    if json_payload is not None:
        payload["json"] = sanitize_for_logging(json_payload)
    if multipart_fields is not None:
        payload["multipart_fields"] = sanitize_for_logging(multipart_fields)
    if files is not None:
        payload["files"] = sanitize_for_logging(files)

    _emit(
        {
            "event": "image_generation/request",
            "mode": mode,
            "method": method,
            "endpoint": _sanitize_url(endpoint),
            "configured_model": configured_model,
            "timeout": timeout,
            "headers": sanitize_for_logging(_mapping_from_headers(headers)),
            "payload": payload,
        }
    )


def log_image_generation_response_meta(
    *,
    mode: str,
    endpoint: str,
    status_code: int | None = None,
    headers: Any = None,
) -> None:
    """Log image-generation response metadata."""

    if not llm_debug_enabled():
        return

    _emit(
        {
            "event": "image_generation/response_meta",
            "mode": mode,
            "endpoint": _sanitize_url(endpoint),
            "status_code": status_code,
            "request_id": request_id_from_headers(headers),
            "headers": sanitize_for_logging(_mapping_from_headers(headers)),
        }
    )


def log_image_generation_error_meta(
    *,
    mode: str,
    endpoint: str,
    status_code: int | None = None,
    headers: Any = None,
    response_body: str | None = None,
    error: str | None = None,
) -> None:
    """Log image-generation error details, keeping the backend body intact."""

    if not llm_debug_enabled():
        return

    _emit(
        {
            "event": "image_generation/error_meta",
            "mode": mode,
            "endpoint": _sanitize_url(endpoint),
            "status_code": status_code,
            "request_id": request_id_from_headers(headers),
            "headers": sanitize_for_logging(_mapping_from_headers(headers)),
            "response_body": response_body,
            "error": error,
        }
    )


def sanitize_for_logging(value: Any) -> Any:
    """Return a JSON-serializable copy with credentials redacted."""

    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type == "image" and isinstance(value.get("source"), Mapping):
            source = value["source"]
            if source.get("type") == "base64" and isinstance(
                source.get("data"), str
            ):
                return {
                    **{
                        str(key): sanitize_for_logging(item)
                        for key, item in value.items()
                        if key != "source"
                    },
                    "source": {
                        **{
                            str(key): sanitize_for_logging(item)
                            for key, item in source.items()
                            if key != "data"
                        },
                        "data": {
                            "redacted": True,
                            "characters": len(source["data"]),
                        },
                    },
                }
        if block_type == "image_url" and isinstance(
            value.get("image_url"), Mapping
        ):
            image_url = value["image_url"]
            url = image_url.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                return {
                    **{
                        str(key): sanitize_for_logging(item)
                        for key, item in value.items()
                        if key != "image_url"
                    },
                    "image_url": {
                        **{
                            str(key): sanitize_for_logging(item)
                            for key, item in image_url.items()
                            if key != "url"
                        },
                        "url": {
                            "redacted": True,
                            "characters": len(url),
                        },
                    },
                }
        return {
            str(key): _REDACTED if _is_sensitive_key(str(key)) else sanitize_for_logging(item)
            for key, item in value.items()
        }

    if isinstance(value, list | tuple):
        return [sanitize_for_logging(item) for item in value]

    if hasattr(value, "model_dump"):
        try:
            return sanitize_for_logging(value.model_dump())
        except Exception:
            pass

    if isinstance(value, str | int | float | bool) or value is None:
        return value

    return str(value)


def full_payload_logging_enabled() -> bool:
    """Return True when debug logs should keep full provider request payloads."""

    for name in ("BOX_AGENT_LOG_FULL_PAYLOAD", "BOX_AGENT_LLM_DEBUG_FULL_PAYLOAD"):
        value = os.environ.get(name)
        if value is None:
            continue
        return value.strip().lower() in _TRUTHY
    return False


def summarize_request_payload_for_logging(payload: Any) -> Any:
    """Return a compact provider request payload suitable for debug logs.

    The actual provider request is unchanged.  This only controls the debug
    record written to ``BOX_AGENT_LOG_FILE``/AgentLogger so long system prompts,
    skill bodies, generated artifacts, and image/tool payloads do not balloon
    logs during multi-turn runs.
    """

    if not isinstance(payload, Mapping):
        return _summarize_request_value(payload)

    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "messages" and isinstance(value, list):
            summary[key] = [_summarize_message_for_logging(message) for message in value]
        elif key == "system":
            summary[key] = _summarize_request_value(value)
        elif key == "tools" and isinstance(value, list):
            summary[key] = [_summarize_tool_schema_for_logging(tool) for tool in value]
        else:
            summary[key] = _summarize_request_value(value)
    summary["_payload_logging"] = {
        "mode": "summary",
        "full_payload_env": "BOX_AGENT_LOG_FULL_PAYLOAD=1",
    }
    return summary


def _summarize_message_for_logging(message: Any) -> Any:
    if not isinstance(message, Mapping):
        return _summarize_request_value(message)

    compacted: dict[str, Any] = {}
    for key, value in message.items():
        if key == "content":
            compacted[key] = _summarize_request_value(value)
        elif key == "tool_calls" and isinstance(value, list):
            compacted[key] = [_summarize_tool_call_for_logging(tool_call) for tool_call in value]
        else:
            compacted[key] = _summarize_request_value(value)
    return compacted


def _summarize_tool_call_for_logging(tool_call: Any) -> Any:
    if not isinstance(tool_call, Mapping):
        return _summarize_request_value(tool_call)

    compacted: dict[str, Any] = {}
    for key, value in tool_call.items():
        if key == "function" and isinstance(value, Mapping):
            function = dict(value)
            if "arguments" in function:
                function["arguments"] = _summarize_request_value(
                    function["arguments"],
                    preview_chars=_REQUEST_ARGUMENT_PREVIEW_CHARS,
                )
            compacted[key] = _summarize_request_value(function)
        else:
            compacted[key] = _summarize_request_value(value)
    return compacted


def _summarize_tool_schema_for_logging(tool: Any) -> Any:
    if not isinstance(tool, Mapping):
        return _summarize_request_value(tool)

    if "function" in tool and isinstance(tool["function"], Mapping):
        function = tool["function"]
        return {
            "type": tool.get("type"),
            "function": {
                "name": function.get("name"),
                "description": _summarize_request_value(function.get("description")),
                "parameters": _summarize_schema_for_logging(function.get("parameters")),
            },
        }

    return {
        "name": tool.get("name"),
        "description": _summarize_request_value(tool.get("description")),
        "input_schema": _summarize_schema_for_logging(tool.get("input_schema")),
    }


def _summarize_schema_for_logging(schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return _summarize_request_value(schema)

    properties = schema.get("properties")
    property_names = list(properties.keys()) if isinstance(properties, Mapping) else []
    return {
        "type": schema.get("type"),
        "required": _summarize_request_value(schema.get("required", [])),
        "property_count": len(property_names),
        "properties": property_names[:_REQUEST_LIST_PREVIEW_ITEMS],
        "properties_omitted": max(0, len(property_names) - _REQUEST_LIST_PREVIEW_ITEMS),
    }


def _summarize_request_value(value: Any, *, preview_chars: int = _REQUEST_TEXT_PREVIEW_CHARS) -> Any:
    if isinstance(value, str):
        if len(value) <= preview_chars:
            return value
        return {
            "type": "text",
            "characters": len(value),
            "preview": value[:preview_chars],
            "omitted_characters": len(value) - preview_chars,
        }

    if isinstance(value, Mapping):
        return {
            str(key): _summarize_request_value(item, preview_chars=preview_chars)
            for key, item in value.items()
        }

    if isinstance(value, list | tuple):
        items = [
            _summarize_request_value(item, preview_chars=preview_chars)
            for item in value[:_REQUEST_LIST_PREVIEW_ITEMS]
        ]
        if len(value) > _REQUEST_LIST_PREVIEW_ITEMS:
            items.append({"omitted_items": len(value) - _REQUEST_LIST_PREVIEW_ITEMS})
        return items

    return value


def _emit(record: dict[str, Any]) -> None:
    from ..payload_privacy import private_payloads

    if private_payloads():
        record = {"event": record.get("event"), "payload_redacted": True}
    full_record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "level": "DEBUG",
        **record,
    }

    sink = _SINK.get()
    sink_handled = False
    if sink is not None:
        try:
            sink(full_record)
            sink_handled = True
        except Exception:
            logger.debug("LLM debug sink failed", exc_info=True)

    line = json.dumps(full_record, ensure_ascii=False, default=str)
    logger.debug(line)

    file_path = os.environ.get("BOX_AGENT_LOG_FILE", "").strip()
    if file_path:
        try:
            with open(file_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            logger.debug("Failed to write BOX_AGENT_LOG_FILE=%s", file_path, exc_info=True)
        return

    if not sink_handled:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def _is_sensitive_key(key: str) -> bool:
    return key.lower().replace("_", "-") in _SENSITIVE_KEYS


def _mapping_from_headers(headers: Any) -> dict[str, Any]:
    if headers is None:
        return {}
    if isinstance(headers, Mapping):
        return dict(headers)
    try:
        return dict(headers.items())
    except Exception:
        return {"value": str(headers)}


def request_id_from_headers(headers: Any) -> str | None:
    mapping = _mapping_from_headers(headers)
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    known_keys = (
        "x-request-id",
        "request-id",
        "x-stainless-request-id",
        "cf-ray",
        "x-raccoon-request-id",
        "x-ws-request-id",
    )
    for key in known_keys:
        if key in lowered:
            return str(lowered[key])
    for actual_key, value in lowered.items():
        if actual_key.endswith("-request-id") or actual_key.endswith("_request_id"):
            return str(value)
    return None


def _sanitize_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, _REDACTED if _is_sensitive_key(key) else value))

    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))
