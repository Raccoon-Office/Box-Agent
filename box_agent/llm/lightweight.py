"""Lightweight LLM completion service.

Single-shot prompts (titles, summaries, rewrites) that must
NOT spin up an Agent session, load tools/skills/MCP, touch memory, or write
to conversation history. Buffers :func:`LLMClient.generate_stream` with an
idle timeout, no tools, no extended thinking, and a structured result.

The ACP ``llm/prompt`` extension method is a thin shell around this
service — see :meth:`box_agent.acp.BoxACPAgent.extMethod`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

from ..schema import Message
from .buffered_stream import generate_buffered_stream

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from .llm_wrapper import LLMClient


@dataclass(frozen=True)
class LightweightResult:
    """Outcome of a single lightweight prompt call."""

    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    duration_ms: int


class LightweightPromptError(Exception):
    """Base error for lightweight prompt failures.

    Carries a stable ``code`` so the ACP layer can return it as structured
    JSON-RPC error data without leaking internals.
    """

    code: str = "lightweight_failed"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class LightweightTimeout(LightweightPromptError):
    code = "timeout"


class LightweightInvalidArgs(LightweightPromptError):
    code = "invalid_args"


class LightweightContentFiltered(LightweightPromptError):
    """The provider refused the prompt on content-moderation grounds.

    Distinct from a generic failure: this is a *model refusal*, not a system
    error. The host should fall back to a purpose-appropriate default (e.g. a
    neutral title like "新对话") rather than surfacing the message — the
    ``message`` here is a short technical note for logs only, NOT user-facing
    copy and NOT suitable as a title/summary value.
    """

    code = "content_filter"


async def run_lightweight_prompt(
    llm: "LLMClient",
    prompt: str,
    *,
    system_prompt: str | None = None,
    session_id: str = "",
    turn_id: str = "",
    title: str = "Box-Agent",
    call_kind: str = "utility",
    timeout: float = 30.0,
) -> LightweightResult:
    """Run a single tool-free LLM completion.

    Args:
        llm: Shared :class:`LLMClient` from the ACP server. Reused so
            provider/model/auth stay consistent with the main agent.
        prompt: The user prompt. Must be non-empty after stripping.
        system_prompt: Optional system message. Empty/whitespace strings are
            treated as absent.
        session_id: Optional caller-owned session id for upstream trace grouping.
        turn_id: Optional caller-owned turn id for upstream trace grouping.
        title: Optional upstream trace title.
        timeout: Maximum seconds without a stream event. Raised as
            :class:`LightweightTimeout` on expiry.

    Returns:
        :class:`LightweightResult` with the model text, token usage, finish
        reason and elapsed milliseconds.

    Raises:
        LightweightInvalidArgs: ``prompt`` is empty or ``timeout`` is non-positive.
        LightweightTimeout: The LLM stream was idle for ``timeout`` seconds.
        LightweightContentFiltered: The provider refused on moderation grounds.
        LightweightPromptError: Any other LLM-side failure.
    """
    cleaned_prompt = (prompt or "").strip()
    if not cleaned_prompt:
        raise LightweightInvalidArgs("prompt must be a non-empty string")
    if timeout <= 0:
        raise LightweightInvalidArgs("timeout must be positive")

    messages: list[Message] = []
    if system_prompt and system_prompt.strip():
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=cleaned_prompt))

    started = perf_counter()
    try:
        response = await generate_buffered_stream(
            llm,
            messages=messages,
            tools=None,
            thinking_enabled=False,
            session_id=session_id,
            turn_id=turn_id,
            title=title,
            call_kind=call_kind,
            idle_timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise LightweightTimeout(
            f"lightweight prompt was idle for {timeout:.1f}s"
        ) from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as structured error
        # Classify so the host can distinguish a model refusal (content
        # moderation) from a real failure, and so we never leak the provider's
        # raw JSON blob. Unlike the chat path, lightweight callers (title /
        # summary) consume this programmatically — the friendly message is for
        # logging, the *code* drives the host's fallback.
        from .error_messages import classify_llm_error

        friendly = classify_llm_error(exc)
        if friendly.is_soft:
            raise LightweightContentFiltered(
                "content moderation refused the prompt"
            ) from exc
        raise LightweightPromptError(friendly.message) from exc

    duration_ms = int((perf_counter() - started) * 1000)
    text = getattr(response, "content", "") or ""
    finish_reason = getattr(response, "finish_reason", "") or ""
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

    return LightweightResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        duration_ms=duration_ms,
    )
