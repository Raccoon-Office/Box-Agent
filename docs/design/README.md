# Review Design Index

This directory is a routing index for active design sources, not a duplicate
documentation tree and not evidence that an implementation is correct. Open
only the documents relevant to the changed paths, then verify their claims
against the current merge-base diff, source, tests, and runtime evidence.

## Evidence and ownership rules

Use the following order when sources disagree:

1. Repository policy in `AGENTS.md`, `CONTRIBUTING.md`, the pull-request
   template, the [maintainer review guide](../REVIEW_GUIDE.md), and the
   [pull request review standard](../PR_REVIEW_STANDARD.md) define contribution
   and review gates.
2. Current source, focused regression tests, and the merge-base diff define
   implemented behavior.
3. The active design and protocol documents below define intended boundaries
   and compatibility contracts.
4. Release notes, implementation plans, pull-request descriptions, and old
   validation reports explain history; they do not override current source.

`.understand-anything/` may be used for navigation only. Its generated graph or
cached summary is not design authority and must not be edited by hand.

## Design routing by changed area

For an overall view of Tool and Skill ownership and execution, start with
[Tool / Skill organization and workflow diagrams](tool-skill-workflows.md).

| Changed area | Read first | Current boundary to verify | Primary proof anchors |
| --- | --- | --- | --- |
| Session cwd, task directories, legacy artifact roots | [Artifact protocol](../ARTIFACT_PROTOCOL.md), [host integration](../INTEGRATION.md), and [migration history](../changes/README.md#2026-09-09--cwd-owned-task-organization-and-legacy-output-root-deprecation) | Session cwd is stable across ACP, CLI, and shared tools. Legacy output selectors are ignored; task directories organize files without becoming runtime state. Tool result adapters discover cwd-rooted artifacts; Skills own their file layout. | ACP/session tests, Tool Engine artifact tests, bounded scan tests, Skill command probes, and packaged-host migration checks |
| `box_agent/core.py`, `runtime.py`, `agent.py`, `events.py`, `schema.py`, `session_log.py`, `loop_guards.py`, `hooks.py`, `artifacts.py`, `turn_policy.py`, `tools/base.py` | [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | Product adapters and capabilities depend on the stable API/kernel, not the reverse. `runtime.py` is the only production bridge allowed to import `core.py`. Kernel, shared contract, scheduling, cancellation, security, persistence, or event changes require core-owner review. | `tests/test_architecture_boundaries.py` plus focused Core, Session Log, ACP, and public-contract tests |
| `box_agent/kernel/`, `box_agent/plugins/`, `box_agent/composition.py`, Agent-loop extraction or capability replacement | [Kernel/plugin architecture design](../superpowers/specs/2026-09-03-kernel-plugin-architecture-design.md), then [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | ACP and CLI remain adapters over `Agent`; the stable path is `Agent` → `runtime/core` compatibility facade → outer composition and static `PluginHost` → immutable `KernelServices` → `AgentLoopKernel`. Only resolved kernel-owned Ports cross the boundary; plugin discovery is explicit and startup-static. | `tests/test_kernel_compatibility.py`, `tests/test_plugin_host.py`, `tests/test_architecture_boundaries.py`, and CLI/ACP public-path sentinels |
| `box_agent/hook_bus.py`, `kernel/hook_types.py`, `plugins/hooks.py`, HookDispatchPort, HookProviderPort | [HookBus](../HOOKBUS.md), then the kernel/plugin and Tool Engine designs in this table | Composition registers provider contributions with their plugin identity and freezes one bus per Run. HookBus owns matching, ordered dispatch, decision reduction and draining; Tool Engine revalidates final arguments and publishes the effective visible result. Context assembly and session configuration stay with their existing owners. Legacy callbacks retain their public contract. | `tests/test_hook_bus.py`, `tests/test_hook_plugins.py`, `tests/test_plugin_host.py`, `tests/test_kernel_compatibility.py`, and Tool Engine permission/result regressions |
| Agent-loop history, `context_input.py`, token accounting, summarization, tool-result visibility | [Skill, Tool 与 Context 职责](skill-engine.md), then [Context compression](../CONTEXT_COMPRESSION.md) / [上下文压缩](../CONTEXT_COMPRESSION_CN.md) | Context owns request copies, real text coverage and input budgets. It receives the exact complete `PreparedTools` from Tool Engine and preserves its definitions, exposure and callable target bindings; each Tool execution still checks permissions. Kernel owns execution order and durable history; existing compression helpers retain their roles. | `tests/test_context_input.py`, `test_skill_context_regressions.py`, `test_core.py`, provider/stream retry tests, and token-limit regressions |
| `box_agent/tools/`, ToolEnginePort, local/MCP exposure, tool dispatch and results | [Tool Engine design](tool-refactor/design.md), [implementation guide](tool-refactor/runtime.md), then [development guide](../DEVELOPMENT_GUIDE.md) | Model calls use one run-scoped Engine and request-bound definitions/targets; standalone callers use `runtime.invoke_tool_with_permissions`. Both reach one validated `Tool.invoke` seam. Kernel owns final messages and durable call/result records; current Skill state follows the separate [Skill Engine design](skill-engine.md). Local discovery cannot expand capabilities or permissions. | `tests/test_tool_engine_compatibility.py`, `test_tool_engine_call_records.py`, `test_tool_engine_permission_stream.py`, `test_local_tool_search.py`, `test_schedule_tool_contract.py`, schema/filesystem/ACP/session tests |
| Skill discovery, `get_skill` / `list_skills`, explicit selection, Skill context budgets and recovery | [Skill Engine 实现说明](skill-engine.md), then [Session and Skill ownership](../WORKFLOW_OWNERSHIP.md) | Skill owns valid immutable source snapshots, selection and read facts. SkillLoader still renders catalog metadata for the CLI/ACP system template; Context owns body coverage, ordinary reference projection and paging budgets. Oversized selections require a real offered reader allowed for all selected Skills; direct callers block on missing-source recovery before provider/log writes, while ACP preserves partial recovery through the shared Skill runtime. Composition binds run-scoped Context to the final plugin Skill/Store. Native hash snapshots and bounded inline references use the existing `request/context` event without requiring new Store methods. Real reads remain tool messages; child scope and Tool permissions stay enforced. | `tests/test_skill_catalog_tool.py`, `test_skill_usage_guidance.py`, `test_skill_entry_boundaries.py`, `test_context_input.py`, `test_skill_context.py`, `test_skill_context_regressions.py`, `test_skill_reference_persistence.py`, `test_agent_session_persistence.py`, `test_skill_plugin_composition.py`, `test_acp.py`, and child delegation tests |
| `image_inspection_tool.py`, provider multimodal blocks, ACP structured image attachments | [Development and extension guide](../DEVELOPMENT_GUIDE.md), [ACP integration index](../INTEGRATION.md), and provider adapters in `box_agent/llm/` | `inspect_images` is a read-only Tool. Canonical image blocks stay provider-neutral until adapter conversion; ACP translates current-turn attachments into one validated call through the shared permission/retry seam, bounds only the model-history copy, and includes the full inspection call in turn usage. | `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`, `tests/test_acp.py`, `tests/test_token_meter.py`, and `tests/test_permission_negotiation.py` |
| MCP loading, managed bootstrap, `mcp_config`, `tool_search`, catalog or hot reload | [Development and extension guide](../DEVELOPMENT_GUIDE.md), [production guide](../PRODUCTION_GUIDE.md), and the checked-in `box_agent/config/config-example.yaml` | CLI and ACP share the user-owned MCP config and reconcile hosted search plus runtime-advertised stdio servers before discovery. First migration enables legacy defaults; later starts preserve explicit user disable choices. Deferred loading remains the default for other tools. Name conflicts, catalog readiness, runtime paths, and catalog generations must fail closed. | `tests/test_mcp_bootstrap.py`, `tests/test_mcp.py`, `tests/test_mcp_tool_search.py`, `tests/test_mcp_config_tool.py`, runtime packaging tests, CLI and ACP wiring tests |
| ACP session metadata, host rendering, protocol events, stdout/stderr | [ACP integration index](../INTEGRATION.md) and the matching protocol document below | ACP owns wire translation; shared behavior remains in shared contracts. Protocol stdout stays clean. Additive fields are preferred and each protocol's compatibility section defines its version boundary. | `tests/test_acp.py` plus the protocol-specific tests or a real host probe |
| `agent_session.py`, `session_context.py`, `session_assembly.py`, `session_prompts.py`, `plugins/runtime.py`, `plugins/builtins.py`, ACP/CLI session lifecycle | [Agent Session](../AGENT_SESSION.md), then [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | Managed `AgentSession.open` prepares configured capabilities once through a reusable PluginRuntime, binds fresh run services and closes owned resources. Adapters retain protocol/UI and borrowed capabilities; Config identity, legacy APIs and Session Log persistence remain compatible. | `tests/test_session_plugins.py`, `tests/test_session_adapter_assembly.py`, `tests/test_plugin_runtime_lifecycle.py`, `tests/test_plugin_host_context.py`, `tests/test_managed_kernel_services.py`, and ACP/CLI regressions |
| `box_agent/api/`, `agent_service.py`, `agent_run.py`, `run_control.py`, `sdk.py`, CLI/ACP run adapters | [Agent Session](../AGENT_SESSION.md), [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md), and [ACP integration index](../INTEGRATION.md) | `RunRequest`, `ControlCommand`, `EventEnvelope`, and `RunResult` form the host-neutral run contract. `AgentService` owns run lifecycle; CLI and ACP translate their host protocols, while SDK consumers use the same events and controls. Pause, resume, cancellation, injection, and permission responses remain run-scoped and cooperative. | `tests/test_run_api.py`, `test_run_control.py`, `test_sdk.py`, plus ACP/CLI public-path regressions |
| Session recovery, explicit third-party Skills, artifact ownership | [Session and Skill ownership](../WORKFLOW_OWNERSHIP.md) | Session Log is the only durable session-state source. Legacy workflow state is downgraded, host-selected Skills are explicit, and filenames never grant executable policy. | `tests/test_session_log.py`, `tests/test_agent_session_persistence.py`, `tests/test_skill_preload.py`, and ACP recovery tests |
| `sub_agent`, child capability resolution, child model routing | [Sub-agent delegation](../SUB_AGENT_DELEGATION.md) / [子 Agent 委派](../SUB_AGENT_DELEGATION_CN.md) | The model-facing request stays flat; runtime-derived policy limits defaults to trusted local readers, keeps process/external/unknown MCP capabilities fail-closed, scopes path writes, and enforces hard budgets before the child LLM starts. | `tests/test_sub_agent_capabilities.py`, `tests/test_sub_agent_tool.py`, Core and ACP tests |
| Memory matching, promotion, persistence, or host cards | [Memory integration](../MEMORY_INTEGRATION.md), [Memory Match](../MEMORY_MATCH_PROTOCOL.md), and [Memory Proposal](../MEMORY_PROPOSAL_PROTOCOL.md) | Keep model-facing retrieval, persistence/promotion, and ACP rendering contracts distinct; preserve configuration gates and structured diagnostics. | Focused memory tests and applicable ACP/configuration tests |
| `box_agent/session_trace.py`, CLI native tracing, `SessionTraceWriter`, `traced_session_turn` | [Agent Trace diagnostics](../../README.md#agent-trace-diagnostics) / [Agent Trace 诊断](../../README_CN.md#agent-trace-诊断), then [Session and Skill ownership](../WORKFLOW_OWNERSHIP.md) | Hosts bind diagnostic session/turn scopes; shared trace utilities record lifecycle events and existing LLM/tool instrumentation supplies call details. CLI-owned run tasks settle before turn closure, and context restoration isolates later turns. Traces are best-effort diagnostics, not recovery state: Session Log remains authoritative. ACP wire behavior, kernel policy and model requests stay unchanged. | `tests/test_cli_session_trace.py`, `tests/test_session_trace.py`, `tests/test_architecture_boundaries.py`, and native CLI/viewer probes |
| `box_agent/trace_viewer/`, `box-agent trace-viewer`, session trace diagnostics | [Agent Trace Viewer design](../superpowers/specs/2026-08-20-agent-trace-viewer-design.md) | The viewer is a read-only consumer of `box-agent-session-trace/v1`. Static mode reads browser-selected files; optional service mode stays loopback-only and reads either one flat top-level ledger or one bounded `source directory / trace` comparison level. Input-first matching, compatible filename fallback, and N-way deltas remain consumer-only and do not alter Core, trace-writer, provider, or ACP contracts. | `tests/test_trace_viewer.py`, `tests/test_trace_viewer_server.py`, `tests/js/trace_model.test.js`, package-content checks, and a real browser directory/live-refresh probe |
| Controlled PPTX research, repair, rendering, or document Skills | [Controlled HTML PPTX architecture](../PPTX_CONTROLLED_HTML_ARCHITECTURE.md) / [受控 HTML PPTX 架构](../PPTX_CONTROLLED_HTML_ARCHITECTURE_CN.md), then [development guide](../PPTX_CONTROLLED_HTML_DEVELOPMENT.md) / [开发指南](../PPTX_CONTROLLED_HTML_DEVELOPMENT_CN.md) | PPT-specific stages, validators, recovery instructions, and evidence live in the Skill. Session Log plus durable artifacts provide recovery context; the generic runtime does not own the domain lifecycle. | PPT contract tests, manifest checks when a built-in Skill changes, and render/visual/runtime proof when behavior is user-visible |
| Packaging, dependency pins, version surfaces, frozen runtime, officev3 consumption | [Production guide](../PRODUCTION_GUIDE.md) / [生产指南](../PRODUCTION_GUIDE_CN.md) and [Release state](../RELEASE_STATE.md) | Source tests do not prove packaged behavior. Report build, install, probe, host restart, and fresh live-task verification as separate boundaries. | Lock/build checks, artifact manifests/hashes, install/probe logs, and host restart/live-task evidence |
| Pull request review policy or repository CI command | [Pull request review standard](../PR_REVIEW_STANDARD.md) / [PR 审查规范](../PR_REVIEW_STANDARD_CN.md) and [`general_review/ci/preflight.sh`](../../general_review/ci/preflight.sh) | Box-Agent owns its review policy and deterministic CI command; deployment-specific orchestration and prompts stay outside this repository. | Shell syntax, exact-Head CI result, review output, and human approval |

## ACP and host protocol contracts

Use [INTEGRATION.md](../INTEGRATION.md) as the protocol directory and open the
specific contract touched by the diff:

- [Action Hint](../ACTION_HINT_PROTOCOL.md)
- [Artifact](../ARTIFACT_PROTOCOL.md)
- [Environment Context](../ENV_CONTEXT_PROTOCOL.md)
- [Filesystem Policy](../FILESYSTEM_POLICY_PROTOCOL.md)
- [Host Progress Events](../integration/host-progress-events.md)
- [Memory Match](../MEMORY_MATCH_PROTOCOL.md)
- [Memory Proposal](../MEMORY_PROPOSAL_PROTOCOL.md)
- [User Decision](../USER_DECISION_PROTOCOL.md) / [用户决策](../USER_DECISION_PROTOCOL_CN.md)

Do not infer that a CLI behavior is an ACP wire contract. Verify which entry
point owns translation and whether the underlying behavior is actually shared.

## Recent cross-cutting decisions

The following current-main decisions affect reviews across otherwise separate
areas. Their rationale, compatibility impact, proof, and rollback references
are recorded in the [change-history index](../changes/README.md):

- all runtime tool execution paths converge on validated `Tool.invoke()`;
- `write_file` is the atomic one-shot and ordered-chunk write protocol, including
  final-chunk replay safety and whole-body safety validation;
- normal unsummarized history preserves exact tool-call arguments;
- MCP schemas are exposed lazily per session by default;
- Skill owns sources and session read facts; Context assembles ordinary
  references and budgets against the exact tools prepared by Tool Engine;
  composition connects the final plugin instances, while Kernel owns history;
- host-allowlisted child-model routing and workflow-specific presentation state
  remain outside the generic kernel.

## Keeping this index current

Update this index when a material subsystem, ownership boundary, public
protocol, compatibility rule, or source-of-truth document is added, renamed, or
retired. Keep English/Chinese document pairs aligned when both exist. Record the
corresponding migration, compatibility impact, known gap, and rollback reference
in `../changes/README.md` or in a focused history document linked from it.

Do not add every implementation note here or copy deployment-specific Review
Agent prompts and configuration into Box-Agent.
