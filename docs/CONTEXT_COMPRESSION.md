# Context Compression

How Box-Agent keeps the LLM message history under the model's context
window while preserving correctness, multi-provider compatibility, and
prompt-cache friendliness.

## Goals & non-goals

**Goals**
- Keep the message list under `token_limit` indefinitely, even across
  hundreds of tool calls.
- Strict invariant: every compression path **monotonically reduces or
  preserves** the token count. A compression path that grows the history
  is a bug.
- Cheap-first: cost (LLM calls, latency, cache invalidation) escalates
  only when cheaper layers cannot meet the budget.
- Provider-neutral: identical behavior on Anthropic / OpenAI-protocol /
  DeepSeek / Qwen / MiniMax M2 paths.
- Lossless for downstream tooling: events, logger, and on-disk artifacts
  always carry the original, full data. Only the model's view is
  compressed.

**Non-goals**
- Reversibility. Once compressed, the model's view of a step cannot be
  un-compressed inside the same run. The logger holds the originals for
  post-mortem inspection.
- Semantic preservation across all tool outputs. We optimize for the
  shapes that dominate real workloads (large file content, repeated
  tool calls, long execution rounds), not arbitrary worst cases.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        run_agent_loop step                          │
│                                                                     │
│  ┌─────────────────┐    ┌────────────────────┐                      │
│  │  Layer 1        │    │  Layer 2           │                      │
│  │  _micro_compact │───▶│  _maybe_summarize  │────┐                 │
│  │  (every step,   │    │  (token-triggered, │    │                 │
│  │   0 LLM calls)  │    │   1 LLM call)      │    │                 │
│  └─────────────────┘    └────────────────────┘    │                 │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │  LLM.generate_   │       │
│                                          │  stream(...)     │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ append assistant │       │
│                                          │ message          │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                  (abort path: cancel/max_tokens/error)              │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ _cleanup_        │       │
│                                          │ incomplete_      │       │
│                                          │ messages         │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ tool exec        │       │
│                                          │   │              │       │
│                                          │   ▼              │       │
│                                          │ Layer 0          │       │
│                                          │ _compact_visible │       │
│                                          │ _tool_content_   │       │
│                                          │ for_model        │       │
│                                          │ (per-call,       │       │
│                                          │  artifact-aware) │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                            (next step)              │
└─────────────────────────────────────────────────────────────────────┘
```

Layer numbering reflects when each layer fires within a step, **not**
priority: Layer 0 fires last in code order but applies first to any
given tool result.

| Layer | Trigger | Cost | Operates on | Scope of compression |
| ----- | ------- | ---- | ----------- | -------------------- |
| 0 — visible tool content / arguments | Per tool call & per assistant append | 0 LLM, O(content) | Single message just before append | Generated-artifact tool outputs and inline-script arguments |
| 1 — `_micro_compact` | Every step | 0 LLM, O(messages) | Whole `messages` list, in place | Old tool-role messages |
| 2 — `_maybe_summarize` | `_estimate_tokens > token_limit` or `api_total_tokens > token_limit` | 1 LLM call per round | Whole `messages` list, replaces | Assistant + tool exec sequences |
| Cleanup — `_cleanup_incomplete_messages` | Abort paths (cancel / max_tokens / empty stream / error) | 0 LLM | Tail of `messages` | Incomplete in-flight turn only |

## Layer 0 — Visible tool content & argument compaction

Selectively shrinks generated-artifact content before it lands in the
model-visible message history. The full content is still:
- emitted as `ToolCallResult` to event consumers (CLI/ACP/sub-agent),
- written to disk under `{workspace}/output/`,
- logged verbatim in the agent log.

**Compacted tool outputs** (`_compact_visible_tool_content_for_model`)
- Currently scoped to `read_file` whose path matches the
  generated-artifact heuristics in `_path_needs_compact_model_context`.
- Replacement format:
  ```
  [Full tool output omitted from model history]
  Tool: read_file
  Path: output/deck.html
  Lines returned: 1240
  Characters returned: 58213
  Reason: generated/QA artifact content can bloat future LLM turns;
  call read_file again with offset/limit if exact content is needed.

  Preview first 20 lines:
  …
  ```

**Compacted tool-call arguments** (`_compact_tool_call_arguments_for_model`)
- `write_file.content` and `edit_file.{old_str,new_str}` with paths
  matching `_MODEL_CONTEXT_PATH_EXTS` get a structured placeholder with
  a 12–20 line preview.
- Catch-all: any string argument >`_MODEL_CONTEXT_CONTENT_THRESHOLD`
  characters is compacted.

**Whitelist (current, intentionally narrow):**
- Extensions: `.html`, `.htm`, `.json`, `.md`, `.txt`, `.log`, `.xml`
- Special filenames: `qa.json`, `html_self_check.json`,
  `visual_review.md`, `vision-review-prompt.txt`
- Path-part heuristic: anything under a path containing `qa/`

This layer is deliberately whitelist-driven today; broadening to a
size-only heuristic is an open improvement.

## Layer 1 — Micro-compact (every step)

Cheap, zero-LLM-call compaction that runs **before every LLM call**.

```python
def _micro_compact(messages: list[Message]) -> int:
    """Replace old tool-result content with short placeholders.

    Token-aware: keeps the recent N tool results intact, but shrinks the
    keep window if those alone bust the per-window token budget so that
    a few enormous outputs cannot bypass compaction entirely.

    Always preserves at least the most recent tool message.
    """
```

### Algorithm

```
tool_indices = indices of messages where role == "tool"

# Conservative lower bound on the keep window
keep_count = min(_KEEP_RECENT_TOOL_RESULTS, len(tool_indices))   # 3

# Token-aware shrink — preserves ≥1 tool message
while keep_count > 1:
    recent = tool_indices[-keep_count:]
    if sum(_approx_tokens(msgs[i]) for i in recent) <= _KEEP_RECENT_TOOL_TOKEN_BUDGET:
        break        # window fits
    keep_count -= 1  # too large, shrink

# Compact everything older than the keep window
for idx in tool_indices[:-keep_count]:
    if len(content) > _MIN_COMPACT_LEN:    # 200
        messages[idx] = Message(
            role="tool",
            content=f"[Previous result from {tool_name}: {first_line[:100]}...]",
            tool_call_id=...,    # preserved (protocol correctness)
            name=...,            # preserved
        )
```

### Constants

| Name | Value | Meaning |
| ---- | ----- | ------- |
| `_KEEP_RECENT_TOOL_RESULTS` | `3` | Lower bound on the keep window |
| `_KEEP_RECENT_TOOL_TOKEN_BUDGET` | `12_000` | Token cap above which the keep window starts shrinking |
| `_MIN_COMPACT_LEN` | `200` | Tool results shorter than this are not worth compacting |

### Properties

- **Idempotent.** Once a tool message is compacted, its content is short
  enough (`< _MIN_COMPACT_LEN`) that the next invocation will not
  re-touch it. The compacted prefix is therefore stable, which keeps
  the LLM prompt cache hot.
- **Protocol-safe.** `tool_call_id` and `name` are preserved so the
  assistant↔tool message pairing remains valid for every provider.
- **First-line anchor.** The first line of the original output is kept
  in the placeholder; this is usually the tool's most useful summary
  and prevents the model from re-calling the same tool just to recall
  what it returned.

### Behavioral examples

| Scenario | Old behavior (N-recent only) | New behavior (token-aware) |
| -------- | ---------------------------- | -------------------------- |
| 10 small tool results | Last 3 kept, 7 compacted | Identical |
| 3 × 50KB `read_file` results | 3 kept (~38k tokens leak through) | Window shrinks to 1, 2 compacted |
| 1 × 100KB tool result | 1 kept (correct) | 1 kept (correct, never shrinks below 1) |

## Layer 2 — Summarization (token-triggered)

Triggered only when token estimation crosses `token_limit`. One LLM
call per round (per user turn), then the message list is replaced.

### Trigger condition

```python
estimated = _estimate_tokens(messages)   # tiktoken cl100k_base
if estimated <= token_limit and api_total_tokens <= token_limit:
    return None   # no compaction
```

Both client-side estimate and provider-reported `api_total_tokens` must
be under the limit; whichever crosses first triggers compaction.

`token_limit` itself is derived from `AgentConfig`:
```
context_token_limit = int((context_window - max_output_tokens) * 0.9)
```
Box-Agent defaults to `context_window = 180_000`. For user-configured endpoints,
omitting `max_output_tokens` defaults to `63_999`, giving
`context_token_limit = 104_400` (~104k). Hosted `xiaohuanxiong.com` endpoints
default to `max_output_tokens = 80_000`, giving `context_token_limit = 90_000`
(~90k). Both values are user-overridable in `config.yaml`. The 10% headroom
absorbs token-estimate drift and the summarization request itself.

### Algorithm

```python
async def _maybe_summarize(llm, messages, token_limit, api_total_tokens, skip_check):
    if skip_check:
        return None, False, 0
    if estimate(messages) <= token_limit and api_total_tokens <= token_limit:
        return None, False, estimated

    user_indices = [i for i, m in enumerate(messages) if m.role == "user" and i > 0]
    new_messages = [messages[0]]   # keep system prompt

    for idx, user_idx in enumerate(user_indices):
        user_msg = messages[user_idx]
        exec_msgs = messages[user_idx + 1 : next_user_or_end]

        # Drop orphan summary markers — prevents stale summaries from
        # piling up across many compaction cycles.
        if _is_summary_marker(user_msg) and not exec_msgs:
            continue

        new_messages.append(user_msg)

        if exec_msgs:
            try:
                summary = await _create_summary(llm, exec_msgs, idx + 1)
            except Exception:
                # Failure path: DROP exec_msgs. Token count strictly
                # decreases, never increases. Conversation flow stays
                # intact (user_msg is still there).
                summary = ""
            if summary:
                new_messages.append(Message(
                    role="user",
                    content=f"{_SUMMARY_MARKER}\n\n{summary}",
                ))

    return new_messages, True, estimated
```

### Round boundaries

A "round" = one user message and everything between it and the next
user message. The system prompt and user messages are preserved as-is;
only the assistant/tool exec messages within a round get summarized.

### Summary markers

Each summarized round leaves a sentinel:
```
[Assistant Execution Summary]

<summary text>
```
Stored as a `user`-role message (so the next assistant turn sees it as
context). The marker prefix is the constant `_SUMMARY_MARKER` and is
used by `_is_summary_marker` to detect orphan markers in subsequent
compaction cycles.

### `_create_summary` contract

```python
async def _create_summary(llm, messages, round_num) -> str:
    """Single LLM call. Raises on failure."""
    response = await llm.generate(
        messages=[system_role, user_prompt],
        tools=None,                # explicit — uniform across providers
        thinking_enabled=False,    # explicit — uniform across providers
    )
    return response.content        # NEVER returns the un-summarized input
```

Prompt requirements:
1. Focus on tasks completed and tools called.
2. Keep key execution results.
3. Under ~800 tokens.
4. **Use the same language as the input** (avoids EN↔ZH double
   translation in mixed-language sessions).
5. Skip user content; summarize agent execution only.

### Properties

- **Token-monotonic.** Any execution path of `_maybe_summarize` —
  successful summary, failed summary, orphan-marker collapse — strictly
  reduces or preserves the token count. The old bloat bug
  (`return summary_content` in the `except` branch) is fixed: failure
  now drops `exec_msgs` instead of replacing them with an even larger
  concatenated placeholder.
- **Marker collapsing.** Consecutive summary markers with no fresh
  exec_msgs between them collapse to a single marker, bounding the
  growth of "summary of summary" residue across many cycles.
- **Provider-uniform.** `tools=None` + `thinking_enabled=False` are
  passed explicitly. Anthropic / OpenAI / DeepSeek / Qwen / MiniMax M2
  paths produce the same shape of output — except for providers that
  emit thinking blocks unconditionally (e.g. MiniMax M2), where the
  wire-level toggle is honored but provider behavior cannot be
  suppressed.

## Cleanup — `_cleanup_incomplete_messages` (abort paths)

Called from five abort sites in `run_agent_loop`:

| Site | Trigger |
| ---- | ------- |
| Cancel after stream | User pressed Esc / ACP `cancel` during streaming |
| `MAX_TOKENS` | Provider stopped with `finish_reason="length"` |
| Cancel before tools | Cancel signal raised after stream, before tool exec |
| Empty-args loop break | Model repeated empty-arg tool_calls past `EMPTY_ARGS_LIMIT` |
| Cancel after tool | Cancel signal raised between sequential tool calls |

### Definition of "incomplete"

```
last_assistant = most recent role == "assistant" in messages
trailing_tool_count = number of tool messages after last_assistant
expected_tool_count = len(last_assistant.tool_calls or [])
has_content = bool(last_assistant.content or last_assistant.thinking)

incomplete = (
    expected_tool_count > 0 and trailing_tool_count < expected_tool_count
    or expected_tool_count == 0 and not has_content
)
```

- **Incomplete (delete the turn)**: assistant declared tool_calls but
  at least one tool response is missing, OR assistant produced no
  content / thinking / tool_calls at all (provider cut off the stream
  before any output landed).
- **Complete (keep the turn)**: assistant produced content (or
  thinking), and any declared tool_calls all have matching tool
  responses.

### Why this changed

The previous unconditional "delete from last assistant onwards" was
correct for 4 of the 5 abort sites but wrong for the
mid-stream-cancel site, where `assistant_msg` for the current step has
not yet been appended. There, the "last assistant" is the **previous,
completed** turn — and the old logic would erase it, corrupting the
message list for any resumption.

The new shape-based check is a no-op for complete turns (so completed
prior turns are safe) and behaviorally identical to the old code for
the other four sites (where the just-appended assistant is genuinely
incomplete).

## Token estimation

`_estimate_tokens` and `_approx_tokens_for_content` both use
`tiktoken.get_encoding("cl100k_base")`. On `ImportError` or
initialization failure they fall back to `len(text) // 4`, which is
intentionally conservative (overestimates English, roughly matches CJK).

The same encoder is used for:
- Layer 1 keep-window budget evaluation
- Layer 2 trigger check
- LLM debug log payload summarization (`llm/debug_logging.py`)

This means the budget numbers (`12_000`, `context_token_limit`) are
self-consistent: a message that contributes T tokens to one estimate
contributes T tokens to the other.

## Provider compatibility matrix

| Concern | Anthropic | OpenAI-compat | DeepSeek | Qwen | MiniMax M2 |
| ------- | --------- | ------------- | -------- | ---- | ---------- |
| `tools=None` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `thinking_enabled=False` | ✓ | ignored | ignored | honored where supported | honored on wire, provider still emits |
| `tool_call_id` preserved | required | required | required | required | required |
| Layer 1 placeholder shape | ✓ | ✓ | ✓ | ✓ | ✓ |
| Layer 2 summary marker as `user` role | ✓ | ✓ | ✓ | ✓ | ✓ |

No provider-specific branching anywhere in the compression path.

## Interaction with adjacent systems

- **Logger (`AgentLogger`).** Compaction layers operate on `messages`
  in place; the logger captures the original `LLMResponse` /
  `ToolResult` payloads before any compaction. The on-disk log is the
  source of truth for post-mortem.
- **Memory extractor.** `MemoryExtractor.maybe_extract` is invoked with
  a snapshot of `messages` taken **before** Layer 2 replaces them
  (`trigger="pre_summarize"`). This guarantees memory extraction sees
  the full execution detail, not the summarized form.
- **Event stream.** Compression never affects the event stream — all
  consumers (CLI render, ACP, sub-agent) see full `ToolCallResult`,
  `ContentEvent`, etc. The model's view and the user's view diverge
  intentionally.
- **Sub-agent.** `SubAgentTool` runs `run_agent_loop` with its own
  `token_limit` (default `40_000`); all compression layers apply
  independently to the sub-agent's message list.

## Tests

Coverage lives in `tests/test_core.py`:

| Test | Layer | What it locks down |
| ---- | ----- | ------------------ |
| `test_micro_compact_no_op_when_few_tool_msgs` | 1 | No-op when ≤ N tool messages |
| `test_micro_compact_replaces_old_tool_results` | 1 | Compacts old, keeps recent |
| `test_micro_compact_preserves_short_content` | 1 | Skips below `_MIN_COMPACT_LEN` |
| `test_micro_compact_preserves_tool_call_id` | 1 | Protocol fields kept |
| `test_micro_compact_first_line_hint` | 1 | First line preserved as anchor |
| `test_micro_compact_token_budget_shrinks_keep_window_when_recent_oversized` | 1 | Token-aware keep window |
| `test_micro_compact_preserves_at_least_one_recent_when_single_giant` | 1 | Lower bound respected |
| `test_create_summary_passes_thinking_disabled_and_no_tools` | 2 | Cross-provider call shape |
| `test_create_summary_propagates_exceptions` | 2 | Failure raises (no bloat) |
| `test_maybe_summarize_drops_exec_msgs_on_llm_failure` | 2 | Token-monotonic failure path |
| `test_maybe_summarize_inserts_summary_marker` | 2 | Marker shape |
| `test_maybe_summarize_collapses_orphan_summary_markers` | 2 | No marker pile-up |
| `test_maybe_summarize_skip_check_short_circuits` | 2 | `skip_check` honored |
| `test_maybe_summarize_below_threshold_noop` | 2 | No work when under budget |
| `test_cleanup_keeps_complete_assistant_turn` | cleanup | Complete turn untouched |
| `test_cleanup_removes_empty_assistant_turn` | cleanup | Empty turn dropped |
| `test_cleanup_removes_partial_tool_call_turn` | cleanup | Partial tool_calls dropped |
| `test_cleanup_keeps_complete_tool_call_turn` | cleanup | All tool responses present → keep |
| `test_cleanup_keeps_thinking_only_assistant` | cleanup | Thinking counts as output |
| `test_cleanup_noop_when_no_assistant_turn` | cleanup | No-op on bare conversation |

## Open improvements

These are intentionally **not** in the current implementation. They are
listed here so future work can pick them up with context.

1. **Generalize Layer 0 beyond the PPT whitelist.** Today
   `_MODEL_CONTEXT_PATH_EXTS` / `_MODEL_CONTEXT_PATH_NAMES` are biased
   toward HTML/JSON/MD generated by the PPT skill. A size-only fallback
   (`len(value) > N` for any path) would cover Python / CSV / SQL
   generated files without per-skill tuning.
2. **Age out assistant `thinking` blocks.** With `deep_think` enabled,
   thinking blocks accumulate (up to 8k tokens each). Layer 1 only
   touches `role == "tool"`. A targeted compactor for old thinking
   blocks would help long deep-think sessions without escalating to
   Layer 2.
3. **Incremental Layer 2.** Re-summarizing every round on every trigger
   re-pays the LLM cost; tagged-as-summarized rounds could short-circuit
   on subsequent triggers.

## File reference

- `box_agent/core.py` — all compression code lives here:
  - Constants: `_KEEP_RECENT_TOOL_RESULTS`, `_KEEP_RECENT_TOOL_TOKEN_BUDGET`,
    `_MIN_COMPACT_LEN`, `_SUMMARY_MARKER`,
    `_MODEL_CONTEXT_PATH_EXTS`, `_MODEL_CONTEXT_PATH_NAMES`,
    `_MODEL_CONTEXT_PATH_PARTS`, `_MODEL_CONTEXT_CONTENT_THRESHOLD`
  - Layer 0: `_compact_visible_tool_content_for_model`,
    `_compact_tool_call_arguments_for_model`,
    `_summarize_tool_argument_for_model`,
    `_path_needs_compact_model_context`,
    `_tool_argument_needs_compaction`
  - Layer 1: `_micro_compact`, `_approx_tokens_for_content`
  - Layer 2: `_maybe_summarize`, `_create_summary`,
    `_is_summary_marker`
  - Cleanup: `_cleanup_incomplete_messages`
  - Token estimation: `_estimate_tokens`, `_estimate_tokens_fallback`
- `box_agent/config.py` — `AgentConfig.context_token_limit` property
- `box_agent/agent.py` — `Agent.__init__(token_limit=...)` plumbing
- `tests/test_core.py` — all compression tests
