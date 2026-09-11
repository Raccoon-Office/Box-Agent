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
| Session/output directories | `params.cwd`, `artifact_mode`, `artifact_root_dir`, `BOX_AGENT_OUTPUT_DIR`, artifact scan, PPT, research | Session cwd is stable; legacy path selectors warn and are ignored; General-mode task folders are model-created file organization only. | Supersedes fixed per-session output-root behavior. | [2026-09-09 cwd-owned task organization](#2026-09-09--cwd-owned-task-organization-and-legacy-output-root-deprecation) |
| Tool name aliases | `Tool.aliases`, `build_tool_name_index`, OpenClaw, Hermes | Compatibility names are execution-only, use canonical Box-Agent argument schemas, and fail closed on conflicts. | Built-in mappings complete the generic alias mechanism in `fad2436`. | [2026-08-20 built-in aliases](#2026-08-20--built-in-tool-name-compatibility-aliases) |
| Filesystem path resolution | `SearchFilesTool`, `path_candidates.py`, `PATH_NOT_FOUND`, ACP file-access prompt | Missing paths may return bounded structural candidates, but the model must retry a specific path and the permission engine remains final authority. | Hardens the broad-Home-search block without adding aliases or automatic authorization. | [2026-08-20 path candidates](#2026-08-20--bounded-structural-candidates-for-missing-filesystem-paths) |
| File writes | `box_agent/tools/file_tools.py`, `write_file` | Ordered chunks commit atomically, with bounded transactions, replay protection, and whole-body safety checks. | PR #37 hardens PR #34; both remain relevant. | [PR #37](#2026-08-17--transactional-write-safety-follow-up-pr-37), [PR #34](#2026-08-17--unified-transactional-write_file-protocol-pr-34) |
| Tool Engine and local discovery | `tools/engine/`, `ToolEnginePort`, `local_tool_exposure.py`, `tool_messages.py`, `create_scheduled_task` | One execution/result chain, kernel-owned final replies and stable local/MCP discovery; the schedule tool stays unchanged. | Reorganizes the kernel tool helpers and extends PR #31; subsequent Skill behavior is recorded separately below. | [2026-09-09 Tool Engine](#2026-09-09--tool-engine-ownership-and-local-discovery) |
| Skill discovery and ordinary references | `skill_runtime.py`, `skill_context.py`, `context_input.py`, `get_skill`, `list_skills`, `skill/change`, `skillReferences` | Skill 管来源快照与会话事实；Context 管普通资料、真实覆盖和预算，接收 Tool 已准备的完整工具集合；Kernel 保留真实历史。 | 替代原固定全文预加载及 system 激活；经现有 factory 装配 Context，借用最终 Skill/Store，保持旧日志、Tool 权限与调度工具合同。Tool 默认装配仍为独立后续。 | [2026-09-09 Skill Engine](#2026-09-09--skill-engine-ordinary-references-and-session-facts) |
| Tool invocation | `box_agent/tools/base.py`, `schema_validation.py`, `Tool.invoke` | Tool schemas and arguments fail closed before `execute()` is called. | Current at this baseline. | [PR #33](#2026-08-17--validate-tool-arguments-before-execution-pr-33) |
| Image inspection | `inspect_images`, `vision_review`, canonical image blocks, structured image attachments, transient follow-up | Image inspection is instruction-driven and read-only; `proxy` returns utility-model text, while `native` uses a bounded one-request main-model overlay that never enters durable history. | PR #62 replaced `vision_review`; PR #76 is being rebased as an additive native strategy. | [PR #62](#2026-08-21--instruction-driven-image-inspection-pr-62), [PR #76](#2026-08-23--request-only-native-image-inspection-pr-76) |
| Shell safety inspection | `shell_inspection.py`, `safety.py`, `bash_tool.py`, dangerous commands, DWS | Policy checks inspect shell structure and executable invocations while treating embedded-language bodies as data; bounded parsing fails closed for policy-relevant ambiguity. | Pending PR #63; must be reviewed as a security-boundary change. | [PR #63](#2026-08-21--structure-aware-shell-policy-inspection-pr-63) |
| Context compression | `box_agent/core.py`, tool-call arguments, history summarization | Normal unsummarized history retains exact tool-call arguments; whole-history summarization remains a separate boundary. | Current at this baseline. | [PR #35](#2026-08-17--preserve-tool-call-arguments-in-normal-history-pr-35) |
| Agent kernel and plugin composition | `box_agent/kernel/`, `box_agent/plugins/`, `composition.py`, `KernelServices`, `AgentLoopKernel`, `PluginHost` | ACP/CLI public entry points keep their signatures while the shared loop consumes an immutable Port bundle resolved by an explicit startup-static plugin host. | Pending implementation; reorganizes ownership without adding discovery, hot reload, or a protocol migration. | [2026-09-03 kernel/plugin boundary](#2026-09-03--stable-kernel-and-static-plugin-composition) |
| Hook dispatch and static providers | `hook_bus.py`, `kernel/hook_types.py`, `plugins/hooks.py`, HookDispatchPort, HookProviderPort | One frozen bus per Run dispatches lifecycle observers, tool-argument decisions and visible-result decisions; cleanup finishes before plugin disposal. | Extends static plugin composition and the Tool Engine execution seam while preserving legacy hooks, context ownership and Session Log contracts. | [2026-09-10 HookBus](#2026-09-10--run-scoped-hookbus-and-static-hook-providers), [kernel/plugin boundary](#2026-09-03--stable-kernel-and-static-plugin-composition), [Tool Engine](#2026-09-09--tool-engine-ownership-and-local-discovery) |
| MCP deferred loading | `mcp_tool_catalog.py`, `mcp_tool_search.py`, `tool_search` | Ordinary MCP schemas are hidden by default until session-scoped activation; `alwaysLoad` remains eager. | Current; later research hardening may also apply to research paths. | [PR #31](#2026-08-17--deferred-mcp-catalog-and-session-exposure-pr-31), [later hardening](#other-target-branch-changes-after-or-adjacent-to-those-prs) |
| Sub-agent delegation | `sub_agent_tool.py`, `sub_agent_capabilities.py`, `required_tools`, `write_scope`, `files` | The public request is flat; runtime-derived policy limits implicit tools to trusted local readers, keeps process/external/unknown MCP capabilities fail-closed, and scopes path writes. | Supersedes the caller-authored nested constraint contract while retaining its runtime enforcement goals. | [2026-08-19 flattened contract](#2026-08-19--flattened-sub-agent-contract-with-derived-policy) |
| Shared live session ownership | `agent_session.py`, `session_assembly.py`, `session_context.py`, `plugins/runtime.py`, `cli.py`, `acp/` | Managed sessions prepare capabilities once, reuse a PluginRuntime and bind fresh run services. Owned resources close after active runs; borrowed host resources retain their owner. | Extends PR #114 with scoped preparation and cleanup; legacy creation, Config identity and Session Log persistence remain compatible. | [2026-09-10 managed lifecycle](#2026-09-10--managed-session-assembly-and-plugin-lifecycle), [2026-09-09 AgentSession](#2026-09-09--shared-agent-session-state-and-configuration) |
| Session and workflow ownership | `session_log.py`, explicit Skills, `WAITING_FOR_USER`, legacy workflow files | Session Log is the sole durable Agent-session source. Skills/plugins own domain progress and recovery instructions; legacy checkpoint and owner files are ignored but not deleted. | PR #100 supersedes the proposed runtime owner/checkpoint lifecycle while retaining generic Tool safety boundaries. | [PR #100](#2026-09-02--session-log-only-recovery-pr-100), [earlier owner design](#2026-08-20--workflow-owner-precedence-for-third-party-skills) |
| Native CLI session traces | `box_agent/cli.py`, `box_agent/session_trace.py`, `SessionTraceWriter`, `BOX_AGENT_SESSION_TRACE_ENABLED` | CLI creates best-effort v1 traces by default, with one file per invocation and one scope per user turn; Session Log remains the only durable recovery source. Existing opt-out, redaction and retention apply. | Adds native CLI production of traces; read together with the existing viewer and Session Log contracts, not as a replacement for them. | [2026-09-08 native CLI tracing](#2026-09-08--native-cli-session-tracing) |
| Agent Trace diagnostics | `box_agent/trace_viewer/`, `box-agent trace-viewer`, `box-agent-session-trace/v1` | The packaged viewer is a read-only v1 trace consumer; static access stays browser-local and the optional directory service is loopback-only, authority-validated, explicit-path, and size-bounded. Flat ledgers stay top-level; comparison roots add exactly one `source / trace` level with input-first, filename-assisted grouping. | The 2026-09-04 comparison extension preserves the original writer, Core, provider, ACP, and flat-ledger contracts. | [2026-09-04 multi-source comparison](#2026-09-04--input-matched-multi-source-agent-trace-comparison), [2026-08-20 trace viewer](#2026-08-20--local-agent-trace-diagnostics) |
| Model routing and controlled presentations | `box_agent/llm/model_routing.py`, PPTX Skill, Session Log, controlled PPTX | Automatic child-model routing keeps its host allowlist. Presentation progress, validation, and recovery instructions belong to the Skill instead of an internal runtime state machine. | PR #100 supersedes the presentation-lifecycle portion of PR #30; model-routing constraints remain in force. | [PR #100](#2026-09-02--session-log-only-recovery-pr-100), [PR #30](#2026-08-14--runtime-routing-and-presentation-reliability-pr-30) |
| Configurable operational limits | `box_agent/config.py`, `box_agent/core.py` (`provider_stale_seconds`), `image_generation_tool.py` (`max_dimension`), `setup.py` (`generate_image` gating), `openai_client.py` (SenseNova prefixes) | Hardcoded stale/image/thinking limits become config/env with unchanged defaults; generic image endpoints clamp oversized sizes and unconfigured `generate_image` is not registered. | Pending; defaults unchanged except the generic image clamp and `generate_image` gating. | [2026-08-21 configurable limits](#2026-08-21--configurable-runtime-operational-limits) |

For research execution, Todo/progress behavior, browser routing, or contributor
branch history, also check
[other target-branch changes](#other-target-branch-changes-after-or-adjacent-to-those-prs).
Release, provider API, and ACP compatibility have their own sources under
[long-lived release and compatibility history](#long-lived-release-and-compatibility-history).

## Pending material changes

### 2026-09-10 — managed session assembly and plugin lifecycle

- **Change:** `refactor(session): manage capability assembly through scoped plugins`
  on `codex/kernel-plugin-refactor`. This extends [PR #114](https://github.com/Raccoon-Office/Box-Agent/pull/114),
  merged as `7c85e82`; see [Agent Session](../AGENT_SESSION.md).
- **Durable effect:** `AgentSession.open` prepares models, memory, tools/Skills/MCP,
  prompts and hooks through shared session initializers. ACP shares an application
  PluginRuntime; CLI owns a private runtime by default. Each run receives fresh
  context and services. Closing a session settles the active stream first;
  initialization failure rolls back owned resources, and interrupted cleanup
  remains retryable. Borrowed capabilities retain their existing owner.
- **Compatibility:** synchronous `create`, direct Agent APIs and no-argument
  plugin factories remain supported. `AgentRunOptions` and the loop bridge add
  an optional internal `kernel_services` binding. Context factories receive
  declared dependencies and scope context; a longer-lived plugin cannot depend
  on a shorter-lived one. No config key, dynamic discovery, hot reload or Session
  Log migration is added. Existing prompt ordering and lazy discovery remain.
- **Target consistency:** read together with the Tool Engine change below and
  upstream reasoning/utility-session fixes. Managed sessions retain run-scoped
  tool execution, reasoning policy propagation and utility tool suppression.
  Shared preparation preserves the stable cwd contract from PR #118, including
  ordinary task subdirectories and ignored legacy artifact roots. Session Log
  degradation hints and unavailable-Skill recovery remain effective after their
  assembly moves out of the adapters.
  Interrupted rollback of a failed Run activation retains its session owner,
  including runtime Port validation failures. Session close retries those Run
  resources before releasing Session dependencies and prevents a new activation
  from bypassing unfinished cleanup.
- **Proof anchors:** `tests/test_session_plugins.py`, `test_session_adapter_assembly.py`,
  `test_plugin_runtime_lifecycle.py`, `test_plugin_host_context.py`,
  `test_managed_kernel_services.py`, and existing ACP/CLI/kernel/provider suites.
  Exact execution and generated-graph results belong in the submitting PR.
- **Runtime and rollback:** source and Python-package checks do not establish
  installed host behavior. Packaged consumers require rebuild/install/probe,
  host restart and a fresh live task. Revert session assembly, lifecycle and
  adapter wiring together; existing Session Logs require no migration.


### 2026-09-10 — run-scoped HookBus and static hook providers

- **Change:** [PR #121](https://github.com/Raccoon-Office/Box-Agent/pull/121)
  integrates HookBus with the managed Session lifecycle. [HookBus](../HOOKBUS.md) describes the public
  Python integration, handler decisions and lifecycle.
- **Ownership:** composition creates one HookBus per Run, registers legacy
  callbacks and `HookProviderPort` contributions with their owner identity,
  then freezes the registry. Handlers run in priority and registration order.
  Agent and AgentSession carry explicit plugin descriptors; factories bind
  resolved host configuration. The bus does not assemble model context or
  introduce durable session state.
- **Tool arguments and results:** a before-tool decision may replace the
  complete argument object or deny execution. Tool Engine revalidates final
  arguments, paths and execution constraints before permission negotiation;
  permission retries do not run the before-hook again. Result handlers may
  replace or suppress visible text in model history and host events without
  changing the actual success flag, raw output, separately persisted content,
  or artifact references. Calls rejected before invocation skip new result
  handlers and report `executed=false` through the completion observer.
- **Compatibility:** existing `hooks` arguments, hook classes, loaders and
  callback argument semantics remain available. Legacy callbacks retain their
  ordinary-error and timeout behavior. New observers warn and continue on
  ordinary failure; invalid, failed or timed-out interceptors deny execution
  or suppress text. Cancellation and `SessionLogDurabilityError` propagate.
  The default path adds no custom provider, configuration schema, protocol
  field or Session Log migration.
- **Lifecycle:** matcher and handler share a per-hook budget, bounded by the
  chain and Run deadlines. Closing stops new dispatch, drains in-flight
  calls and releases registrations before plugin disposal. Repeated
  cancellation retains the cleanup task and its resources. Cancellation is
  cooperative: handlers that block synchronously or ignore cancellation can
  delay cleanup. Managed runs wait for the Bus and their RUN-only extension
  providers before allowing Session resources to close. Cleanup tasks carry
  exception objects as results to preserve cancellation causes on Python 3.10.
- **Proof anchors:** `tests/test_hook_bus.py` and `tests/test_hook_plugins.py`
  cover ordering, decisions, deadlines, cancellation, run isolation and
  visible tool results. Plugin-host, kernel compatibility, Tool Engine,
  permissions, session and ACP regressions cover integration boundaries.
  Exact-Head execution results belong in the submitting PR's Proof section.
- **Runtime and rollback:** packaged consumers need a rebuild, install, host
  restart and fresh task verification. Revert the HookBus integration and its
  structured tool-dispatch path together; preserve existing Session Logs and
  the legacy hooks API. Changes to kernel contracts and tool permission
  boundaries require core-maintainer review.
### 2026-09-09 — Skill Engine ordinary references and session facts

- **实现范围：** [Skill Engine 实现说明](../design/skill-engine.md)记录本 PR 的源码边界；不声明已合并、发布或更新安装运行时。原 [Tool 阶段文档](../design/tool-refactor/design.md)保留历史含义，不再作为当前 Skill 正文位置的依据。
- **行为变化：** 取消固定名称/关键词驱动的自动全文及 system 激活；新增完整本地目录分页工具 `list_skills`，真实 `get_skill` / `skill_view` 返回 tool 正文。目录名称和简介仍由 `SkillLoader.get_skills_metadata_prompt` 生成，经 CLI/ACP 现有 system 模板插入；Context 负责读取正文与宿主资料的投影。显式选择使用当前 user 请求副本，原始输入不改写。多选整组正文放不下时，只有本次真实可调用且允许整组名称的分页 reader 才能改走提示；否则在 provider 前阻断，短资料无 reader 仍可直接交付。资料按下一完整请求、同批共享额度及 UTF-8 成本核算预算。宿主资料按追加后实际 Message 的转义与包装增量核算预算；连显式选择分页提示都放不下时，Context 返回预算型阻断，Kernel 最多压缩并重组一次，仍无法容纳才在 provider 前以 Error/Done 结束。安全输入额度已在上游预留输出，Context 不重复扣除；完整输入检查覆盖 schema、额外消息和 transient 声明成本。压缩恢复不重放工具或通知，只有请求记录 flush 成功后才登记宿主资料交付，失败保留待恢复资料。
- **职责与装配：** `SkillRuntime` 只拥有来源解析、不可变快照和选择/读取事实，不保存消息、可见性或预算。Context 的 `SkillReferenceContext` 负责正文覆盖、分页和普通资料投影；`DefaultContextEngine` 经现有 plugin factory 按 run 创建，替换与来源核对后借用最终 Skill/Store。`prepare_request` 接收 Tool Engine 已准备的同一个完整 `PreparedTools`，核算 schema 开销但不改 definitions、曝光或可调用目标绑定；schema 展示不代表授权，每次 Tool 执行仍检查权限。集中装配让各模块共用最终依赖，代价是装配层需要明确连接并测试替换关系；具体处理仍留在各自模块。
- **使用与入口边界：** 通用规则要求采用方法后遵循适用流程、必需参考和验证；阻塞时采用获准恢复或报告未完成，不把必需项改称可选。规则同时进入目录提示与 `get_skill` 描述，正文仍是普通资料、不授予权限。Agent/ACP/默认装配只从真实 Get/List 推断来源。直接调用者的无来源历史恢复在模型及本次日志写入前阻断，并保留构造后 tuple API 提供正文的兼容入口。ACP 在 `prepare_resume` 前通过共享 SkillRuntime 的 partial restore 策略恢复当前可用子集，独立会话 Loader 缺失时使用真实工具 Loader；无来源时仍可继续会话，不改写原 Skill 记录。
- **会话与兼容：** Agent 持有会话 Skill 服务；Context 从已验证 tool 历史回报读取事实，当前覆盖逐请求核实，Kernel 保留真实历史。原生 Session Log 按 hash 保存宿主资料快照；第三方 Store 无快照文件接口时使用有界 `inlineContent` 与 `sha256`。两者均关联既有 `request/context`，由 Kernel flush 后才请求 provider，不扩展 Store 必需方法或新增事件。旧 `skill/change` 的名称/hash/顺序字段不改含义。沿用 `main` 的 `8702f84` 升级恢复策略：采用当前有效 Skill，版本/来源差异通过普通资料诊断说明，旧日志不改、新正文不冒用旧 hash；直接调用者遇来源缺失、禁用、损坏或 required 失败仍阻断；ACP 保留 main 的可继续恢复策略，跳过不可用及损坏的可选记录，legacy 缺失字段不作为历史正文或可见范围的证明。只有可验证的旧 system 后缀在请求投影中移除。计费用 `preloaded` / `get_skill` 旧枚举有意保留，不表示恢复自动全文策略。
- **能力边界：** child 明确委派的 required 闭包仍可代读，但正文进入普通资料、Get/List 受分配范围和父 profile 限制，不增加 `required_tools`。外层插件装配拒绝 Skill reader 与最终内置工具目录使用不同 Loader；Kernel 只接收 Port。原 Skill 文件和 Tool 权限不改；Tool 默认引擎创建/注册/释放是独立后续，`create_scheduled_task` 不变。
- **验证入口：** `tests/test_skill_catalog_tool.py`、`test_skill_usage_guidance.py`、`test_skill_entry_boundaries.py`、`test_context_input.py`、`test_skill_budget_recovery.py`、`test_skill_context.py`、`test_skill_context_regressions.py`、`test_skill_reference_persistence.py`、`test_agent_run_options.py`、`test_agent_session_persistence.py`、`test_sub_agent_capabilities.py`、`test_sub_agent_tool.py`、`test_skill_plugin_composition.py`、`test_plugin_host.py` 和 `test_acp.py`。跨版本投影默认从固定 `3e83bb3` Git 对象提取原读取器与 schema，不依赖邻接 checkout；也可指定 `BOX_AGENT_LEGACY_WORKTREE`。仅旧对象不可用且未指定路径时跳过，指定路径会核对固定 HEAD。执行结果、跳过项和真实任务结果须按最终提交另行报告，不从此条目推断通过。
- **回退边界：** 以完整代码版本和兼容的 Session Log 为边界，不手工改写旧日志或删除快照；旧读者接受新增字段不代表其模型请求行为与本实现相同。不在本 PR 引入新市场、自动安装、通用权限系统或任务调度框架。

### 2026-09-09 — Tool Engine ownership and local discovery

- **Change:** `codex/refactor-tool-engine`, initially based on `d2f665f` and
  rebased onto `25868b4`; no merge reference yet. [Design](../design/tool-refactor/design.md),
  [implementation guide](../design/tool-refactor/runtime.md) and
  [verification results](../design/tool-refactor/verification.md) describe this Tool
  phase; the separate Skill Engine phase is not included.
- **Durable behavior:** `tools/engine/` prepares request-bound definitions and
  real targets, executes calls and streams permission continuations, and
  finishes serial/parallel results through one path. `kernel/tool_messages.py`
  owns pre-effect call records and final replies; Session Log remains the only
  durable session source. Legacy kernel helpers re-export the implementation.
- **Compatibility:** public entry points, Tool instances, session resources and
  original Skill activation/recovery remain. Standalone invocation adds only
  optional event/cancellation context. Local low-frequency schemas become
  searchable alongside MCP; activation changes visibility, never authority.
  `create_scheduled_task` keeps its original name, Python class, schema, Skill
  text and direct exposure. No schedule-name migration is included. Restoring
  history never executes the draft again.
- **Corrections included:** permission retry progress retains the parent call;
  each parallel result uses its own arguments; Hook-modified paths are checked
  again; durability failures propagate; cancellation prevents a new execution
  attempt. Prepared definitions also retain immutable aliases for provider-side
  call recovery without adding alias schemas to the model. Ordinary failures
  do not trigger a general automatic retry.
- **Proof anchors:** call-record, permission-stream, result-commit, preparation,
  local-discovery, original-schedule-contract and capability-baseline tests; original
  core/ACP/Skill/session regressions. Exact-Head CI, real-task and packaged-host
  status are recorded separately in the verification record.
- **Rollback:** revert the Tool phase as one unit. There is no Session Log
  migration, new public configuration requirement, or new Skill scheduler.

### 2026-09-09 — shared Agent session state and configuration

- **Change:** `refactor(session): share configured Agent sessions across adapters`
  on `codex/kernel-plugin-refactor`; this entry records the implementation before
  a new PR or merge reference is available. See [Agent Session](../AGENT_SESSION.md).
- **Durable effect:** `AgentSession` owns live cancellation, injection, Skill,
  turn state, and the session Config reference. ACP's `SessionState` extends it
  with protocol metadata; CLI and ACP use the same session event entrypoint.
  Streams close before host completion so interrupted-turn persistence and
  cancellation cleanup finish before a later turn starts.
- **Compatibility:** `Agent.run()` and `Agent.run_events()` remain available.
  Existing sessions retain their Config reference when an adapter replaces its
  defaults; constructor values remain resolved at creation. This is not a
  general hot-reload API. Session Log stays the sole durable session-state
  source, with no schema or user-config migration.
- **Proof anchors:** `tests/test_agent_session.py`, `tests/test_cli_runtime.py`,
  `tests/test_cli_session_trace.py`, `tests/test_architecture_boundaries.py`,
  and the existing Agent/ACP/kernel/persistence suites. Exact execution results
  belong in the submitting PR's Proof section.
- **Runtime and rollback:** source changes require a runtime rebuild, install,
  host restart, and fresh live-task verification before packaged behavior is
  established. Revert the session extraction and adapter wiring together;
  existing Session Logs need no migration.

### 2026-09-09 — cwd-owned task organization and legacy output-root deprecation

- **Implementation:** `fix/stable-session-cwd` is rebased onto `7c85e82`,
  retaining PR #113's Tool Engine/result adapters and PR #114's shared live
  AgentSession. The accompanying Draft PR records the final tested Head.
- **Change:** General sessions keep `session/new.params.cwd` unchanged and no
  longer create a fixed per-conversation or `output/` directory. A model may
  create a semantic child directory when an independent task would otherwise
  clutter the selected workspace; this remains ordinary file organization.
- **Compatibility:** `artifact_mode`, `artifact_root`, `artifact_root_dir`, and
  `session_workspace_dir` spellings are accepted for the current compatibility
  series, warned once per ACP session, and ignored. Remove them from the next
  major schema. The legacy output-directory environment variable is stripped
  from Skill subprocesses and has no replacement. With the output-mode selector
  removed, `append_file` uses the Tool catalog's default discoverable exposure.
- **Artifact boundary:** pre/post discovery scans the original cwd recursively,
  excluding VCS, dependencies, virtual environments, caches, and Box-Agent
  internals. File-count and elapsed-time caps disable only the incomplete diff;
  explicit structured/text tool paths remain available.
- **Isolation and scan failures:** each workspace tool set has its own reserved
  Skill scratch subdirectory, so finishing one session cannot delete another
  session's temporary files. An incomplete pre- or post-scan disables the entire
  changed-file diff, including filesystem access failures; it never represents
  an empty workspace.
- **PPT/research:** both workflows consume the absolute directory selected in
  conversation. A research-backed deck shares that same directory and never
  changes session cwd.
- **PowerShell PPT images:** the image-status synchronizer accepts a narrowly
  scoped `Set-Location -LiteralPath ... -ErrorAction Stop;` prefix for a task
  subdirectory. The Skill uses this command instead of an unsupported Bash-tool
  `workspaceDir` parameter, and a failed directory switch stops synchronization.
- **Host action:** pass the user-selected workspace as `params.cwd`; stop
  allocating a conversation directory or sending legacy path selectors.
- **Runtime gap:** source tests and distribution builds are separate from a
  standalone runtime rebuild, host installation/restart, and fresh live-task
  verification. Those packaged-host steps remain required before rollout.
- **Rollback:** revert the runtime change together with the corresponding host
  cwd/path configuration. Preserve existing session cwd values and generated
  task files; do not automatically move, merge, or delete user directories.
- **Proof anchors:** ACP/tool/core/task-registry tests, PPT and research Skill
  contracts, Roadmap cwd-boundary tests, and the generated builtin Skill
  manifest.

### 2026-09-08 — native CLI session tracing

- **Change:** `feat(cli): record native session traces` on
  `codex/kernel-plugin-refactor`, following `7c750e2`; no PR or merge reference
  exists yet. This adds a CLI trace producer without changing the earlier
  viewer comparison or Session Log-only recovery decisions.
- **Durable behavior:** CLI tracing is enabled by default under the existing
  `~/.box-agent/log/sessions/` directory, with `cli-<unique-id>.jsonl` per
  invocation and distinct interactive turn IDs. `/clear` resets conversation
  history without erasing diagnostics; Goal autopilot continuations share the
  originating user turn. Startup probes and idle CLI commands stay outside
  the turn scope. The v1 lifecycle includes input, final output, internal stop
  reason and duration, reusing existing LLM/tool records without fake usage
  totals. Cancelled interactive run tasks settle before the turn closes.
- **Compatibility and privacy:** diagnostic path/write failures do not fail
  CLI tasks, and the caller's trace context is restored. Public run arguments,
  prompt construction, provider requests and ACP protocol behavior are
  unchanged. No Session Log migration or second recovery state is introduced.
  Trace contents remain sensitive despite shared redaction and retention;
  `BOX_AGENT_SESSION_TRACE_DIR` selects the directory and
  `BOX_AGENT_SESSION_TRACE_ENABLED=0` disables both ACP and CLI tracing.
- **Proof anchors:** `tests/test_cli_session_trace.py` covers task/interactive
  execution, Goal continuation, cancellation, exceptions, opt-out, invalid
  paths and context isolation; `tests/test_session_trace.py` covers the shared
  writer and existing ACP integration. Run these together with
  `tests/test_trace_viewer.py`, `tests/test_trace_viewer_server.py`, and
  `tests/test_architecture_boundaries.py`. Source CLI model/tool smoke and
  viewer inspection exercised native traces without harness writer injection.
- **Residual gaps and runtime:** the broader Windows suite is not all green;
  baseline home/path/runtime expectations and shared fixed-session test state
  were checked separately, and viewer socket failures passed isolated reruns.
  The smoke also retained a model-specific auxiliary judgement HTTP 422; it
  is not full business-case acceptance. No new runtime build/install, host
  restart or packaged-host task was verified for this feature.
- **Rollback:** disable tracing using the existing environment switch, noting
  that it also affects ACP, or revert this CLI producer and lifecycle-helper
  change for a CLI-only rollback. Preserve existing diagnostic files and
  Session Log data; consumers require no schema migration.

### 2026-09-08 — conservative ACP/CLI runtime extraction

- **Task:** centralize protocol-independent construction, prompt segments,
  Skill preload state, Goal continuation budgets, MCP registry reconciliation,
  and usage/artifact/cleanup observation. Keep ACP wire translation and CLI
  interaction policy in their existing entry points. Move terminal rendering
  behind `CliRenderer` while retaining `Agent.run()` and its rendering wrappers.
- **Compatibility:** retain ACP `SessionState`, `_run_turn`, historical import
  paths and factory hooks. `AgentRunHandle` proxies the original state; it is
  not a second state owner or a completed session-ownership migration. This is
  the first incremental slice, not a claim that all adapter assembly is thin.
- **Target consistency:** rebase onto `origin/main` at `56ee390`. The older
  kernel extraction is superseded by the upstream extraction and lifecycle
  fixes; `core.py`, `kernel/`, `composition.py`, `plugins/`, and `runtime.py`
  have no changes relative to that target. Do not replay the old extraction
  over newer kernel behavior.
- **Later integration refresh (2026-09-08):** the five feature commits,
  including native CLI tracing, were rebased onto upstream `6ef43f9` with
  unchanged patches. Kimi provider compatibility, Windows build guards and
  PPTX optional-host test support remain intact. The
  [latest integration record](../superpowers/plans/2026-09-04-thin-acp-cli-agent-runtime.md#main-integration-refresh--2026-09-08-6ef43f9)
  separates fresh source checks from the earlier full-suite and live evidence;
  it does not claim a new packaged-runtime release.
- **Proof:** see the dated verification section in the
  [implementation plan](../superpowers/plans/2026-09-04-thin-acp-cli-agent-runtime.md).
  Compare same-entrypoint requests before interpreting model-output differences;
  changing runtime paths and transport IDs is not a prompt-policy change.
- **Risk:** compatibility wrappers and host-specific sequencing intentionally
  remain. Remove them only after consumer migration and direct parity tests.
  Source/build checks and a local wheel import probe do not establish an
  installed officev3 runtime or a restarted host. Live model runs with missing
  inputs, tool failures, or plan/approval pauses are not task-acceptance proof.

### 2026-09-06 — kernel lifecycle follow-up

- Agent and kernel event streams close their owned tool iterators explicitly.
  Event and parallel tools receive their configured cancellation grace period;
  late results remain observed. Foreground Bash cancellation reaps the process
  tree and communication task, including when cancellation repeats during cleanup.
- Direct adapter calls and permission retries preserve `SessionLogDurabilityError`
  instead of converting it into an ordinary tool failure and continuing execution.
- Runtime Port validation preserves dynamic method proxies on Python 3.12+ without
  accepting missing members or forwarding unsupported special methods.
- A session whose disposal was interrupted cannot activate again until
  `dispose_session` completes. Other sessions and cleanup retries remain usable.
- Proof anchors: kernel compatibility, Bash process cancellation, permission
  negotiation, and plugin host regression tests. These source checks do not
  establish installed runtime or OfficeV3 behavior.

### 2026-09-04 — input-matched multi-source Agent Trace comparison

- Change: implementation on `codex/kernel-plugin-refactor`; no PR, merge, or
  packaged-runtime reference exists yet.
- Durable consumer behavior: **Compare sources** accepts an explicitly selected
  root whose immediate child directories are sources and whose source-level
  top-level `.jsonl` files are runs. It supports any number of sources, groups
  exact normalized first-turn inputs first, and uses normalized filenames only
  as a fallback when known inputs do not conflict. Repeated runs remain
  selectable newest-first within each source. Duration, LLM/tool call, token,
  and error deltas use a user-selected reference source.
- Compatibility and security: the original flat ledger and single-trace views
  are unchanged. Comparison adds one bounded `root / source / trace` read mode,
  not recursive discovery. Explicit-path selection, loopback `Host`/`Origin`
  validation, symlink rejection, 50 MiB per-file and 200 MiB per-root response
  limits, read-only behavior, and no-telemetry behavior remain in force. The
  trace writer, Agent loop, ACP, CLI launch contract, and provider contracts do
  not change.
- Proof anchors: `tests/test_trace_viewer.py`,
  `tests/test_trace_viewer_server.py`, and `tests/js/trace_model.test.js` cover
  the new navigation, bounded source scan, same-name source identity, N-way
  grouping, conflict rejection, repeated-run ordering, and exact deltas. A
  loopback Chromium probe loaded 28 baseline/current traces into 10 matched
  input groups, switched repeated runs, drilled into an existing trace detail,
  returned without losing selection, and produced no console warnings or
  errors.
- Runtime boundary and rollback: the comparison page was served directly from
  the source checkout, and wheel/sdist construction included the updated
  assets. The package was not installed, no host was restarted, and no fresh
  packaged task was run. Revert the comparison endpoint, model helpers, and
  comparison view to remove the feature; the flat ledger requires no data
  migration or rollback.

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
  level for its original flat ledger and requires a user-entered path when the
  browser picker is unavailable. The 2026-09-04 entry extends the consumer with
  a separately selected, bounded source-directory level while preserving this
  flat mode. Revert the eventual PR implementation and rebuild any consuming
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
