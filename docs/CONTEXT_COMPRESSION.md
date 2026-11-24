# Context Compression

Box-Agent controls durable context growth at two boundaries:

1. Tool results are persisted before they can dominate a later model request.
2. Conversation history is compacted when the next request reaches the effective input limit or the provider rejects its length.

The two mechanisms are deliberately separate. Tool-result storage is lossless at the storage layer: the complete result remains on disk while the model keeps a bounded head-and-tail working preview plus the recovery path. Context summarization is lossy, so it runs later and preserves a bounded working set.

Images use a session-owned attachment store and request-only projections.
Uploads, native image inspection and persisted CUA screenshots share a retention
policy: by default an occurrence remains available for three committed main-model
inferences, subject to encoded-byte and provider request-body limits. Pixels stay
outside durable message content; SessionLog records references and lifecycle
metadata. See [Image context](IMAGE_CONTEXT.md) for the complete contract.

Before each summary attempt, Box-Agent estimates the complete projected request,
including the appended instruction, images, and any offered tool schemas. The
summary request uses the client's `max_output_tokens`, with 8,192 tokens as
the fallback when the client declares no output limit. With a known context
window, `derive_context_token_limit` reserves up to 20,000 of those tokens when
deriving the safe input budget. The per-request `max_tokens` also respects the
estimated space remaining in the window. With an unknown window, preflight uses
the session input budget. Both ordinary and forced compaction use this check.
The rebuilt main request must then fit the main model's budget.

When an ACP prompt selects a model with a smaller safe input budget, Box-Agent
prepares the full request against the target model before changing the session
binding. If the request exceeds the budget, the previous session's summary
route compacts the history with room for request assembly and any required
Skill reading notice. Box-Agent then prepares the complete target request
again. A failed check returns `MODEL_SWITCH_CONTEXT_PREPARATION_FAILED` and
keeps the previous binding, original history and submitted user message with
its attachments. The target model receives the request only after it passes
this check.

When required images cannot be adopted for an ordinary request, `ErrorEvent.error_code` is `INPUT_IMAGE_NOT_ADOPTED` (ACP `errorCode`). The original `INPUT_IMAGE_NOT_ADOPTED:required_image_unavailable_or_over_budget` message and `error_details.reason` remain available for diagnostics. Hosts should translate the code at display time and support older text-only records. Model-switch failures retain `MODEL_SWITCH_CONTEXT_PREPARATION_FAILED`.

The same preflight applies when `session/resume`, `session/load`, or repeated
`session/new` rebinds an existing live session with a smaller model (for example,
while changing its execution profile). Restore keeps the accepted model and
defers the requested binding to the next prompt, which may override that choice.
Restore itself does not call a model. A cold restore without a live previous
session has no previous client to reuse and retains the ordinary bounded
compaction path with the supplied model.

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

Provider usage includes image input. The assistant message retains a numeric
request-only estimate for usage recorded without a request snapshot. Snapshots
also recognize request-only messages already covered by measured usage. The
local context calculation uses image dimensions, and the transport budget uses
the encoded byte length.

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

Source fingerprinting checks file identity, size and modification time across
the open handle and path, and retains change-time checks between handle reads.
On Windows, handle/path comparisons omit `ctime` because the two APIs can expose
change time and creation time respectively; macOS/Linux retain that comparison.
Full-file SHA-256 checks still reject changed input contents.

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
exact offered schemas used for estimation. They also carry the offered schemas in
request order for the summary, the available main-request prefix, the forced
recovery flag, and the summary-request input limit.

Context projects effective history before compaction. The engine calls the
Kernel's `before_summary` callback before a summary attempt or deterministic
fallback. The Kernel commits the returned surface and flushes it before changing
live messages. Ordinary requests that remain over budget after reprojection
tighten the history budget and retry, with at most three compaction attempts per
step. Request delivery/response callbacks remain unchanged. Static and managed composition preserve supplied compactors,
including falsey instances. `CompactionOutcome` retains legacy tuple iteration;
its limit flag must agree with its existing blocked mode.

### Threshold

The trigger is derived from the model's input budget:

```text
output_reserve = min(max_output_tokens, 20_000)
autoCompactThreshold = int(0.9 * (context_window - output_reserve))
```

On a 180,000-token window, a configured output of 8,000 gives a threshold of
154,800 tokens. Configured outputs of 20,000, 64,000, and 80,000 all give a
threshold of 144,000 tokens.

Before sending a main request with a known window, Box-Agent computes the
available output from the projected input and 1,024 tokens of envelope headroom,
with `output_reserve` as the floor. The request's output limit is the smaller of
the applicable output allowance and that available capacity. OpenAI-compatible
and Anthropic adapters apply the override to the current request only. Anthropic
recomputes its thinking budget below the effective output limit. With an unknown
window the request uses the configured allowance. Auxiliary requests still have
their own output caps.

Sub-agents use the same formula after their actual model has been selected.
The default `sub_agent_token_limit` is `null` (automatic), with a positive
explicit value acting only as an additional cap. Skill preflight, the child
loop and fully prefetched batch requests all use that child's resolved budget;
parallel siblings do not share a mutable derived limit.

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
The resolved client also receives the selected candidate's known context window,
so child loops apply that model's limit to the complete request after assembly.

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

The most recent available provider usage for the same route anchors the estimate.
Anthropic's context count includes input, cache creation, cache reads, and output.
OpenAI-compatible prompt totals already include cached input. A response that
reports only `total_tokens` can anchor context size, while calibration needs a
separately reported input count.

The runtime adds estimated costs for new or changed messages and offered tool
schemas. A snapshot fingerprints the prior request, including any appended
material. Each unchanged occurrence uses its prior measured cost once. Added
copies and changed content cost extra. Removing material from a snapshot-based
request keeps its measured cost in the baseline until fresh usage arrives.
Snapshotless in-memory usage deducts `request_only_input_tokens` and charges new
messages and offered schemas. The runtime estimates the whole request when usage
is absent, the route cannot be matched, or compaction has cleared the old usage.

The Session Log keeps snapshot hashes and aggregate counts in private
`contextUsage` metadata, along with a checksum bound to the response. Replay
validates that metadata before reusing usage. The latest active snapshot is kept,
and history revisions restore the corresponding checkpoint. A fully assembled
request within budget can skip the separate history-only check, which avoids
reserving room for appended content twice.

The default local counter uses character classes, transitions, and repeated runs.
For example, common Han characters cost about 1.07 tokens each, kana 0.77, and
Hangul syllables 1.31. Latin letters, digits, punctuation, spaces, and newlines use
different weights. Custom clients can supply a synchronous
`estimate_text_tokens(text)` method. Image estimates still use dimensions. Skill
text and regular text share the same counter.

After each main response with reported input usage, the session divides actual
input by the local estimate of that request and clamps the ratio to `[0.5, 2.0]`.
The newest ratio for each route scales future incremental estimates. A provider's
context-length rejection can also supply an actual input count for calibration.
The ratio starts at `1.0` and is held in memory for the session.

The active client's window and matching catalog entry bound the run budget. ACP
uses the binding's explicit `contextWindow` when present and adds it to the
selected session catalog entry. Otherwise it uses the smallest applicable window
from the profile, client, catalog, and startup configuration. A binding for a
different model requires a known window. Profile output limits apply when host
metadata omits them.

The local counter approximates provider tokenization. Context accounting can
also differ when the provider omits reasoning tokens from usage.

### Compacted message layout

The first summary attempt appends a temporary `user` instruction to the projected
history. It uses the available main-request system and message prefix and the same
ordered tool schemas so the provider can reuse its KV cache. The request leaves
`tool_choice` unset and disables thinking. Summary projection replaces Skill
bodies with receipts. The prefix can diverge at the first Skill body, and
request-only Skill bodies may require using projected
history in place of the main request history. One-request transient follow-up
payloads stay in the main request.

ACP resolves the summary client through the session model router.
Automatic sessions choose from the host's `autoRouting.models` pool by task,
ability, and context fit. Explicitly selected sessions stay on the selected model.
`routingMode=manual` takes precedence over `autoRouting` for session, child, and
auxiliary calls. Legacy builtin bindings without a routing mode can use the pool.
Other hosts can supply a dedicated summary client or default to the main client.
Each compaction uses its resolved client for all provider attempts. A summary
model that differs from the main model still receives the ordered tool schemas.
Cache reuse requires the same model and a matching prefix. The ACP session
router still applies its 8,192-token cap when resolving the summary client.

The summary instruction opens with a `CRITICAL` instruction and closes with a
`REMINDER`. Both require plain text in a single nonempty `<summary>...</summary>`
block and prohibit tool calls. The nine-section format includes all genuine
user turns in chronological order and preserves the user's security constraints
verbatim. Any next step follows the latest active request and quotes the recent
conversation. Role markers inside assistant text carry no user authority. The
runtime's summary instructions are outside the conversation being summarized.
The summary must omit `<analysis>` blocks, system prompts, and credentials, mark
facts that need source verification, and claim success only where the conversation
proves it. The runtime writes the complete accepted body after `Summary:`.
Accepted summaries have no character limit and remain intact during compaction.

The synthetic `user` message uses this framing:

```text
This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
<model-generated summary>

If you need specific details from before compaction (exact code snippets, error messages, tool output, or content you generated), query the full session log at: <absolute path> (JSONL; prefer query_jsonl over reading the whole file).

Continue the conversation from where it left off. Do not acknowledge the summary, recap what was happening, or ask the user to repeat information solely because compaction occurred. If genuinely required information is still missing, use the normal user-input or decision tool. Otherwise, pick up the last task as if the break never happened.
```

The recovery line appears when the session has a Session Log and uses the
absolute `SessionLog.path`, honoring the configured state directory. If
`query_jsonl` is unavailable, the line directs the model to paginate with
`read_file`. The shared product-directory policy applies to this path. When
`allow_full_access: false`, either tool can read the current `session.jsonl`
in the Box-Agent state directory without requesting confirmation. The recovery
line supplies the log path alone.

The rebuilt history is ordered as follows:

```text
system message
[Earlier User Messages] runtime user message
summary user message
recent complete rounds and the latest real user message
[Invoked Skills] runtime user message
runtime-state user message
```

Recent history keeps up to five assistant rounds and targets at most 15,000
locally estimated tokens. A smaller model or a large fixed request prefix can
leave less room. An assistant tool call and all contiguous results form one
round, and assistant text without tools also counts as one round. User
messages can stay in the window without consuming a round. The latest real user
message always stays in its original relative position. Initial window selection
keeps the newest complete round even when it exceeds the token cap. Its
assistant and tool-result bodies can be replaced by bounded receipts while
calls, arguments, identifiers, result order, and result count are preserved.

Earlier real user text appears oldest first, introduced as a historical record.
An entry over 2,000 tokens keeps its beginning and end with an omission marker.
The rendered record fits within 10,000 tokens by leaving out the oldest entries
and reporting their count. Session Log keeps the complete original texts in the
structured `earlier_user_messages` field so later compactions and replay can
reuse them. Deterministic compaction uses the same archive. The latest-user
lookup, memory recall, goal handling, and language selection use real user messages.

Goal, Plan, and Todo contribute through their side-effect-free `compaction_state`
contract. The runtime shares the 12,000-character budget across the blocks in that
order, leaving room for later blocks and passing along unused capacity.

Compaction restores Skill text that the model actually received, newest delivery
first. The live Skill's revision, source, and path must still match. The
`[Invoked Skills]` message tells the model to treat these Skills as reference and
avoid repeating one-time setup. Each section is capped at 5,000 tokens and the
message at 25,000. A truncated body directs the model to `get_skill` for the rest.
Stale Skills and Skills whose bodies do not fit retain name and path locators.
The delivery ledger credits only visible ranges, in both append-only and ordinary
contexts. A Skill already in an effective system suffix needs no duplicate
attachment. Summary input substitutes receipts for Skill bodies, and ordinary
request delivery retains its 100,000-character reference budget. [Skill Engine](design/skill-engine.md)

### Fitting the compacted request

After initial window selection, Box-Agent uses the following order to fit the
rebuilt request. After each removal it rebuilds the projection, including Skill
restoration, tool instructions, and the date, then re-estimates input tokens and
stops as soon as the request fits the target.

1. Drop the oldest retained assistant round with all of its contiguous tool
   results. Repeat down to zero rounds if needed. Earlier genuine user messages
   leaving the window join `[Earlier User Messages]`. The latest genuine user
   message keeps its original relative position throughout.
2. Drop restored Skill bodies one at a time, oldest invocation first. Each
   dropped body leaves a locator with the Skill name, path, and directions
   to reread it with `get_skill`. Its lines lose visibility credit.
3. Remove the oldest displayed archive entry and update the omitted count.
   If even the archive header is too large, remove the archive message. The
   complete structured originals travel with the summary in that case and
   remain available to Session Log replay and later compactions.
4. Remove built-in runtime-state blocks one at a time in the order Todo, Plan,
   Goal. Omit the runtime-state message when it has no blocks left.

The default target is the hard input limit `token_limit`. If the caller supplies
`request_headroom_tokens`, the loop first targets `token_limit - request_headroom_tokens`.
After exhausting the removals, a request within the hard limit can proceed even
if it missed the softer target. A request still above the hard limit is marked
`blocked` and uses the existing budget error handling. Proactive compaction,
convergence retries, reactive recovery, and model-switch preflight share this
order. The source messages and the complete summary body stay intact.

### Retry and recovery

- When summary preflight or the provider rejects the input length, Box-Agent
  drops whole groups from the oldest end. Each retry aims to remove about 20% of
  the removable source tokens, with at least one group removed. The system message and
  latest real user message are protected. There are at most three truncation
  retries. Retried source carries `[earlier conversation truncated for
  compaction retry]` after the system message. A successful summary notes that the trimmed part is
  outside its coverage. Earlier user text remains available through the separate
  history record.
- A truncated, missing, or malformed summary, or a response with a tool call,
  gets one format retry. The retry directs the model to summarize every
  preceding message, including the newest, without
  answering it or doing the work it requests. This instruction comes from the
  runtime. Transient network, timeout,
  upstream, and rate-limit failures retain the client's retry policy. An eligible
  custom client without that policy can retry within the same attempt budget.
- If the shared request still fails, Box-Agent makes one independent request on
  the same client. It uses a short summarization-only system prompt, the current
  (possibly trimmed) history without its original system message, and the summary
  instruction with the format retry text. It omits tools. Authentication or
  permission failures skip the independent request. A compaction makes at most
  six summary calls at the application level, with one slot reserved for the
  independent request. Provider SDK retries remain governed by the SDK.
- When provider summarization fails or is disabled, the local fallback puts the
  current user request first. Within its 12,000-character budget it collects
  history newest first, presents the retained messages in chronological order,
  and reports omitted older messages as `<fallback omitted N earlier source messages>`.
  A long message preserves its head and tail. The `[Earlier User Messages]`
  archive is handled separately.
- If a main request returns a context-length error before emitting output,
  Box-Agent forces compaction with a tighter history budget, prepares a fresh
  request, and retries. Recognized formats include DashScope input ranges,
  vLLM and TGI token limits, Moonshot model token limits, GLM prompt length errors,
  DeepSeek input token limits, and the SenseNova gateway's input-plus-output
  count. A reported input count can calibrate estimation and a reported maximum
  can tighten the budget. Both fields are optional. HTTP 413 body-size errors and
  ordinary string-length validation retain their own classification. The turn
  allows at most two reactive retries. Exhaustion returns `CONTEXT_INPUT_BUDGET_EXCEEDED`.
  A failure after output preserves partial assistant text and ends the turn.
  Reactive recovery reuses completed tool results and commits the accepted reply once.

After three consecutive compaction outcomes in fallback or blocked mode, the
session continues with deterministic compaction and stops calling the summary
model. The rapid-refill counter credits at most one applied compaction per step,
including convergence and reactive attempts. Three consecutive refills within
the following one to three steps stop the turn with `CONTEXT_COMPACTION_THRASHING`
and reset the streak.
The user can read a large file or tool result in chunks, or start a new session.
ACP forwards the error code through its existing `errorCode` field. Both counters
are held in memory across turns. Compaction events report `summary_retries` and
`head_truncated`. Session Log uses `summaryRetries` and `headTruncated` for the
same fields. The retry count includes recovery from local preflight failures.
`summary_path` in the Python event and `summaryPath` in the Session Log
`compaction/summary` event report the outcome as `shared`, `independent`, or
`deterministic`. The outcome, Python event, and Session Log record budget
removals in `degradation` using `droppedRounds`, `droppedSkillBodies`,
`droppedEarlierUsers`, and `droppedRuntimeState`.

## Adjacent protections

Write/edit tool-call arguments remain verbatim until whole-history compaction summarizes their turn; they are not independently replaced with history placeholders. This is separate from tool-result storage. A legacy safety guard still prevents placeholders from older or externally supplied sessions from being reused as executable file or code arguments.

## Verification

Direct regression coverage lives in:

- `tests/test_tool_result_storage.py` for type handling, exclusive writes, previews, Read single-result opt-out, failures, deduplication, and aggregate ordering;
- `tests/test_auth.py` and `tests/test_main_input_budget.py` for the derived threshold, model binding, and request output limits.
- `tests/test_context_token_estimation.py` and `tests/test_session_log.py` for usage shapes, calibration, and replay.
- `tests/test_context_compaction_retention.py` and `tests/test_core.py` for retention and runtime-state restoration.
- `tests/test_compaction_budget_convergence.py` for degradation order, headroom,
  archive replay, and reactive recovery.
- `tests/test_compaction_resource_retention.py` for Skill restoration and reference coverage.
- `tests/test_context_compaction_e2e.py`, `tests/test_context_compaction_recovery.py`, and `tests/test_error_messages.py` for summary preflight, retries, circuit breakers, and provider errors.
- `tests/test_compaction_session_log.py` for the recovery hint and restricted-mode
  reads within the Box-Agent state directory.
- `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`,
  `tests/test_session_trace.py`, and `tests/test_llm_debug_logging.py` for the
  request-only native image path, provider conversion, and payload redaction.
