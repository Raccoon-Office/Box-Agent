# Auxiliary output token budgets

Auxiliary requests use isolated clients so their output limits do not change the
main conversation, scheduled/expert execution, or either sub-agent execution
strategy (including batch-file synthesis). Global configuration and model catalog
budgets remain unchanged. The shared policy is in `box_agent/llm/task_budgets.py`.

| Request | Per-request output limit |
| --- | --- |
| Conversation title | 8,000 |
| Known lightweight purposes, follow-up suggestions, connectivity probes | 8,192 |
| Context compaction | The selected client's output limit, subject to available context. ACP caps its summary client at 8,192. A client without a declared limit defaults to 8,192. |
| Memory import, extraction, local update, conflict resolution | 8,192 |
| Core-memory promotion/replacement | 8,192, or 16,384 for larger replacements |
| Full memory compaction | 16,384 for bounded inputs |
| Continuation judgment | 8,192; 12,288 when thinking is enabled |
| Proxy image inspection | 8,192; 16,384 when thinking is enabled |
| Long-page web extraction summary | 8,192, including requests injected through MCP |

Known lightweight purposes are `title` (including existing title aliases),
`openclaw-memory`, `questionnaire-memory`, `tag-extract`,
`voice_transcript_cleanup`, `voice_summary`,
`local_conversation_context_summary`, `local_share_summary`, `expert-draft`,
`schedule-draft`, `summary`, `context_summary`, and `rewrite`. Unknown purposes
retain their existing budgets.

Manual model selection is preserved. Automatic selection still uses the existing
catalog/tag resolver. A task cap never increases a smaller declared client/model
limit. Thinking flags and provider dialects are unchanged; disabling thinking at
the call site does not guarantee that a provider produces no reasoning tokens.

Full text transforms and core-memory replacements use a conservative character
estimate (two tokens per character plus 1,024 tokens of envelope headroom) to
select an 8,192 or 16,384 tier. Inputs requiring more room retain their existing
budget instead of being forced into a smaller tier. This estimate is deliberately
conservative and is not a model tokenizer. Full memory compaction uses the same
oversize protection with a 16,384 baseline. Original inputs are not truncated by
this policy.

When a provider reports `length` or `max_tokens`, context compaction can use
one complete, nonempty `<summary>...</summary>` block. It keeps only the body
and retries if the summary tags are missing, unbalanced, or repeated, the body
is empty, or the response contains a tool call. Other auxiliary requests still
treat these finish reasons as failures even when the output parses as JSON.
Memory planners and maintenance use their failure fallbacks, lightweight RPCs
return `output_truncated`, and web and image tools return errors.

Validation must cover final request budgets, unchanged parent/child execution
budgets, model routing/thinking parity, smaller model limits, long replacements,
and rejection of truncated output. Source tests do not establish packaged runtime
or provider-side TPM behavior; those require a rebuilt runtime and a fresh client
request.
