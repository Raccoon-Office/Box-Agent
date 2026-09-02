# Maintainer Review Guide

Use this guide when reviewing non-trivial PRs. The goal is to make review
decisions evidence-based, consistent, and easy to explain.

## Review Order

1. **Task**: Confirm the PR has one clear behavior or subsystem scope.
2. **Proof**: Check the submitted tests, probes, logs, screenshots, manifests,
   or runtime checks actually prove the changed behavior.
3. **Risk**: Check compatibility, packaging/runtime impact, migration,
   configuration, rollback, and cross-repository follow-up.
4. **Ownership**: Confirm the change lives in the right layer.
5. **Diff Hygiene**: Check for unrelated changes, generated files, local config,
   logs, workspace files, and stale graph/cache artifacts.

If Task, Proof, or Risk is missing, ask for that before doing a deep style pass.

## Ownership Checks

- Shared agent-loop behavior belongs in shared core modules such as
  `box_agent/core.py`, `box_agent/events.py`, and related shared helpers.
- CLI should handle terminal UX, slash commands, rendering, and local prompts.
  It should not fork behavior that ACP also needs.
- ACP should translate shared events to protocol updates and host extension
  methods. stdout must remain protocol-only.
- Provider wire behavior belongs in `box_agent/llm/`.
- Tool semantics belong in `box_agent/tools/` and should return structured
  `ToolResult` data.
- Skill loading belongs to `box_agent/skill_loader.py`, `box_agent/skills/`,
  and `box_agent/skills/_manifest.json`.
- PPT/document generation is skill-driven unless the PR explicitly changes a
  core contract.
- Packaged runtime behavior is not proven by source changes alone.

## Required Proof By Change Type

| Change type | Minimum proof |
| --- | --- |
| Shared core loop, events, cancellation, goals, Session Log | Focused regression test plus relevant core/persistence/ACP tests |
| CLI-only behavior | Focused CLI test or command output, plus no ACP duplication |
| ACP/runtime behavior | ACP test or probe; stdout/stderr boundary considered |
| Tool behavior | Direct tool test covering success and important failure path |
| MCP loading/config | Loader test or documented manual config/probe |
| Memory behavior | Memory-focused test and config-gating check when relevant |
| Built-in skill manifest | `uv run python scripts/generate_skills_manifest.py` and manifest diff |
| Recommended/on-demand skill | Manifest exclusion check plus officev3 recommendation-card impact note |
| Packaged runtime | Runtime build/install/probe status, or explicit source-only limitation |
| Docs-only change | Link/path check and `git diff --check` |

## Blockers

Request changes when any of these apply:

- The PR lacks a clear Task / Proof / Risk explanation.
- Shared behavior is duplicated across CLI and ACP without a strong reason.
- The proof does not cover the changed behavior.
- Runtime-sensitive behavior is claimed as verified without runtime evidence.
- Generated graph/cache files, logs, credentials, `workspace/`, or local config
  are included.
- Built-in skills changed without regenerating `box_agent/skills/_manifest.json`.
- The PR changes user-facing behavior without updating relevant docs.
- The diff includes unrelated refactors or formatting churn.

## Useful Local Commands

```bash
git diff --check
uv run pytest tests/ -q
uv run pytest tests/test_core.py -q
uv run pytest tests/test_acp.py -q
uv run pytest tests/test_memory.py -q
uv run python scripts/generate_skills_manifest.py
uv run box-agent-build-runtime
```

Run the smallest command that proves the claim first, then broaden when the
change touches shared behavior or runtime packaging.
