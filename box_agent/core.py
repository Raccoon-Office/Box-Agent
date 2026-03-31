"""Shared agent execution core.

This module contains the **single source of truth** for the agent loop.
It yields structured ``AgentEvent`` objects via an ``AsyncGenerator``.
CLI, ACP, and any future consumer all drive the same generator.

No ``print()`` or ``input()`` calls live here — all I/O is delegated
to the consumer through the event stream.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from time import perf_counter
from typing import Callable

import tiktoken

from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    LogFileEvent,
    StepEnd,
    StepStart,
    StopReason,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
)
from .logger import AgentLogger
from .schema import LLMResponse, Message, StreamEvent
from .tools.base import Tool, ToolResult

# Type alias — consumers supply a zero-arg callable that returns True
# when the execution should be cancelled.
CancelChecker = Callable[[], bool]

# Regex to match sandbox file references like [foo.png] or [PNG Image]
_ARTIFACT_REF_RE = re.compile(r"\[([^\]\n]+\.\w{1,10})\]", re.IGNORECASE)

# Image extensions for artifact_type classification
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg"}


def _detect_artifacts(
    tool_call_id: str,
    tool_name: str,
    content: str,
    workspace_dir: str | None,
) -> list[ArtifactEvent]:
    """Scan tool output for file references and emit ArtifactEvents."""
    if not workspace_dir or not content:
        return []

    from pathlib import Path

    ws = Path(workspace_dir)
    artifacts: list[ArtifactEvent] = []

    for match in _ARTIFACT_REF_RE.finditer(content):
        filename = match.group(1)
        # Build candidate paths — Jupyter writes to workspace/sandbox/<session_id>/<file>,
        # so we also glob workspace/sandbox/*/<file> to cover all sandbox sessions.
        candidates = [ws / filename, ws / "sandbox" / filename]
        # Add all sandbox session subdirectories
        sandbox_dir = ws / "sandbox"
        if sandbox_dir.is_dir():
            for session_subdir in sandbox_dir.iterdir():
                if session_subdir.is_dir():
                    candidates.append(session_subdir / filename)

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                ext = candidate.suffix.lower()
                art_type = "image" if ext in _IMAGE_EXTS else "file"
                mime, _ = mimetypes.guess_type(str(candidate))
                artifacts.append(ArtifactEvent(
                    tool_call_id=tool_call_id,
                    artifact_type=art_type,
                    filename=filename,
                    path=str(candidate),
                    mime_type=mime or "application/octet-stream",
                    size_bytes=candidate.stat().st_size,
                ))
                break  # found, no need to check other candidates

    return artifacts


# ── Workspace diff-based artifact detection ─────────────────────

# Directories to skip when scanning the workspace
_IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".ipynb_checkpoints"}


def _snapshot_workspace(workspace_dir: str) -> set[Path]:
    """Snapshot files in workspace root (1 level) + sandbox/ subtree.

    Skips directories in ``_IGNORE_DIRS`` to avoid expensive traversal.
    """
    ws = Path(workspace_dir)
    if not ws.is_dir():
        return set()

    files: set[Path] = set()

    # Workspace root — 1 level only (non-recursive)
    for entry in ws.iterdir():
        if entry.is_file():
            files.add(entry)

    # sandbox/ subtree — recursive
    sandbox = ws / "sandbox"
    if sandbox.is_dir():
        for entry in sandbox.rglob("*"):
            if entry.is_file() and not any(p in entry.parts for p in _IGNORE_DIRS):
                files.add(entry)

    return files


def _detect_new_files(
    tool_call_id: str,
    pre_files: set[Path],
    post_files: set[Path],
    already_emitted: set[str],
    workspace_dir: str,
) -> list[ArtifactEvent]:
    """Create ArtifactEvents for files that appeared after tool execution."""
    new_files = post_files - pre_files
    if not new_files:
        return []

    artifacts: list[ArtifactEvent] = []
    for fpath in sorted(new_files):
        # Skip dotfiles and temp files
        if fpath.name.startswith(".") or fpath.name.startswith("~") or fpath.suffix == ".tmp":
            continue
        # Skip if already emitted by regex detection
        if str(fpath) in already_emitted:
            continue

        ext = fpath.suffix.lower()
        art_type = "image" if ext in _IMAGE_EXTS else "file"
        mime, _ = mimetypes.guess_type(str(fpath))
        artifacts.append(ArtifactEvent(
            tool_call_id=tool_call_id,
            artifact_type=art_type,
            filename=fpath.name,
            path=str(fpath),
            mime_type=mime or "application/octet-stream",
            size_bytes=fpath.stat().st_size,
        ))

    return artifacts


# ── Token estimation helpers ────────────────────────────────────


def _estimate_tokens(messages: list[Message]) -> int:
    """Estimate token count using tiktoken (cl100k_base)."""
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        return _estimate_tokens_fallback(messages)

    total = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total += len(encoding.encode(msg.content))
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total += len(encoding.encode(str(block)))
        if msg.thinking:
            total += len(encoding.encode(msg.thinking))
        if msg.tool_calls:
            total += len(encoding.encode(str(msg.tool_calls)))
        total += 4  # per-message overhead
    return total


def _estimate_tokens_fallback(messages: list[Message]) -> int:
    """Rough fallback when tiktoken is unavailable."""
    total_chars = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total_chars += len(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total_chars += len(str(block))
        if msg.thinking:
            total_chars += len(msg.thinking)
        if msg.tool_calls:
            total_chars += len(str(msg.tool_calls))
    return int(total_chars / 2.5)


# ── Summarization ───────────────────────────────────────────────


async def _create_summary(
    llm,
    messages: list[Message],
    round_num: int,
) -> str:
    """Summarize one execution round via an LLM call."""
    if not messages:
        return ""

    summary_content = f"Round {round_num} execution process:\n\n"
    for msg in messages:
        if msg.role == "assistant":
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            summary_content += f"Assistant: {text}\n"
            if msg.tool_calls:
                names = [tc.function.name for tc in msg.tool_calls]
                summary_content += f"  → Called tools: {', '.join(names)}\n"
        elif msg.role == "tool":
            preview = msg.content if isinstance(msg.content, str) else str(msg.content)
            summary_content += f"  ← Tool returned: {preview}...\n"

    try:
        prompt = (
            f"Please provide a concise summary of the following Agent execution process:\n\n"
            f"{summary_content}\n\n"
            "Requirements:\n"
            "1. Focus on what tasks were completed and which tools were called\n"
            "2. Keep key execution results and important findings\n"
            "3. Be concise and clear, within 1000 words\n"
            "4. Use English\n"
            "5. Do not include \"user\" related content, only summarize the Agent's execution process"
        )
        response: LLMResponse = await llm.generate(
            messages=[
                Message(role="system", content="You are an assistant skilled at summarizing Agent execution processes."),
                Message(role="user", content=prompt),
            ]
        )
        return response.content
    except Exception:
        return summary_content


async def _maybe_summarize(
    llm,
    messages: list[Message],
    token_limit: int,
    api_total_tokens: int,
    skip_check: bool,
) -> tuple[list[Message] | None, bool, int]:
    """Check token usage and summarize if needed.

    Returns:
        (new_messages_or_None, skip_next, estimated_tokens)
    """
    if skip_check:
        return None, False, 0

    estimated = _estimate_tokens(messages)
    if estimated <= token_limit and api_total_tokens <= token_limit:
        return None, False, estimated

    # Build summarized message list
    user_indices = [i for i, m in enumerate(messages) if m.role == "user" and i > 0]
    if len(user_indices) < 1:
        return None, False, estimated

    new_messages: list[Message] = [messages[0]]  # system prompt

    for idx, user_idx in enumerate(user_indices):
        new_messages.append(messages[user_idx])

        next_boundary = user_indices[idx + 1] if idx < len(user_indices) - 1 else len(messages)
        exec_msgs = messages[user_idx + 1 : next_boundary]

        if exec_msgs:
            summary = await _create_summary(llm, exec_msgs, idx + 1)
            if summary:
                new_messages.append(
                    Message(role="user", content=f"[Assistant Execution Summary]\n\n{summary}")
                )

    return new_messages, True, estimated


# ── Cleanup helper ──────────────────────────────────────────────


def _cleanup_incomplete_messages(messages: list[Message]) -> int:
    """Remove trailing incomplete assistant + tool messages. Returns removed count."""
    last_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            last_idx = i
            break
    if last_idx == -1:
        return 0
    removed = len(messages) - last_idx
    del messages[last_idx:]
    return removed


# ── Main loop ───────────────────────────────────────────────────


async def run_agent_loop(
    *,
    llm,
    messages: list[Message],
    tools: dict[str, Tool],
    max_steps: int = 50,
    token_limit: int = 80000,
    is_cancelled: CancelChecker | None = None,
    logger: AgentLogger | None = None,
    workspace_dir: str | None = None,
) -> AsyncIterator[AgentEvent]:
    """Execute the agent loop, yielding structured events.

    This is the single source of truth for the agent execution loop.
    It does **not** print anything to stdout.  Consumers (CLI, ACP,
    JSON-RPC) decide how to render each event.

    Args:
        llm: LLM client (must have an async ``generate()`` method).
        messages: Message history (mutated in-place).
        tools: ``{name: Tool}`` dict.
        max_steps: Maximum LLM call iterations.
        token_limit: Token threshold for triggering summarization.
        is_cancelled: Optional callable — return ``True`` to stop.
        logger: Optional ``AgentLogger`` for file-based logging.
        workspace_dir: Workspace directory for artifact detection.
    """
    cancelled = is_cancelled or (lambda: False)

    if logger:
        logger.start_new_run()
        log_path = logger.get_log_file_path()
        if log_path:
            yield LogFileEvent(path=str(log_path))

    api_total_tokens = 0
    skip_next_token_check = False
    run_start = perf_counter()

    for step in range(max_steps):
        # ── Cancellation check (top of step) ────────────────
        # No cleanup needed here — messages are consistent at step boundaries.
        if cancelled():
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        step_start = perf_counter()

        # ── Summarization ───────────────────────────────────
        result = await _maybe_summarize(llm, messages, token_limit, api_total_tokens, skip_next_token_check)
        new_msgs, skip_next_token_check, est_before = result
        if new_msgs is not None:
            yield SummarizationEvent(estimated_tokens=est_before, api_tokens=api_total_tokens, token_limit=token_limit)
            messages.clear()
            messages.extend(new_msgs)

        # ── Step start ──────────────────────────────────────
        yield StepStart(step=step + 1, max_steps=max_steps)

        # ── LLM call (streaming) ──────────────────────────────
        tool_list = list(tools.values())
        if logger:
            logger.log_request(messages=messages, tools=tool_list)

        try:
            # Stream thinking/text deltas, accumulate for final response
            text_content = ""
            thinking_content = ""
            finish_event: StreamEvent | None = None
            thinking_header_yielded = False
            content_header_yielded = False

            async for chunk in llm.generate_stream(messages=messages, tools=tool_list):
                if chunk.type == "thinking":
                    if not thinking_header_yielded:
                        yield ThinkingEvent(content="", _streaming=True, _header=True)
                        thinking_header_yielded = True
                    thinking_content += chunk.delta
                    yield ThinkingEvent(content=chunk.delta, _streaming=True)
                elif chunk.type == "text":
                    if not content_header_yielded:
                        yield ContentEvent(content="", _streaming=True, _header=True)
                        content_header_yielded = True
                    text_content += chunk.delta
                    yield ContentEvent(content=chunk.delta, _streaming=True)
                elif chunk.type == "finish":
                    finish_event = chunk

            if finish_event is None:
                msg = "LLM stream ended without a finish event"
                yield ErrorEvent(message=msg, is_fatal=True)
                yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
                return

            # Build LLMResponse equivalent from streamed data
            response = LLMResponse(
                content=text_content,
                thinking=thinking_content if thinking_content else None,
                tool_calls=finish_event.tool_calls,
                finish_reason=finish_event.finish_reason or "stop",
                usage=finish_event.usage,
            )

        except Exception as exc:
            from .retry import RetryExhaustedError

            if isinstance(exc, RetryExhaustedError):
                msg = f"LLM call failed after {exc.attempts} retries\nLast error: {exc.last_exception!s}"
            else:
                msg = f"LLM call failed: {exc!s}"
            yield ErrorEvent(message=msg, is_fatal=True, exception=exc)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return

        # ── Token tracking ──────────────────────────────────
        if response.usage:
            api_total_tokens = response.usage.total_tokens
            yield TokenUsageEvent(total_tokens=api_total_tokens)

        # ── Log response ────────────────────────────────────
        if logger:
            logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
            )

        # ── Append assistant message ────────────────────────
        assistant_msg = Message(
            role="assistant",
            content=response.content,
            thinking=response.thinking,
            tool_calls=response.tool_calls,
        )
        messages.append(assistant_msg)

        # ── No tool calls → done ───────────────────────────
        if not response.tool_calls:
            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=response.content)
            return

        # ── Cancellation check (before tools) ──────────────
        if cancelled():
            _cleanup_incomplete_messages(messages)
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        # ── Execute tool calls ──────────────────────────────
        # Split into regular (sequential) and parallel_safe groups.
        regular_calls = []
        parallel_calls = []
        for tc in response.tool_calls:
            fn_name = tc.function.name
            if fn_name in tools and getattr(tools[fn_name], "parallel_safe", False):
                parallel_calls.append(tc)
            else:
                regular_calls.append(tc)

        # 1. Sequential execution for regular tools (preserves ordering)
        for tc in regular_calls:
            tc_id = tc.id
            fn_name = tc.function.name
            fn_args = tc.function.arguments

            yield ToolCallStart(tool_call_id=tc_id, tool_name=fn_name, arguments=fn_args)

            # Snapshot workspace before tool execution for diff-based artifact detection
            pre_files: set[Path] = set()
            if workspace_dir:
                pre_files = _snapshot_workspace(workspace_dir)

            if fn_name not in tools:
                result = ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
            else:
                try:
                    result = await tools[fn_name].execute(**fn_args)
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc!s}"
                    trace = traceback.format_exc()
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                    )

            # Log tool result
            if logger:
                logger.log_tool_result(
                    tool_name=fn_name,
                    arguments=fn_args,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
                )

            yield ToolCallResult(
                tool_call_id=tc_id,
                tool_name=fn_name,
                success=result.success,
                content=result.content,
                error=result.error,
            )

            # Detect and yield structured artifacts (images, files) from tool output
            if result.success and workspace_dir:
                # Regex-based: detect [filename.ext] references in output
                regex_artifacts = _detect_artifacts(tc_id, fn_name, result.content, workspace_dir)
                for artifact in regex_artifacts:
                    yield artifact
                # Diff-based: catch files not referenced in output text
                post_files = _snapshot_workspace(workspace_dir)
                already = {a.path for a in regex_artifacts}
                for artifact in _detect_new_files(tc_id, pre_files, post_files, already, workspace_dir):
                    yield artifact

            # Append tool message
            tool_msg = Message(
                role="tool",
                content=result.content if result.success else f"Error: {result.error}",
                tool_call_id=tc_id,
                name=fn_name,
            )
            messages.append(tool_msg)

            # Cancellation check after each tool
            if cancelled():
                _cleanup_incomplete_messages(messages)
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        # 2. Parallel execution for parallel_safe tools (e.g. sub_agent)
        if parallel_calls:
            # Emit all ToolCallStart events first
            for tc in parallel_calls:
                yield ToolCallStart(
                    tool_call_id=tc.id,
                    tool_name=tc.function.name,
                    arguments=tc.function.arguments,
                )

            async def _run_parallel(tc):
                fn_name = tc.function.name
                fn_args = tc.function.arguments
                if fn_name not in tools:
                    return tc, ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
                try:
                    r = await tools[fn_name].execute(**fn_args)
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc!s}"
                    trace = traceback.format_exc()
                    r = ToolResult(
                        success=False,
                        content="",
                        error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                    )
                return tc, r

            gathered = await asyncio.gather(*[_run_parallel(tc) for tc in parallel_calls])

            for tc, result in gathered:
                tc_id = tc.id
                fn_name = tc.function.name
                fn_args = tc.function.arguments

                if logger:
                    logger.log_tool_result(
                        tool_name=fn_name,
                        arguments=fn_args,
                        result_success=result.success,
                        result_content=result.content if result.success else None,
                        result_error=result.error if not result.success else None,
                    )

                yield ToolCallResult(
                    tool_call_id=tc_id,
                    tool_name=fn_name,
                    success=result.success,
                    content=result.content,
                    error=result.error,
                )

                # Append tool message
                tool_msg = Message(
                    role="tool",
                    content=result.content if result.success else f"Error: {result.error}",
                    tool_call_id=tc_id,
                    name=fn_name,
                )
                messages.append(tool_msg)

        # ── Step end ────────────────────────────────────────
        elapsed = perf_counter() - step_start
        total = perf_counter() - run_start
        yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)

    # ── Max steps exhausted ─────────────────────────────────
    msg = f"Task couldn't be completed after {max_steps} steps."
    yield DoneEvent(stop_reason=StopReason.MAX_STEPS, final_content=msg)
