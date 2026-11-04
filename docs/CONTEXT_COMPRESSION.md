# Context Compression

Box-Agent controls durable context growth at two boundaries:

1. Tool results are persisted before they can dominate a later model request.
2. Conversation history is summarized only when the next request approaches the model's effective input limit.

The two mechanisms are deliberately separate. Tool-result storage is lossless at the storage layer: the complete result remains on disk while the model keeps a bounded head-and-tail working preview plus the recovery path. Context summarization is lossy, so it runs later and preserves a bounded working set.

Images use a session-owned attachment store and request-only projections.
Uploads, native image inspection and persisted CUA screenshots share a retention
policy: by default an occurrence remains available for three committed main-model
inferences, subject to encoded-byte and provider request-body limits. Pixels stay
outside durable message content; SessionLog records references and lifecycle
metadata. See [Image context](IMAGE_CONTEXT.md) for the complete contract.

Before sending a summary request, Box-Agent estimates the complete history plus the appended summary instruction against the session's safe input budget. Both ordinary and forced compaction apply this check. An over-budget summary skips the provider call and uses the local bounded fallback; the rebuilt main request is then checked before sending.

## Request lifecycle

```text
tool finishes
  -> persist its result and any session-owned image attachments
  -> append tool messages and attachment references
  -> before the next LLM request, enforce the fresh-result aggregate budget
  -> select retained images and enforce image/request-body budgets
  -> reserve the request-only image cost from the text-history limit
  -> compact conversation history when necessary
  -> attach the selected image overlay and call the LLM
  -> commit the accepted response and advance the image inference counter
```

## Request-only native image input

Omitting `inspect_images` strategy selects native when the current model declares
image support; otherwise it uses the configured proxy and returns text. Explicit
native requests retain their capability checks. Capability resolution uses an
explicit declaration first, then current-model catalog tags, then the maintained
model-name allowlist.

The inspection tool validates, orientation-normalizes and downsamples its bounded
PNG/JPEG inputs before archival. The shared image context persists those exact
bytes, then hydrates selected occurrences into provider-neutral `input_image`
blocks. It does not resize archived images during request preparation. Selection
first enforces the retention window and encoded-byte budget, then checks the
complete provider body. Excluded new images produce an explicit not-delivered
notice; a completed tool operation is not automatically repeated.

The overlay is added only to the in-memory provider request. Pixels are excluded
from public ToolResult serialization, durable message content and ordinary
AgentLogger history. Session traces and provider debug logs redact raw image
payloads. A summary may receive still-active images when its model and budgets
permit; summary calls do not advance the main-model inference counter or retire
images merely because the summary provider has a smaller body limit.

An empty or discarded retry retains the same image age. An accepted, committed
main-model response advances the counter, including tool-call responses within
a user turn. Other tools' transient overlays, and CUA with `persist_images: false`,
retain their one-request behavior. Session restore and history edits restore
image occurrences together with their committed inference counter.

Provider usage includes image input. The assistant message retains the numeric
request-only token estimate, which is subtracted from the text-history estimate
before adding the current request's overlay. Image cost is estimated from pixel
dimensions; base64 length is used for transport budgets, not conversational text.

## Large-table processing before context growth

For CSV/TSV/Excel calculations, shared capability-gated guidance prefers a small
schema/sample and data profile, then full-record processing with `execute_code`
or an existing Python interpreter through `bash`. Parsing may be chunked, but
sampling is not evidence of complete computation. Validate record counts,
types, missing values, parse errors and task-specific reconciliation; never
silently drop bad records. Return aggregates, validation results and bounded
exceptions, then reopen saved outputs and verify their format and key totals.

With file tools enabled, `validate_table` enforces a completion gate for existing
local CSV/TSV inputs explicitly referenced by the current user turn. After read
authorization it pins source SHA-256 and parses every original record to EOF,
including superseded, cancelled and tail records. The declared contract must
cover every column: required/nullable, text, integer, finite decimal with bounds
and scale, date, boolean or explicit enum values.

User supplements received during a run add their explicitly referenced inputs to
the same gate without discarding earlier receipts or relaxing the locked purpose.
Quoted and Markdown paths are matched as whole paths, not as extra bare filenames.
The CSV parser supports fields up to at least 16 Mi characters, retaining any
higher process-wide allowance; parser errors still leave EOF coverage unverified.

The default `profile="declared_fields"` applies the user's actual field rules,
including legitimate negative amounts, revision zero or higher precision.
Column names never select domain semantics. Explicit `profile="transaction_v1"`
for matching order/refund requirements enforces required IDs, integer revision >= 1,
finite nonnegative amount with at most two decimal places, status and present
region/currency as enums, date as a date, and is_test as a boolean. These fields
cannot be weakened to nullable/text. The selected profile is locked with the
field contract and inherited by later calls; it cannot be downgraded after
discovering invalid data.

Receipts contain source/contract hashes, logical record counts, per-field counts
and at most 20 error samples. Session Log events `table.inputs`, `table.validation`
and `completion.check` retain audit evidence. Model claims and public result JSON
cannot credit a gate. Missing receipts allow at most two repair prompts;
incomplete scans, changed inputs, failed checks or exhausted repair attempts end
with `COMPLETION_GATE_UNSATISFIED`. Scanned field contracts cannot be relaxed,
and later turns must revalidate.

Interactive user questions and plan-approval pauses retain pending checks.
`completion.paused` stores capability-owned inputs, hashes, locked contracts,
roles and scan receipts in Session Log; a reopened session restores them before
adding any newly supplied inputs. An in-memory conversation uses a scoped
checkpoint on its existing tools. Transcript text and public tool JSON are never
replayed as validation evidence. Resumed checks recheck current file hashes;
missing capabilities or unsupported checkpoints block completion. A terminal
success, failure or cancellation closes the pending task with `completion.closed`,
so an unrelated later task starts fresh. Only Session Log provides durable state.

Declare `purpose` before the first scan: `diagnosis` only inspects/reports errors;
`computation` requires valid inputs and is the initial default. Omitted later
calls inherit the run's locked purpose. The `table.purpose` event records this
decision; it cannot be changed after discovering invalid data. Both purposes
still require every original input/field, EOF and hashes matching the validated version. Diagnosis
must truthfully report findings and deliver/read back requested artifacts;
computation additionally requires every input to be valid.

For user-requested in-place edits/appends, declare `source_mode="edit_target"`
on that file's first validation call, before writing. The default `input` role
remains immutable, and roles cannot change after scanning starts. An edit target
must first receive a complete original scan; the original receipt is retained.
Each edited version must be fully rescanned with the declared field contract and the
final version must be valid, even in diagnosis mode. A write without a new scan
requests validation instead of treating an expected edit as irreparable source
damage. Other input files retain their immutable hashes. This declaration must
match the user's instruction; it does not authorize writes or prove edit intent,
and normal tool permissions and producer write receipts remain unchanged.
For schema-changing edits, declare `output_fields` alongside the original
`fields` before the first scan. Both contracts are locked. Later calls can omit
`fields` to inherit the applicable final contract; original and final scans must
each cover every column and record.

Pure file management uses explicit roles before the operation. `delete_target`
without fields prepares deletion, even for a malformed CSV; completion requires
the path to be absent. `replace_target` with the required final `fields` prepares
whole-file replacement without processing old records; after writing, call
the validator again to fully validate the new file. These preparation calls
return `operation_ready=true`, `validation_complete=null`, `data_valid=null`,
and record `table.file_operation`, not data-scan evidence. They retain the old
hash but never certify the old data or authorize the actual write. Missing
deletion or final validation blocks completion. Other inputs remain mandatory,
and roles/contracts cannot be switched after declaration. These roles must
match user instructions, not be used to skip validation of calculation inputs.

For a rename or move, declare `source_mode="move_target"`, `destination_path`
and the complete original `fields` before the first scan and before moving.
The destination must be different and absent, and is locked with the contract.
Both the declared absolute path and its resolved target are retained; the
declared path must still resolve to that target before and after hashing,
including after Session Log restore.
Unlike deletion preparation, this call still scans every original field and
record to EOF. After the authorized move, completion requires the original
path to be absent and the destination to be readable, without replaced symlinks,
with the full SHA-256 matching the original receipt. The existing computation
or diagnosis validity rule still applies. Copying without removing the source,
changing bytes, missing scans, or missing validation of other inputs blocks
completion. The original receipt and destination survive an interactive pause
and Session Log restore. This declaration neither moves a file nor changes
writer permissions or chat-reference protection; it verifies an already
completed move. A rename that also changes data is not covered by this role.

For scan calls, tool `success` and `validation_complete` mean the validation ran completely.
`data_valid` independently reports data quality, or `null` for an incomplete
scan. A completed diagnosis can return `success=true, validation_complete=true,
data_valid=false` and finish normally; the same invalid data still blocks a
computation task. Legacy `valid` keeps its original meaning. Consumers must not
use tool success as a substitute for data validity. Task termination,
`completion.check`, and `table.validation` retain their separate outcomes.
Purpose must match the user's task; this declaration neither infers user intent
nor replaces business-delivery acceptance.

This proves **whole-source validation against the declared contract**. Business
meaning, allowed enums and bounds still come from task requirements; the gate
does not infer them or prove subsequent calculations/output correctness. Compute
and readback reconciliation remain necessary. Excel, paths discovered only in
tool output and sessions without this tool are outside this gate. Plan approval
and waiting for user input are not evidence of completed data processing.
`read_file` pagination, JSONL policy, 600-second waits and 429 behavior are unchanged.

## Oversized tool-result storage

`ToolResultStorage` in `box_agent/tool_result_storage.py` owns persistence, preview rendering, and per-conversation deduplication. The shared loop invokes it for both sequential and parallel tools; CLI and ACP do not duplicate this policy.

### Immediate per-result check

The minimum shared model-facing result limit is 20,000 characters and scales with the active model's safe input window as `max(20,000, context_token_limit * 0.25)`. Tools that do not declare `max_result_size_chars` use this contextual limit; a tool may still declare a lower explicit operational bound. Only ordinary results that have not already been processed enter this generic policy.

The following results are not processed a second time:

- a tool result whose `model_context` projection was actually selected is frozen by `tool_use_id`;
- `read_file`, `query_jsonl`, and `search_files` declare infinity and rely on line/character pagination, cursor/structured summarization, and result-count/character pagination respectively;
- `bash` and `bash_output` also declare infinity because they already apply one 50,000-character 40% head + 60% tail truncation inside the tool.

Read-like tools are not externalized by the single-result check because persisting every normal page only to make the model read it again would create a loop. Infinity opts out of that immediate check, but a large parallel batch can still externalize its largest pages to satisfy the aggregate request budget. A tool can also request persistence of complete recoverable text through `ToolResult.persistence_content`.

When an eligible result exceeds the limit:

- a string is saved as `.txt`;
- an array containing only text blocks is serialized as formatted JSON and saved as `.json`;
- files live under `~/.box-agent/sessions/<session>/tool-results/<tool_use_id>.<ext>`;
- exclusive-create (`x`, equivalent to `wx`) prevents overwriting an existing result;
- a stable `<persisted-output>` head-and-tail preview replaces the model-facing content.

The original result is preserved when it is below the limit, contains an image or any other non-text block, or cannot be persisted. Empty output is normalized to `(<Tool name> completed with no output)`; for Bash this is `(Bash completed with no output)`.

Each `tool_use_id` receives one decision. A persisted replacement is cached and reused in later loop iterations, so the result is not written repeatedly. Results already present when a session is resumed are frozen and are not retroactively externalized.

Tools may still bound their own output for operational reasons. When they do, they pass the complete persistable text through `ToolResult.persistence_content`; only `ToolResultStorage` writes it. Bash uses this path for both successful and failed commands: the complete output is saved, while the model keeps the tool-generated head/tail plus the saved path instead of receiving a second generic 2,000-character head preview. Semantic `model_context` projections remain a separate tool concern; once selected, neither the immediate check nor the fresh aggregate budget processes them again.

### Preview format

The preview algorithm:

1. Keeps at most 2,000 characters in total.
2. Preserves both the beginning and the end with an explicit omitted-middle marker.
3. Prefers nearby newline boundaries when that avoids partial lines.
4. Preserves the exact head and tail even for a single long line.

For an ordinary unprocessed result, the model-facing form is:

```text
<persisted-output>
Output too large (...). Full output saved to: ...

Preview (head + tail, up to 2.0KB):
...
</persisted-output>
```

For a self-bounded tool that supplies `persistence_content`, the body is labeled `Tool-bounded output` and contains the tool's existing bounded result instead of another generic preview.

### Aggregate fresh-result budget

Before every LLM request, results first seen during the current conversation are checked against a contextual aggregate budget of `max(50,000, context_token_limit * 0.50)` characters. The pass:

1. Considers only fresh `tool_use_id` values.
2. Excludes selected `model_context` projections, but still counts self-bounded tools whose per-result declaration is infinity. Infinity disables immediate per-result persistence; it does not exempt a parallel batch from the aggregate budget.
3. Sorts eligible results from largest to smallest.
4. Keeps a head-and-tail preview plus the recovery path, shrinking each preview according to the batch budget, and counts the wrapper's actual model-facing length.
5. Persists and replaces the largest results until the actual remaining fresh content is at or below the budget.

Unsupported blocks and persistence failures remain unchanged. Since IDs are marked seen during the pass, later requests do not reconsider the same results. This handles a batch of parallel tool calls whose individual results are below the per-result limit but are too large together.

## Context-limit compaction

### Capability and commit boundary

The Kernel supplies a `CompactionInput` to `CompactEnginePort`. The default
engine delegates to the existing compression algorithm; it does not replace
the request Context interface or introduce a second Skill loading policy.
Inputs distinguish the tool catalog used for runtime-state recovery from the
exact offered schemas used for estimation, and retain forced recovery and the
summary-request input limit.

Context projects effective history before compaction. The engine calls the
Kernel's `before_summary` callback before a summary attempt or deterministic
fallback. The Kernel commits the returned surface and flushes it before changing
live messages; request delivery/response callbacks and the one-retry input-budget
rule are unchanged. Static and managed composition preserve supplied compactors,
including falsey instances. `CompactionOutcome` retains legacy tuple iteration;
its limit flag must agree with its existing blocked mode.

### Threshold

The trigger is derived from the model's input budget:

```text
autoCompactThreshold = 0.9 * (context_window - max_output_tokens)
```

`LLMConfig.context_token_limit` reserves the configured maximum output budget,
then keeps 10% of the remaining input budget as headroom for estimation drift
and the summary request.

ACP model bindings may override both values with the selected model's
`contextWindow` and `maxTokens`. The Agent derives its input limit when the
session is created and recomputes it whenever the binding changes between
turns. When the binding omits a capability, the Agent uses the applicable
profile, client, catalog, or `config.yaml` values.

Automatic routing also checks each candidate's input budget before ranking it.
When the output limit is known, it uses the same `derive_context_token_limit`
calculation, including any smaller output cap for that auxiliary call. A missing
catalog output limit inherits the current client's output limit; if neither is
known, routing retains the legacy 80% window budget. Candidates with no remaining
input budget are excluded. If any candidate declares a context window, only
candidates with known, sufficient capacity are eligible. An insufficient pool
raises `AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED`; it never falls back to the largest
insufficient window or to a candidate with unknown capacity. Entirely missing
window metadata retains legacy routing with `context_capacity=unknown`; this
does not establish capacity safety. Known-capacity diagnostics include
`context_capacity=estimated` and `input_token_budget`. These checks use routing
estimates and do not replace the full-request accounting and compaction below.
If a capacity-checked model cannot be bound, `AUTO_MODEL_BINDING_FAILED` stops
the call instead of silently inheriting the previous, unverified client budget.

### Explaining a model-routing decision

Lexical task signals separate clearly delimited translation source from the
instruction. For example, translating a sentence containing “error” or “fixed”
does not itself add a `debug` tag or require ability 3. This applies to direct
Chinese/English translation requests with paired quotes or a single-line source
after a colon. Additional analysis/fix actions, unclosed quotes, and ambiguous
boundaries retain the original classification. Full request text, length
thresholds, and token estimates are unchanged. Explicit image/tool/capability
requirements and desktop thinking/expert signals still apply, as do manual
bindings and session continuity rules.

`resolve_model_client` emits `model.routing` records through the existing Python
INFO logger and, when a session trace context is active, the session JSONL trace.
ACP sends Python logs to stderr; the desktop host collects them in
`box-agent-stderr.log` under its logs directory. Trace files use the existing
`BOX_AGENT_SESSION_TRACE_DIR` / `log/sessions` location and retention policy;
`BOX_AGENT_SESSION_TRACE_ENABLED=0` disables that trace transport. Other hosts
must enable INFO logging to retain decisions made outside a trace context.

Group records by `routing_id`. An Agent attempt records `decision`, followed by
`bound`, `inherited`, or `binding_failed`; a rejected selection only records
`decision`. `strategy` distinguishes child tasks and utility calls. Within a trace,
the outer record also carries `session_id` and `turn_id`. The desktop's main-model
selection uses `[local-agent] model routing` in its Electron log, with its own
`routing_id` and `session_id`: `decision` is followed by `persisted` or
`preparation_failed`. These IDs identify attempts within each engine, not one
cross-process transaction. A selected candidate does not prove that a binding
was saved or that a provider request succeeded.

The versioned `decision` contains task tags, required ability, estimated input,
capacity certainty, and up to 64 candidate summaries. `candidate_count` and
`candidates_truncated` expose omissions; the selected candidate is always kept.
For each candidate, inspect `input_token_budget`, `matched_tags`,
`rejection_reasons`, and `score_breakdown`:

| Field / reason | Meaning |
| --- | --- |
| `context_capacity`, `context_unknown`, `ability`, `vision` in `rejection_reasons` | Candidate failed the corresponding constraint; `score=null` means it did not enter ranking. |
| `highest_score` | An eligible model won the existing score calculation. |
| `catalog_order_tiebreak` | Eligible scores tied; the earlier catalog entry won. |
| `previous_model_eligible` (desktop) | The current model still qualifies and was retained, even if another model scores higher. `minimum_ability_level` includes the current model's ability. |
| `manual_model`, `no_auto_model_pool`, `task_context_not_supplied` | A fixed or existing binding was retained without a new automatic ranking. |
| `no_context_candidate`, `no_ability_candidate`, `no_vision_candidate` | No candidate passed the relevant constraint. |

Scores preserve the existing policy: 100 per matching tag, ability × 20 for
non-simple tasks or ability × -5 for simple tasks, and 25 for a `fast` model on
a simple task. The desktop also adds 2 for its catalog default. Constraint
rejections take precedence over scores. For example, a model with a matching
`code` tag and ability 3 scores 160; another ability-3 model without matching
tags scores 60. A model with insufficient input capacity or ability cannot win
by having more matching tags.

Records project only these diagnostic fields: they do not copy the task text,
file paths, credentials, full model bindings, or exception text. Model names and
tags are bounded. Logging failures do not change routing outcomes. These records
explain local policy and client preparation, not model quality or a gateway's
eventual backend choice.

See the [routing evaluation baseline](../test_workspace/README.md#自动路由评测基线2026-10-31)
for offline policy checks and controlled comparisons of captured fixed-model and
automatic runs. Reports keep observed routing decisions, actual model requests,
and independent task outcomes separate.

### Estimating the next request

Provider clients attach usage metadata to the assistant message produced by a real API response. Compaction finds the newest such message and computes its complete response context as:

```text
input_tokens
+ cache_creation_input_tokens
+ cache_read_input_tokens
+ output_tokens
```

When usage has a request snapshot for the same route, the runtime starts with measured usage and adds estimates for new or changed messages and new or changed offered tool schemas. Removed material is left in the baseline until the next provider response updates it. Snapshots are kept in memory. Without a usable snapshot the runtime estimates the complete request, retaining unpaired usage from older sessions and low-level callers as a protective floor.

The standard client estimates text at one token per two UTF-8 bytes, rounded up, with the legacy character/byte estimate as a floor. This offline approximation can overestimate or underestimate actual usage. Custom clients may provide `estimate_text_tokens(text)`. Custom clients without a synchronous counter still use the byte bound. Low-level estimates without a client retain the legacy heuristic. Images are estimated separately. Skill overlays use the same counter as the final request. Compaction tries to shorten the existing summary to leave room for request assembly, then the final request is checked against the full input budget.

The run budget is capped by the active client's known window and matching catalog entry after reserving its output cap. ACP uses the binding's explicit `contextWindow` when present and sets that window on the selected model's entry in the session catalog, even if the entry omitted it. Otherwise it uses the smallest known window from the applicable profile/client, matching catalog entry, and startup configuration. A binding for a different model without any known window is rejected locally. Profile output limits are retained when host metadata omits them. These checks depend on accurate capability declarations. Detecting a silent reduction in the service's limit requires evidence from the service.

### Compacted message layout

Compaction makes one summary request by appending a temporary `user` instruction to the exact existing message list. It does not serialize messages into a new prompt and does not split or roll the source. This preserves the complete provider message prefix so the summary request can reuse its KV cache. ACP resolves this call through the session model router and caps it at 8,192 output tokens. Automatic sessions may select only from the host-provided `autoRouting.models` pool, ranked by the summary task tags, ability, and context fit. Context fit uses the active Agent's safe input limit so a small-window utility model is not selected for a larger history. Explicitly selected sessions stay on that exact model; there is no independent lite-model switch. ACP ignores `autoRouting` when `routingMode=manual`, even if the host sends a stale or malformed pool. This applies to session, child-agent, and utility bindings; legacy builtin bindings without a routing mode retain pool-based routing. Other hosts may supply a dedicated summary client or fall back to the main client. Tools and thinking are disabled. The instruction requires a chronological list of every user message and places all structured analysis inside one `<summary>...</summary>` block, with an embedded nine-section output example. The response must consist of exactly one non-empty summary block; only its inner text is written after `Summary:` and the tags are discarded. The requested and locally enforced summary limit is 8,000 characters; if the rebuilt history leaves less than 1,024 tokens of headroom, the same summary is deterministically tightened to 4,000 and finally 2,000 characters without another provider call. If the call fails, is malformed, or returns empty output, an explicitly lossy deterministic bounded fallback is used. If the rebuilt request still exceeds the limit, the turn ends with a normal context-limit error; compaction does not invoke a domain recovery state machine.

The model output is wrapped in this synthetic `user` message:

```text
This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
<model-generated summary>

Continue the conversation from where it left off. Do not acknowledge the summary, recap what was happening, or ask the user to repeat information solely because compaction occurred. If genuinely required information is still missing, use the normal user-input or decision tool. Otherwise, pick up the last task as if the break never happened.
```

The rebuilt history is ordered as follows:

```text
system message
summary user message
bounded recent messages
runtime-state user message
```

Recent selection applies to user, assistant, and tool messages. Assistant tool calls stay grouped with their contiguous tool results. Selection targets at most 5 messages and 20,000 total characters, and the latest real user message is always retained verbatim. When the newest complete protocol group alone exceeds the character cap, compaction keeps the exact assistant tool calls, arguments, result IDs, ordering, and result count while replacing already-summarized assistant reasoning and tool-result bodies with bounded receipts. A request is blocked only when the remaining exact user text, tool-call arguments, system instructions, tool schemas, bounded summary, and runtime state still cannot fit.

Compaction does not discover, reread, or replay recent files.

Current goal, todo, and plan state are read through their explicit, side-effect-free `compaction_state` contract; compaction never invokes a normal tool call. Their combined runtime-state message is capped at 12,000 characters. SkillRuntime owns selection and versioned read facts. Context places selected or restored Skill bodies in ordinary request material, or they arrive through real reading-tool replies; it checks coverage against final visible text. Full Skill bodies are not pinned in the system message, and historical `get_skill` calls are never replayed. See [Skill Engine](design/skill-engine.md) for paging, source validation and request-commit behavior. Internal summary/runtime-state messages are excluded whenever control policy asks for the latest real user text.

If the rebuilt request still exceeds the safe limit, the outcome is marked blocked instead of silently sending a known-oversized request.

## Adjacent protections

Write/edit tool-call arguments remain verbatim until whole-history compaction summarizes their turn; they are not independently replaced with history placeholders. This is separate from tool-result storage. A legacy safety guard still prevents placeholders from older or externally supplied sessions from being reused as executable file or code arguments.

## Verification

Direct regression coverage lives in:

- `tests/test_tool_result_storage.py` for type handling, exclusive writes, previews, Read single-result opt-out, failures, deduplication, and aggregate ordering;
- `tests/test_core.py` for pre-request enforcement, usage-plus-delta estimation, exact-prefix one-shot summarization, fallback estimation, bounded retention, and runtime-state restoration;
- `tests/test_auth.py` for the derived threshold.
- `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`,
  `tests/test_session_trace.py`, and `tests/test_llm_debug_logging.py` for the
  request-only native image path, provider conversion, and payload redaction.
