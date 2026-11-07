# Stable tool and Skill context

The session tool interface has two versions. New CLI and ACP sessions use version 2 by default. The public `Agent` API retains version 1 as its default, and historical session logs without a version marker are interpreted as version 1. A session keeps its selected version for its lifetime.

Version 2 keeps the system prompt and provider-facing tool definitions stable during an ordinary session. Tool and Skill discovery adds information to the conversation at legal message boundaries. Compaction, explicit history edits, model or protocol changes, and system policy changes can rebuild the prefix.

`tool_search` returns a tool's description, input schema and versioned `tool_ref`. Call discovered tools using `call_tool` with that reference and JSON arguments. The reference identifies the current definition and connection generation; it does not grant permission. Authorization and validation still apply at execution time. If a connection or definition changes, rediscover the tool. References from an earlier process must also be rediscovered after session restoration.

Discovery returns bounded, complete schema entries. Oversized or omitted entries receive a notice rather than an executable reference. Required tool descriptions are delivered again after compaction removes them. Execution events, permission checks, budgets and hooks retain the real target's identity, while model history retains the original `call_tool` call and call ID.

Skill recommendations are bounded runtime messages, not system prompt changes. They are snapshots of the current visible recommendations, and later snapshots supersede earlier ones. Use `list_skills` for the full catalog and `get_skill` for the chosen Skill's instructions. Skill metadata and instructions do not grant tools or permissions. Selected or restored guidance is delivered as durable runtime messages or bounded reading instructions; long guidance can require paged reading.

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
