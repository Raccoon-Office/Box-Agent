# Box-Agent Kernel 与 Plugin Host 重组设计

## 1. 文档状态

- 状态：已实现（源码重组；打包运行时尚未构建或安装）
- 设计基线：`7fc40296d80a6b9bc9a44349211470a08334999e`
- 本地整合基线：`a8d6ad4234b2f10db82f51a8daa9e42063fc8341`；保留该主干的模型续跑、
  图片引用与签名 URL 修复、截图持久化和中间素材过滤行为。
- 目标：在不改变外部接口和运行行为的前提下，将 `box_agent/core.py`
  重组为边界清晰的 Kernel，并为启动时可替换实现建立 Plugin Host、强类型注册表和
  Kernel-owned Ports。
- 当前实现提交、推送和运行时交付仍分别受仓库贡献与发布流程约束。
- 审查修复补充：普通串行工具的事件流关闭须等待工具清理；Python 3.10 下保留清理
  异常备注与原始异常；Port 校验的任意异常或取消都须释放已创建的插件资源。

## 2. 背景与问题

`box_agent/core.py` 是 CLI、ACP、自建 UI 和子 Agent 共用的执行内核。当前文件约
6195 行，包含 76 个顶层函数；`run_agent_loop()` 本身约 3400 行。它同时承担：

- Agent step 状态机和停止条件；
- LLM 流式调用、活动心跳、Provider stale 和截断恢复；
- 上下文估算、压缩、摘要回退和运行状态恢复；
- 工具可见性、别名、预算、串并行调度、取消和权限协商；
- 工具结果的模型历史、宿主事件、Session Log、Trace、资源回执和产物处理；
- Plan 审批、Memory 匹配/抽取/提升、搜索批次控制和搜索结果去重。

这些职责已经形成相对清晰的函数簇，但都被一个大型函数中的闭包状态和分支串联。
继续在同一文件中增加策略会扩大以下风险：

1. 串行和并行工具结果处理逐渐分叉；
2. 修改一个恢复分支时意外改变事件顺序或消息协议；
3. `Any` 类型依赖使 Memory、Permission、Session 和 MCP 暴露能力难以独立替换；
4. 单元测试只能围绕整个 `core.py` 建立，模块级契约不清晰；
5. 文件移动、插件化和行为修改若同时发生，无法可靠判断回归来源。

## 3. 目标与非目标

### 3.1 目标

1. 保持 ACP、CLI、公共 Python API 和 Provider/Tool 协议不变。
2. 保持相同输入下的 LLM 请求、工具执行、消息历史、事件序列、Session Log 和停止原因不变。
3. 将循环状态机、上下文、流、工具执行和工具结果收口拆成可独立测试的模块。
4. 由 Kernel 定义 Ports，由现有实现以结构化方式满足 Ports。
5. 通过 Plugin Host 在 Agent 启动或 session 创建时完成实现解析和依赖注入。
6. 保留当前实现作为唯一默认实现，使不配置插件的运行路径完全兼容。
7. 为后续替换 LLM、Memory、Session、Permission、Hook 和工具目录实现建立稳定接缝。

### 3.2 非目标

1. 不改变任何模型提示、默认限制、事件字段、错误文案或配置默认值。
2. 不引入运行中热加载、热卸载或跨进程插件市场。
3. 不重新引入已经删除的 Workflow 状态机或 `WorkflowPolicy`。
4. 不移动现有 LLM Provider、Tool、Memory、Session Log 和 Hook 的具体业务实现。
5. 不把 CLI、ACP、officev3 或文档/PPT 等产品逻辑放入 Kernel。
6. 不借重组进行无关重命名、格式化、性能优化或兼容层清理。

## 4. 架构原则

### 4.1 Kernel 拥有协议不变量

以下规则必须由 Kernel 唯一控制，不能交给任意插件重新排序或绕过：

- assistant 的每个 tool call 最终都有且只有一条对应 tool message；
- tool message 在任何工具结果事件向消费者 `yield` 前写入 `messages`；
- `messages` 保持原地修改语义；
- `StepStart`、LLM/Tool 事件、`StepEnd`、`DoneEvent` 的顺序保持稳定；
- 取消、错误、超时、截断和 `StopReason` 的映射保持稳定；
- 同一 assistant 响应中的相同变更型工具调用只执行一次；
- 上下文超限时只能压缩成功或明确阻断，不能静默丢弃关键协议消息；
- 可见工具输出与写回模型的历史内容是两条独立通道；
- Session Log durability error 不得被普通插件吞掉；
- 串行和并行执行最终经过同一个结果收口契约。

### 4.2 Ports 属于 Kernel

`kernel/ports.py` 描述 Kernel 需要的行为。插件实现依赖这些 Port；Kernel 不导入
`box_agent.plugins`。这保证依赖方向为：

```text
plugins/default implementations -> kernel/ports <- AgentLoopKernel
                         ^
                         |
                 PluginHost 负责装配
```

将 `ports.py` 放进 `plugins/` 会让 Kernel 依赖插件层，形成反向依赖，因此不采用。

### 4.3 Plugin Host 不是 Service Locator

Plugin Host 只在 composition boundary 工作：发现、验证、解析、激活并生成一个不可变的
`KernelServices`。`AgentLoopKernel` 在热路径中直接使用 `KernelServices`，不得在每个
step 或工具调用中查询全局 Registry。

### 4.4 先模块化，后插件化

第一阶段只移动逻辑并建立模块契约；第二阶段再用 Ports 包装已经稳定的模块边界。
任何阶段都不能同时包含行为调整。真正可替换的内部实现需要依赖注入，这是内部结构变化，
但不是外部行为变化。

## 5. 目标目录结构

```text
box_agent/
├── __init__.py                    # 公共导出保持不变
├── agent.py                       # Agent / AgentRunOptions 保持不变
├── runtime.py                     # 稳定运行时桥接保持不变
├── core.py                        # 兼容门面；保留旧入口和迁移期重导出
│
├── kernel/
│   ├── __init__.py
│   ├── loop.py                    # AgentLoopKernel，唯一循环状态机
│   ├── state.py                   # ToolBudgetState / ToolExecutionState
│   ├── ports.py                   # Kernel-owned Protocols
│   ├── context_engine.py          # 上下文估算与压缩
│   ├── stream_controller.py       # LLM 流控制和恢复
│   ├── tool_engine.py             # 工具调度和执行
│   ├── permission_gateway.py      # 权限协商和重试
│   └── tool_result_pipeline.py    # 工具结果统一收口
│
└── plugins/
    ├── __init__.py
    ├── host.py                    # 生命周期、scope 和装配
    ├── registries.py              # 强类型 Registry
    ├── descriptors.py             # ID、版本、依赖、scope
    └── defaults.py                # 当前实现的默认注册
```

已有 `box_agent/llm/`、`box_agent/tools/`、`memory.py`、`session_log.py`、`hooks.py`
继续保留原位置。Plugin Registry 注册现有类或轻量 adapter，不复制具体实现。

## 6. 模块职责

### 6.1 `core.py`：兼容门面

迁移期间 `core.py` 不再拥有主要实现，但继续提供：

- 与当前完全相同签名的 `run_agent_loop()`；
- `runtime.py` 当前使用的权限协商入口；
- 当前测试直接导入的私有 helper 和常量的临时重导出；
- 稳定的 `box_agent.core` logger 名称。

`core.py` 保留旧参数和延迟读取时序，将调用委托给外层 composition；composition 通过
`PluginHost` 生成不可变 `KernelServices`，再构造 `AgentLoopKernel`。它们都不得新增产品策略。

### 6.2 `kernel/loop.py`：AgentLoopKernel

`AgentLoopKernel` 只负责流程和状态转换：

1. 初始化 run/session 依赖；
2. 修复进入循环前的消息协议；
3. 执行每个 step 的取消、注入和边界检查；
4. 调用 Context Engine；
5. 调用 Stream Controller；
6. 根据 LLM outcome 结束或调用 Tool Engine；
7. 将工具执行 outcome 交给 Tool Result Pipeline；
8. 更新 step/run 状态；
9. 发出 `StepEnd`、Memory 和最终 `DoneEvent`。

它不直接解析搜索结果、不直接写文件产物、不直接实现 Provider 流读取，也不直接处理
Session Log 底层格式。

### 6.3 `kernel/state.py`

只抽取已经形成稳定模块契约、且无需改变事件时序的状态：

- `ToolBudgetState`：直接/委派工具预算、按工具计数和连续空搜索状态；
- `ToolExecutionState`：单次调用或批次的活动时间、超时和取消观察状态。

其余跨 step 的编排状态继续由 `AgentLoopKernel` 局部持有。状态对象不执行 I/O，字段默认值
与迁移前局部变量一致。

### 6.4 `kernel/context_engine.py`

迁移现有：

- `_summary_message_text`；
- `_create_summary`；
- `_deterministic_history_fallback`；
- `_message_chars`；
- transient follow-up token 估算和验证；
- `_bound_text_middle`、`_bound_retained_messages`；
- 上下文估算、消息分组和最近消息选择；
- `_restore_runtime_state`；
- `_maybe_summarize`；
- compaction marker 判断和 `CompactionOutcome`。

输出应是明确的 `CompactionOutcome`，Kernel 根据 `mode` 决定继续或阻断。

### 6.5 `kernel/stream_controller.py`

迁移：

- Provider stale 配置解析；
- `_stream_with_activity`；
- thinking/text/activity chunk 的标准化；
- 重复流检测；
- `StreamInterrupted` 和结构化 LLM error 处理；
- provider stale、tool argument limit、max_tokens 和正常文本截断恢复；
- `LLMResponse` 和 `LLMOutputEvent` 的构造。

返回 `StreamOutcome`，其中显式区分：

- 正常响应；
- 需要下一 step 重试；
- 已产生最终 `DoneEvent`；
- fatal error；
- cancellation。

迁移时不得调整重试次数、提示文本、token boost 或事件时机。

### 6.6 `kernel/tool_engine.py`

负责工具调用前和执行期间的行为：

- 当前 step 工具目录和兼容别名解析；
- MCP generation/offer 校验；
- browser intent 校验；
- Plan 审批门禁；
- 历史占位符修改恢复门禁；
- 单工具、总工具、搜索和委派预算；
- 同响应完全相同调用去重；
- 串行、并行和 turn-ending 工具分组；
- EventEmittingTool 队列转发；
- 并行信号量、超时、取消和结果补齐。

输出必须保证每个输入 ToolCall 都有一个 `ToolExecutionRecord`，包括被预算阻止、重复、
超时或未知工具的调用。

### 6.7 `kernel/permission_gateway.py`

迁移权限请求规范化、PolicyDecision 构造、一次性授权消费和多权限门禁重试。保留：

- 最多四次权限重试；
- 同一请求批准后重复出现时停止；
- negotiator 异常转换；
- 无 negotiator 时仍产生兼容的 `PermissionRequestEvent`；
- `runtime.invoke_tool_with_permissions()` 复用同一实现。

### 6.8 `kernel/tool_result_pipeline.py`

所有串行和并行结果必须收敛到同一个 pipeline。阶段顺序固定为：

```text
ToolResult
  -> permission retry result
  -> browser snapshot persistence
  -> active Skill promotion
  -> transient follow-up validation
  -> nested/search-file budget accounting
  -> result Hook transformation
  -> capability-specific normalization (current web_search)
  -> context-resource decision
  -> model-history content selection
  -> repeated framework-error compaction
  -> oversized result persistence
  -> append tool message
  -> resource ledger update
  -> Session Trace
  -> ToolCallResult / WebSearch / Permission events
  -> artifact detection
```

“append tool message”必须先于任何可能向消费者交还控制权的结果事件。

## 7. Ports 设计

Ports 使用 `typing.Protocol` 或现有稳定抽象，只描述 Kernel 实际使用的最小方法集。

| Port | 最小职责 | 当前默认实现 |
| --- | --- | --- |
| `LLMPort` | 主循环所需的 `generate_stream` | `LLMClientBase` 或兼容的仅流式实现 |
| `SummaryLLMPort` | 非流式 `generate`，用于生成上下文摘要 | 当前 summary LLM；未单独提供时主 LLM 仅在实际触发摘要时被调用 |
| `PermissionGatewayPort` | `negotiate(request) -> bool` | ACP/CLI negotiator |
| `MemoryLookupPort` | 为当前请求匹配弱上下文 | `MemoryManager` |
| `MemoryExtractionPort` | 在生命周期点后台抽取记忆 | `MemoryExtractor` |
| `MemoryPromotionPort` | 候选、标记和提升计划 | 当前 Memory Manager |
| `SessionStorePort` | append、flush、surface replacement | `SessionLog` |
| `HookBusPort` | 生命周期通知和工具拦截 | `HookManager` |
| `ToolCatalogPort` | 提供 Tool 的可变映射、名字索引、查找和遍历 | 当前 `dict[str, Tool]` |
| `ToolExposurePort` | 每 step 暴露和校验动态 MCP 工具 | `MCPToolExposureManager` |
| `ToolResultStorePort` | 大结果处理和 fresh budget | `ToolResultStorage` |

不创建重复的 `ToolPort`、`ToolResult` 或新的 LLM 抽象层；现有 `Tool`、`ToolResult` 和
`LLMClientBase` 应尽可能直接满足 Port。

## 8. Plugin Host 和 Registry

### 8.1 生命周期

第一版生命周期固定为：

1. `Discover`：读取显式注册项和内置默认项，不实例化具有副作用的对象；
2. `Validate`：验证 ID、版本、Port 类型、重复键、作用域和必需配置；
3. `ResolveDependencies`：构建依赖 DAG，拒绝缺失依赖和环；
4. `Activate`：按拓扑序创建实例，生成不可变 `KernelServices`；
5. `Dispose`：按逆序执行同步/异步关闭。

第一版不扫描任意目录，不通过 Python entry points 自动加载第三方包，也不热替换已激活实例。

### 8.2 作用域

- process：同一 Host 生命周期内复用一个实例；
- session：按显式、可哈希的 session key 隔离并复用实例；
- run：每次 activation 创建实例，由该 activation 逆序释放。

默认 composition 捕获的对象仍由调用方拥有，因此使用 run scope 且不注册 disposer；Host
只管理 Registry 绑定，不擅自关闭 LLM、Memory、Session 或 Tool 实例。Plugin Host 必须阻止
session-scoped 可变对象被不同 session key 意外共享。

### 8.3 Registry 规则

- singleton Port 在激活时必须解析为恰好一个实现；
- Tool、Hook 和结果 processor 可多重注册；
- 同一 key/version 冲突必须在 LLM 调用前失败；
- 依赖缺失和依赖环必须在 Activate 前失败；
- 激活完成后，当前 session 的 Registry 视图不可变；
- Kernel 不直接持有 Registry，只持有解析完成的 `KernelServices`。

## 9. 外部兼容契约

以下接口、导入路径、签名和默认值保持不变：

- `box_agent.Agent`；
- `box_agent.AgentRunOptions`；
- `Agent.default_run_options()`；
- `Agent.run_events()` 和 `Agent.run()`；
- `box_agent.runtime.run_agent_loop()`；
- `box_agent.runtime.invoke_tool_with_permissions()`；
- `box_agent.core.run_agent_loop()`；
- `Message`、`AgentEvent`、`ToolResult`、`StopReason`；
- 当前配置键及默认值；
- Session Log 记录 schema；
- ACP wire event 和 stdout/stderr 边界。

`runtime.py` 继续作为生产代码访问低层循环的唯一稳定桥接。`core.py` 继续存在，直至独立
兼容弃用决策获批；本次重组不删除它。

## 10. 行为等价标准

对同一确定性测试输入，重组前后必须满足：

```text
相同 Provider 请求消息和 Tool schemas
-> 相同 LLM 调用次数
-> 相同 Tool 调用顺序、参数和次数
-> 相同最终 messages 内容和顺序
-> 相同 AgentEvent 类型、字段和顺序
-> 相同 Session Log 语义记录
-> 相同 artifact、permission 和 memory 事件
-> 相同 StopReason 和 final_content
```

时间戳、耗时和临时文件绝对路径仅在现有测试已经允许变化时继续允许变化。

## 11. 错误和安全边界

1. Plugin discovery/validation/activation 错误必须发生在首个 LLM 请求之前。
2. 可选插件失败可回退默认实现的前提是配置没有显式要求该插件；显式选择失败必须 fail closed。
3. Session Log durability error 继续穿透普通工具异常包装并终止不安全运行。
4. Hook 的现有“记录告警但不使 Core 崩溃”语义保持不变。
5. Tool 参数仍必须通过 `Tool.invoke()` 的 schema 验证；Plugin Host 不得绕过它直接调用 `execute()`。
6. 权限批准继续是一次性授权，并由 Tool 消费；Registry 不能缓存用户授权为全局状态。
7. 第三方实现不得获得超出对应 Port 的 Agent 内部对象。

## 12. 渐进迁移策略

### 阶段 0：行为基线

在移动代码前补足 characterization tests，锁定：

- `inspect.signature` 可见签名；
- LLM 请求和工具 schema；
- 串行、并行、重复、超时和取消事件序列；
- 最终 `messages`；
- Session Log 和 Trace 关键记录；
- ACP 和 CLI 入口行为；
- `box_agent.core` logger 名称。

### 阶段 1：移动无状态 helper

先移动搜索、消息序列化、产物检测和上下文纯函数。`core.py` 重导出旧名字。每次移动只涉及
一个函数簇，不改变调用点语义。

### 阶段 2：显式状态对象

引入 `ToolBudgetState` 和 `ToolExecutionState`，只替代具有独立稳定契约的局部状态；其他
编排状态保留在循环中，避免大规模更新时机变化。

### 阶段 3：Context 和 Stream

提取 `ContextEngine`，再提取 `StreamController`。两者先使用现有具体依赖，暂不引入 Registry。

### 阶段 4：统一 Tool Result Pipeline

先让串行、并行分支调用同一结果收口实现，再移动调度逻辑。此阶段必须重点验证 append-before-yield
和权限重试后的结果。

### 阶段 5：Tool Engine 和 Permission Gateway

移动预算、调用去重、串并行调度、超时和权限逻辑。保留 `runtime.invoke_tool_with_permissions()`
的现有路径。

### 阶段 6：Ports 和默认注册

用窄 Protocol 替代 Kernel 内部的 `Any`。现有对象通过结构化兼容直接满足 Port，并由不转移
所有权的默认 descriptor 注册；此时仍只有当前默认实现。

### 阶段 7：静态 Plugin Host

引入 `PluginHost`、Registry、descriptor 和默认注册。`core.py` 将旧参数映射到 Host 组装输入；
未配置插件的调用必须生成与此前等价的 `KernelServices`。

### 阶段 8：独立评估外部插件发现

仅在有明确第三方插件用例后，另行设计 entry points、签名/信任、版本协商和打包发现。本设计不自动
授权这一阶段。

## 13. 验证矩阵

至少执行：

```text
uv run pytest tests/test_core.py -v
uv run pytest tests/test_architecture_boundaries.py -v
uv run pytest tests/test_hooks.py tests/test_inject.py -v
uv run pytest tests/test_permission_negotiation.py -v
uv run pytest tests/test_truncation_continuation.py -v
uv run pytest tests/test_length_retry_no_double_render.py -v
uv run pytest tests/test_llm_activity.py tests/test_llm_debug_logging.py -v
uv run pytest tests/test_session_log.py tests/test_session_trace.py -v
uv run pytest tests/test_mcp_tool_search.py -v
uv run pytest tests/test_sub_agent_tool.py -v
uv run pytest tests/test_acp.py -v
uv run pytest tests/test_cli_runtime.py tests/test_cli_config.py -v
uv run pytest tests/ -v
git diff --check
```

若影响 frozen runtime，还必须分别报告：source tests、runtime build、runtime install、probe、host
restart 和 fresh live task；源码测试不能替代打包验证。

## 14. 架构边界测试调整

现有 `tests/test_architecture_boundaries.py` 应扩展而非削弱：

- 只有兼容门面/运行时装配层可以组合 Kernel 和 Plugins；
- `box_agent/kernel/**` 不得导入 `box_agent.acp`、`box_agent.cli` 或 `box_agent.plugins`；
- `box_agent/plugins/**` 可以导入 `kernel.ports`，不得导入 `kernel.loop` 的内部实现；
- 产品和能力模块继续不得直接依赖 Kernel 实现；
- Kernel 继续不得包含 PPT、presentation 或其他具体工作流 token；
- 旧 Workflow 状态机模块继续保持删除状态。

## 15. 主要风险与缓解

| 风险 | 缓解措施 |
| --- | --- |
| 异步生成器多包一层后取消/`aclose` 语义改变 | characterization test 覆盖主动取消、消费者中断和 Provider stale |
| 结果 pipeline 调整事件顺序 | 为顺序建立精确事件快照；先 append tool message 后 yield |
| logger 名称因模块移动改变 | 内核日志显式沿用 `box_agent.core`，日志迁移另行设计 |
| 私有 helper 移动导致现有测试或外部调试代码失败 | `core.py` 迁移期重导出，单独决定弃用 |
| Ports 变成大而全接口 | Memory 等能力拆成最小 Protocol，按使用方定义 |
| Registry 成为全局 Service Locator | Activate 后生成不可变 `KernelServices`，Kernel 不查 Registry |
| session 可变状态跨 Agent 泄漏 | descriptor 明确 scope，Host 为每个 session 创建实例 |
| PyInstaller 漏收新模块或动态实现 | 第一版静态注册；执行 build/install/probe 验证 |
| 重组夹带行为改进 | 一个提交只移动一个职责；行为改进必须另立任务和测试 |
| 重新引入领域 Workflow | 默认 Registry 不注册 WorkflowPolicy，边界测试继续禁止 Kernel 工作流状态机 |

## 16. 回滚策略

迁移始终保持 `core.py` 兼容入口，模块边界可按函数簇审查。最终实现以一个聚焦重构提交交付；
若出现回归，可整体 revert 该提交恢复单体 Core。Plugin Host 保留由当前实现组成的默认 profile，
无需配置或持久数据迁移。

## 17. 已确认决策

1. 采用“兼容门面 + Kernel + Ports + 启动时 Plugin Host”的两阶段方案。
2. `ports.py` 归 Kernel 所有。
3. `core.py` 在本次迁移中保留。
4. ACP、CLI、Agent、runtime、event 和配置接口不变。
5. 第一版不做运行时热加载/卸载。
6. 不重新引入 WorkflowPolicy 或旧 Workflow 状态机。
7. 先证明行为等价，再开放替换实现。
