# Sub-agent Delegation

`sub_agent` runs one self-contained task in an isolated message history. The
parent remains responsible for deciding whether delegation is worthwhile,
merging results, resolving conflicts, writing final deliverables, and final
verification.

## Request contract

Only `task` is required:

```json
{
  "task": "Compare the supplied API documents and report incompatible changes.",
  "title": "API docs",
  "skills": [],
  "required_tools": ["read_file"],
  "budget": {"max_steps": 60, "max_tool_calls": 100}
}
```

Machine-readable defaults are present in the tool schema:

| Field | Default |
| --- | --- |
| `title` | `""` |
| `skills` | `[]` |
| `required_tools` | all current inherited parent tools |
| `budget` | `{"max_steps": 60, "max_tool_calls": 100}` |

`execution`, `capabilities`, `inputs`, and `constraints` are not request fields.
Schema validation rejects them before starting a child model. An explicit empty
`required_tools` array runs the child without tools. Budget values are capped by
the configured sub-agent limits.

## One general-purpose loop

Every invocation uses the same iterative general-purpose agent loop. There is
no caller-selectable or runtime-selected execution strategy and no separate
file-batch path. File paths and other context belong in the self-contained task;
the child uses its resolved tools to inspect them as needed.

## Inherited tools, Skills, and constraints

At invocation time, omitted `required_tools` resolves to every tool in the
parent agent's current live tool map except `sub_agent` itself and parent-owned
deferred discovery. An explicit list resolves a strict subset; an unavailable
name fails before the child model starts. Passing the original instances
preserves their permission engines, workspace policy, sessions, and other
runtime state.

The child inherits the finalized parent system prompt, with parent-only MCP
discovery guidance removed. Selected top-level `skills` and their required Skill
dependencies are added to the child system prompt. Skill `allowed-tools`
metadata neither adds tools nor changes the explicit `required_tools` boundary.
Related Skills are not loaded automatically.

## Diagnostics

Successful calls return `ToolResult.raw_output` containing:

- `type: "sub_agent_delegation"`;
- `capability_source: "parent"`, `requested_tools`, and `resolved_tools`;
- requested and resolved Skills;
- effective budget and `defaults_applied`; and
- model/tool call counts and token usage.

Invalid requests and capability-resolution failures return stable structured
error payloads. ACP progress-event grouping and model-routing behavior are
unchanged.
