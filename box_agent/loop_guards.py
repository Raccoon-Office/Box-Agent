"""Loop guards & continuation nudges for the agent execution loop.

These are the *pure, stateless* building blocks behind the family of
opt-in circuit breakers that keep :func:`box_agent.core.run_agent_loop`
from flailing or stopping prematurely:

- tool-call budget messages (cap repeated web_search etc.),
- the near-limit and no-progress wrap-up nudges.

Everything here is side-effect-free so it can be unit-tested in isolation. The
actual loop wiring — counters, one-shot flags, message injection — stays
in ``core`` where the loop state lives.

Where to put things when adding a new circuit breaker:

- Pure logic (decide *whether* to fire, build *what text* to inject,
  constants/thresholds) → here, as a function or dataclass that takes
  loop facts as plain arguments and returns a value. No ``yield``, no
  ``messages`` mutation, no reference to loop-local variables.
- Wiring (the counters/flags it reads, the ``messages.append`` +
  ``yield InjectedMessageEvent``, the ``continue``/``return``) → in
  ``core.run_agent_loop``, calling into the pure helper here.

This split keeps ``core`` focused on control flow and keeps every
breaker's decision logic independently testable.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .config import ToolLimitsConfig

# ── Constants ────────────────────────────────────────────────────

WEB_SEARCH_TOOL_NAME: Final = "web_search"
SEARCH_FILES_TOOL_NAME: Final = "search_files"
_DEFAULT_TOOL_LIMITS: Final = ToolLimitsConfig()

# Backward-compatible aliases for callers/tests that inspect the shipped
# defaults. Runtime execution reads the active ToolLimitsConfig instead.
WEB_SEARCH_BATCH_SIZE: Final = _DEFAULT_TOOL_LIMITS.web_search.batch_size
WEB_SEARCH_TOTAL_LIMIT: Final = _DEFAULT_TOOL_LIMITS.web_search.total_calls
DEEP_RESEARCH_WEB_SEARCH_TOTAL_LIMIT: Final = (
    _DEFAULT_TOOL_LIMITS.web_search.deep_research_total_calls
)
SEARCH_FILES_EMPTY_RESULT_LIMIT: Final = (
    _DEFAULT_TOOL_LIMITS.search_files.consecutive_empty_limit
)

# Per-turn call caps for tools the model tends to over-request.
TOOL_CALL_LIMITS: Final[dict[str, int]] = {
    WEB_SEARCH_TOOL_NAME: WEB_SEARCH_TOTAL_LIMIT,
}

# Setup/bookkeeping tools that must NOT count toward the final-summary
# wrap-up threshold. That threshold targets process-log answers after many
# *substantive* tool calls; loading skills, publishing the plan/todos, or
# touching memory are runtime scaffolding, not the activity it targets.
# Counting them can trip the wrap-up nudge before real work begins.
FINAL_SUMMARY_EXCLUDED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_skill",
        "plan_write",
        "todo_write",
        "todo_read",
        "memory_read",
        "memory_write",
        "memory_search",
        "request_user_input",
        "request_user_decision",
    }
)

# Reserve this many trailing steps for synthesis (near-limit wrap-up).
WRAPUP_REMAINING: Final[int] = (
    _DEFAULT_TOOL_LIMITS.general.wrapup_remaining_steps
)

# Abort after this many consecutive all-empty-args tool_call turns.
EMPTY_ARGS_LIMIT: Final[int] = 2

# Stop a provider stream before a short exact pattern can flood the UI and
# conversation history. Whitespace is ignored so relays that alternate a tag
# with blank lines are still caught, while the minimum pattern length and
# repeat count keep ordinary prose out of the guard.
STREAM_REPEAT_MIN_PATTERN_CHARS: Final[int] = 4
STREAM_REPEAT_MAX_PATTERN_CHARS: Final[int] = 80
STREAM_REPEAT_LIMIT: Final[int] = 8
STREAM_REPEAT_WINDOW_CHARS: Final[int] = 4096
STREAM_REPEAT_MIN_CHUNKS: Final[int] = 4


def repeated_stream_pattern(text: str) -> str | None:
    """Return a short suffix pattern repeated pathologically in ``text``.

    Detection is deliberately limited to eight exact repeats of a 4-80
    character whitespace-insensitive pattern. The pattern must contain a
    letter or number and at least two distinct characters, which avoids
    tripping on normal markdown separators or a long punctuation run.
    """
    if not isinstance(text, str) or not text:
        return None
    compact = re.sub(r"\s+", "", text[-STREAM_REPEAT_WINDOW_CHARS:])
    max_pattern_length = min(
        STREAM_REPEAT_MAX_PATTERN_CHARS,
        len(compact) // STREAM_REPEAT_LIMIT,
    )
    for pattern_length in range(
        STREAM_REPEAT_MIN_PATTERN_CHARS,
        max_pattern_length + 1,
    ):
        repeated_length = pattern_length * STREAM_REPEAT_LIMIT
        suffix = compact[-repeated_length:]
        pattern = suffix[-pattern_length:]
        if (
            pattern * STREAM_REPEAT_LIMIT == suffix
            and any(char.isalnum() for char in pattern)
            and len(set(pattern)) >= 2
        ):
            return pattern
    return None


# ── Tool-call budget messages ────────────────────────────────────


def tool_call_budget_message(tool_name: str, limit: int) -> str:
    """Synthetic tool-error text returned once a tool's per-turn budget is hit."""
    return (
        f"Tool call budget reached for {tool_name} ({limit} calls this turn). "
        f"Do not call {tool_name} again; continue the current deliverable and "
        "final response from the evidence and tool results already collected. "
        "If anything is missing, briefly mark it as a gap instead of searching "
        "again."
    )

def tool_call_budget_wrapup_text(tool_name: str, limit: int) -> str:
    """One-shot wrap-up nudge injected when a tool's per-turn budget is hit."""
    return (
        f"⚠️ 本轮 {tool_name} 调用已达到预算上限（{limit} 次）。"
        f"现在请停止继续调用 {tool_name} 或继续联网搜索，"
        "仅基于已经获得的资料继续完成当前交付物和最终回复；缺口简要标注即可。"
    )


def total_tool_call_budget_message(limit: int) -> str:
    """Synthetic error once the per-loop total tool budget is exhausted."""
    return (
        f"Total tool call budget reached ({limit} calls this task). "
        "Do not call any more tools; synthesize the final answer from the "
        "evidence and tool results already collected."
    )


def delegated_tool_call_budget_message(limit: int) -> str:
    """Synthetic error after delegated child work reaches its separate cap."""
    return (
        f"Delegated tool call budget reached ({limit} child calls this task). "
        "Do not start another sub_agent. Continue with the parent tools to merge, "
        "verify, finalize, and deliver the artifacts already produced."
    )


def delegated_tool_call_budget_wrapup_text(limit: int) -> str:
    """One-shot guidance when the delegated-work budget is exhausted."""
    return (
        f"⚠️ 本任务的子 Agent 内部工具预算已达到上限（{limit} 次）。"
        "不要再启动新的 sub_agent；请使用主 Agent 剩余工具额度完成结果合并、"
        "最终验证与交付。"
    )


def total_tool_call_budget_wrapup_text(limit: int) -> str:
    """One-shot synthesis nudge for the total tool-call hard limit."""
    return (
        f"⚠️ 本任务工具调用总预算已达到上限（{limit} 次）。"
        "现在请停止调用任何工具，仅基于已有结果直接给出完整最终答案；"
        "缺口简要标注即可。"
    )


def search_files_result_is_empty(result: Any) -> bool:
    """Return whether a successful search_files call found no matches.

    Prefer structured metadata so wording changes do not disable the guard.
    The text fallback keeps compatibility with older or third-party tool
    implementations that only return the standard no-match sentence.
    """
    if not bool(getattr(result, "success", False)):
        return False
    raw_output = getattr(result, "raw_output", None)
    if isinstance(raw_output, dict):
        returned_matches = raw_output.get("returned_matches")
        timed_out = raw_output.get("timed_out") is True
        if isinstance(returned_matches, int) and not isinstance(returned_matches, bool):
            return returned_matches == 0 and not timed_out
    content = getattr(result, "content", "")
    return isinstance(content, str) and content.strip().lower() == "no matches found."


def search_files_empty_result_message(limit: int) -> str:
    """Synthetic tool error after repeated empty file searches."""
    return (
        f"search_files circuit breaker is open after {limit} consecutive empty results. "
        "Do not call search_files again this turn. Use known paths with read_file, "
        "inspect already collected evidence, or explain the missing file instead of "
        "trying more search patterns."
    )


def search_files_empty_result_guidance(limit: int) -> str:
    """One-shot model guidance when the empty-search breaker opens."""
    return (
        f"⚠️ search_files 已连续 {limit} 次返回空结果，文件搜索熔断器已打开。"
        "本轮不要再调用 search_files；请改用已知路径配合 read_file、基于已有证据继续，"
        "或明确说明文件缺失。不要继续更换 pattern/path 盲搜。"
    )


# ── Near-limit / no-progress wrap-up nudges ──────────────────────


def near_limit_wrapup_text(step: int, max_steps: int) -> str:
    """Reserve the final steps for synthesis: stop gathering, answer now.

    ``step`` is the 0-based loop index (as in ``run_agent_loop``).
    """
    remaining = max_steps - step
    return (
        f"⚠️ 步数预算即将用尽（已到第 {step + 1}/{max_steps} 步，约剩 {remaining} 步）。"
        "现在请停止调用任何工具、停止继续搜索或探索。"
        "仅基于你已经收集到的信息，在本轮直接给出完整、可独立阅读的最终答案/总结："
        "包含关键结论、数据、以及已产出的文件路径；若有未覆盖的缺口，简要标注即可，"
        "不要再去调查。"
    )


def no_progress_wrapup_text(no_progress_steps: int) -> str:
    """Force a synthesis after N consecutive steps with no useful tool result."""
    return (
        f"⚠️ 已连续 {no_progress_steps} 步没有取得有效进展"
        "（工具调用持续失败或无有用输出）。"
        "现在请立即停止调用任何工具、停止重试当前路径。"
        "仅基于你已经收集到的信息，在本轮直接给出完整、可独立阅读的"
        "最终答案/总结：包含关键结论、已知数据与已产出的文件路径；"
        "对未能获取的信息，简要标注为缺口即可，不要再继续调查。"
    )


# ── Mid-turn injection wrapper ───────────────────────────────────


def format_injected_message(text: str) -> str:
    """Wrap mid-stream user input so it steers the active task."""
    return (
        "The user sent the following message while the current task was already running.\n"
        "Treat it as mid-turn guidance, a constraint, or a clarification for the current task, "
        "not as a new standalone task.\n"
        "If it asks a question, answer it briefly if useful, then continue the original task. "
        "Do not stop or switch tasks unless the user explicitly asks you to stop, cancel, or change the task.\n\n"
        f"Mid-turn user message:\n{text}"
    )


def format_runtime_context_update(text: str) -> str:
    """Wrap an authoritative host/runtime state change without impersonating the user."""
    return (
        "The host runtime supplied the following internal state update while the current "
        "task was running. Treat it as authoritative runtime context, not as a user message. "
        "Use it when continuing the current task, but do not quote this wrapper to the user.\n\n"
        f"Runtime state update:\n{text}"
    )


# ── Suspected-truncation continuation ────────────────────────────
#
# Some upstream models / relay gateways stop a streamed text turn
# mid-sentence yet report a *normal* finish_reason ("stop"/"end_turn")
# or omit it entirely. The existing ``finish_reason in ("length",
# "max_tokens")`` guard in ``core`` never fires for these, so the half
# sentence is presented as a finished answer. The helpers below let the
# loop detect that case (conservatively) and inject a one-shot
# continuation so the model finishes the thought in the same message.

# Only consider a turn truncated when the model actually produced a
# non-trivial amount of text. Short replies legitimately end without
# terminal punctuation (e.g. a bare "好的" / a single path), and we do
# not want to chase those.
MIN_TOKENS_FOR_TRUNCATION_CHECK: Final[int] = 50

# Character-count fallback for the same "non-trivial reply" gate when the
# provider omits usage (or reports completion_tokens=0). Production
# gateways send usage, so this only guards degenerate/no-usage paths.
MIN_CHARS_FOR_TRUNCATION_CHECK: Final[int] = 40

# Trailing characters that count as a *clean* ending — if the text ends
# with any of these we never treat it as truncated. Covers CJK + ASCII
# sentence punctuation, closing quotes/brackets, colons/semicolons
# (section leads), markdown emphasis/inline-code closers, table pipes,
# and dashes.
_CLEAN_ENDING_CHARS: Final[frozenset[str]] = frozenset(
    "。．.！!？?…⋯"  # sentence terminators
    "」』）)】］]｝}＞>"  # closing brackets
    "\"'”’《》"  # quotes
    "：:；;"  # colon / semicolon (list or section lead-ins)
    "*`"  # markdown emphasis / inline code closers
    "|"  # table row
    "—～~"  # dashes / tilde
)

# Markdown structural last-lines that are complete as-is.
_TABLE_ROW_RE: Final = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM_RE: Final = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")
_ATOMIC_ASCII_REPLY_RE: Final = re.compile(
    r"^[A-Za-z0-9./\\:@+#%?&=~_-]{1,256}$"
)
_ATOMIC_REPLY_WORDS: Final[frozenset[str]] = frozenset(
    {"ok", "done", "success", "failed", "true", "false", "none", "null"}
)


def _looks_like_atomic_ascii_reply(text: str) -> bool:
    """Return true for complete machine-like status, ID, URL, or path replies."""
    if not _ATOMIC_ASCII_REPLY_RE.fullmatch(text):
        return False
    return (
        text.casefold() in _ATOMIC_REPLY_WORDS
        or (text.upper() == text and any(char.isalpha() for char in text))
        or any(char.isdigit() for char in text)
        or any(char in "._/\\:@+#%?&=~-" for char in text)
    )


def looks_like_truncated_output(text: str) -> bool:
    """Conservatively decide whether assistant text was cut mid-thought.

    Bias: prefer a false negative (miss a genuinely truncated reply that
    happens to end without punctuation) over a false positive (re-prompt
    a perfectly complete answer). Any "clean ending" signal — terminal
    punctuation, a closed bracket/quote/emphasis, or a complete markdown
    structural line (code fence, table row, list item) — returns False.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    if _looks_like_atomic_ascii_reply(stripped):
        return False
    if stripped[-1] in _CLEAN_ENDING_CHARS:
        return False
    last_line = stripped.rsplit("\n", 1)[-1].strip()
    if last_line.startswith("```"):
        return False
    if _TABLE_ROW_RE.match(last_line):
        return False
    if _LIST_ITEM_RE.match(last_line):
        return False
    return True


def reply_is_substantial(content_len: int, completion_tokens: int | None) -> bool:
    """Gate truncation handling to non-trivial replies only.

    Prefer the provider's completion-token count; fall back to character
    length when usage is absent or zero (degenerate / no-usage gateways),
    so a short reply without usage is not chased as a truncation.
    """
    if completion_tokens:
        return completion_tokens >= MIN_TOKENS_FOR_TRUNCATION_CHECK
    return content_len >= MIN_CHARS_FOR_TRUNCATION_CHECK


def truncation_continuation_text(tail: str) -> str:
    """One-shot continuation prompt for a suspected mid-sentence cutoff.

    Deliberately NOT wrapped by ``format_injected_message``: this is not
    a user interjection but a system-detected continuation instruction,
    so it must carry its own framing. ``tail`` is a short slice of where
    the previous reply stopped, to anchor the model.
    """
    return (
        "（系统提示）你上一条回复似乎在生成过程中被意外中断了，"
        f"结尾停在：“…{tail}”。\n"
        "请直接接着上面的结尾继续写完剩余内容，保持原有的格式、结构与语气；"
        "不要重复任何已经输出过的内容，也不要重新开头或重述前面已说过的部分，"
        "从断点处自然衔接即可。如果上一条其实已经表达完整，只需补一句简短收尾。"
    )
