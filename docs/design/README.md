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

| Changed area | Read first | Current boundary to verify | Primary proof anchors |
| --- | --- | --- | --- |
| `box_agent/core.py`, `runtime.py`, `agent.py`, `events.py`, `schema.py`, `session_log.py`, `loop_guards.py`, `hooks.py`, `artifacts.py`, `turn_policy.py`, `tools/base.py` | [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | Product adapters and capabilities depend on the stable API/kernel, not the reverse. `runtime.py` is the only production bridge allowed to import `core.py`. Kernel, shared contract, scheduling, cancellation, security, persistence, or event changes require core-owner review. | `tests/test_architecture_boundaries.py` plus focused Core, Session Log, ACP, and public-contract tests |
| `box_agent/kernel/`, `box_agent/plugins/`, `box_agent/composition.py`, Agent-loop extraction or capability replacement | [Kernel/plugin architecture design](../superpowers/specs/2026-09-03-kernel-plugin-architecture-design.md), then [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | ACP and CLI remain adapters over `Agent`; the stable path is `Agent` → `runtime/core` compatibility facade → outer composition and static `PluginHost` → immutable `KernelServices` → `AgentLoopKernel`. Only resolved kernel-owned Ports cross the boundary; plugin discovery is explicit and startup-static. | `tests/test_kernel_compatibility.py`, `tests/test_plugin_host.py`, `tests/test_architecture_boundaries.py`, and CLI/ACP public-path sentinels |
| Agent-loop history, token accounting, summarization, tool-result visibility | [Context compression](../CONTEXT_COMPRESSION.md) / [上下文压缩](../CONTEXT_COMPRESSION_CN.md) | Tool-call arguments remain exact while the containing turn remains in normal history. Tool-result compaction, micro-compaction of old tool-role messages, and whole-history summarization are separate layers. | `tests/test_core.py`, provider/stream retry tests, and token-limit regressions |
| `box_agent/tools/`, tool dispatch, argument schemas, filesystem mutation | [Development and extension guide](../DEVELOPMENT_GUIDE.md) / [开发与扩展指南](../DEVELOPMENT_GUIDE_CN.md) | Runtime callers use `Tool.invoke(arguments)` so JSON Schema validation fails closed before `execute()`. File writes must preserve workspace/permission, atomicity, integrity, placeholder, and document-safety boundaries. | `tests/test_tool_schema_validation.py`, `tests/test_tools.py`, `tests/test_file_tool_size_guard.py`, and permission-specific tests |
| `image_inspection_tool.py`, provider multimodal blocks, ACP structured image attachments | [Development and extension guide](../DEVELOPMENT_GUIDE.md), [ACP integration index](../INTEGRATION.md), and provider adapters in `box_agent/llm/` | `inspect_images` is a read-only Tool. Canonical image blocks stay provider-neutral until adapter conversion; ACP translates current-turn attachments into one validated call through the shared permission/retry seam, bounds only the model-history copy, and includes the full inspection call in turn usage. | `tests/test_image_inspection_tool.py`, `tests/test_multimodal_message_conversion.py`, `tests/test_acp.py`, `tests/test_token_meter.py`, and `tests/test_permission_negotiation.py` |
| MCP loading, managed bootstrap, `mcp_config`, `tool_search`, catalog or hot reload | [Development and extension guide](../DEVELOPMENT_GUIDE.md), [production guide](../PRODUCTION_GUIDE.md), and the checked-in `box_agent/config/config-example.yaml` | CLI and ACP share the user-owned MCP config and reconcile hosted search plus runtime-advertised stdio servers before discovery. First migration enables legacy defaults; later starts preserve explicit user disable choices. Deferred loading remains the default for other tools. Name conflicts, catalog readiness, runtime paths, and catalog generations must fail closed. | `tests/test_mcp_bootstrap.py`, `tests/test_mcp.py`, `tests/test_mcp_tool_search.py`, `tests/test_mcp_config_tool.py`, runtime packaging tests, CLI and ACP wiring tests |
| ACP session metadata, host rendering, protocol events, stdout/stderr | [ACP integration index](../INTEGRATION.md) and the matching protocol document below | ACP owns wire translation; shared behavior remains in shared contracts. Protocol stdout stays clean. Additive fields are preferred and each protocol's compatibility section defines its version boundary. | `tests/test_acp.py` plus the protocol-specific tests or a real host probe |
| Session recovery, explicit third-party Skills, artifact ownership | [Session and Skill ownership](../WORKFLOW_OWNERSHIP.md) | Session Log is the only durable session-state source. Legacy workflow state is downgraded, host-selected Skills are explicit, and filenames never grant executable policy. | `tests/test_session_log.py`, `tests/test_agent_session_persistence.py`, `tests/test_skill_preload.py`, and ACP recovery tests |
| `sub_agent`, child capability resolution, child model routing | [Sub-agent delegation](../SUB_AGENT_DELEGATION.md) / [子 Agent 委派](../SUB_AGENT_DELEGATION_CN.md) | The model-facing request stays flat; runtime-derived policy limits defaults to trusted local readers, keeps process/external/unknown MCP capabilities fail-closed, scopes path writes, and enforces hard budgets before the child LLM starts. | `tests/test_sub_agent_capabilities.py`, `tests/test_sub_agent_tool.py`, Core and ACP tests |
| Memory matching, promotion, persistence, or host cards | [Memory integration](../MEMORY_INTEGRATION.md), [Memory Match](../MEMORY_MATCH_PROTOCOL.md), and [Memory Proposal](../MEMORY_PROPOSAL_PROTOCOL.md) | Keep model-facing retrieval, persistence/promotion, and ACP rendering contracts distinct; preserve configuration gates and structured diagnostics. | Focused memory tests and applicable ACP/configuration tests |
| `box_agent/trace_viewer/`, `box-agent trace-viewer`, session trace diagnostics | [Agent Trace Viewer design](../superpowers/specs/2026-08-20-agent-trace-viewer-design.md) | The viewer is a read-only consumer of `box-agent-session-trace/v1`. Static mode reads browser-selected files; optional service mode stays loopback-only, scans only explicit top-level `.jsonl` files, and does not alter Core, trace-writer, provider, or ACP contracts. | `tests/test_trace_viewer.py`, `tests/test_trace_viewer_server.py`, `tests/js/trace_model.test.js`, package-content checks, and a real browser directory/live-refresh probe |
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
