# Review Design Index

This directory is the bounded design source configured for the automated
`general-reviewer`. It is a routing index, not a duplicate documentation tree
and not evidence that an implementation is correct. Open only the documents
relevant to the changed paths, then verify their claims against the current
merge-base diff, source, tests, and runtime evidence.

## Evidence and ownership rules

Use the following order when sources disagree:

1. Repository policy in `AGENTS.md`, `CONTRIBUTING.md`, the pull-request
   template, and the [maintainer review standard](../REVIEW_GUIDE.md) defines
   contribution and review gates.
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
| `box_agent/core.py`, `runtime.py`, `agent.py`, `events.py`, `schema.py`, `loop_guards.py`, `workflow_policy.py`, `hooks.py`, `artifacts.py`, `turn_policy.py`, `tools/base.py` | [Layered architecture](../ARCHITECTURE.md) / [分层架构](../ARCHITECTURE_CN.md) | Product adapters and capabilities depend on the stable API/kernel, not the reverse. `runtime.py` is the only production bridge allowed to import `core.py`. Kernel, shared contract, scheduling, cancellation, security, or event changes require core-owner review. | `tests/test_architecture_boundaries.py` plus focused Core, ACP, and public-contract tests |
| Agent-loop history, token accounting, summarization, tool-result visibility | [Context compression](../CONTEXT_COMPRESSION.md) / [上下文压缩](../CONTEXT_COMPRESSION_CN.md) | Tool-call arguments remain exact while the containing turn remains in normal history. Tool-result compaction, micro-compaction of old tool-role messages, and whole-history summarization are separate layers. | `tests/test_core.py`, provider/stream retry tests, and token-limit regressions |
| `box_agent/tools/`, tool dispatch, argument schemas, filesystem mutation | [Development and extension guide](../DEVELOPMENT_GUIDE.md) / [开发与扩展指南](../DEVELOPMENT_GUIDE_CN.md) | Runtime callers use `Tool.invoke(arguments)` so JSON Schema validation fails closed before `execute()`. File writes must preserve workspace/permission, atomicity, integrity, placeholder, and document-safety boundaries. | `tests/test_tool_schema_validation.py`, `tests/test_tools.py`, `tests/test_file_tool_size_guard.py`, and permission-specific tests |
| MCP loading, `mcp_config`, `tool_search`, catalog or hot reload | [Development and extension guide](../DEVELOPMENT_GUIDE.md) and the checked-in `box_agent/config/config-example.yaml` | Deferred loading is the default. Ordinary MCP schemas stay hidden until session-scoped `tool_search` activates them; `alwaysLoad` is the explicit eager exception. Name conflicts, catalog readiness, and catalog generations must fail closed. | `tests/test_mcp.py`, `tests/test_mcp_tool_search.py`, `tests/test_mcp_config_tool.py`, CLI and ACP wiring tests |
| ACP session metadata, host rendering, protocol events, stdout/stderr | [ACP integration index](../INTEGRATION.md) and the matching protocol document below | ACP owns wire translation; shared behavior remains in shared contracts. Protocol stdout stays clean. Additive fields are preferred and each protocol's compatibility section defines its version boundary. | `tests/test_acp.py` plus the protocol-specific tests or a real host probe |
| `sub_agent`, child capability resolution, child model routing | [Sub-agent delegation](../SUB_AGENT_DELEGATION.md) / [子 Agent 委派](../SUB_AGENT_DELEGATION_CN.md) | Strict calls validate declared capabilities before a child LLM call, receive only resolved live capabilities, and obey hard step/tool budgets. Legacy compatibility must not silently weaken strict calls. | `tests/test_sub_agent_capabilities.py`, `tests/test_sub_agent_tool.py`, Core and ACP tests |
| Memory matching, promotion, persistence, or host cards | [Memory integration](../MEMORY_INTEGRATION.md), [Memory Match](../MEMORY_MATCH_PROTOCOL.md), and [Memory Proposal](../MEMORY_PROPOSAL_PROTOCOL.md) | Keep model-facing retrieval, persistence/promotion, and ACP rendering contracts distinct; preserve configuration gates and structured diagnostics. | Focused memory tests and applicable ACP/configuration tests |
| Controlled PPTX routing, research, checkpoints, repair, rendering, or document Skills | [Controlled HTML PPTX architecture](../PPTX_CONTROLLED_HTML_ARCHITECTURE.md) / [受控 HTML PPTX 架构](../PPTX_CONTROLLED_HTML_ARCHITECTURE_CN.md), then [development guide](../PPTX_CONTROLLED_HTML_DEVELOPMENT.md) / [开发指南](../PPTX_CONTROLLED_HTML_DEVELOPMENT_CN.md) | PPT-specific recognition, checkpoint state, recovery, tool restrictions, and evidence stay in workflows/Skills rather than the generic kernel. Durable artifacts are the recovery source of truth. | PPT contract tests, manifest checks when a built-in Skill changes, and render/visual/runtime proof when behavior is user-visible |
| Packaging, dependency pins, version surfaces, frozen runtime, officev3 consumption | [Production guide](../PRODUCTION_GUIDE.md) / [生产指南](../PRODUCTION_GUIDE_CN.md) and [Release state](../RELEASE_STATE.md) | Source tests do not prove packaged behavior. Report build, install, probe, host restart, and fresh live-task verification as separate boundaries. | Lock/build checks, artifact manifests/hashes, install/probe logs, and host restart/live-task evidence |
| Review configuration, local CI, prompts, or Review Agent behavior | [Automated review integration](../AUTOMATED_REVIEW.md) and [maintainer review standard](../REVIEW_GUIDE.md) / [维护者 Review 标准](../REVIEW_GUIDE_CN.md) | Box-Agent owns its rules and deterministic `general_review/ci/preflight.sh`; `teamwork_review_agents` owns the generic orchestration and prompts. A successful Preflight result is Head-SHA-specific and does not approve design. | Config validation, shell syntax, exact-Head Preflight, Review Agent output, and human approval |

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

Do not add every implementation note here. Do not copy generic Review Agent
prompts into Box-Agent; configure the review engine to read them from the
`teamwork_review_agents` repository.
