# Memory System Integration Guide

Box-Agent provides persistent cross-session memory with core memory plus topic-sharded context memory:

| Type | Purpose | Recall behavior | Storage |
|------|---------|-----------------|---------|
| **Core memory** | User identity, explicit preferences, local defaults, durable behavioral rules | Automatically injected into the system prompt at session start | `~/.box-agent/memory/MEMORY.md` |
| **Memory summary** | Lightweight routing guide for deciding whether a request should search memory | Injected with core memory; does not contain full context entries | `~/.box-agent/memory/memory_summary.md` |
| **Context / experience memory** | Project context, task templates, historical notes, decisions, deadlines, prior pitfalls | Topic-routed search on demand via `memory_search`; weak auto-match may surface v2 hits | `~/.box-agent/memory/v2/experiences/<topic>.md` |

This split keeps high-signal user facts always available while giving the model a small Codex-style routing summary for deciding when `memory_search` is worth calling. Full project/history notes stay out of the prompt unless searched.

Compatibility policy: v2 is an overlay, not a migration. Pre-v2 `CONTEXT.md` and `context/<topic>.md` files remain on disk and are searched only as a read-only fallback for explicit `memory_search`; they are not auto-matched, not rewritten, and not eligible for promotion.

---

## 1. Configuration

Add these values to `config.yaml` if you need to override the defaults:

```yaml
enable_memory: true                    # Enable memory tools and startup core recall
memory_dir: "~/.box-agent/memory"      # Memory storage directory

enable_memory_extraction: true         # Auto-extract useful memory from agent lifecycle points
memory_extraction_cooldown: 300        # Seconds between extraction attempts
memory_extraction_step_interval: 10    # Extract every N agent steps
```

Set `enable_memory: false` to disable the memory manager and memory tools.

---

## 2. Tool interface

### `memory_write` — write persistent memory

```json
{
  "name": "memory_write",
  "arguments": {
    "content": "- 用户偏好中文回答\n- Q2 goal: launch data dashboard by 6/30",
    "category": "context",
    "mode": "append"
  }
}
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | Yes | Markdown bullet-style memory content |
| `category` | `core` or `context` | No | `core` for explicit user identity/preferences/rules; `context` for project/task history. Default: `core` |
| `mode` | `append` or `overwrite` | No | `append` merges/appends; `overwrite` replaces the target file |
| `topic` | string | No | Context bucket when `category="context"`, for example `preferences`, `project`, `feedback`, or `general` |

#### Core writes

`category="core"` writes to `MEMORY.md`.

Use core only when the user explicitly states durable personal information or preferences, for example:

```text
- User prefers concise Chinese responses
- User is a product manager in the data platform team
- 用户用于本地查询的默认城市是北京
- Do not add emoji in final answers
```

#### Context writes

`category="context"` writes to topic-sharded v2 experience memory under `v2/experiences/<topic>.md`.

When an LLM client is available, append-mode context writes are model-merged with existing memory:

1. Box-Agent sends the candidate memory plus current `MEMORY.md` and context memory to the LLM.
2. The LLM returns a structured operation plan: `add`, `replace`, `drop`, or `noop`.
3. Code applies the plan safely:
   - `replace` and `drop` require an exact full-line match.
   - `add` is line-deduped against both Core and Context.
   - Invalid model JSON falls back to append-with-dedup.

When no LLM is available, context writes use append-with-dedup directly.

### `memory_read` — read all persistent memory

```json
{
  "name": "memory_read",
  "arguments": {}
}
```

Returns both `MEMORY.md` and context memory when present.

### `memory_search` — search context memory

```json
{
  "name": "memory_search",
  "arguments": {
    "query": "weekly report"
  }
}
```

Search is case-insensitive and ranks exact matches first, then multi-term overlap for longer natural-language queries. Both explicit search and automatic matching require complete English/ASCII tokens, including compound names such as `box-agent` and `cache_store`: `user`/`use` do not match `/Users`, and `and` does not match `Understand`. Chinese phrases retain substring matching. Automatic matching uses the host-sanitized current request when supplied, excluding ACP-added UI-language instructions and wrapped history. When `topic` is omitted, Box-Agent first uses the topic sidecar index (`v2/experiences/_index.json`) to route the query to likely topic files, then falls back to all v2 topics if the routed search finds nothing. If v2 has no match, explicit `memory_search` falls back to legacy context read-only. Core memory is already present in the prompt, so `memory_search` only searches context / experience memory.

Models should call `memory_search` with short durable keys instead of whole action sentences. For example, `排产平台融资 ppt 的演讲稿发我` should become searches such as `排产平台`, `融资路演`, or `ppt`; `上次 EACCES 怎么修` should become `EACCES`, `npm cache`, or `runtime install`.

---

## 3. CLI integration

No additional integration code is needed. When `enable_memory: true`:

- **Startup**: `MEMORY.md` is recalled and injected into the system prompt if non-empty. `memory_summary.md` is also injected when searchable v2 or legacy context exists, so the model can decide whether to call `memory_search` without an extra routing model call.
- **During a session**: the agent can call `memory_write`, `memory_read`, and `memory_search`.
- **Lifecycle extraction**: when `enable_memory_extraction: true`, the agent loop asks the LLM to extract cross-session-useful memory at protected lifecycle points. Explicit user profile/preferences/local defaults can go to core; project and task history go to topic-sharded v2 experience memory.

Manual editing is also possible:

```bash
vim ~/.box-agent/memory/MEMORY.md
vim ~/.box-agent/memory/v2/experiences/preferences.md
```

`memory_summary.md` is generated from the v2 topic index and legacy-presence marker; inspect it when debugging routing, but do not treat manual edits as durable because the manager refreshes it.

---

## 4. ACP / Runtime integration

Memory tools are registered as normal tools and are available through standard ACP tool calls.

### 4.1 Writing memory

A host can prompt the agent to remember something:

```python
prompt_text = "请记住：用户偏好简洁的中文回答"
```

The agent may then call:

```text
memory_write(content="- 用户偏好简洁的中文回答", category="core", mode="append")
```

For project context:

```text
memory_write(content="- Weekly report format: progress/issues/next week", category="context", mode="append")
```

With an LLM-backed memory tool, context writes return a strategy label such as:

```text
Memory updated (context, applied). Current context memory: ...
Memory updated (context, no_change). Current context memory: ...
Memory updated (context, fallback_appended). Current context memory: ...
```

### 4.2 Automatic recall

On ACP `newSession`, Box-Agent:

1. Reads `MEMORY.md`.
2. Builds a memory block if core memory exists.
3. Appends the block to the session system prompt.

Format:

```text
--- MEMORY START ---

[Core Memory]
- 用户偏好中文回答
- 用户希望结果简洁

--- MEMORY END ---
```

Full context memory is not injected automatically. The model sees only `memory_summary.md` as a routing guide, and should call `memory_search` when the current request may depend on saved preferences, historical decisions, repo conventions, previous pitfalls, specific paths/errors, or recurring workflows.

---

## 5. Storage layout

```text
~/.box-agent/memory/
├── MEMORY.md              # Core memory, always recalled at session start
├── memory_summary.md      # Generated routing summary for memory_search decisions
├── v2/
│   ├── state.json         # No-migration cutover marker
│   └── experiences/       # Searchable v2 experience memory by topic
│       ├── _index.json    # Topic routing index
│       ├── general.md
│       ├── preferences.md
│       └── project.md
├── context/               # Legacy fallback only; not auto-matched/promoted
│   └── ...
├── CONTEXT.md             # Legacy fallback only, if present
└── .openclaw_imported # Marker for one-time OpenClaw import, when applicable
```

`MEMORY.md` and topic files under `v2/experiences/` are plain UTF-8 markdown files. Bullet points are recommended because model merge and line-level safety checks operate on full lines. The topic files are buckets such as `preferences`, `project`, `feedback`, and `general`; they are not intended to grow one file per project.

---

## 6. Automatic memory extraction

When `enable_memory_extraction` is enabled, `MemoryExtractor` analyzes recent conversation at lifecycle points:

- before context summarization (`pre_summarize`)
- every configured step interval (`step_interval`)
- at loop end (`loop_end`) only when the turn has high-signal evidence such as explicit preferences, "remember" instructions, tool-backed work, verified fixes, root cause notes, or enough multi-turn substance

The extractor can write explicit user-stated profile facts, preferences, and local defaults to `MEMORY.md`. For example, if the user says they are in Beijing while asking for weather, the extractor should save a cautious default such as `- 用户用于本地查询的默认城市是北京`, not infer a permanent residence.

Project context, task patterns, historical notes, decisions, deadlines, and behavioral feedback still go to topic-sharded v2 experience memory. This keeps one-off task details out of core memory, and avoids running a memory extraction pass after every trivial stop.

New v2 experience entries may include provenance metadata in the entry header:

```text
<!-- ctx id=... source=extractor topic=project session_id=chat-a turn_id=chat-a-turn-17 trigger=loop_end -->
```

`session_id` is the host-owned conversation id from ACP `_meta.session_id`; `turn_id` is the host-owned user-visible turn id from ACP prompt `_meta.turnId` / `_meta.turn_id`; `trigger` records the lifecycle point that wrote the entry. These fields are optional and older entries simply omit them. They are for audit/debugging and future trace-back flows, not for search ranking or promotion eligibility.

---

## 7. One-time OpenClaw import

At startup, if memory is enabled, Box-Agent attempts a one-time import from:

```text
~/.openclaw/**/USER.md
~/.openclaw/**/MEMORY.md
```

The LLM filters those files for durable user identity/preferences/habits and appends useful results to `MEMORY.md`. A `.openclaw_imported` marker prevents repeated imports.

---

## 8. Python API

```python
from box_agent.memory import MemoryManager

mgr = MemoryManager(memory_dir="~/.box-agent/memory")

# Core memory
mgr.append_core("- 用户偏好中文")
print(mgr.read_core())

# Context memory
mgr.append_context("- Weekly report format: progress/issues/next week", topic="preferences")
print(mgr.search("weekly report", topic="preferences"))

# Startup recall block for system prompt injection
block = mgr.recall()

# LLM-assisted context merge
await mgr.update_context_with_llm(
    "- Weekly report should include progress, issues, and next-week plan",
    llm_client,
)
```

Legacy aliases remain for compatibility:

```python
mgr.read_manual_memory()
mgr.write_manual_memory("- 用户偏好中文")
mgr.read_all()
mgr.write_all("- 用户偏好中文")
```


---

## 9. Verified correction memory

Correction memory learns concrete repairs and supplies relevant guidance **before**
the model selects its next operation. It is separate from always-injected core memory.

```mermaid
flowchart LR
    A[Repeated failures] --> B[Changed operation succeeds]
    B --> C[Runtime evidence receipt]
    C --> D[Specific remedy and applicability]
    D --> E[Validate and remember automatically]
    E --> F[Match current tools and Skill revisions]
    F --> G[Bounded guidance before the next model request]
```

### What users see

There is no draft approval task for the user. Once a specific reusable remedy is
saved with matching runtime evidence, the tool reports:

> 已记住这个解决办法，下次遇到相同情况会提前提醒。

Users may say “忘掉这条” or “这个办法不适用了”; the agent uses the existing delete or
supersede tool. A failure by itself never produces a “saved” message. Internal staging
is a storage implementation detail, not a user-facing workflow.

### Learning and evidence

- Runtime observation requires actually executed tool calls. Denied calls and fabricated
  model statements cannot issue evidence.
- Two distinct failures for the same fingerprint and subject within one run, followed by
  a successful call with changed arguments or an observed file repair, can issue an opaque `verification_id`.
  Observations and receipts are bounded in-memory data with a six-hour validation window.
- Success must match the original tool/version and operation identity. File/URL targets
  must agree. Simple shell calls must retain their executable/script/subcommand identity;
  unrelated successful commands and help/version/dry-run calls do not verify a repair.
  `uv run` and `npm run` retain the actual script identity. An unchanged command may
  verify a repair only after an executed edit/append or committed write to its script/input
  in the same run. Compound or dynamic shell/code
  calls conservatively receive no receipt.
- A successful call proves an observed execution outcome, **not universal correctness**.
  The model must still provide specific repair steps, the failure they address, and their
  applicability. It must not infer “a known fix” merely from repeated failures.
- `memory_write_correction` requires the receipt, `lesson`, `error_fingerprint`, and
  `subject_name`. It verifies identity and evidence before saving and activating the
  remedy in one call. No `confirm` or `draft_id` round-trip is exposed by the tool.
- Receipts bind the tool and source-validated active Skills at execution time. A Skill
  remedy is stored against that exact source revision. The tool supports `tool` and
  `skill` subjects; other historic subject kinds remain readable through the storage API
  but are not automatically inferred from arbitrary model claims.
- Failure strings, lessons, and scope metadata pass the preference/credential checks.
  Raw commands and tool output are not copied into durable evidence; its record contains
  the executed call identifier and an argument digest. One-shot setup repairs must not
  be proposed as reusable lessons.

### Using remembered repairs

When memory is enabled, the default Context engine checks current offered tools and
selected/read, source-valid Skills **on every request preparation**, including the first
request. Matching uses exact subject identity and version/revision, with task text used
only to rank matching records. A versioned record is not used when the current version
is unknown or different.

At most three complete records and 1,800 characters are supplied, further bounded by
available request budget. Oversize records are omitted, never truncated into a different
instruction. Guidance is request-only reference data; it does not change durable history,
tool arguments, permissions, or user instructions. Revoked records disappear from the
next prepared request. Custom Context implementations may opt into `bind_memory`; their
existing contracts remain unchanged.

General user-text auto-match excludes corrections so it cannot bypass subject/version
checks. Explicit `memory_search` still finds verified active corrections, including their
error fingerprint even when `symptom` was omitted. No vector database or full correction
file injection into the system prompt is introduced.

### Storage, maintenance, and compatibility

- Records remain in `~/.box-agent/memory/v2/experiences/corrections.md` with their existing
  lifecycle and a new `verification` evidence field.
- Unverified and legacy active records without evidence stay inspectable, but are excluded
  from default search and automatic recall. Revalidate and save them with fresh evidence;
  do not silently bless old template lessons.
- Generic decay, deduplication, conflict arbitration and compaction do not alter correction
  records. Generic context overwrite/add/replace/drop cannot mutate their reserved topic
  or records. Corrections are also excluded from promotion into always-injected core memory.
- An evidenced replacement supersedes the previous record and retains its history. Used
  receipts cannot reactivate deleted/superseded records or authorize different remedies. Repeated
  failures and direct active writes cannot overwrite an existing concrete remedy with a template.
- Receipt expiry is not a TTL on remembered repairs. Active records remain until superseded,
  deleted, or excluded by subject/version applicability.
- The development PR's former `confirm`/`draft_id` tool schema is replaced by the one-call
  evidence contract. Hosts must rebuild/install the runtime and restart to adopt it. Source
  tests do not prove that an installed desktop client is using these changes.
