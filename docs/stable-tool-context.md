# Stable tool and Skill context

The session tool interface has two versions. New CLI and ACP sessions use version 2 by default. The public `Agent` API retains version 1 as its default, and historical session logs without a version marker are interpreted as version 1. A session keeps its selected version for its lifetime.

Version 2 keeps the system prompt and provider-facing tool definitions stable during an ordinary session. Tool and Skill discovery adds information to the conversation at legal message boundaries. Compaction, explicit history edits, model or protocol changes, and system policy changes can rebuild the prefix.

`tool_search` returns a tool's description, input schema and versioned `tool_ref`. Call discovered tools using `call_tool` with that reference and JSON arguments. The reference identifies the current definition and connection generation; it does not grant permission. Authorization and validation still apply at execution time. If a connection or definition changes, rediscover the tool. References from an earlier process must also be rediscovered after session restoration.

Configured first-party `web_search` and `web_extract` have local, stable schemas and are included in the basic tool set before MCP connections are ready. An ordinary message can reach the model immediately. Only an actual web invocation waits for its backing server, then delegates to the current MCP tool with the existing argument validation, authentication and result handling. Each invocation resolves the current backend, so reconnecting it does not replace the model-facing tool definition.

Execution events and durable tool call/result records include the backend `tool_id` and `server_name`. These identifiers use the same MCP namespace as the backing tools and remain stable across reconnections.

Both preloaded and searched descriptions identify `callable_function: "call_tool"`
and include invocation guidance next to the capability's schema. The display `name`
is not a directly callable function in version 2. Descriptions no longer supply the
old unconditional `call_example` with empty arguments: `{}` is invalid for tools
with required inputs. Build `arguments_json` from the actual `input_schema` and
task inputs. Older descriptions and already-recorded calls remain readable; this
metadata change does not alter reference admission or the `call_tool` schema.
Version 2 also explains how to map workflow examples written for another runtime
to currently available capabilities. This mapping preserves the workflow's business
steps, user constraints, approvals and manual choices; it does not authorize a
different task or make an unavailable API callable.

If a turn stops after announcing an action, continuation can restate the current,
already-delivered route and complete schema named by the continuation verdict.
Hidden, changed or expired routes are excluded. This hint neither grants permission
nor increases the retry budget. An empty response after tool use also permits
remaining authorized work instead of forcing a premature final summary; completed
actions must not be repeated.

Discovery returns bounded, complete schema entries. Oversized or omitted entries receive a notice rather than an executable reference. Required tool descriptions are delivered again after compaction removes them. Execution events, permission checks, budgets and hooks retain the real target's identity, while model history retains the original `call_tool` call and call ID.

Ordinary MCP tools use `mcp__<server>__<tool>` model-facing names, including
user-installed servers without connector metadata. Provider-invalid, long or
ambiguous separator names receive a deterministic hash suffix. Existing browser
public names and the explicit hosted `web_search`/`web_extract` replacements keep
their public contracts. Native tools and tools from different MCP servers can
therefore share an upstream name without replacing or hiding each other.

Installed MCP configuration, credentials and upstream `tools/call` names do not
change. `tool_search` accepts original names and previous public names/IDs;
ambiguous short names require `server_name` or a qualified ID such as
`mcp:seko/generate_image`. Version 1 additionally accepts an old direct-call name
only when its target is unique, currently offered, and does not shadow any
registered native name/alias or current MCP name. Hidden and unactivated tools
still participate in direct-call ambiguity checks. New definitions advertise
only the canonical name; permission and connection-generation checks still apply.

Restored sessions keep their protocol version and historical calls/results.
A runtime notice requests rediscovery using the server and original tool name;
completed historical calls are not replayed. Version 2 requires fresh references
and does not accept legacy direct-call aliases. Custom Skills/scripts should use
qualified names (or server-scoped discovery) when a short name is ambiguous.
The new naming takes effect when connections are created by the updated runtime;
existing live tool objects are not renamed in place.

After a successful connector hit, `tool_search` also returns `connector_catalogs`:
the exact model-facing names and short purposes of that connector's discoverable
tools, including tools on its other MCP servers. This metadata does not activate
sibling tools or issue their schemas or references; use `tool_names` to discover
the tools needed for later steps. Authorization, visibility and name-conflict
filters apply before building the catalog.

For MCP tools without a connector ID, a successful hit returns `server_catalogs`
instead. Each group identifies its `server_name` and contains only discoverable
tools without a connector ID from that same server. Different user/system MCP
servers remain separate, even when a server name equals a connector ID. This
fallback does not assign a connector identity or change tool authorization.

Purposes are whitespace-normalized and limited to 160 characters. Catalogs share
an 8,000-character JSON budget per search across connector and server groups,
with existing connector groups taking priority, separate from the existing schema
budget. `tool_count` gives the eligible total, `omitted_count` reports tools
omitted from a group; `connector_catalogs_omitted_count` and
`server_catalogs_omitted_count` report whole groups omitted for size. Omitted tools
remain searchable. An unchanged catalog is sent
once per session; connection-generation or visibility changes allow an update.
Version 2 also permits redelivery on the next matching search if compaction or
projection removes the catalog from the model's history. Catalogs are not eagerly
restored, nor inserted into the system prompt or top-level tool definitions.
Version 1 retains session-level deduplication without history-based redelivery.

Skill recommendations are bounded runtime messages, not system prompt changes. They are snapshots of the current visible recommendations, and later snapshots supersede earlier ones. Use `list_skills` for the full catalog and `get_skill` for the chosen Skill's instructions. Skill metadata and instructions do not grant tools or permissions. Selected or restored guidance is delivered as durable runtime messages or bounded reading instructions; long guidance can require paged reading.

New version 2 catalog and tool-description messages record `source: runtime` and
`runtime_kind` in Session Log and the `llm.request` trace. The model receives
a host-reference label with the Skill catalog and corresponding fields within
tool-description JSON. Messages saved before these fields were introduced keep
their original wire content. Reordering the same rendered Skill recommendations
reuses the previous snapshot.

In the deferred MCP mode of version 2, the managed `playwright` server exposes
its schemas through `tool_search` even when its host config has `alwaysLoad`
enabled. Search by capability or by exact names scoped to `playwright`, then use
`call_tool` with the returned references. A navigation hit includes its page
snapshot companion, including on an exact lookup. A navigation or snapshot hit
also includes the click schema when the current user turn explicitly asks to
click. The same visibility, authorization and discovery budget apply to the
companions. The MCP connection follows the normal background discovery path.
Model-facing Playwright receipts point automatic snapshot references to the
inline snapshot tool and omit private console cache links. The original
diagnostic result and explicitly requested file outputs remain available.

Compaction does not promote discoveries into the system prompt or top-level tool definitions. It restores complete, acknowledged tool descriptions (including schemas and existing refs) at the message tail when missing. Current definition, visibility, permission and connection-generation checks still apply; stale or unacknowledged discoveries are not restored. Required/always-load descriptions also retain their existing recovery path. Compaction alone does not disconnect MCP or issue new references.

A new turn only marks earlier Skill reads as candidates for cleanup; it never deletes or rewrites their historical messages. Current explicit selections, pending restores and methods actually read/reused this turn are protected. When there is no current method signal, retain earlier methods conservatively: a turn boundary alone does not prove a task switch. At actual compaction, reclaimable bodies become revision/path/reread notices, while current verified pages are retained or restored within the reference budget. Never restore unread pages or treat a summary as a complete method. If a body cannot fit or its source changed, retain a reading notice instead. This policy also applies to forced compaction; it does not introduce a new manual trigger.

Method bodies are managed separately from both model-generated summaries and deterministic fallback summaries. Compaction-source and result projection run only after the existing pressure/force decision, and restored resources count toward the existing post-compaction budget. Trigger thresholds, timing, cooldown and ordinary request preparation are unchanged. Session Log remains the durable source; only the committed compacted surface replaces history.

After compaction has already triggered, an over-budget result may shorten the
generated summary below the preferred 2,000-character floor, first to 1,000 and
then to 512 characters. Each candidate includes the restored resources when
measuring the budget. This does not add a summary model call, change the trigger,
or truncate protected user input or tool-call groups. If that protected context
still cannot fit, the existing budget error remains.

In version 2, `get_skill` owns a per-turn recovery check. If previously delivered
instructions are absent, execution must first reread the relevant method. Readers,
discovery, table validation and user decisions remain available. For an unrelated
method only, `get_skill(skill_name=..., not_needed_reason=...)` explicitly defers it
for this turn without recording a read. Current selections, dependencies and
methods named by the current user input cannot be deferred. A new turn or user
supplement reopens the decision; an interactive pause preserves its checkpoint.
A partially read method needs its previously delivered pages again, not unread pages.
Partial rereads cannot recover a previously complete method; summaries never count
as reads. Required deliverables,
including separate receipts, must be checked against the full method before
completion. Applicability and prose requirements still require model judgment;
the recovery check does not claim to validate every arbitrary Skill instruction.

Recovery guidance treats a compacted summary as a source locator, not evidence.
Before reusing facts across tasks or changing an artifact after compaction, the
model must use a tool to reopen the relevant source or verified report, then
read back the resulting artifact. A summary or earlier assistant prose does not
count as that readback. A list of excluded IDs or a mixed-status group does not
establish each record's status; preserve the grouping unless the source provides
the individual mapping, and omit optional details that cannot be verified. Deferring an
old method does not waive this source check. User prohibitions on installation,
network access and delegation continue to apply during recovery and tool errors;
general setup advice does not override them. Use existing permitted capabilities
or report the blocker. These are model instructions, not a deterministic semantic
validator or an execution sandbox for natural-language restrictions. In particular,
the Python sandbox's existing environment preparation and missing-module retry
can install packages; use an already prepared environment under a no-install
constraint. Limit `find_spec` checks to top-level module names: dotted lookups
import parents. Catch `ImportError` during probes and imports and report the
missing dependency without re-raising; a found spec does not verify transitive
dependencies. The compaction trigger and
the session's fixed system/tool interface remain unchanged.

If a helper or source path is missing, `read_file` first suggests an existing
same-named file resolved from the active directory or workspace, provided the
current read permissions allow it. The failed call does not read or substitute
that file; the model must explicitly inspect the candidate before using it.

Complete numeric key/value lines are valid answer endings under a normal provider
finish. The suspected-truncation detector does not append a closing sentence to
such a line; an explicit token-limit finish still uses the existing continuation.

Compaction rewrites message history and is a cache boundary of its own. The [cache evaluation](../test_workspace/inputs/context_cache/README.md) checks append-only loading separately from offline compaction/recovery contracts. It never reports a rewritten-history sample as proof that ordinary loading preserved the cache prefix.

For rollback of **new sessions**, set the following in the host YAML configuration:

```yaml
agent:
  tool_interface_version: 1
```

Existing version 2 sessions continue using version 2 and require a runtime that understands their history; changing the setting does not migrate them. Version 1 retains the existing direct tool exposure and Skill catalog behavior. Public API callers can explicitly select the new protocol with `Agent(..., tool_interface_version=2)`.

Source tests establish behavior only for the tested source tree. A packaged host needs its own build, installation, restart and live-task verification before its runtime can be considered updated. Stable inputs do not guarantee provider cache hits.

Table quality checks are independent of compaction policy. For explicitly named
CSV/TSV inputs, the table capability admits Python, Shell and sub-agent execution
only after the original-input validation/preparation required by the declared
operation. File readers, Skill loading and tool discovery remain available.
The tool engine invokes capability-owned checks against the actual resolved tool,
including calls routed through `call_tool`; no model-visible control field or
mutable top-level definition is added. Final-state validation still runs at
completion. Runs with completion checks disabled retain their existing behavior.

Report guidance requires computing derived numbers into shared structured results,
using those values to generate prose and charts, then reading back the artifacts.
This is model guidance, not a universal semantic validator. Offline evaluation
checks explicit metric contracts and surfaces other numeric claims for independent
review. It must distinguish correct artifacts from successful tool/ACP responses.
