# Review Change History Index

This directory is the bounded change-history source configured for the
automated `general-reviewer`. It records material decisions that a reviewer
must compare with the current target branch: compatibility, migration,
security, release, rollback, and follow-up constraints. It is not a changelog
of every commit.

## How reviewers use this history

1. Determine the current merge base and inspect the complete target-branch and
   change-branch histories for the affected paths.
2. Treat current source and tests as implementation truth. Use pull-request
   descriptions as rationale and claimed proof, not as proof for a later Head.
3. Check whether a newer change superseded an older contract. In particular,
   transactional `write_file` must be reviewed as PR #34 plus the safety
   follow-up in PR #37, not from PR #34 alone.
4. Separate source, built artifact, installed runtime, restarted host, and fresh
   live-task status. Evidence at one boundary does not prove the next.
5. Re-run applicable proof for the exact reviewed Head; never reuse a result
   from an older SHA.

Useful target-consistency commands include:

```bash
git merge-base <target-ref> <change-ref>
git diff --merge-base <target-ref> <change-ref>
git log --oneline <merge-base>..<target-ref> -- <relevant-paths>
git log --oneline <merge-base>..<change-ref> -- <relevant-paths>
```

## Freshness and known documentation drift

This index was reconciled against `origin/main` at `3610807` on 2026-08-18.
Reviewers must inspect newer target commits rather than assuming this snapshot
is still current.

- [Release state](../RELEASE_STATE.md) is authoritative for the release
  artifacts and hashes it actually records, but its current `Unreleased`
  heading still says `0.8.85`. At this baseline, `pyproject.toml`,
  `box_agent/__init__.py`, and `uv.lock` identify source version `0.8.87`.
  Therefore the release document must not be used to claim the current source
  version, publication, or an installed officev3 runtime.

## Quick routing by affected area

Use this table to find the history most likely to affect a change. It is a
starting point for contributors and reviewers, not a substitute for the
current source, tests, or linked details. When a row names more than one
decision, read those entries together.

| Area | Affected paths or keywords | Current effective decision | Relationship | Details |
| --- | --- | --- | --- | --- |
| File writes | `box_agent/tools/file_tools.py`, `write_file` | Ordered chunks commit atomically, with bounded transactions, replay protection, and whole-body safety checks. | PR #37 hardens PR #34; both remain relevant. | [PR #37](#2026-08-17--transactional-write-safety-follow-up-pr-37), [PR #34](#2026-08-17--unified-transactional-write_file-protocol-pr-34) |
| Tool invocation | `box_agent/tools/base.py`, `schema_validation.py`, `Tool.invoke` | Tool schemas and arguments fail closed before `execute()` is called. | Current at this baseline. | [PR #33](#2026-08-17--validate-tool-arguments-before-execution-pr-33) |
| Context compression | `box_agent/core.py`, tool-call arguments, history summarization | Normal unsummarized history retains exact tool-call arguments; whole-history summarization remains a separate boundary. | Current at this baseline. | [PR #35](#2026-08-17--preserve-tool-call-arguments-in-normal-history-pr-35) |
| MCP deferred loading | `mcp_tool_catalog.py`, `mcp_tool_search.py`, `tool_search` | Ordinary MCP schemas are hidden by default until session-scoped activation; `alwaysLoad` remains eager. | Current; later research hardening may also apply to research paths. | [PR #31](#2026-08-17--deferred-mcp-catalog-and-session-exposure-pr-31), [later hardening](#other-target-branch-changes-after-or-adjacent-to-those-prs) |
| Sub-agent delegation | `sub_agent_tool.py`, `sub_agent_capabilities.py`, `required_tools`, `write_scope`, `files` | The public request is flat; runtime-derived policy limits implicit tools to trusted local readers, keeps process/external/unknown MCP capabilities fail-closed, and scopes path writes. | Supersedes the caller-authored nested constraint contract while retaining its runtime enforcement goals. | [2026-08-19 flattened contract](#2026-08-19--flattened-sub-agent-contract-with-derived-policy) |
| Workflow ownership | `workflow_owner_store.py`, explicit Skills, ACP decisions, presentation recovery | Runtime-selected owner precedes artifact discovery. Unknown Skills use the generic external lifecycle; foreign `deck.json` files cannot activate controlled finalization. | Pending implementation; hardens external-Skill and controlled-presentation recovery. | [2026-08-20 owner hardening](#2026-08-20--workflow-owner-precedence-for-third-party-skills) |
| Model routing and controlled presentations | `box_agent/llm/model_routing.py`, `box_agent/workflows/presentation_*`, controlled PPTX | Automatic child-model routing uses a host allowlist, while presentation-specific state and recovery remain outside the generic kernel. | PR #30 is the main record; later research hardening must be checked where relevant. | [PR #30](#2026-08-14--runtime-routing-and-presentation-reliability-pr-30), [later hardening](#other-target-branch-changes-after-or-adjacent-to-those-prs) |

For research execution, Todo/progress behavior, browser routing, or contributor
branch history, also check
[other target-branch changes](#other-target-branch-changes-after-or-adjacent-to-those-prs).
Release, provider API, and ACP compatibility have their own sources under
[long-lived release and compatibility history](#long-lived-release-and-compatibility-history).

## Pending material changes

### 2026-08-21 — managed web-search/web-extract bootstrap and runtime dispatch

- Change: [PR #73](https://github.com/Raccoon-Office/Box-Agent/pull/73),
  superseding the incomplete host-registration boundary in
  [PR #64](https://github.com/Raccoon-Office/Box-Agent/pull/64); no merge or
  release reference exists yet.
- Runtime contract: `manifest.json` advertises `box-agent-web-extract` as a
  stdio MCP that reuses the packaged `box-agent-acp` entry with
  `--web-extract-mcp`. Source installs may continue to use the existing
  `box-agent-web-extract-mcp` console script.
- Shared bootstrap contract: CLI and ACP reconcile hosted `web_search` and
  runtime-advertised stdio MCP servers into the user-owned `mcp.json` before
  discovery. The first migration enables the legacy disabled defaults; a schema
  marker makes later starts preserve explicit user disable choices. Existing
  unrelated MCP entries are retained and malformed files are never overwritten.
- Compatibility: runtime fields and the top-level config marker are additive.
  The bootstrap changes the old opt-in default for these two managed public-web
  tools to enabled and removes the OfficeV3 manifest-registration dependency.
- Proof anchors: `tests/test_mcp_bootstrap.py`, `tests/test_runtime_entry.py`,
  `tests/test_build_runtime.py`, source MCP suites, the packaged runtime
  manifest, and MCP initialize/list/call probes against CLI and the built binary.
- Runtime boundary: source tests, runtime build, install, host restart, and a
  fresh live task remain separate evidence checkpoints.
- Rollback: revert the implementation, rebuild the prior runtime, and remove
  managed entries or set `disabled: true`; unrelated user MCP config remains
  intact.

### 2026-08-19 — flattened sub-agent contract with derived policy

- Change: implementation on `codex/simplify-subagent-contract`; no merge or
  release reference exists yet.
- Public contract: callers pass flat `task`, `title`, `required_tools`, `skills`,
  `files`, `write_scope`, and `budget` fields. The former nested `execution`,
  `capabilities`, `inputs`, and `constraints` objects are rejected rather than
  selecting a legacy or weaker execution mode.
- Security boundary: omitted tools resolve only to available trusted local
  readers. Explicit tools still pass runtime capability classification;
  `bash` additionally requires a parent-session permission negotiator and each
  delegated command requires one-shot approval. Other process tools, external side
  effects, and unknown MCP tools fail closed. Path-based write tools require a
  non-empty scope enforced by a wrapper before the live parent tool runs.
  Parent `PermissionEngine` checks remain final authority.
- Batch behavior: `files` remains neutral task input. It infers the existing
  completeness-checked local batch path only when `read_file` is the sole
  resolved tool; additional tools keep the general loop. File-count, per-file,
  aggregate, cancellation, and synthesis-timeout boundaries remain intact.
- Compatibility: this is a breaking model-facing schema migration. Existing
  hosts normally render generic tool arguments/results and require no protocol
  change, but packaged prompts/runtimes must be rebuilt and validated before
  desktop adoption.
- Proof anchors: `tests/test_sub_agent_capabilities.py`,
  `tests/test_sub_agent_tool.py`, config/system-prompt contract tests, ACP/Core
  tests, and the repository preflight.
- Rollback: revert the eventual implementation commit and rebuild the previous
  packaged runtime; no data migration is required.

### 2026-08-20 — workflow owner precedence for third-party Skills

- Change: implementation on `codex/fix-workflow-ownership`; no merge or release
  reference exists yet.
- Durable contract: explicit third-party Skills select the generic external
  lifecycle and persist a runtime-owned session record before execution. New
  ACP handles resume that owner before any filesystem heuristic.
- Safety boundary: Skill files and same-named artifacts cannot select executable
  workflow adapters. Controlled legacy recovery accepts only canonical,
  structurally compatible outline/deck files; foreign nested schemas are logged
  and ignored.
- Artifact-root boundary: registered controlled checkpoints may use nested roots,
  but emitted scaffold/validate/patch/finalize commands carry absolute paths so
  `BOX_AGENT_OUTPUT_DIR` cannot redirect them.
- Compatibility: ownerless legacy controlled sessions retain a narrow canonical
  fallback. Existing third-party Skills require no metadata change and continue
  under `external_skill`.
- Proof anchors: workflow-owner/checkpoint store tests, ACP cross-session Skill
  tests, foreign-deck recovery tests, and controlled checkpoint command tests.
- Runtime boundary: source verification does not prove OfficeV3 adoption until
  the runtime is rebuilt, installed, restarted, and replayed with a fresh task.

## Recent material changes on `main`

### 2026-08-17 — transactional write safety follow-up (PR #37)

- Change: [PR #37, `fix(tools): harden transactional write safety`](https://github.com/Raccoon-Office/Box-Agent/pull/37),
  merge `6f2fc61`, implementation `5807233`.
- Durable contract: an identical committed final chunk may be replayed during
  the active turn only when its chunk hash and the current target-file digest
  still match. Conflicting retries and targets changed after commit fail.
- Safety boundary: the complete assembled UTF-8 body is checked before
  `os.replace` for model-history placeholders and PPTX self-check bypasses,
  including patterns split across chunks. Transactions are bounded to 10 MiB
  and 2,048 chunks.
- Compatibility and residual risk: receipts are process-local and cleared with
  the turn; a new chunk zero replaces the old receipt. Restored bounds may
  reject writes that briefly relied on the unbounded PR #34 behavior. Packaged
  OfficeV3 behavior still requires rebuild/install/restart/live-task proof.
- Proof anchors: `tests/test_file_tool_size_guard.py`, `tests/test_tools.py`,
  `tests/test_data_dashboard_fragments.py`, Core/stream retry tests, and PPTX
  guard tests. The PR reported 290 focused tests passing and one unrelated
  full-suite failure.
- Rollback recorded by the PR: revert `5807233`.

### 2026-08-17 — preserve tool-call arguments in normal history (PR #35)

- Change: [PR #35, `fix(context): preserve tool call arguments in history`](https://github.com/Raccoon-Office/Box-Agent/pull/35),
  merge `d5d151f`, implementation `c1987d8`.
- Durable contract: tool-call arguments remain exact while their containing
  turn is present in normal history. They may disappear only when Layer 2
  replaces that whole history region with a conversation summary.
- Unchanged boundaries: tool-result compaction, micro-compaction of old
  tool-role results, whole-history summarization, placeholder detection, and
  provider/ACP prompt markers were not redesigned.
- Compatibility and residual risk: later turns can inspect exact prior writes
  and edits, at the cost of higher context usage for large argument histories.
  Review token-limit and provider behavior when changing this path.
- Design/proof anchors: [Context compression](../CONTEXT_COMPRESSION.md),
  `box_agent/core.py`, `tests/test_core.py`, file/PPTX artifact tests, and
  stream/length retry tests. The PR reported 165 focused tests passing.
- Rollback recorded by the PR: revert `c1987d8`.

### 2026-08-17 — unified transactional `write_file` protocol (PR #34)

- Change: [PR #34, `feat(tools): replace staged writes with transactional write_file chunks`](https://github.com/Raccoon-Office/Box-Agent/pull/34),
  merge `aed8329`, implementation `15367c6`.
- Durable contract: small files use `write_file(path, content)`; large UTF-8
  files use ordered calls on the same path, starting with
  `chunk_index=0, final=false` and committing only on the final chunk. The
  destination remains unchanged while a transaction is incomplete.
- Migration: callers and Skills must not use the former
  `staged_file_write begin/append/commit` protocol. Incomplete transactions are
  discarded at turn cleanup; append/edit operations remain separate tools.
- Historical caution: review of PR #34 identified missing final-chunk replay
  safety and missing whole-body PPTX bypass validation. PR #37 supplies the
  current safety contract and restored size/chunk limits. Do not approve code
  that merely recreates the original PR #34 behavior.
- Proof anchors: `box_agent/tools/file_tools.py`, the file-delivery prompt,
  Data Dashboard/PPTX Skills, `tests/test_tools.py`, size-guard tests, and
  retry/cleanup tests.
- Rollback recorded by the PR: revert `15367c6`; on current main, assess PR #37
  at the same time rather than reverting only one half of the contract.

### 2026-08-17 — validate tool arguments before execution (PR #33)

- Change: [PR #33, `feat(tools): validate arguments before tool execution`](https://github.com/Raccoon-Office/Box-Agent/pull/33),
  merge `9922ef0`, final hardening commit `204bd77`.
- Durable contract: runtime paths call `Tool.invoke(arguments)`, validate the
  tool's JSON Schema and then the argument instance immediately before
  execution, and do not call `execute()` for invalid input.
- Failure contract: invalid arguments return structured
  `INVALID_TOOL_ARGUMENTS`; malformed schemas fail closed as
  `INVALID_TOOL_SCHEMA`. Diagnostics redact schema/argument values that could
  expose secrets. Event-emitting context and the SubAgent
  `INVALID_DELEGATION_SPEC` contract are preserved.
- Compatibility and residual risk: valid calls retain their `execute()`
  behavior; previously tolerated invalid calls now fail earlier. Schema
  quarantine at registration time and validator caching remain out of scope.
- Design/proof anchors: [Development guide](../DEVELOPMENT_GUIDE.md),
  `box_agent/tools/base.py`, `box_agent/tools/schema_validation.py`,
  `tests/test_tool_schema_validation.py`, and Core/MCP/Hook/SubAgent/CLI tests.
- Rollback: revert the PR's invocation/validation commits if an established
  valid schema is proven incompatible; retain value redaction in any repair.

### 2026-08-17 — deferred MCP catalog and session exposure (PR #31)

- Change: [PR #31, `Feat/mcp deferred tool search`](https://github.com/Raccoon-Office/Box-Agent/pull/31),
  merge `f7acf5b` with hardening commits on the feature branch.
- Durable contract: `tools.mcp.deferred_loading_enabled` defaults to `true`.
  Connected ordinary MCP tools live in a process catalog but their schemas are
  hidden until `tool_search` activates selected hits for the current session.
  `alwaysLoad` tools remain eager.
- Safety/consistency boundary: protected-name collisions, duplicate
  model-facing names, catalog loading, hot-reload generations, and a changed
  execution target must fail closed or require a new search. Child agents may
  inherit only currently visible real tools.
- Compatibility and migration: setting `deferred_loading_enabled: false`
  restores legacy eager exposure. Existing servers without `alwaysLoad` become
  deferred; no secret or persistent-data migration is required.
- Proof anchors: `box_agent/tools/mcp_tool_catalog.py`,
  `box_agent/tools/mcp_tool_search.py`, MCP loader/config wiring,
  `tests/test_mcp_tool_search.py`, `tests/test_mcp.py`, ACP/CLI/SubAgent tests.
  The PR explicitly left real provider/MCP and packaged-runtime E2E to the
  release environment.
- Rollback recorded by the PR: disable deferred loading or revert the feature
  and hardening commits together.

### 2026-08-14 — runtime routing and presentation reliability (PR #30)

- Change: [PR #30, `fix(runtime): stabilize model routing and presentation workflows`](https://github.com/Raccoon-Office/Box-Agent/pull/30),
  merge `dda0c5b`, implementation `4f06d75`.
- Durable contract: automatic child-model routing accepts only a host-provided
  allowlist; manual sessions continue to inherit their bound model. ACP stream
  extraction and controlled-presentation research, repair, checkpoint, and
  routing behavior stay behind shared/workflow contracts rather than host-only
  copies.
- Packaging impact: source/package version moved to `0.8.87` and OpenAI was
  pinned to `2.8.0`. The PR did not rebuild, install, or probe an OfficeV3
  packaged runtime, so source success is not release proof.
- Design/proof anchors: [Layered architecture](../ARCHITECTURE.md),
  [controlled PPTX architecture](../PPTX_CONTROLLED_HTML_ARCHITECTURE.md),
  `box_agent/llm/model_routing.py`, presentation workflows, ACP/Core/SubAgent
  tests, build-runtime tests, version surfaces, and `uv.lock`.
- Rollback recorded by the PR: revert `4f06d75`; assess config/version/lock and
  packaged-runtime compatibility together.

### Other target-branch changes after or adjacent to those PRs

- `a9d1671` (2026-08-18) further hardened research execution boundaries across
  Core, Jupyter, SubAgent capabilities, MCP search, research Skills, and
  controlled-presentation workflows. It has no detailed commit-body TPR, so a
  reviewer touching those paths must inspect its source/tests directly rather
  than inheriting PR #30 or #31 proof.
- `3610807` (2026-08-18) requires feature branches to rebase onto the latest
  base `main` before a PR is opened or updated. Merging `main` into the feature
  branch is disallowed; rewriting a published branch requires explicit
  authority and `--force-with-lease`, never `--force`.
- `34ff2d3` (2026-08-17) changed Todo creation/progress behavior in the shared
  loop. Reviews touching Todo or progress events must include both
  `tests/test_todo_tool.py` and applicable Core/host rendering coverage.
- [PR #29](https://github.com/Raccoon-Office/Box-Agent/pull/29) changed browser
  intent routing, the Browser Skill, MCP configuration guidance, environment
  context, and the built-in Skill manifest, but its PR body contains an empty
  TPR template. Do not treat that page as sufficient historical proof; inspect
  merge `94ea22f`, current source, manifest, and focused browser/MCP/env tests.

## Long-lived release and compatibility history

- [Release state](../RELEASE_STATE.md): published versions, artifact hashes,
  shipped behavior, runtime platforms, and known release gaps.
- [Third-party API compatibility](../THIRD_PARTY_API_COMPATIBILITY.md):
  Anthropic/OpenAI protocol selection and malformed SSE ordering diagnostics.
- [ACP integration version table](../INTEGRATION.md#版本与变更): protocol
  introduction points. Each linked protocol document owns its detailed
  compatibility and migration rules.
- [Design index](../design/README.md): active design and ownership routing for
  the subsystem affected by a historical change.

## Keeping this index current

Add or update an entry when a change alters a public or host protocol, stable
kernel/tool contract, security boundary, compatibility default, migration,
release artifact, rollback procedure, cross-repository dependency, or packaged
runtime expectation. Include the change/merge reference, durable effect,
compatibility or migration impact, proof anchors, residual gap, and rollback.

Keep the quick-routing table aligned with the detailed history. Add or revise a
row when a new entry changes the current effective decision or creates a
superseding, hardening, rollback, or read-together relationship. Prefer stable,
searchable path, module, protocol, and configuration names, while keeping the
summary useful to a human reader.

Do not copy every commit, paste generated release notes, or claim that a PR's
tests passed for a later Head. Retire obsolete entries only after their
compatibility and rollback value has ended; otherwise mark what superseded
them.
