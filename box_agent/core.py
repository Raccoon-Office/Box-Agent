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
from typing import Any, Callable

import tiktoken

from .events import (
    AgentEvent,
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LogFileEvent,
    PPTProgressEvent,
    PermissionRequestEvent,
    StepEnd,
    StepStart,
    StopReason,
    SubAgentEvent,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult,
    ToolCallStart,
)
from .hooks import HookManager
from .logger import AgentLogger
from .schema import LLMResponse, Message, StreamEvent
from .tools.base import EventEmittingTool, Tool, ToolResult

# Type alias — consumers supply a zero-arg callable that returns True
# when the execution should be cancelled.
CancelChecker = Callable[[], bool]

# Regex to match sandbox file references like [foo.png] or [PNG Image]
_ARTIFACT_REF_RE = re.compile(r"\[([^\]\n]+\.\w{1,10})\]", re.IGNORECASE)

# Image extensions for artifact_type classification
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg"}

# Pattern to match <!--PLOT_DATA:...--> markers embedded by code execution.
# These carry interactive chart payloads already sent to the frontend via SSE;
# they must NOT be fed back into the model context.
_PLOT_DATA_RE = re.compile(r"<!--PLOT_DATA:.+?-->", re.DOTALL)


def _strip_plot_data(text: str) -> str:
    """Remove ``<!--PLOT_DATA:...-->`` markers from code-execution stdout.

    The markers contain chart data already delivered to the frontend through
    SSE events.  Keeping them in the model context wastes tokens and can
    cause context-length issues.

    Returns a short placeholder when stripping leaves the string empty.
    """
    cleaned = _PLOT_DATA_RE.sub("", text).strip()
    return cleaned if cleaned else "图表已生成"


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


# ── Micro-compact (Layer 1) ─────────────────────────────────

# Number of recent tool messages to keep intact.
_KEEP_RECENT_TOOL_RESULTS = 3
# Tool results shorter than this are not worth compacting.
_MIN_COMPACT_LEN = 200


def _micro_compact(messages: list[Message]) -> int:
    """Replace old tool-result content with short placeholders.

    Walks the message list, finds tool-role messages, keeps the last
    ``_KEEP_RECENT_TOOL_RESULTS`` intact, and replaces earlier ones
    whose content exceeds ``_MIN_COMPACT_LEN`` with a one-liner.

    This is a cheap, zero-LLM-call operation that runs every step.

    Returns:
        Number of messages compacted.
    """
    # Collect indices of tool messages
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_indices) <= _KEEP_RECENT_TOOL_RESULTS:
        return 0

    compacted = 0
    for idx in tool_indices[:-_KEEP_RECENT_TOOL_RESULTS]:
        msg = messages[idx]
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if len(content) <= _MIN_COMPACT_LEN:
            continue
        tool_name = msg.name or "unknown"
        # Preserve the first line as a hint (often contains the key result)
        first_line = content.split("\n", 1)[0][:100]
        messages[idx] = Message(
            role="tool",
            content=f"[Previous result from {tool_name}: {first_line}...]",
            tool_call_id=msg.tool_call_id,
            name=msg.name,
        )
        compacted += 1

    return compacted


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
    permission_negotiator: Any | None = None,
    hooks: list | None = None,
    memory_extractor: Any | None = None,
    inject_queue: asyncio.Queue[str] | None = None,
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
        permission_negotiator: Optional negotiator (has async
            ``negotiate(permission_request)`` method) for in-band
            permission escalation.  When present, denied tool calls
            with ``permission_request`` are negotiated with the host
            and retried on grant.  When absent, ``PermissionRequestEvent``
            is yielded for backward compatibility.
        hooks: Optional list of lifecycle hook objects.  Each hook may
            implement any subset of the ``BaseHook`` interface.  Hooks
            are called at key lifecycle points (step start/end, tool
            start/result, done, error).  Loaded identically by CLI
            and ACP from ``config.yaml``.
        memory_extractor: Optional ``MemoryExtractor`` instance for
            lifecycle-triggered memory extraction.  When present,
            extraction is attempted before context compression and
            every N steps.
        inject_queue: Optional queue for in-stream message injection.
            When present, queued user messages are drained at each
            step boundary and appended to the conversation before
            the next LLM call.
    """
    cancelled = is_cancelled or (lambda: False)
    hook_mgr = HookManager(hooks)

    if logger:
        logger.start_new_run()
        log_path = logger.get_log_file_path()
        if log_path:
            yield LogFileEvent(path=str(log_path))

    if hook_mgr.hooks:
        await hook_mgr.fire_agent_start(messages=messages, tools=tools, max_steps=max_steps)

    api_total_tokens = 0
    skip_next_token_check = False
    run_start = perf_counter()

    for step in range(max_steps):
        # ── Cancellation check (top of step) ────────────────
        # No cleanup needed here — messages are consistent at step boundaries.
        if cancelled():
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
            return

        step_start = perf_counter()

        # ── Drain inject queue (in-stream injection) ───────
        if inject_queue:
            while not inject_queue.empty():
                injected_text = inject_queue.get_nowait()
                messages.append(Message(role="user", content=injected_text))
                yield InjectedMessageEvent(content=injected_text)

        # ── Micro-compact (Layer 1) ────────────────────────
        # Cheap: replace old tool results with placeholders
        _micro_compact(messages)

        # ── Summarization (Layer 2) ────────────────────────
        result = await _maybe_summarize(llm, messages, token_limit, api_total_tokens, skip_next_token_check)
        new_msgs, skip_next_token_check, est_before = result
        if new_msgs is not None:
            # Snapshot messages before compression, then extract in background
            if memory_extractor:
                _snapshot = list(messages)
                asyncio.create_task(memory_extractor.maybe_extract(_snapshot, "pre_summarize"))
            yield SummarizationEvent(estimated_tokens=est_before, api_tokens=api_total_tokens, token_limit=token_limit)
            messages.clear()
            messages.extend(new_msgs)

        # ── Step start ──────────────────────────────────────
        yield StepStart(step=step + 1, max_steps=max_steps)
        if hook_mgr.hooks:
            await hook_mgr.fire_step_start(step=step + 1, max_steps=max_steps)

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
                if hook_mgr.hooks:
                    await hook_mgr.fire_error(message=msg, is_fatal=True, exception=None)
                    await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
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
            if hook_mgr.hooks:
                await hook_mgr.fire_error(message=msg, is_fatal=True, exception=exc)
                await hook_mgr.fire_done(stop_reason=StopReason.ERROR, final_content=msg)
            yield ErrorEvent(message=msg, is_fatal=True, exception=exc)
            yield DoneEvent(stop_reason=StopReason.ERROR, final_content=msg)
            return

        # ── Token tracking ──────────────────────────────────
        if response.usage:
            api_total_tokens = response.usage.total_tokens
            yield TokenUsageEvent(total_tokens=api_total_tokens)

        # ── Hook: LLM response ─────────────────────────────
        if hook_mgr.hooks:
            await hook_mgr.fire_llm_response(response=response)

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

        # ── No tool calls → done (or continue if injected) ──
        if not response.tool_calls:
            # Check inject queue — if messages are pending, continue
            # the loop so the LLM sees them on the next iteration.
            if inject_queue and not inject_queue.empty():
                elapsed = perf_counter() - step_start
                total = perf_counter() - run_start
                if hook_mgr.hooks:
                    await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                continue

            elapsed = perf_counter() - step_start
            total = perf_counter() - run_start
            if hook_mgr.hooks:
                await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
                await hook_mgr.fire_done(stop_reason=StopReason.END_TURN, final_content=response.content)
            # Extract memory at agent loop end (background)
            if memory_extractor:
                asyncio.create_task(memory_extractor.maybe_extract(messages, "loop_end"))
            yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=response.content)
            return

        # ── Cancellation check (before tools) ──────────────
        if cancelled():
            _cleanup_incomplete_messages(messages)
            if hook_mgr.hooks:
                await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
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

            # Hook: tool start (interceptor — may modify arguments)
            if hook_mgr.hooks:
                fn_args = await hook_mgr.fire_tool_start(
                    tool_call_id=tc_id, tool_name=fn_name, arguments=fn_args,
                )

            # Snapshot workspace before tool execution for diff-based artifact detection
            pre_files: set[Path] = set()
            if workspace_dir:
                pre_files = _snapshot_workspace(workspace_dir)

            if fn_name not in tools:
                result = ToolResult(success=False, content="", error=f"Unknown tool: {fn_name}")
            else:
                tool = tools[fn_name]
                if isinstance(tool, EventEmittingTool):
                    # Wire queue, run in background, drain in foreground
                    event_queue: asyncio.Queue = asyncio.Queue()
                    tool._event_queue = event_queue
                    tool._parent_tool_call_id = tc_id

                    exec_done = asyncio.Event()
                    exec_result: ToolResult | None = None

                    async def _seq_exec(t=tool, a=fn_args):
                        nonlocal exec_result
                        try:
                            exec_result = await t.execute(**a)
                        except Exception as exc:
                            detail = f"{type(exc).__name__}: {exc!s}"
                            trace = traceback.format_exc()
                            exec_result = ToolResult(
                                success=False,
                                content="",
                                error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
                            )
                        finally:
                            exec_done.set()

                    exec_task = asyncio.create_task(_seq_exec())
                    while not exec_done.is_set() or not event_queue.empty():
                        try:
                            evt = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                            yield evt
                        except (asyncio.TimeoutError, TimeoutError):
                            continue
                    while not event_queue.empty():
                        yield event_queue.get_nowait()
                    await exec_task
                    tool._event_queue = None
                    tool._parent_tool_call_id = ""
                    result = exec_result  # type: ignore[assignment]
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

            # ── Permission negotiation + retry ──────────────
            if not result.success and result.permission_request and permission_negotiator:
                granted = await permission_negotiator.negotiate(result.permission_request)
                if granted:
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
                    # Re-log after retry
                    if logger:
                        logger.log_tool_result(
                            tool_name=fn_name,
                            arguments=fn_args,
                            result_success=result.success,
                            result_content=result.content if result.success else None,
                            result_error=result.error if not result.success else None,
                        )

            # Hook: tool result (interceptor — may modify content/error)
            tc_content = result.content
            tc_error = result.error
            if hook_mgr.hooks:
                tc_content, tc_error = await hook_mgr.fire_tool_result(
                    tool_call_id=tc_id, tool_name=fn_name,
                    success=result.success, content=tc_content, error=tc_error,
                )

            yield ToolCallResult(
                tool_call_id=tc_id,
                tool_name=fn_name,
                success=result.success,
                content=tc_content,
                error=tc_error,
            )

            # Emit permission request event if tool was denied with escalation info
            # (only for legacy consumers without a negotiator)
            if not result.success and result.permission_request and not permission_negotiator:
                pr = {k: v for k, v in result.permission_request.items() if k != "type"}
                yield PermissionRequestEvent(tool_call_id=tc_id, **pr)

            # Detect and yield structured artifacts (images, files) from tool output
            if result.success and workspace_dir:
                # Regex-based: detect [filename.ext] references in output
                regex_artifacts = _detect_artifacts(tc_id, fn_name, tc_content, workspace_dir)
                for artifact in regex_artifacts:
                    yield artifact
                # Diff-based: catch files not referenced in output text
                post_files = _snapshot_workspace(workspace_dir)
                already = {a.path for a in regex_artifacts}
                for artifact in _detect_new_files(tc_id, pre_files, post_files, already, workspace_dir):
                    yield artifact

            # Append tool message (use possibly-modified content from hooks).
            # Strip <!--PLOT_DATA:--> markers so chart payloads (already sent
            # to the frontend via SSE) don't bloat the model context.
            msg_content = _strip_plot_data(tc_content) if result.success else f"Error: {tc_error}"
            tool_msg = Message(
                role="tool",
                content=msg_content,
                tool_call_id=tc_id,
                name=fn_name,
            )
            messages.append(tool_msg)

            # Cancellation check after each tool
            if cancelled():
                _cleanup_incomplete_messages(messages)
                if hook_mgr.hooks:
                    await hook_mgr.fire_done(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                yield DoneEvent(stop_reason=StopReason.CANCELLED, final_content="Task cancelled by user.")
                return

        # 2. Parallel execution for parallel_safe tools (e.g. sub_agent, ppt_emit_html)
        if parallel_calls:
            # Emit all ToolCallStart events and apply hook interceptors
            par_args_map: dict[str, dict[str, Any]] = {}  # tc.id → (possibly modified) args
            for tc in parallel_calls:
                yield ToolCallStart(
                    tool_call_id=tc.id,
                    tool_name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                par_fn_args = tc.function.arguments
                if hook_mgr.hooks:
                    par_fn_args = await hook_mgr.fire_tool_start(
                        tool_call_id=tc.id, tool_name=tc.function.name, arguments=par_fn_args,
                    )
                par_args_map[tc.id] = par_fn_args

            # Wire a shared event queue onto EventEmittingTool instances
            par_event_queue: asyncio.Queue[SubAgentEvent | PPTProgressEvent] = asyncio.Queue()
            emitting_tools: list[EventEmittingTool] = []
            for tc in parallel_calls:
                tool = tools.get(tc.function.name)
                if isinstance(tool, EventEmittingTool):
                    tool._event_queue = par_event_queue
                    tool._parent_tool_call_id = tc.id
                    emitting_tools.append(tool)

            async def _run_parallel(tc):
                fn_name = tc.function.name
                fn_args = par_args_map[tc.id]
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

            # Run gather in a background task; drain the queue in the
            # foreground generator loop so events are yielded in real-time.
            gather_done = asyncio.Event()

            async def _gather_wrapper():
                try:
                    return await asyncio.gather(*[_run_parallel(tc) for tc in parallel_calls])
                finally:
                    gather_done.set()

            gather_task = asyncio.create_task(_gather_wrapper())

            # Yield progress events as they arrive (real-time)
            while not gather_done.is_set() or not par_event_queue.empty():
                try:
                    evt = await asyncio.wait_for(par_event_queue.get(), timeout=0.1)
                    yield evt
                except (asyncio.TimeoutError, TimeoutError):
                    continue
            # Drain any stragglers enqueued between the last get() and now
            while not par_event_queue.empty():
                yield par_event_queue.get_nowait()

            gathered = await gather_task  # already done

            # Clean up queue references
            for tool in emitting_tools:
                tool._event_queue = None
                tool._parent_tool_call_id = ""

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

                # ── Permission negotiation + retry ──────────────
                if not result.success and result.permission_request and permission_negotiator:
                    granted = await permission_negotiator.negotiate(result.permission_request)
                    if granted:
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
                        if logger:
                            logger.log_tool_result(
                                tool_name=fn_name,
                                arguments=fn_args,
                                result_success=result.success,
                                result_content=result.content if result.success else None,
                                result_error=result.error if not result.success else None,
                            )

                # Hook: tool result (interceptor)
                par_content = result.content
                par_error = result.error
                if hook_mgr.hooks:
                    par_content, par_error = await hook_mgr.fire_tool_result(
                        tool_call_id=tc_id, tool_name=fn_name,
                        success=result.success, content=par_content, error=par_error,
                    )

                yield ToolCallResult(
                    tool_call_id=tc_id,
                    tool_name=fn_name,
                    success=result.success,
                    content=par_content,
                    error=par_error,
                )

                # Emit permission request event if tool was denied with escalation info
                # (only for legacy consumers without a negotiator)
                if not result.success and result.permission_request and not permission_negotiator:
                    pr = {k: v for k, v in result.permission_request.items() if k != "type"}
                    yield PermissionRequestEvent(tool_call_id=tc_id, **pr)

                # Append tool message — strip <!--PLOT_DATA:--> markers from
                # model context (same rationale as the sequential block above).
                msg_content = _strip_plot_data(par_content) if result.success else f"Error: {par_error}"
                tool_msg = Message(
                    role="tool",
                    content=msg_content,
                    tool_call_id=tc_id,
                    name=fn_name,
                )
                messages.append(tool_msg)

        # ── Step end ────────────────────────────────────────
        elapsed = perf_counter() - step_start
        total = perf_counter() - run_start
        yield StepEnd(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)
        if hook_mgr.hooks:
            await hook_mgr.fire_step_end(step=step + 1, elapsed_seconds=elapsed, total_elapsed_seconds=total)

        # ── Periodic memory extraction (background) ──────────
        if memory_extractor:
            asyncio.create_task(memory_extractor.maybe_extract(messages, "step_interval"))

    # ── Max steps exhausted ─────────────────────────────────
    msg = f"Task couldn't be completed after {max_steps} steps."
    if memory_extractor:
        asyncio.create_task(memory_extractor.maybe_extract(messages, "loop_end"))
    if hook_mgr.hooks:
        await hook_mgr.fire_done(stop_reason=StopReason.MAX_STEPS, final_content=msg)
    yield DoneEvent(stop_reason=StopReason.MAX_STEPS, final_content=msg)
