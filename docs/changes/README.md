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

This index was reconciled against `origin/main` at `f6bac85` on 2026-09-02.
Reviewers must inspect newer target commits rather than assuming this snapshot
is still current.

- [Release state](../RELEASE_STATE.md) records the published `v0.9.7`
  artifacts and hashes. A later source-only PR does not update an installed
  officev3 runtime; build, install, restart, probe, and fresh-task evidence
  remain separate checkpoints.

## Quick routing by affected area

Use this table to find the history most likely to affect a change. It is a
starting point for contributors and reviewers, not a substitute for the
current source, tests, or linked details. When a row names more than one
decision, read those entries together.

| Area | Affected paths or keywords | Current effective decision | Relationship | Details |
| --- | --- | --- | --- | --- |
| Tool name aliases | `Tool.aliases`, `build_tool_name_index`, OpenClaw, Hermes | Compatibility names are execution-only, use canonical Box-Agent argument schemas, and fail closed on conflicts. | Built-in mappings complete the generic alias mechanism in `fad2436`. | [2026-08-20 built-in aliases](#2026-08-20--built-in-tool-name-compatibility-aliases) |
| Filesystem path resolution | `SearchFilesTool`, `path_candidates.py`, `PATH_NOT_FOUND`, ACP file-access prompt | Missing paths may return bounded structural candidates, but the model must retry a specific path and the permission engine remains final authority. | Hardens the broad-Home-search block without adding aliases or automatic authorization. | [2026-08-20 path candidates](#2026-08-20--bounded-structural-candidates-for-missing-filesystem-paths) |
| File writes | `box_agent/tools/file_tools.py`, `write_file` | Ordered chunks commit atomically, with bounded transactions, replay protection, and whole-body safety checks. | PR #37 hardens PR #34; both remain relevant. | [PR #37](#2026-08-17--transactional-write-safety-follow-up-pr-37), [PR #34](#2026-08-17--unified-transactional-write_file-protocol-pr-34) |
| Tool invocation | `box_agent/tools/base.py`, `schema_validation.py`, `Tool.invoke` | Tool schemas and arguments fail closed before `execute()` is called. | Current at this baseline. | [PR #33](#2026-08-17--validate-tool-arguments-before-execution-pr-33) |
| Image inspection | `inspect_images`, `vision_review`, canonical image blocks, structured image attachments, transient follow-up | Image inspection is instruction-driven and read-only; `proxy` returns utility-model text, while `native` uses a bounded one-request main-model overlay that never enters durable history. | PR #62 replaced `vision_review`; PR #76 is being rebased as an additive native strategy. | [PR #62](#2026-08-21--instruction-driven-image-inspection-pr-62), [PR #76](#2026-08-23--request-only-native-image-inspection-pr-76) |
| Shell safety inspection | `shell_inspection.py`, `safety.py`, `bash_tool.py`, dangerous commands, DWS | Policy checks inspect shell structure and executable invocations while treating embedded-language bodies as data; bounded parsing fails closed for policy-relevant ambiguity. | Pending PR #63; must be reviewed as a security-boundary change. | [PR #63](#2026-08-21--structure-aware-shell-policy-inspection-pr-63) |
| Context compression | `box_agent/core.py`, tool-call arguments, history summarization | Normal unsummarized history retains exact tool-call arguments; whole-history summarization remains a separate boundary. | Current at this baseline. | [PR #35](#2026-08-17--preserve-tool-call-arguments-in-normal-history-pr-35) |
| Agent kernel and plugin composition | `box_agent/kernel/`, `box_agent/plugins/`, `composition.py`, `KernelServices`, `AgentLoopKernel`, `PluginHost` | ACP/CLI public entry points keep their signatures while the shared loop consumes an immutable Port bundle resolved by an explicit startup-static plugin host. | Pending implementation; reorganizes ownership without adding discovery, hot reload, or a protocol migration. | [2026-09-03 kernel/plugin boundary](#2026-09-03--stable-kernel-and-static-plugin-composition) |
| MCP deferred loading | `mcp_tool_catalog.py`, `mcp_tool_search.py`, `tool_search` | Ordinary MCP schemas are hidden by default until session-scoped activation; `alwaysLoad` remains eager. | Current; later research hardening may also apply to research paths. | [PR #31](#2026-08-17--deferred-mcp-catalog-and-session-exposure-pr-31), [later hardening](#other-target-branch-changes-after-or-adjacent-to-those-prs) |
| Sub-agent delegation | `sub_agent_tool.py`, `sub_agent_capabilities.py`, `required_tools`, `write_scope`, `files` | The public request is flat; runtime-derived policy limits implicit tools to trusted local readers, keeps process/external/unknown MCP capabilities fail-closed, and scopes path writes. | Supersedes the caller-authored nested constraint contract while retaining its runtime enforcement goals. | [2026-08-19 flattened contract](#2026-08-19--flattened-sub-agent-contract-with-derived-policy) |
| Session and workflow ownership | `session_log.py`, explicit Skills, `WAITING_FOR_USER`, legacy workflow files | Session Log is the sole durable Agent-session source. Skills/plugins own domain progress and recovery instructions; legacy checkpoint and owner files are ignored but not deleted. | PR #100 supersedes the proposed runtime owner/checkpoint lifecycle while retaining generic Tool safety boundaries. | [PR #100](#2026-09-02--session-log-only-recovery-pr-100), [earlier owner design](#2026-08-20--workflow-owner-precedence-for-third-party-skills) |
| Agent Trace diagnostics | `box_agent/trace_viewer/`, `box-agent trace-viewer`, `box-agent-session-trace/v1` | The packaged viewer is a read-only v1 trace consumer; static access stays browser-local and the optional directory service is loopback-only, authority-validated, explicit-path, top-level JSONL, and size-bounded. | Pending review; adds diagnostics without changing the trace writer, Core, provider, or ACP contracts. | [2026-08-20 trace viewer](#2026-08-20--local-agent-trace-diagnostics) |
| Model routing and controlled presentations | `box_agent/llm/model_routing.py`, PPTX Skill, Session Log, controlled PPTX | Automatic child-model routing keeps its host allowlist. Presentation progress, validation, and recovery instructions belong to the Skill instead of an internal runtime state machine. | PR #100 supersedes the presentation-lifecycle portion of PR #30; model-routing constraints remain in force. | [PR #100](#2026-09-02--session-log-only-recovery-pr-100), [PR #30](#2026-08-14--runtime-routing-and-presentation-reliability-pr-30) |
| Configurable operational limits | `box_agent/config.py`, `box_agent/core.py` (`provider_stale_seconds`), `image_generation_tool.py` (`max_dimension`), `setup.py` (`generate_image` gating), `openai_client.py` (SenseNova prefixes) | Hardcoded stale/image/thinking limits become config/env with unchanged defaults; generic image endpoints clamp oversized sizes and unconfigured `generate_image` is not registered. | Pending; defaults unchanged except the generic image clamp and `generate_image` gating. | [2026-08-21 configurable limits](#2026-08-21--configurable-runtime-operational-limits) |

For research execution, Todo/progress behavior, browser routing, or contributor
branch history, also check
[other target-branch changes](#other-target-branch-changes-after-or-adjacent-to-those-prs).
Release, provider API, and ACP compatibility have their own sources under
[long-lived release and compatibility history](#long-lived-release-and-compatibility-history).

## Pending material changes

### 2026-09-03 — stable kernel and static plugin composition

- Change: [PR #107](https://github.com/Raccoon-Office/Box-Agent/pull/107),
  with local integration against `main` at `a8d6ad4`; no merge or
  packaged-runtime reference exists yet.
- Durable architecture: ACP and CLI continue through `Agent.run_events()` and
  `Agent.run()`. The shared execution path is `Agent` → `runtime/core`
  compatibility facade → outer composition and `PluginHost` → immutable
  `KernelServices` → `AgentLoopKernel`. Kernel modules own loop invariants and
  Port contracts; plugin modules own descriptors, typed registries, dependency
  resolution, scope caches, activation, rollback, and disposal.
- Compatibility: the public Agent, Core, runtime, ACP, and CLI signatures and
  defaults remain unchanged. Streaming `LLMPort` stays distinct from the
  conditional `SummaryLLMPort`, preserving legacy streaming-only callers until
  summarization is actually required. Disabled optional capabilities produce
  no binding. There is no configuration or persistent-data migration.
- Lifecycle boundary: plugin registration is explicit and startup-static; no
  package scanning or hot reload is introduced. Factories and disposers run
  outside the host state lock, callback re-entry fails fast, runtime-checkable
  Ports are validated before publication, and cancellation leaves interrupted
  cleanup retryable.
- Review fixes: closing a run during an ordinary serial tool waits for that
  tool's cleanup before returning. Cleanup diagnostics preserve the primary
  exception and cancellation on Python 3.10. Any runtime Port validation
  exception disposes the newly created instance before activation fails.
- Target consistency: the extracted kernel retains main's model-guided turn
  continuation, signed web-image URL repair, image-reference normalization,
  intermediate-asset filtering, and managed screenshot persistence.
- Proof anchors: `tests/test_kernel_compatibility.py`,
  `tests/test_plugin_host.py`, `tests/test_tool_engine.py`,
  `tests/test_tool_result_pipeline.py`, `tests/test_architecture_boundaries.py`,
  and the CLI/ACP public-path sentinels. Focused source tests and a source-level
  model connectivity probe do not prove a packaged runtime.
- Runtime boundary: no runtime package was built or installed, no ACP host was
  restarted, and no fresh packaged live task was verified.
- Rollback: revert the eventual implementation commit to restore the monolithic
  Core organization. No configuration or data rollback is required.

### 2026-09-02 — Session Log-only recovery (PR #100)

- Change: [PR #100](https://github.com/Raccoon-Office/Box-Agent/pull/100);
  the branch is rebased onto `origin/main` at `f6bac85`, with no merge or
  packaged-runtime reference yet.
- Durable contract: Session Log is the sole durable Agent-session source for
  messages, tool calls/results, Goal, Plan, Todo, active Skill, compaction, and
  turn boundaries. Domain Skills derive progress from that log plus durable
  artifacts instead of CompletionGate, WorkflowPolicy, checkpoint, or owner
  stores.
- Wait and budget contract: interactive pauses use `WAITING_FOR_USER`; generic
  execution budgets come from run options. CLI and ACP translate the shared
  state without owning a second lifecycle.
- Migration: legacy checkpoint and workflow-owner files are ignored but not
  migrated or deleted. Removed completion/workflow configuration and metadata
  require downstream consumers to use Session Log events and generic run
  options.
- Safety boundary: removing the presentation runtime does not remove generic
  Tool enforcement. PPTX self-check bypass guards, direct-deck rewrite guards,
  and exact image-status command validation remain fail closed with negative
  regression tests.
- Proof anchors: `tests/test_session_log.py`,
  `tests/test_agent_session_persistence.py`,
  `tests/test_waiting_for_user.py`, `tests/test_acp.py`, Tool safety tests,
  the generated Skill manifest, repository preflight, and package build.
- Runtime boundary: source tests and package construction do not prove an
  installed officev3 runtime. Build/install/probe/restart/fresh-task evidence
  remains a downstream release step.
- Rollback: revert PR #100 as one architectural change and rebuild the prior
  runtime. Do not delete legacy state files as part of rollback.

### 2026-08-21 — managed web-search/web-extract bootstrap and runtime dispatch

- Change: [PR #73](https://github.com/Raccoon-Office/Box-Agent/pull/73),
  superseding the incomplete host-registration boundary in
  [PR #64](https://github.com/Raccoon-Office/Box-Agent/pull/64); no merge or
  release reference exists yet.
- Runtime contract: `manifest.json` advertises `box-agent-web-extract` as a
  stdio MCP that reuses the packaged `box-agent-acp` entry with
  `--web-extract-mcp`. Source installs may continue to use the existing
  `box-agent-web-extract-mcp` console script.
- Windows parity: [PR #74](https://github.com/Raccoon-Office/Box-Agent/pull/74)
  makes `scripts/build_win_runtime.py` reuse the generic
  PyInstaller hidden-import/collection helpers and the same manifest builder,
  so the Windows frozen binary includes the dynamically imported MCP server and
  advertises `bin/box-agent-acp.exe --web-extract-mcp`. This closes the blocking
  cross-platform gap reported on PR #64.
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
  fresh live task remain separate evidence checkpoints. Windows manifest and
  packaging arguments are covered deterministically on macOS; a built Windows
  binary MCP probe still requires a Windows runner.
- Rollback: revert the implementation, rebuild the prior runtime, and remove
  managed entries or set `disabled: true`; unrelated user MCP config remains
  intact.

### 2026-08-23 — request-only native image inspection (PR #76)

- Change: [PR #76](https://github.com/Raccoon-Office/Box-Agent/pull/76), rewritten on the current `inspect_images` contract rather than restoring the retired `vision_review` Tool.
- Durable contract: `inspect_images` adds `strategy="native"`; `strategy="proxy"` remains the default. Native mode validates and downsamples images in the Tool, then supplies canonical image blocks to the active main model for one request only.
- Context and privacy boundary: raw Base64 never enters durable messages, persistence, compaction, cache fingerprints, session traces, or provider debug logs. Core reserves a pixel-derived image estimate from the text-history limit and rejects an aggregate overlay above 30% of the safe input budget. The payload is released after a non-empty or normally completed provider response and retained only for an empty provider-stale retry.
- Capability and protocol boundary: only explicitly opted-in Tools may use the transient seam. Known text-only main models fail closed and direct the caller back to `proxy`; Anthropic coalesces an adjacent tool-result user turn with the transient image user turn to preserve provider alternation rules.
- Proof anchors: `tests/test_core.py`, `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`, `tests/test_session_trace.py`, and `tests/test_llm_debug_logging.py`.
- Runtime boundary and rollback: source tests do not prove OfficeV3 adoption. Runtime build/install/probe, host restart, and a fresh live native-image task remain required. Roll back PR #76 to retain the merged proxy-only `inspect_images` behavior.

### 2026-08-21 — instruction-driven image inspection (PR #62)

- Change: [PR #62, `feat(tools): add instruction-driven image inspection`](https://github.com/Raccoon-Office/Box-Agent/pull/62), merged implementation `c5f5526ef83da8ac7e4ebc29b335ec1ae184b17b`.
- Durable contract: the registered read-only Tool is `inspect_images(image_paths, instruction)`. It accepts one to six local PNG/JPEG files and returns the vision model's non-empty response verbatim with deterministic input metadata. Provider-neutral image blocks are translated only in the OpenAI-compatible and Anthropic adapters.
- Host boundary: ACP structured current-turn image attachments invoke `inspect_images` through the shared validated permission/retry seam and include the inspection request in turn token accounting. Full successful output remains available to the host, while the text injected into later model history is bounded with head-and-tail retention and explicitly delimited as untrusted visual evidence. Prompt-scoped grants are cleared once before attachment processing and remain valid for the rest of that prompt. Sub-agents may receive the Tool only as an explicitly selected trusted read-only network capability.
- Breaking migration: the former model-facing `vision_review(image_paths, output_path?, instructions?, mode?)` Tool and its report-writing behavior are retired. Model/tool callers must rename the Tool to `inspect_images`, rename `instructions` to required `instruction`, and stop passing `mode` or `output_path`. `VisionReviewTool` remains only as a Python import alias whose instances expose the new Tool name/schema; it is not a legacy runtime adapter.
- Compatibility and residual risk: configured model capability metadata now takes precedence for the bound model; automatic routing may select a different configured `vision` candidate when the current binding is explicitly text-only. Incorrect provider capability metadata can still prevent registration or cause a provider request to fail.
- Proof anchors: `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`, `tests/test_acp.py`, `tests/test_token_meter.py`, `tests/test_permission_negotiation.py`, `tests/test_sub_agent_capabilities.py`, and `tests/test_tool_schema_validation.py`.
- Runtime boundary and rollback: source tests do not prove OfficeV3 adoption. Runtime build/install/probe, host restart, and a fresh live multimodal task remain required. Roll back PR #62 and rebuild/reinstall the previous runtime if hosts or model prompts still depend on `vision_review`.

### 2026-08-21 — structure-aware shell policy inspection (PR #63)

- Change: [PR #63, `fix(tools): inspect shell structure for policy checks`](https://github.com/Raccoon-Office/Box-Agent/pull/63), implementation `ce0e55e3f3dd5cdb1da1031f2fdd0f5bc40eb322`.
- Durable contract: dangerous-command and DingTalk DWS policy checks classify actual shell executable invocations, direct execution wrappers, parameter/command substitutions, nested shell/eval payloads, `find -exec` actions, groups, and redirections instead of scanning embedded Python or heredoc bodies as top-level shell commands.
- Security boundary: real destructive commands and DWS control-plane calls remain blocked through wrappers, chaining, substitutions, groups, and bounded nested shells. Dynamic executables require dangerous-command approval; the product-specific DWS gate fails closed only when the dynamic command carries DWS evidence, so unrelated constructs such as `$(printf git) status` are not mislabeled as DingTalk violations. Policy-relevant content beyond the inspection depth and malformed candidate regions fail closed.
- Compatibility and residual risk: the inspector is deliberately bounded rather than a complete Bash AST. Unrecognized shell grammar is allowed only when no dangerous-command or DWS candidate appears; deletion backup remains best-effort when a dynamic command hides its final path arguments. Future policy capabilities must add their own ambiguity checks instead of treating every dynamic executable as product-specific evidence.
- Proof anchors: `tests/test_shell_inspection.py`, `tests/test_safety.py`, `tests/test_tools.py`, `tests/test_bash_tool.py`, and `tests/test_permission_negotiation.py`.
- Runtime boundary and rollback: source verification does not prove OfficeV3 adoption until the runtime is rebuilt, installed, restarted, and exercised with a fresh task. Roll back PR #63 and rebuild the prior runtime if valid shell workflows regress.

### 2026-08-20 — local Agent Trace diagnostics

- Change: implementation prepared for
  [PR #61](https://github.com/Raccoon-Office/Box-Agent/pull/61); no merge or
  release reference exists yet.
- Durable boundary: `box_agent/trace_viewer/` is a read-only consumer of
  `box-agent-session-trace/v1`. It reconstructs directory summaries,
  waterfalls, raw events, and the system → user → assistant/tool → final
  conversation chain without changing the writer, Core, provider, or ACP
  contracts.
- Privacy and service boundary: static mode reads only browser-selected files.
  The optional HTTP service rejects non-loopback binds and requests whose
  `Host` or `Origin` does not match its exact loopback authority. It accepts an
  explicit local directory, ignores symlinks, nested files, and non-JSONL
  files, caps each trace at 50 MiB and a directory at 200 MiB, and exposes no
  mutation or remote telemetry path. Trace prompts, tool data, and outputs
  remain sensitive local artifacts.
- Compatibility and packaging: the CLI subcommand and package assets are
  additive; unknown v1 fields and events remain visible. Source tests and a
  successful wheel/sdist build do not prove installation into OfficeV3 or any
  other frozen runtime.
- Proof anchors: `tests/test_trace_viewer.py`,
  `tests/test_trace_viewer_server.py`, `tests/js/trace_model.test.js`, package
  content checks, and a real Chromium directory probe that detected a new trace
  without a page refresh.
- Residual gap and rollback: the service intentionally scans only one directory
  level and requires a user-entered path when the browser picker is unavailable.
  Revert the eventual PR implementation and rebuild any consuming
  package/runtime; no data migration is required.

### 2026-08-20 — built-in tool-name compatibility aliases

- Change: generic execution-only alias support is implemented by `fad2436`;
  the built-in compatibility mappings are implemented by `ab37daf`. No merge
  or release reference exists yet.
- Durable tool contract: `read_file`, `write_file`, `edit_file`, `bash`,
  `generate_image`, `sub_agent`, `request_user_input`, and `get_skill` accept
  the documented equivalent OpenClaw or Hermes names. Underscore names also
  accept their generated hyphenated call form. Wrapped tools preserve the
  aliases of the capability they gate.
- Compatibility boundary: provider-facing schemas continue to advertise only
  canonical Box-Agent names. Aliases select the same tool implementation and
  canonical parameter schema; they do not translate foreign argument formats.
  Empty, repeated, inherited-inapplicable, or conflicting names fail closed
  when the offered-tool index is built. Deferred MCP activation reserves the
  same canonical, alias, and generated call-name namespace so conflicts are
  rejected before a later model step.
- Proof anchors: `tests/test_tool_aliases.py`, the provider pseudo-tool-call
  tests in `tests/test_thinking.py`, and an ACP prompt regression that builds
  the complete offered-tool index. `JsonlQueryTool` explicitly does not inherit
  the distinct `read_file` compatibility name from its implementation base.
- Runtime boundary: source tests do not prove packaged OfficeV3 adoption until
  the runtime is rebuilt, installed, restarted, and exercised in a fresh task.
- Rollback: remove the built-in alias declarations and wrapper forwarding while
  retaining canonical names. No configuration or persistent-data rollback is
  required.

### 2026-08-20 — bounded structural candidates for missing filesystem paths

- Change: implementation `46f8f73`, building on broad-Home-search guard
  `0df1dec`; no merge or release reference exists yet.
- Durable tool contract: when `search_files` cannot find a requested path, it
  may return `raw_output.code=PATH_NOT_FOUND` with up to three existing
  candidates derived from a case-insensitive match against an immediate Home
  child. Relative paths and unresolved absolute paths beneath an active root
  use the same bounded structural rule; fuzzy aliases and recursive Home
  searches remain out of scope.
- Security boundary: Home candidates are discovered only when the current
  permission policy already allows reading Home; restricted sessions receive
  an empty candidate list without Home enumeration or existence probes. The
  tool does not retry, execute, or authorize candidates. The model must select
  and explicitly retry a specific absolute path, after which the existing
  `PermissionEngine` remains final authority. ACP-listed roots are
  pre-authorized roots rather than an exhaustive declaration of every path
  that may be requested.
- Compatibility: successful searches and file-only result enumeration are
  unchanged. Missing-path failures gain structured diagnostic data and a
  retry hint. There is no configuration or data migration.
- Proof anchors: `tests/test_tools.py`, `tests/test_permission_negotiation.py`,
  `tests/test_system_prompt_contract.py`, `tests/test_session_integration.py`,
  and the ACP file-access-context tests. Source probes used both configured
  SenseNova models; packaged runtime rebuild/install/restart remains separate.
- Rollback: remove the candidate helper and missing-path augmentation, and
  restore the prior prompt wording. No persistent data rollback is required.

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

### 2026-08-21 — configurable runtime operational limits

- Change: implementation on `feat/configurable-runtime-limits`; no merge or
  release reference exists yet.
- Configuration surface: previously hardcoded operational values become
  config/env. The stale timeout and built-in SenseNova detection keep their
  historical defaults; the generic image clamp and tool-registration gate are
  intentional default behavior changes described below:
  - `agent.provider_stale_seconds` (default 180) plus
    `BOX_AGENT_PROVIDER_STALE_SECONDS`, threaded through
    `Agent` → `run_agent_loop` → `_stream_with_activity` and into sub-agents.
    Non-positive, non-finite (`inf`/`nan`), or unparseable overrides are ignored
    so the stale guard cannot be silently disabled.
  - `image_generation.max_dimension` (default 1024) plus `BOX_AGENT_IMAGE_MAX_DIM`
    (`0` disables).
  - `BOX_AGENT_SENSENOVA_MODEL_PREFIXES` is an operator-only additive override
    for model families whose thinking and pseudo-tool-call wire contract matches
    SenseNova. Built-ins are always kept; extra prefixes require an explicit
    family boundary suffix to avoid broad accidental matches.
- Compatibility default change: generic/custom OpenAI-compatible image endpoints
  now clamp an explicit oversized `size` to the configured longest edge (default
  1024) to avoid 504s. OpenAI, Doubao/Seedream, and host-aligned `/images/gen`
  endpoints keep their own size rules and are not clamped.
- Registration change: `generate_image` is no longer registered when no image
  endpoint is configured (via config or env); an unconfigured tool could only
  fail on every call.
- Proof anchors: `tests/test_image_generation_tool.py`,
  `tests/test_llm_activity.py`, `tests/test_openai_client_sensenova.py`,
  `tests/test_sub_agent_tool.py`.
- Rollback: revert the implementation commit; unset the new env vars / config
  keys to restore prior values. Only the generic image clamp and the
  `generate_image` gating change any default behavior.
- Runtime boundary: source tests and Python builds do not prove OfficeV3
  adoption. The packaged runtime must be rebuilt, installed, restarted, and
  exercised in a fresh task before claiming desktop delivery.

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
