# Sub-agent Delegation

This document is the source-of-truth contract for the `sub_agent` tool added to
the current `0.8.79` development tree. It covers explicit capability
resolution, execution strategies, limits, compatibility, and host diagnostics.
For UI progress rendering, also read
[Host progress event integration](integration/host-progress-events.md).

## Execution model

A sub-agent has its own message history but reuses the parent session's live LLM
client and resolved tool instances. This preserves shared runtime state such as
the Jupyter kernel while preventing the child from seeing unrelated parent
conversation history. Only the child's final result returns to the parent;
structured progress can also be forwarded to ACP hosts.

The parent agent remains responsible for deciding whether delegation is worth
the startup/merge cost, resolving conflicts, writing the final deliverable, and
performing final verification. Recursive `sub_agent` delegation is always
denied.

## New-style request

Supplying `capabilities` selects strict new-style delegation. The smallest valid
request is:

```json
{
  "task": "Read the provided files and summarize them.",
  "capabilities": {
    "required_tools": ["read_file"]
  }
}
```

The full shape is:

```json
{
  "title": "API docs",
  "task": "Compare the supplied API documents and report incompatible changes.",
  "execution": {"strategy": "batch_files"},
  "capabilities": {
    "required_tools": ["read_file"],
    "optional_tools": [],
    "skills": []
  },
  "inputs": {"files": ["docs/api-v1.md", "docs/api-v2.md"]},
  "constraints": {
    "read_only": true,
    "network": true,
    "write_scope": null,
    "external_side_effect": false
  },
  "budget": {"max_steps": 1, "max_tool_calls": 2}
}
```

`required_tools` must be non-empty. Unknown fields and invalid values return an
`INVALID_DELEGATION_SPEC` diagnostic before any child LLM call. The caller may
correct that declaration once; strict requests never silently fall back to the
legacy path.

## Capability resolution

The runtime normalizes requested names, expands selected Skills and their
`required_skills`, adds their `allowed-tools` routing metadata, and intersects
the result with the parent's live tool map and the declared constraints.
`related_skills` are suggestions only and are not auto-loaded.

Defaults remain read-only and side-effect-free while allowing network access:

| Constraint | Default | Effect |
| --- | --- | --- |
| `read_only` | `true` | Denies write and process tools. |
| `network` | `true` | Allows tools marked as network-capable; set to `false` to deny them. |
| `write_scope` | `null` | No delegated writes are allowed under the default read-only policy. |
| `external_side_effect` | `false` | Denies tools that change external systems. |

To delegate file writes, set `read_only: false` and provide a non-empty
`write_scope`. The runtime wraps `write_file`, `append_file`, and `edit_file`
and rejects targets outside those roots before invoking the live tool. Other
write-capable tools are rejected because their scope cannot be enforced by this
path. Existing `PermissionEngine` checks remain the final resource-level gate.

Required capability failures stop before execution. Optional tools may be
omitted and are reported in `denied_tools`. An unknown required MCP tool returns
`REQUIRED_TOOL_NOT_READY` while MCP discovery is still loading and
`REQUIRED_TOOL_NOT_FOUND` after discovery is ready.

## Strategies and hard limits

### `general_loop`

Use for heterogeneous work, independent network research, or tasks that need an
iterative tool loop.

- Default and maximum budget: 12 model steps and 16 total tool calls.
- The child receives only the resolved tools and selected Skill instructions.
- Both per-tool loop guards and the total `max_tool_calls` budget are enforced.
- Calls can still run in parallel when the resolved tool is marked
  `parallel_safe`; the child loop currently uses the core defaults of eight
  concurrent calls and a 900-second per-batch timeout.

### `batch_files`

Use when several known local text files need the same read-only summary,
comparison, evaluation, or extraction. Prefer one batch over creating one child
per file.

- `required_tools` must be exactly `["read_file"]`.
- `inputs.files` must contain 1–32 unique paths.
- Reads run concurrently and each file must be proven complete by structured
  `read_file` metadata.
- Per-file limit: 64,000 selected characters.
- Aggregate limit: 200,000 characters.
- Budget is fixed to one synthesis step; `max_tool_calls` must cover every file.
- After prefetch, the child makes exactly one tool-free synthesis call with
  thinking disabled.
- `sub_agent_batch_synthesis_timeout_seconds` adds a wall-clock cap to that
  synthesis call (default `300`; `0` disables this extra cap and leaves the
  provider request timeout in control).

If any read fails, is truncated, cannot prove completeness, or exceeds a limit,
the runtime returns `BATCH_FILES_PREFETCH_FAILED` and makes no synthesis model
call. A synthesis timeout returns `BATCH_SYNTHESIS_TIMEOUT`.

## Legacy compatibility

A call containing `task` and no `capabilities` field uses the legacy child loop.
It inherits the parent's eligible tools and parent system prompt, retains the
legacy 40-step loop and configured `sub_agent_token_limit`, and does not apply
the new declaration schema. Passing `capabilities: null` is an invalid strict
request, not a legacy request.

New callers should always use explicit capability declarations. The legacy path
exists only for older prompts and hosts.

## Diagnostics and host integration

Successful strict executions return `ToolResult.raw_output` with:

- `type: "sub_agent_delegation"`
- strategy, requested/resolved tools and Skills, denied optional tools
- normalized constraints, budgets, and defaults applied
- model/tool call counts and token usage
- `aggregate_chars` for successful `batch_files` calls

Pre-execution and batch failures use
`type: "sub_agent_delegation_error"` plus a stable `code`, `message`, and
`retryable` flag. ACP progress events remain grouped under the parent tool call
with `rawOutput.type: "sub_agent_progress"`; diagnostics belong to the final
parent `sub_agent` result rather than progress-card heuristics.

## Configuration

```yaml
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
sub_agent_token_limit: 50000
sub_agent_batch_synthesis_timeout_seconds: 600
```

Both sub-agent settings appear as commented advanced overrides in
`box_agent/config/config-example.yaml`. Keeping them commented lets runtime
upgrades revise their defaults without pinning newly generated user configs.

## Implementation and proof

- Schema and resolution: `box_agent/tools/sub_agent_capabilities.py`
- Execution and diagnostics: `box_agent/tools/sub_agent_tool.py`
- Live tool/Skill/MCP state wiring: `box_agent/tools/setup.py`,
  `box_agent/agent.py`, `box_agent/cli.py`, `box_agent/acp/__init__.py`
- Total tool-call guard: `box_agent/core.py`, `box_agent/loop_guards.py`
- Regression coverage: `tests/test_sub_agent_capabilities.py`,
  `tests/test_sub_agent_tool.py`, `tests/test_core.py`, `tests/test_acp.py`
