# Session and Skill Ownership

This document defines ownership after the removal of the checkpoint,
Completion Gate, and workflow-owner runtimes.

## Durable session ownership

Session Log is the sole durable source for an Agent session. It owns generic
facts only:

- messages and tool call/result history;
- Goal, Plan, Todo, and active Skill state;
- compaction records and turn boundaries;
- the immutable normalized session cwd.

Opening a Session with a different cwd fails. Legacy checkpoint and owner files
may remain on disk for audit, but Box-Agent does not read, rewrite, migrate, or
delete them automatically.

Legacy synthetic workflow messages are filtered during replay. Recovery keeps
the generic conversation and durable artifacts without rebuilding the old
domain state machine.

### Optional manual cleanup

Box-Agent intentionally leaves historical files in place. After stopping every
Agent process for the workspace, an operator may inspect

- `<workspace>/.box-agent/checkpoints/`
- `<workspace>/.box-agent/workflow-owners/`

and move individual reviewed files to an archive directory. Prefer an explicit
per-file move over a recursive delete. Removing these files is not required for
the new runtime because it never reads them.

## Skill ownership

A Skill or plugin owns its domain terminology, stages, validators, scaffolders,
finalizers, and quality rules. It should expose a small, verifiable interface
and derive progress from Session Log context plus durable artifact files.

Skill activation comes from explicit invocation, the current matcher,
host-selected Skill names, or generic capability metadata. A filename does not
grant executable policy, and Skill metadata does not register code inside the
Agent kernel.

Hosts select exact Skills with ACP `_meta.selected_skill_names` (camelCase
`selectedSkillNames` is also accepted). Old presentation configuration no
longer selects an internal provider. Configuration sections
`tool_limits.completion`, `tool_limits.external_skill`, and
`tool_limits.presentation` are removed; use `tool_limits.general.max_tool_calls`
and `max_delegated_tool_calls` for generic turn budgets.

## Adapter ownership

CLI and ACP are adapters. They may select Skills, translate events, and render
host metadata. They must not infer deliverable completeness or retain a second
workflow lifecycle. ACP maps the internal `WAITING_FOR_USER` reason to
`end_turn` while keeping the generic run status visible to the host.

## Verification anchors

- `tests/test_session_log.py`
- `tests/test_agent_session_persistence.py`
- `tests/test_waiting_for_user.py`
- `tests/test_skill_preload.py`
- `tests/test_acp.py`
- format-specific Skill tests such as `tests/test_pptx_controlled_deck.py`
