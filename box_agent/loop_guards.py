"""Loop guards & continuation nudges for the agent execution loop.

These are the *pure, stateless* building blocks behind the family of
opt-in circuit breakers that keep :func:`box_agent.core.run_agent_loop`
from flailing or stopping prematurely:

- tool-call budget messages (cap repeated web_search etc.),
- the completion gate (force continuation until verifiable evidence
  exists — borrowed in spirit from oh-my-codex's Stop gate, but
  evidence-based rather than prose-pattern-based),
- the near-limit and no-progress wrap-up nudges.

Everything here is side-effect-free (apart from read-only filesystem
stats for artifact checks) so it can be unit-tested in isolation. The
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

import glob
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
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
# touching memory are workflow scaffolding, not the activity it targets.
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


# ── Completion gate ──────────────────────────────────────────────


@dataclass(frozen=True)
class CompletionGate:
    """Opt-in completion gate for the agent loop.

    Borrowed in spirit from oh-my-codex's Stop gate, but deliberately
    evidence-based rather than prose-pattern-based: the gate only ever
    inspects *verifiable facts* (which tools produced a usable result,
    which artifact files exist) — never the assistant's wording.

    When supplied to :func:`box_agent.core.run_agent_loop`, a natural
    END_TURN (the model emits no tool calls) is intercepted: if any
    requirement is unmet, a continuation nudge naming the gaps is injected
    and the loop keeps going. A bounded ``max_continuations`` count plus an
    optional ``deadline_seconds`` guarantee the gate can never trap the
    agent forever — on exhaustion it releases and the turn ends normally.

    Disabled by default (callers pass ``None``); behaviour is then
    byte-for-byte unchanged.
    """

    # Tools that must each have produced at least one successful, non-empty
    # result before END_TURN is allowed.
    required_tools: frozenset[str] = field(default_factory=frozenset)
    # When a host-bound ``report_execution_result`` declares completed work,
    # require exact zero-based coverage of this many acceptance criteria
    # before treating the tool as satisfying ``required_tools``.
    execution_result_criteria_count: int | None = None
    # When true, expose only still-required tools to the model until they have
    # succeeded.  This is intentionally narrower than the completion check:
    # it prevents an alternate implementation path from taking over before a
    # mandatory standard capability (for example native image generation) is
    # attempted.
    restrict_tools_until_required_succeed: bool = False
    # Artifact files that must exist and be non-empty before END_TURN is
    # allowed. Resolved relative to ``workspace_dir`` (absolute paths kept).
    required_artifacts: tuple[str, ...] = ()
    # At least one artifact matching any of these globs must be new or changed
    # compared with ``baseline_artifact_signatures`` before END_TURN is allowed.
    required_changed_artifact_globs: tuple[str, ...] = ()
    baseline_artifact_signatures: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )
    # Changed artifacts with one of these suffixes additionally require a new
    # or updated JSON report whose top-level ``ok`` value is true.
    required_success_report_globs: tuple[str, ...] = ()
    success_report_artifact_suffixes: frozenset[str] = field(
        default_factory=frozenset
    )
    baseline_success_report_signatures: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )
    # Safety valve: max number of continuation nudges the gate may inject.
    max_continuations: int = 3
    # Safety valve: release the gate once the run exceeds this many seconds.
    # ``None`` disables the time limit.
    deadline_seconds: float | None = None
    # Optional total tool-call budget for this gated run. ``run_agent_loop``
    # adopts it only when the caller did not provide a stricter explicit cap.
    max_tool_calls: int | None = None
    # Optional workflow-specific cap for external search.
    web_search_total_limit: int | None = None
    # Tool names excluded from ``max_tool_calls`` for this workflow. Explicit
    # caller-provided budgets keep their original all-tools semantics unless
    # the caller also opts into exemptions through the gate.
    budget_exempt_tools: frozenset[str] = field(default_factory=frozenset)
    # Number of trailing budgeted calls reserved for deterministic completion
    # work. Core emits one evidence-backed nudge at ``max - reserve`` so the
    # model stops discovery and runs patch → validate → render → QA.
    completion_reserve_tool_calls: int = 0
    # A successful call to one of these tools is a valid resumable pause. The
    # completion gate allows END_TURN even when artifact gaps remain; the ACP
    # session retains the gate and resumes it after the user's next answer.
    pause_tools: frozenset[str] = field(default_factory=frozenset)
    # Optional filesystem-backed workflow checkpoint injected before each LLM
    # step.  Unlike conversational history, this state is re-derived from the
    # canonical artifacts, so a long-running model cannot accidentally regress
    # to an already-completed authoring stage after context compaction or
    # attention drift.
    workflow_checkpoint_kind: str | None = None
    # Workflow-owned configuration. The kernel treats these values as opaque;
    # only the selected WorkflowPolicy may interpret their keys.
    workflow_options: dict[str, Any] = field(default_factory=dict)


def completion_gate_gaps(
    gate: CompletionGate,
    succeeded_tools: set[str],
    workspace_dir: str | None,
) -> list[str]:
    """Return human-readable descriptions of unmet gate requirements.

    Empty list means every requirement is satisfied. Pure function: no
    side effects beyond read-only filesystem stats for artifact checks.
    """
    gaps: list[str] = []
    for tool_name in sorted(gate.required_tools):
        if tool_name not in succeeded_tools:
            gaps.append(f"工具 `{tool_name}` 尚未成功调用并返回有效结果")
    base = Path(workspace_dir) if workspace_dir else None
    for artifact in gate.required_artifacts:
        path = Path(artifact)
        if not path.is_absolute() and base is not None:
            path = base / path
        try:
            exists_nonempty = path.is_file() and path.stat().st_size > 0
        except OSError:
            exists_nonempty = False
        if not exists_nonempty:
            gaps.append(f"产物文件 `{artifact}` 不存在或为空")
    changed_artifacts = _changed_artifacts(
        gate.required_changed_artifact_globs,
        gate.baseline_artifact_signatures,
        base,
    )
    if gate.required_changed_artifact_globs and not changed_artifacts:
        patterns = ", ".join(
            f"`{pattern}`" for pattern in gate.required_changed_artifact_globs
        )
        gaps.append(f"尚未产生新的或更新过的交付产物（匹配：{patterns}）")
    report_required_artifacts = [
        path
        for path in changed_artifacts
        if path.suffix.lower() in gate.success_report_artifact_suffixes
    ]
    if (
        gate.required_success_report_globs
        and report_required_artifacts
        and (
            missing_reports := [
                pattern
                for pattern in gate.required_success_report_globs
                if not _has_changed_success_report(
                    (pattern,),
                    gate.baseline_success_report_signatures,
                    base,
                    {path.parent.resolve() for path in report_required_artifacts},
                )
            ]
        )
    ):
        patterns = ", ".join(f"`{pattern}`" for pattern in missing_reports)
        gaps.append(
            "交付物 QA 尚未完成：需要以下新的或更新过的成功报告"
            f"（匹配：{patterns}，且 JSON `ok` 必须为 true）"
        )
    return gaps


def completion_gate_tool_satisfies_requirements(
    gate: CompletionGate,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Return whether a successful tool call satisfies host-bound gate facts."""
    expected_count = gate.execution_result_criteria_count
    if tool_name != "report_execution_result" or expected_count is None:
        return True
    if arguments.get("outcome") != "completed":
        return True
    evaluations = arguments.get("criteria_evaluations")
    if not isinstance(evaluations, list) or len(evaluations) != expected_count:
        return False
    indices = [
        evaluation.get("criterion_index")
        for evaluation in evaluations
        if isinstance(evaluation, dict)
    ]
    return (
        len(indices) == expected_count
        and all(
            isinstance(index, int) and not isinstance(index, bool)
            for index in indices
        )
        and set(indices) == set(range(expected_count))
    )


def artifact_signatures_for_globs(
    patterns: tuple[str, ...],
    workspace_dir: str | None,
) -> dict[str, tuple[int, int]]:
    """Snapshot file signatures for artifact glob checks.

    Keys are resolved absolute paths; values are ``(size, mtime_ns)``. Missing
    workspaces simply produce an empty baseline, so later created artifacts can
    still satisfy the gate.
    """
    base = Path(workspace_dir) if workspace_dir else None
    signatures: dict[str, tuple[int, int]] = {}
    for path in _iter_artifact_glob_matches(patterns, base):
        signature = _artifact_signature(path)
        if signature is not None:
            signatures[str(path.resolve())] = signature
    return signatures


def _iter_artifact_glob_matches(
    patterns: tuple[str, ...],
    base: Path | None,
) -> list[Path]:
    matches: list[Path] = []
    for pattern in patterns:
        path_pattern = Path(pattern)
        if path_pattern.is_absolute():
            candidates = [Path(p) for p in glob.glob(pattern, recursive=True)]
        elif base is not None:
            candidates = list(base.glob(pattern))
        else:
            candidates = []
        matches.extend(
            path
            for path in candidates
            if path.is_file() and not _is_internal_or_dependency_artifact(path)
        )
    return matches


def _is_internal_or_dependency_artifact(path: Path) -> bool:
    """Exclude staging, caches, and package fixtures from delivery evidence."""
    parts = tuple(part.casefold() for part in path.parts)
    excluded_parts = {
        ".box-agent-staging",
        ".cache",
        "__pycache__",
        "node_modules",
    }
    if any(part in excluded_parts for part in parts):
        return True
    fixture_dirs = {"test", "tests", "example", "examples", "demo", "demos"}
    return any(
        parts[index] == "package" and parts[index + 1] in fixture_dirs
        for index in range(len(parts) - 1)
    )


def _artifact_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size <= 0:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _has_changed_artifact(
    patterns: tuple[str, ...],
    baseline: dict[str, tuple[int, int]],
    base: Path | None,
) -> bool:
    return bool(_changed_artifacts(patterns, baseline, base))


def _changed_artifacts(
    patterns: tuple[str, ...],
    baseline: dict[str, tuple[int, int]],
    base: Path | None,
) -> list[Path]:
    changed: list[Path] = []
    for path in _iter_artifact_glob_matches(patterns, base):
        signature = _artifact_signature(path)
        if signature is None:
            continue
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if baseline.get(resolved) != signature:
            changed.append(path)
    return changed


def _has_changed_success_report(
    patterns: tuple[str, ...],
    baseline: dict[str, tuple[int, int]],
    base: Path | None,
    artifact_roots: set[Path],
) -> bool:
    for report in _changed_artifacts(patterns, baseline, base):
        try:
            report_root = report.parent.parent.resolve()
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            report_root in artifact_roots
            and isinstance(payload, dict)
            and payload.get("ok") is True
        ):
            return True
    return False


def completion_gate_text(gaps: list[str]) -> str:
    """Continuation nudge naming the unmet requirements (same tone as the
    near-limit / no-progress wrap-up nudges)."""
    bullet_lines = "\n".join(f"  - {gap}" for gap in gaps)
    return (
        "⚠️ 本轮任务尚未满足完成条件，请勿在此收尾。仍缺：\n"
        f"{bullet_lines}\n"
        "请补齐以上缺口（完成所需的工具调用、产出缺失的文件），完成后再给出最终答复。"
        "不要空转或仅口头声称已完成——以可验证的实际产出补齐为准。"
    )


def completion_budget_reserve_text(
    gaps: list[str],
    reserve_tool_calls: int,
) -> str:
    """Tell a gated artifact workflow to spend its reserved calls on closure."""
    bullet_lines = "\n".join(f"  - {gap}" for gap in gaps)
    return (
        "⚠️ 已进入交付收尾预算。停止继续浏览主题、布局或校验器源码，也不要重建已有产物。\n"
        f"当前仍缺：\n{bullet_lines}\n"
        f"接下来最多保留 {reserve_tool_calls} 次实质工具调用；只执行完成交付必需的"
        "批量补丁、truth/spec 校验、HTML 渲染、self-check 与 runtime probe。"
        "若确实缺少无法推断的用户事实，调用 `request_user_input` 提出一个聚焦问题并结束本轮；"
        "若需要用户在有限且会改变交付结果的选项中决策，调用 `request_user_decision`，不要只输出普通文本选项；"
        "用户补充后将从当前产物继续。"
    )
