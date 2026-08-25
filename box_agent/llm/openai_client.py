"""OpenAI LLM client implementation."""

import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any

from openai import AsyncOpenAI

from ..retry import RetryConfig, StreamInterrupted, async_retry, is_retryable_stream_error
from ..schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from ..tools.argument_limits import (
    PROVIDER_STREAM_ACTIVITY_INTERVAL_SECONDS,
    TOOL_ARGUMENT_ACTIVITY_BUCKET_CHARS,
    streamed_argument_limit,
)
from ..tools.base import tool_call_name_variants
from .base import LLMClientBase
from .error_messages import is_retryable_llm_error
from .debug_logging import (
    log_llm_error_meta,
    log_llm_request,
    log_llm_response_meta,
    request_id_from_headers,
)

logger = logging.getLogger(__name__)

# Fallback completion-token budget when no explicit value is supplied.
# Many OpenAI-protocol relay/proxy gateways default to 4096, which silently
# truncates long tool-call argument streams (we observed `finish_reason="length"`
# cutting JSON mid-string and triggering empty-arguments retry loops). Pin a
# generous default; users can override via ``LLMConfig.max_output_tokens``.
_DEFAULT_MAX_TOKENS = 64000
_DEEP_THINK_REASONING_EFFORT = "high"
_DEFAULT_SENSENOVA_MODEL_PREFIXES = ("sensenova-", "sn-sensenova-")
_SENSENOVA_MODEL_PREFIXES_ENV = "BOX_AGENT_SENSENOVA_MODEL_PREFIXES"
_SENSENOVA_PREFIX_BOUNDARIES = frozenset("-_/:.")
_GEMINI_NO_DISABLE_MARKERS = ("gemini-2.5-pro", "gemini-3.1-pro")
_SENSENOVA_PSEUDO_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([A-Za-z_][\w.-]*)>\s*(.*?)\s*</function>\s*</tool_call>",
    re.DOTALL,
)
_SENSENOVA_PSEUDO_PARAMETER_RE = re.compile(
    r"<parameter=([A-Za-z_][\w.-]*)>\s*(.*?)\s*</parameter>",
    re.DOTALL,
)
_MAX_RECOVERED_SENSENOVA_TOOL_CALLS = 4


def _litellm_extra_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep provider extensions nested under literal ``extra_body`` on the wire.

    The OpenAI SDK merges its own ``extra_body`` argument into the HTTP body.
    LiteLLM expects another provider-owned ``extra_body`` object inside that
    body, so the SDK argument intentionally has two levels.
    """
    return {"extra_body": payload}


def _sensenova_model_prefixes() -> tuple[str, ...]:
    """Built-in SenseNova dialect prefixes plus any added via env.

    ``BOX_AGENT_SENSENOVA_MODEL_PREFIXES`` is an operator-only,
    comma/whitespace-separated additive list for model families whose complete
    request/response contract matches SenseNova, including thinking parameters
    and pseudo-tool-call recovery. Extra prefixes must end at a visible family
    boundary (``-``, ``_``, ``/``, ``:``, or ``.``); built-ins are always kept.
    """
    extra_raw = os.environ.get(_SENSENOVA_MODEL_PREFIXES_ENV, "")
    if not extra_raw.strip():
        return _DEFAULT_SENSENOVA_MODEL_PREFIXES
    candidates = (
        token.strip().casefold()
        for token in re.split(r"[,\s]+", extra_raw)
    )
    extra = tuple(
        dict.fromkeys(
            token
            for token in candidates
            if len(token) >= 3 and token[-1] in _SENSENOVA_PREFIX_BOUNDARIES
        )
    )
    return _DEFAULT_SENSENOVA_MODEL_PREFIXES + extra


def _is_sensenova_model(model: str | None) -> bool:
    """Return whether ``model`` uses the SenseNova OpenAI-compatible dialect."""
    return bool(
        model and model.strip().casefold().startswith(_sensenova_model_prefixes())
    )


def _apply_thinking_params(
    params: dict[str, Any],
    *,
    model: str | None,
    thinking_enabled: bool,
) -> None:
    """Map the session deep-think flag to the provider request dialect."""
    normalized_model = (model or "").strip().casefold()
    if "deepseek" in normalized_model or "doubao" in normalized_model:
        params["extra_body"] = _litellm_extra_body(
            {"thinking": {"type": "enabled" if thinking_enabled else "disabled"}}
        )
        return
    if "qwen" in normalized_model:
        params["extra_body"] = {"enable_thinking": thinking_enabled}
        return
    if "gemini" in normalized_model:
        if thinking_enabled:
            params["reasoning_effort"] = _DEEP_THINK_REASONING_EFFORT
        elif not any(marker in normalized_model for marker in _GEMINI_NO_DISABLE_MARKERS):
            params["reasoning_effort"] = "none"
        return
    if thinking_enabled:
        params["reasoning_effort"] = _DEEP_THINK_REASONING_EFFORT
        return
    if _is_sensenova_model(model):
        # SenseNova-compatible deployments disagree on whether ``none`` is a
        # valid enum value.  Some accept it, while others only accept
        # low/medium/high and reject an otherwise valid request with 422.
        # Omitting the optional field is the only value accepted by both
        # dialects when deep thinking is disabled.
        return


def _tool_parameter_types(
    openai_tools: list[dict[str, Any]] | None,
    source_tools: list[Any] | None = None,
) -> dict[str, dict[str, str]]:
    canonical_names = _canonical_tool_names(openai_tools, source_tools)
    parameter_types: dict[str, dict[str, str]] = {}
    for tool in openai_tools or []:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        types = {
            key: value.get("type", "string")
            for key, value in properties.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        for call_name in tool_call_name_variants(name):
            if canonical_names.get(call_name) == name:
                parameter_types[call_name] = types

    # Aliases are execution-only compatibility names: accept them when
    # recovering SenseNova pseudo tool calls, but never add them to the
    # provider-facing schema above.
    for tool in source_tools or []:
        if isinstance(tool, dict):
            continue
        canonical_name = getattr(tool, "name", None)
        canonical_types = parameter_types.get(canonical_name)
        if canonical_types is None:
            continue
        for alias in getattr(tool, "aliases", ()):
            if isinstance(alias, str) and alias:
                for call_name in tool_call_name_variants(alias):
                    if canonical_names.get(call_name) == canonical_name:
                        parameter_types[call_name] = canonical_types
    return parameter_types


def _canonical_tool_names(
    openai_tools: list[dict[str, Any]] | None,
    source_tools: list[Any] | None,
) -> dict[str, str]:
    """Map declared aliases to canonical names for provider-side limits."""

    names: dict[str, str] = {}
    declared_schema_names: set[str] = set()

    def register(call_name: str, canonical_name: str) -> None:
        existing = names.get(call_name)
        if existing is not None and existing != canonical_name:
            raise ValueError(
                f"Tool call name '{call_name}' for '{canonical_name}' conflicts "
                f"with tool '{existing}'"
            )
        names[call_name] = canonical_name

    for tool in openai_tools or []:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        canonical_name = function.get("name")
        if not isinstance(canonical_name, str) or not canonical_name:
            continue
        if canonical_name in declared_schema_names:
            raise ValueError(f"Duplicate tool schema name '{canonical_name}'")
        declared_schema_names.add(canonical_name)
        for call_name in tool_call_name_variants(canonical_name):
            register(call_name, canonical_name)

    for tool in source_tools or []:
        if isinstance(tool, dict):
            continue
        canonical_name = getattr(tool, "name", None)
        if not isinstance(canonical_name, str) or not canonical_name:
            continue
        for declared_name in (canonical_name, *getattr(tool, "aliases", ())):
            if isinstance(declared_name, str) and declared_name:
                for call_name in tool_call_name_variants(declared_name):
                    register(call_name, canonical_name)
    return names


def _is_sensenova_tool_only_content(content: str) -> bool:
    """Return whether visible content consists only of pseudo tool calls."""

    cursor = 0
    found = False
    for match in _SENSENOVA_PSEUDO_TOOL_CALL_RE.finditer(content):
        if content[cursor : match.start()].strip():
            return False
        found = True
        cursor = match.end()
    return found and not content[cursor:].strip()


def _coerce_pseudo_parameter(raw: str, expected_type: str) -> Any:
    value = raw.strip()
    try:
        if expected_type == "integer":
            return int(value)
        if expected_type == "number":
            return float(value)
        if expected_type == "boolean":
            if value.casefold() == "true":
                return True
            if value.casefold() == "false":
                return False
        if expected_type in {"array", "object"}:
            parsed = json.loads(value)
            if expected_type == "array" and isinstance(parsed, list):
                return parsed
            if expected_type == "object" and isinstance(parsed, dict):
                return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return value


def _recover_sensenova_pseudo_tool_calls(
    source: str,
    openai_tools: list[dict[str, Any]] | None,
    source_tools: list[Any] | None = None,
) -> list[ToolCall]:
    """Recover SenseNova's XML-like dialect when native tool_calls is empty."""
    parameter_types = _tool_parameter_types(openai_tools, source_tools)
    canonical_names = _canonical_tool_names(openai_tools, source_tools)
    if not parameter_types:
        return []

    recovered: list[ToolCall] = []
    for match in _SENSENOVA_PSEUDO_TOOL_CALL_RE.finditer(source):
        if len(recovered) >= _MAX_RECOVERED_SENSENOVA_TOOL_CALLS:
            break
        name, body = match.groups()
        if name not in parameter_types:
            continue
        arguments: dict[str, Any] = {}
        for parameter_match in _SENSENOVA_PSEUDO_PARAMETER_RE.finditer(body):
            parameter_name, raw_value = parameter_match.groups()
            if parameter_name not in parameter_types[name]:
                continue
            arguments[parameter_name] = _coerce_pseudo_parameter(
                raw_value,
                parameter_types[name][parameter_name],
            )
        arguments_len = len(json.dumps(arguments, ensure_ascii=False))
        limit = streamed_argument_limit(canonical_names.get(name, name))
        if limit is not None and arguments_len > limit:
            logger.warning(
                "Ignored oversized recovered SenseNova pseudo tool call: "
                "name=%s arguments_len=%d limit=%d",
                name,
                arguments_len,
                limit,
            )
            continue
        recovered.append(
            ToolCall(
                id=f"sensenova_recovered_{uuid.uuid4().hex}",
                type="function",
                function=FunctionCall(name=name, arguments=arguments),
            )
        )
    return recovered


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control characters that appear inside JSON strings.

    Some model backends emit literal newlines/tabs (or other control chars)
    inside a string value without escaping them. ``json.loads(strict=False)``
    accepts a subset of these, but not all — so we do a second pass that
    scans the raw text and \\uXXXX-escapes every control char it finds
    inside an open string.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def _repair_tool_call_arguments(
    raw_args: str,
    tool_name: str = "?",
    *,
    allow_structural_closure: bool = True,
) -> str | None:
    """Attempt to repair malformed tool_call argument JSON.

    Returns the repaired JSON string on success, or ``None`` when the input
    is unrepairable. Common failure modes handled:

    * unescaped control chars inside string values (llama.cpp / GLM);
    * ``None`` python-literal instead of ``{}``;
    * trailing commas before ``}`` / ``]``;
    * unclosed ``{`` / ``[`` — appended, but *only* when
      ``allow_structural_closure`` is True;
    * excess trailing ``}`` / ``]`` — trimmed (bounded to 50 iterations).

    ``allow_structural_closure`` guards the one pass that can synthesize
    executable semantics from a half-delivered payload. When the upstream
    stream is known to have been cut short (``finish_reason`` was ``None``,
    ``"length"`` or ``"max_tokens"``), the caller passes ``False`` so a
    truncated ``"content":"partial`` cannot be turned into a valid
    ``{"content":"partial"}`` and handed to a filesystem/shell tool. All the
    other passes are byte-conservative (they only delete or escape existing
    chars) and stay enabled regardless.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    if not raw_stripped:
        return "{}"

    if raw_stripped == "None":
        return "{}"

    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        if reserialised != raw_stripped:
            logger.warning(
                "Repaired unescaped control chars in tool_call arguments for %r",
                tool_name,
            )
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fixed = raw_stripped
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    if allow_structural_closure:
        open_curly = fixed.count("{") - fixed.count("}")
        open_bracket = fixed.count("[") - fixed.count("]")
        if open_curly > 0:
            fixed += "}" * open_curly
        if open_bracket > 0:
            fixed += "]" * open_bracket
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
                fixed = fixed[:-1]
            elif fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning(
            "Repaired malformed tool_call arguments for %r: %s -> %s",
            tool_name, raw_stripped[:80], fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            logger.warning(
                "Repaired control-char-laced tool_call arguments for %r",
                tool_name,
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return None


async def _await_if_needed(value: Any) -> Any:
    """Return awaitable SDK values and direct SDK values through one path."""
    if inspect.isawaitable(value):
        return await value
    return value


def _get_field(value: Any, name: str) -> Any:
    """Read a field from an SDK model or a plain mapping."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _reasoning_text_from_aliases(value: Any) -> str:
    """Return reasoning text from common OpenAI-compatible field aliases."""
    for name in ("reasoning", "reasoning_content"):
        reasoning = _get_field(value, name)
        if isinstance(reasoning, str) and reasoning:
            return reasoning
    return ""


class OpenAIClient(LLMClientBase):
    """LLM client using OpenAI's protocol.

    This client uses the official OpenAI SDK and supports:
    - Reasoning content (via reasoning_split=True)
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        retry_config: RetryConfig | None = None,
        max_output_tokens: int = _DEFAULT_MAX_TOKENS,
        auth_token: str = "",
        auth_file: str = "",
        timeout: float = 600.0,
    ):
        """Initialize OpenAI client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            max_output_tokens: Per-request ``max_tokens`` value sent to the API.
            auth_token: Optional in-memory product login token.
            auth_file: Optional auth.json path read before every request.
            timeout: Wall-clock cap (seconds) for each request to the API.
        """
        super().__init__(
            api_key, api_base, model, retry_config,
            auth_token=auth_token, auth_file=auth_file, timeout=timeout,
        )
        self.max_output_tokens = max_output_tokens
        # One-shot override applied to the next request only. The agent loop
        # sets this before retrying a truncated turn so the model has more
        # room to finish tool-call JSON, then it clears itself after the
        # next generate/generate_stream call.
        self._ephemeral_max_output_tokens: int | None = None

        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
        )

    def set_ephemeral_max_output_tokens(self, value: int | None) -> None:
        """Override ``max_tokens`` for the very next request.

        Cleared automatically after the next ``generate`` / ``generate_stream``
        completes so the boost never leaks into unrelated turns.
        """
        self._ephemeral_max_output_tokens = value if value and value > 0 else None

    def _consume_effective_max_tokens(self) -> int:
        # Test doubles instantiate the client via factory helpers that skip
        # ``__init__``; tolerate a missing attribute rather than exploding.
        cap = getattr(self, "_ephemeral_max_output_tokens", None)
        self._ephemeral_max_output_tokens = None
        if cap is not None:
            return max(cap, self.max_output_tokens)
        return self.max_output_tokens

    async def _make_api_request(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> Any:
        """Execute API request (core method that can be retried).

        Args:
            api_messages: List of messages in OpenAI format
            tools: Optional list of tools
            thinking_enabled: When True, request provider-native thinking for
                recognized model families.

        Returns:
            OpenAI ChatCompletion response (full response including usage)

        Raises:
            Exception: API call failed
        """
        params: dict[str, Any] = {
            "messages": api_messages,
            "max_tokens": self._consume_effective_max_tokens(),
        }
        if self.model:
            params["model"] = self.model

        if tools:
            params["tools"] = self._convert_tools(tools)

        _apply_thinking_params(
            params,
            model=self.model,
            thinking_enabled=thinking_enabled,
        )

        auth_headers = self._auth_headers(
            self._request_headers(session_id, turn_id, title, call_kind)
        )
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="openai", mode="completion", api_base=self.api_base, params=params)

        try:
            raw_response = await _await_if_needed(
                self.client.chat.completions.with_raw_response.create(**params)
            )
            log_llm_response_meta(
                provider="openai",
                mode="completion",
                request_id=getattr(raw_response, "request_id", None),
                headers=getattr(raw_response, "headers", None),
            )
            response = await _await_if_needed(raw_response.parse())
        except AttributeError:
            # Test doubles and older SDK-compatible clients may not expose
            # ``with_raw_response``. Keep the request log and fall back to the
            # existing behavior, but request-id metadata will be unavailable.
            response = await _await_if_needed(self.client.chat.completions.create(**params))
        except Exception as exc:
            log_llm_error_meta(provider="openai", mode="completion", exc=exc)
            raise

        # Return full response to access usage info
        return response

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # If already a dict, check if it's in OpenAI format
                if "type" in tool and tool["type"] == "function":
                    result.append(tool)
                else:
                    # Assume it's in Anthropic format, convert to OpenAI
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["input_schema"],
                            },
                        }
                    )
            elif hasattr(tool, "to_openai_schema"):
                # Tool object with to_openai_schema method
                result.append(tool.to_openai_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to OpenAI format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
            Note: OpenAI includes system message in the messages array
        """
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                # OpenAI includes system message in messages array
                api_messages.append({"role": "system", "content": msg.content})
                continue

            # For user messages
            if msg.role == "user":
                api_messages.append(
                    {
                        "role": "user",
                        "content": self._convert_input_content(msg.content),
                    }
                )

            # For assistant messages
            elif msg.role == "assistant":
                assistant_msg = {"role": "assistant"}

                # Always include content — even when empty — so LiteLLM/OpenAI
                # never sees a missing key that gets serialized to `content: null`
                # downstream (network gateway rejects null with 400).
                assistant_msg["content"] = msg.content if msg.content else ""

                # Add tool calls if present
                if msg.tool_calls:
                    tool_calls_list = []
                    for tool_call in msg.tool_calls:
                        tool_calls_list.append(
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": json.dumps(tool_call.function.arguments),
                                },
                            }
                        )
                    assistant_msg["tool_calls"] = tool_calls_list

                # IMPORTANT: Add reasoning_details if thinking is present
                # This is CRITICAL for Interleaved Thinking to work properly!
                # The complete response_message (including reasoning_details) must be
                # preserved in Message History and passed back to the model in the next turn.
                # This ensures the model's chain of thought is not interrupted.
                if msg.thinking:
                    assistant_msg["reasoning_details"] = [{"text": msg.thinking}]

                api_messages.append(assistant_msg)

            # For tool result messages
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )

        return None, api_messages

    @staticmethod
    def _convert_input_content(content: Any) -> Any:
        """Translate canonical multimodal blocks to OpenAI wire blocks."""
        if not isinstance(content, list):
            return content
        converted: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_image":
                media_type = block.get("media_type")
                data = block.get("data")
                if media_type not in {"image/png", "image/jpeg"} or not isinstance(
                    data, str
                ) or not data:
                    raise ValueError("invalid canonical input_image block")
                converted.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{data}",
                        },
                    }
                )
            else:
                converted.append(block)
        return converted

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for OpenAI API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        _, api_messages = self._convert_messages(messages)

        return {
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(
        self,
        response: Any,
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Parse OpenAI response into LLMResponse.

        Args:
            response: OpenAI ChatCompletion response (full response object)

        Returns:
            LLMResponse object
        """
        # Get message from response
        message = response.choices[0].message

        # Extract text content
        text_content = message.content or ""

        # Extract thinking content. OpenAI-compatible providers use different
        # aliases: SenseNova emits ``reasoning``, while other gateways expose
        # ``reasoning_content`` or structured ``reasoning_details`` blocks.
        thinking_content = _reasoning_text_from_aliases(message)
        reasoning_details = _get_field(message, "reasoning_details")
        if not thinking_content and reasoning_details:
            # reasoning_details is a list of reasoning blocks
            for detail in reasoning_details:
                detail_text = _get_field(detail, "text")
                if isinstance(detail_text, str):
                    thinking_content += detail_text

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tool_call in message.tool_calls:
                # Parse arguments from JSON string
                arguments = json.loads(tool_call.function.arguments)

                tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        type="function",
                        function=FunctionCall(
                            name=tool_call.function.name,
                            arguments=arguments,
                        ),
                    )
                )

        finish_reason = getattr(response.choices[0], "finish_reason", None) or "stop"
        pseudo_tool_source = ""
        pseudo_tool_source_is_text = False
        if not tool_calls and _is_sensenova_model(self.model):
            if not text_content.strip() and thinking_content:
                pseudo_tool_source = thinking_content
            elif _is_sensenova_tool_only_content(text_content):
                pseudo_tool_source = text_content
                pseudo_tool_source_is_text = True
        if pseudo_tool_source:
            openai_tools = self._convert_tools(tools) if tools else None
            tool_calls = _recover_sensenova_pseudo_tool_calls(
                pseudo_tool_source,
                openai_tools,
                tools,
            )
            if tool_calls:
                finish_reason = "tool_calls"
                if pseudo_tool_source_is_text:
                    text_content = ""
                logger.warning(
                    "Recovered %d SenseNova pseudo tool call(s)",
                    len(tool_calls),
                )

        # Extract token usage from response
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
            usage=usage,
            provider_response_id=str(response.id) if getattr(response, "id", None) else None,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        """Generate response from OpenAI LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            thinking_enabled: Request provider-native thinking when True for
                recognized model families.
            session_id: Optional caller-owned session id.
            turn_id: Optional caller-owned turn id.
            title: Optional trace title.

        Returns:
            LLMResponse containing the generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)

        # Make API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
                should_retry=is_retryable_llm_error,
            )
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
            )

        # Parse and return response
        return self._parse_response(response, tools)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from OpenAI LLM.

        Yields thinking/text deltas as they arrive. Tool calls are accumulated
        and emitted in the final "finish" event along with token usage.
        """
        request_params = self._prepare_request(messages, tools)

        params: dict[str, Any] = {
            "messages": request_params["api_messages"],
            "max_tokens": self._consume_effective_max_tokens(),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.model:
            params["model"] = self.model
        if request_params["tools"]:
            params["tools"] = self._convert_tools(request_params["tools"])
        _apply_thinking_params(
            params,
            model=self.model,
            thinking_enabled=thinking_enabled,
        )

        should_buffer_sensenova_tool_markup = bool(
            request_params["tools"] and _is_sensenova_model(self.model)
        )

        auth_headers = self._auth_headers(
            self._request_headers(session_id, turn_id, title, call_kind)
        )
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="openai", mode="stream", api_base=self.api_base, params=params)

        # Accumulators
        text_content = ""
        thinking_content = ""
        usage: TokenUsage | None = None
        # ``None`` (not "stop") so we can tell "upstream explicitly said stop"
        # apart from "upstream never sent a finish_reason" in the diagnostics
        # log below. core.py tolerates None via ``finish_reason or "stop"``.
        finish_reason: str | None = None

        # Tool call accumulators: {index: {id, name, arguments_str}}
        tool_acc: dict[int, dict[str, Any]] = {}
        oversized_info: list[dict[str, Any]] = []
        provider_request_id: str | None = None
        provider_response_id: str | None = None

        async def _open_stream() -> Any:
            nonlocal provider_request_id
            try:
                raw_response = await _await_if_needed(
                    self.client.chat.completions.with_raw_response.create(**params)
                )
                provider_request_id = getattr(raw_response, "request_id", None) or request_id_from_headers(
                    getattr(raw_response, "headers", None)
                )
                log_llm_response_meta(
                    provider="openai",
                    mode="stream",
                    request_id=provider_request_id,
                    headers=getattr(raw_response, "headers", None),
                )
                return await _await_if_needed(raw_response.parse())
            except AttributeError:
                return await _await_if_needed(self.client.chat.completions.create(**params))

        import asyncio as _asyncio

        max_attempts = max(1, self.retry_config.max_retries + 1) if self.retry_config.enabled else 1
        any_user_yield = False

        for attempt in range(max_attempts):
            # Reset per-attempt accumulators so a retry doesn't leak state from
            # a half-consumed prior attempt.
            text_content = ""
            thinking_content = ""
            usage = None
            finish_reason = None
            tool_acc = {}
            oversized_info = []
            provider_response_id = None
            last_provider_activity_at: float | None = None
            buffer_sensenova_tool_markup = should_buffer_sensenova_tool_markup
            pending_sensenova_text = ""

            try:
                response_stream = await _open_stream()
            except Exception as exc:
                log_llm_error_meta(provider="openai", mode="stream", exc=exc)
                if attempt < max_attempts - 1 and is_retryable_stream_error(exc):
                    delay = self.retry_config.calculate_delay(attempt)
                    logger.warning(
                        "openai generate_stream open attempt %d/%d failed: %s; retrying in %.2fs",
                        attempt + 1, max_attempts, exc, delay,
                    )
                    if self.retry_callback:
                        try:
                            self.retry_callback(exc, attempt + 1)
                        except Exception:  # pragma: no cover - callback safety
                            logger.exception("retry_callback raised")
                    await _asyncio.sleep(delay)
                    continue
                raise

            try:
                async for chunk in response_stream:
                    if provider_response_id is None and getattr(chunk, "id", None):
                        provider_response_id = str(chunk.id)
                    now = monotonic()
                    if (
                        last_provider_activity_at is None
                        or now - last_provider_activity_at
                        >= PROVIDER_STREAM_ACTIVITY_INTERVAL_SECONDS
                    ):
                        last_provider_activity_at = now
                        yield StreamEvent(
                            type="activity",
                            activity={
                                "protocol": "agent_activity_v1",
                                "phase": "provider_stream",
                            },
                        )
                    # Usage info (sent in the final chunk with choices=[])
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = TokenUsage(
                            prompt_tokens=chunk.usage.prompt_tokens or 0,
                            completion_tokens=chunk.usage.completion_tokens or 0,
                            total_tokens=chunk.usage.total_tokens or 0,
                            input_tokens=chunk.usage.prompt_tokens or 0,
                            output_tokens=chunk.usage.completion_tokens or 0,
                        )

                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]

                    # Finish reason
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    delta = choice.delta
                    if delta is None:
                        continue

                    # Reasoning / thinking content. SenseNova uses
                    # ``reasoning``; DeepSeek-style gateways commonly use
                    # ``reasoning_content``.
                    reasoning_delta = _reasoning_text_from_aliases(delta)
                    if reasoning_delta:
                        thinking_content += reasoning_delta
                        any_user_yield = True
                        yield StreamEvent(type="thinking", delta=reasoning_delta)

                    # Text content
                    if delta.content:
                        text_content += delta.content
                        if buffer_sensenova_tool_markup:
                            pending_sensenova_text += delta.content
                            stripped_pending = pending_sensenova_text.lstrip()
                            could_be_tool_markup = (
                                not stripped_pending
                                or "<tool_call>".startswith(stripped_pending)
                                or stripped_pending.startswith("<tool_call>")
                            )
                            if not could_be_tool_markup:
                                buffer_sensenova_tool_markup = False
                                any_user_yield = True
                                yield StreamEvent(
                                    type="text",
                                    delta=pending_sensenova_text,
                                )
                                pending_sensenova_text = ""
                        else:
                            any_user_yield = True
                            yield StreamEvent(type="text", delta=delta.content)

                    # Tool call deltas
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_acc:
                                tool_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                    "arguments": "",
                                    "activity_bucket": -1,
                                }
                            else:
                                if tc_delta.id:
                                    tool_acc[idx]["id"] = tc_delta.id
                                if tc_delta.function and tc_delta.function.name:
                                    tool_acc[idx]["name"] = tc_delta.function.name
                            if tc_delta.function and tc_delta.function.arguments:
                                tool_acc[idx]["arguments"] += tc_delta.function.arguments
                                entry = tool_acc[idx]
                                arguments_len = len(entry["arguments"])
                                activity_bucket = (
                                    arguments_len // TOOL_ARGUMENT_ACTIVITY_BUCKET_CHARS
                                )
                                if activity_bucket > entry["activity_bucket"]:
                                    entry["activity_bucket"] = activity_bucket
                                    yield StreamEvent(
                                        type="activity",
                                        activity={
                                            "protocol": "agent_activity_v1",
                                            "phase": "tool_arguments",
                                            "tool_name": entry["name"] or "",
                                            "argument_chars": arguments_len,
                                        },
                                    )
                                limit = streamed_argument_limit(entry["name"])
                                if limit is not None and arguments_len > limit:
                                    oversized_info.append(
                                        {
                                            "name": entry["name"] or "",
                                            "arguments_len": arguments_len,
                                            "limit": limit,
                                        }
                                    )
                                    finish_reason = "tool_argument_limit"
                                    break
                        if oversized_info:
                            closer = getattr(response_stream, "aclose", None)
                            if closer is not None:
                                await closer()
                            break
            except Exception as exc:
                log_llm_error_meta(provider="openai", mode="stream", exc=exc)
                if is_retryable_stream_error(exc):
                    if any_user_yield:
                        # Once we've yielded deltas to the consumer we cannot
                        # rewind — surface partial content instead of retrying.
                        logger.warning(
                            "openai stream interrupted after partial yield "
                            "(text=%d chars, thinking=%d chars): %s",
                            len(text_content), len(thinking_content), exc,
                        )
                        raise StreamInterrupted(
                            last_exception=exc,
                            partial_text=text_content,
                            partial_thinking=thinking_content,
                            provider_request_id=provider_request_id,
                        ) from exc
                    if attempt < max_attempts - 1:
                        delay = self.retry_config.calculate_delay(attempt)
                        logger.warning(
                            "openai generate_stream consume attempt %d/%d dropped before any yield: %s; "
                            "retrying from scratch in %.2fs",
                            attempt + 1, max_attempts, exc, delay,
                        )
                        if self.retry_callback:
                            try:
                                self.retry_callback(exc, attempt + 1)
                            except Exception:  # pragma: no cover - callback safety
                                logger.exception("retry_callback raised")
                        await _asyncio.sleep(delay)
                        continue
                raise
            else:
                # Successful consume — break out of the retry loop.
                break

        if oversized_info:
            logger.warning(
                "openai tool argument stream stopped locally: %s request_id=%s",
                oversized_info,
                provider_request_id,
            )
            yield StreamEvent(
                type="finish",
                finish_reason="tool_argument_limit",
                usage=usage,
                provider_response_id=provider_response_id,
                provider_request_id=provider_request_id,
                oversized_tool_calls=oversized_info,
                raw_finish_reason=None,
            )
            return

        # Build tool calls. When a relay truncates output mid-arguments the
        # accumulated ``arguments_str`` is invalid JSON. First try to repair
        # the common malformations that don't imply truncation (unescaped
        # control chars, trailing commas, missing brackets, Python-``None``);
        # only genuinely unparseable payloads flip ``truncated_tool`` on so
        # the agent loop can decide whether to retry.
        #
        # When the upstream stream was cut short (``finish_reason`` is None /
        # length / max_tokens), we deliberately disable the one repair pass
        # that can synthesize new semantics — auto-closing unbalanced ``{`` /
        # ``[``. Otherwise a payload like ``{"path":"/tmp/a","content":"part``
        # would get "fixed" to a valid-looking ``{"path":"/tmp/a","content":"part"}``
        # and get executed against write_file / bash. In that case we route
        # it through the truncation retry path instead.
        allow_closure = finish_reason not in (None, "length", "max_tokens")
        tool_calls: list[ToolCall] = []
        truncated_tool = False
        truncated_info: list[dict[str, Any]] = []
        for idx in sorted(tool_acc):
            entry = tool_acc[idx]
            raw = entry["arguments"]
            try:
                arguments = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                repaired = _repair_tool_call_arguments(
                    raw,
                    entry["name"] or "?",
                    allow_structural_closure=allow_closure,
                )
                if repaired is not None:
                    try:
                        arguments = json.loads(repaired)
                    except json.JSONDecodeError:
                        truncated_tool = True
                        truncated_info.append({"name": entry["name"], "arguments_len": len(raw)})
                        logger.warning(
                            "Truncated tool_call arguments for %r (idx=%d, len=%d): %s",
                            entry["name"], idx, len(raw), exc,
                        )
                        continue
                else:
                    truncated_tool = True
                    truncated_info.append({"name": entry["name"], "arguments_len": len(raw)})
                    logger.warning(
                        "Truncated tool_call arguments for %r (idx=%d, len=%d): %s",
                        entry["name"], idx, len(raw), exc,
                    )
                    continue
            if not entry["name"]:
                truncated_tool = True
                truncated_info.append({"name": "", "arguments_len": len(raw)})
                logger.warning("Tool_call idx=%d has no function name; dropping", idx)
                continue
            tool_calls.append(
                ToolCall(
                    id=entry["id"],
                    type="function",
                    function=FunctionCall(
                        name=entry["name"],
                        arguments=arguments,
                    ),
                )
            )

        # Capture the upstream value before the tool-truncation override so the
        # diagnostics distinguish "gateway clipped tool args" (length, set here)
        # from "gateway ended a text turn" (stop / end_turn / None from upstream).
        raw_finish_reason = finish_reason
        pseudo_tool_source = ""
        pseudo_tool_source_is_text = False
        if not truncated_tool and not tool_calls and _is_sensenova_model(self.model):
            if not text_content.strip() and thinking_content:
                pseudo_tool_source = thinking_content
            elif _is_sensenova_tool_only_content(text_content):
                pseudo_tool_source = text_content
                pseudo_tool_source_is_text = True
        if pseudo_tool_source:
            tool_calls = _recover_sensenova_pseudo_tool_calls(
                pseudo_tool_source,
                params.get("tools"),
                tools,
            )
            if tool_calls:
                finish_reason = "tool_calls"
                if pseudo_tool_source_is_text:
                    text_content = ""
                    pending_sensenova_text = ""
                logger.warning(
                    "Recovered %d SenseNova pseudo tool call(s) from stream",
                    len(tool_calls),
                )
        if pending_sensenova_text:
            any_user_yield = True
            yield StreamEvent(type="text", delta=pending_sensenova_text)
        stream_dropped_mid_tool = truncated_tool and raw_finish_reason is None
        if truncated_tool:
            finish_reason = "length"

        # Always-on (INFO) diagnostics: surfaces the upstream finish_reason —
        # including ``None`` when the gateway omitted it entirely — so a
        # mid-sentence "stop" cutoff is identifiable in box-agent-stderr.log
        # without enabling full LLM debug logging.
        logger.info(
            "openai stream finished: raw_finish_reason=%r final_finish_reason=%r "
            "completion_tokens=%s text_len=%d request_id=%s",
            raw_finish_reason,
            finish_reason,
            usage.completion_tokens if usage else None,
            len(text_content),
            provider_request_id,
        )

        yield StreamEvent(
            type="finish",
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            provider_response_id=provider_response_id,
            provider_request_id=provider_request_id,
            truncated_tool_calls=truncated_info or None,
            raw_finish_reason=raw_finish_reason,
            stream_dropped_mid_tool=stream_dropped_mid_tool,
        )
