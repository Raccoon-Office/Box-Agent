# Box-Agent 分层架构

## 架构决定

Box-Agent 采用稳定公共 API、宿主无关 Kernel 与静态装配边界。产品行为和格式
专用执行策略不应进入 `box_agent/core.py` 或 `box_agent/kernel/`。

```mermaid
flowchart TB
    subgraph HOST["① 外部调用方"]
        Office["officev3 / 编辑器 / ACP 客户端"]
        Terminal["终端用户"]
        Python["Python 调用方"]
    end

    subgraph ADAPTER["② 入口与宿主适配"]
        ACP["ACP Server<br/>协议、模型绑定、权限交互、通知"]
        CLI["CLI<br/>配置与模型探测、命令、终端交互"]
        Inputs["会话输入<br/>Config + SessionOptions + HostBindings<br/>工作区、宿主回调与借入能力"]
    end

    subgraph PLUGINS["③ 插件运行时 · 跨 Run 复用"]
        Runtime["PluginRuntime + PluginHost<br/>静态 Descriptor / 校验 / 依赖排序 / 分 Scope 激活<br/>ACP 应用级共享；CLI 默认由 Session 持有"]
    end

    subgraph SESSION["④ 创建会话 · 每个 Session 一次"]
        Open["AgentSession.open / SessionState.open"]
        Context["SessionContext<br/>保留同一 Config 引用"]
        Prepare["PluginRuntime.open_session<br/>激活 Process / Session Scope<br/>按原 Config 开关准备能力"]
        Assembly["内置 Session 插件 → session_assembly<br/>模型 → Memory → Tools / Skills / MCP → Prompt → Hooks<br/>复用现有能力模块，保留延迟加载时机"]
        Construct["内部构造与完成初始化<br/>create → AgentService.create_agent<br/>finish_session：恢复并绑定 Skills"]
        Session["AgentSession 实例<br/>Config、Agent、PluginSession、取消与注入、回合状态<br/>ACP 使用子类 SessionState；AgentRunHandle 代理同一状态"]
        Open --> Context --> Prepare --> Assembly --> Construct --> Session
    end

    subgraph RUN["⑤ 每次 Run · 复用 Session，新建运行服务"]
        RunEvents["Session.run_events<br/>build_run_options 合并默认值、会话状态与显式覆盖"]
        RunContext["RunContext<br/>引用 SessionContext + 当前 Agent + 最终运行选项"]
        Activate["PluginSession.open_run<br/>复用同一个 PluginHost<br/>复用 Session 实例，激活 Run Scope"]
        Registry["ActivatedRegistry<br/>run.services → 新 KernelServices / HookManager"]
        RunEvents --> RunContext --> Activate --> Registry
    end

    subgraph EXEC["⑥ 公共执行链与输出"]
        Agent["已有 Agent.run_events"]
        Bridge["runtime.run_agent_loop<br/>→ core.run_agent_loop 兼容门面"]
        Composition["composition<br/>校验并传递已绑定 KernelServices<br/>管理 Kernel 事件流收尾"]
        Loop["AgentLoopKernel<br/>唯一 step 循环"]
        ToolEngine["工具能力 tools/engine<br/>准备 / 执行 / 结果适配"]
        ToolMessages["kernel/tool_messages<br/>工具消息提交"]
        Events["AgentEvent → Agent / Session"]
        Output["ACP 通知 / CLI Renderer / Python 消费者"]
        Trace["Session Trace / 日志 / 用量观测"]
        Legacy["兼容：直接 Agent / 同步 create<br/>未托管运行仍每次创建默认 Host"]
        Agent --> Bridge --> Composition --> Loop --> Events --> Output
        Loop -->|"ToolEnginePort"| ToolEngine
        ToolEngine -->|"提交回调"| ToolMessages
        Legacy -.->|"旧路径"| Agent
        Bridge -.->|"运行观测"| Trace
        Output -.->|"事件与用量"| Trace
    end

    Office --> ACP
    Terminal --> CLI
    ACP -->|"newSession"| Inputs
    CLI -->|"启动"| Inputs
    Python -->|"托管 API"| Inputs
    Python -.->|"兼容 API"| Legacy
    Inputs --> Open

    Runtime -.->|"提供同一 Host"| Prepare
    Runtime -.->|"提供同一 Host"| Activate

    ACP -->|"后续 prompt / 自动续跑"| RunEvents
    CLI -->|"任务 / 交互回合 / 自动续跑"| RunEvents
    Session -->|"复用已创建的会话"| RunEvents
    Registry -->|"经 RunOptions 绑定"| Agent
```

宿主调用路径因此为：**ACP/CLI → AgentSession → Agent → runtime → core 兼容门面 →
外层 composition → AgentLoopKernel**。Session 的 PluginRuntime 在创建会话时
准备能力，每次运行提供新的不可变 KernelServices。
依赖方向指向 Kernel 自己拥有的契约。`box_agent/kernel/` 绝不导入
PluginHost、composition、ACP、CLI、officev3 或其他产品适配器。Plugin 依赖
`kernel.ports`；Kernel 只接收已经解析的服务，不查询 Registry。产品层与能力层
也不能直接导入 `box_agent.core`。两个内置适配器都创建并运行 `AgentSession`；
配置流转、会话状态与宿主编排的边界见 [Agent Session](AGENT_SESSION.md)。

## 层级与职责

| 层级 | 主要代码 | 职责 |
| --- | --- | --- |
| 产品 / 接入层 | `box_agent/acp/`、`box_agent/cli.py`、宿主代码 | 协议转换、宿主元数据、ACP 协议渲染、CLI 入口接线与宿主明确选择 Skill |
| 共享会话层 | `agent_session.py`、`agent_run.py`、`session_context.py`、`session_assembly.py` | 配置驱动的能力准备、Agent 状态、运行选项绑定、事件流与资源归属 |
| 能力层 | `box_agent/tools/`（除 `base.py`）、`box_agent/skills/`、`box_agent/llm/` 中的 Provider、`memory.py` | Tool、自包含 Skill、Provider、存储与领域校验器 |
| 稳定公共 API | `agent.py`、`runtime.py`、`core.py`、`events.py`、`schema.py` | 向后兼容的调用方式与事件/schema 契约 |
| 外层装配 | `composition.py`、`plugins/` | 显式 Descriptor、校验、依赖解析、分 Scope 激活、不可变服务装配与释放 |
| 稳定 Kernel | `kernel/`、`session_log.py`、`loop_guards.py`、`hooks.py`、`artifacts.py`、`tools/base.py` | 对话不变量、调用闭合、持久化、Ports 与安全契约；具体工具调度和预算归 tools/engine |

“核心团队维护”表示修改需要核心维护者评审，不表示这些文件永远不能变化。

## 公共接入方式

新的宿主接入可以使用统一运行协议。`RunRequest` 描述一次运行的输入，
`AgentRunHandle` 管理一次运行，`ControlCommand` 负责运行中的外部控制，
`EventEnvelope` 提供带运行 ID 和顺序号的事件，`RunResult` 汇总最终结果：

```python
from box_agent import AgentService, RunRequest, ControlCommand

handle = await AgentService().start(
    RunRequest(
        run_id="run-123",
        session_id="session-456",
        user_message="分析当前目录中的报告",
    ),
    session=session,
)

async for event in handle.events():
    await render_for_host(event.payload)

result = await handle.result()
```

Python SDK 对同一边界提供了更短的调用方式：

```python
from box_agent import AgentClient, RunRequest

client = AgentClient(session)
result = await client.run(
    RunRequest(
        run_id="run-123",
        session_id="session-456",
        user_message="分析当前目录中的报告",
    )
)
```

需要流式输出或中途控制时使用 `await client.start(request)`，再消费返回的
`AgentRunHandle.events()`、调用 `send()` 和 `result()`。SDK 不负责创建或关闭
Session，Session 的生命周期仍由宿主负责。

宿主当前可以通过 `handle.send(ControlCommand.cancel())`、
`handle.send(ControlCommand("pause"))`、`handle.send(ControlCommand("resume"))`、
`handle.send(ControlCommand.inject_message(...))` 控制运行。权限协商通过
`PermissionBroker` 发出带 `request_id` 的 `PermissionRequestEvent`，宿主再发送
`ControlCommand("permission_response", request_id=..., payload={"approved": True})`；
运行内核会在下一次模型或工具动作前暂停，权限响应按请求 ID 唤醒对应等待者。当前适配层仍可使用
下面的兼容 API；它最终继续复用同一个 Session 和 Kernel 执行链。

产品适配器通过 `AgentSession.run_events()` 执行一轮，并提供完整的
`AgentRunOptions` 快照：

```python
from contextlib import aclosing

options = session.build_run_options(
    session_id=host_session_id,
    permission_negotiator=permission_adapter,
    hooks=host_hooks,
)

async with aclosing(session.run_events(options=options)) as events:
    async for event in events:
        await render_for_host(event)
```

确实需要独立低层循环的框架能力（例如 `SubAgentTool`）可以从
`box_agent.runtime` 导入 `run_agent_loop`。其他生产代码不得直接导入
`box_agent.core`。

原有 Agent API 与默认行为保持兼容。`AgentRunOptions` 和下层循环桥接新增可选的
内部 `kernel_services` 参数，由托管 Session 填充；ACP/CLI 调用方不传入
PluginHost、Registry 或服务集合。旧接口保留每次 Run 创建默认 Host 的路径。
独立工具入口 `runtime.invoke_tool_with_permissions()` 还接受可选的
`invocation_context` 和 `is_cancelled`；原 tuple 返回保持不变。ACP 消费
`AgentSession.run_events(options=...)`，由它委派给
`Agent.run_events(options=...)`，再把事件渲染成协议更新。CLI 通过
`cli_renderer.py` 中的 `render_agent_events` 消费相同的会话事件流。
旧入口 `Agent.run()` 复用该消费者，保留签名、终端渲染、记忆提案协商与返回值。
Kernel 与 composition 只产生事件，均不负责渲染。

## Kernel 模块与调用关系

`AgentLoopKernel` 维护唯一状态机和事件顺序，各辅助模块的职责保持窄而明确：

| 模块 | 职责 |
| --- | --- |
| `kernel/loop.py` | Step 编排、StopReason 映射、事件顺序及其他 Kernel 模块的调用 |
| `kernel/context_engine.py` | 上下文估算、压缩、摘要回退、最近消息选择与运行状态恢复 |
| `kernel/stream_controller.py` | Provider 流存活性、活动事件、stale 检测与流恢复 |
| `tools/engine/execution.py` | 共同的校验调用与流式权限继续；旧 permission 模块兼容导出 |
| `tools/engine/engine.py`、`scheduler.py`、`budget.py` | 每 run 调用编排、本次请求目标、原调度/取消与预算 |
| `tools/engine/results.py`、`tools/*_result_adapter.py` | 串并行统一结果完成；浏览器、文件、Skill、搜索和产物的能力适配 |
| `kernel/tool_messages.py` | 副作用前记录最终参数、提交最终回复与修复中断调用 |
| `kernel/ports.py` | Kernel-owned 最小 Protocol 与不可变 `KernelServices` 容器 |

主要调用关系为：

```text
AgentLoopKernel
  -> LLM 请求前调用 Context Engine
  -> 读取 Provider 时调用 Stream Controller
  -> 响应包含 ToolCall 时调用 Tool Engine
       -> 工具请求授权时调用 Permission Gateway
       -> 每个串行或并行完成项都进入 Tool Result Pipeline
  -> 通过 kernel tool_messages 回调提交调用和最终回复
  -> 只从 KernelServices 获取已解析能力
```

`core.py` 保持为兼容门面。原 Core 职责当前映射如下：

| 原 `core.py` 职责 / helper 组 | 当前归属 |
| --- | --- |
| Agent 循环与停止/事件不变量 | `kernel/loop.py` |
| 上下文大小、摘要、压缩与恢复 helper | `kernel/context_engine.py` |
| Provider stale 与活动流 helper | `kernel/stream_controller.py` |
| 权限协商 helper | `tools/engine/execution.py`，旧 kernel 入口兼容导出 |
| 工具调度、并行、取消与预算 | `tools/engine/`，旧 kernel 入口兼容导出 |
| 工具结果及专项适配 | `tools/engine/results.py` 和 tools adapters；最终历史提交由 `kernel/tool_messages.py` 负责 |
| 旧 helper 导入路径与计时默认值 monkeypatch 行为 | `core.py` 重导出 / wrapper |

## 静态 Plugin、Registry 与能力替换

Plugin 装配只在启动/激活边界静态进行，生命周期固定为：

```text
discover -> validate -> resolve dependencies -> activate -> dispose
```

`discover` 只读取调用方显式提供的 Descriptor 集合；`validate` 在任何 factory
运行前校验 ID、版本、声明的 Port 类型、依赖名称与 Registry 基数；依赖解析给出
确定性拓扑顺序；`activate` 创建或复用分 Scope 实例，冻结 exact-Port Registry
视图并生成一个不可变 `KernelServices`；`dispose` 按激活逆序且只执行一次，部分
激活失败时也按同样规则回滚。

每个 Port 只采用一种 Registry 基数语义：

- **required-single**：激活前必须且只能有一个实现；
- **optional-single**：允许零个或一个实现，拒绝歧义；
- **multi**：按确定性注册顺序保留全部实现。

Descriptor 支持 **process**、**session** 和 **run** Scope。Process 实例由同一个
Host 复用直至关闭；Session 实例用显式 session key 隔离，并随该 Session 释放；
Run 实例只属于一次 activation，并在结束时释放。默认兼容路径为每次旧接口调用
创建新 Host，并捕获调用方已有对象但不接管其所有权。

托管 Session 复用同一个 PluginRuntime/Host。`AgentSession.open` 根据原有 Config
开关准备模型、Memory、Tools/Skills/MCP、Prompt 和 Hooks；每次 `run_events` 根据
最终运行选项激活新的 Run 服务。依赖 Config 的资源使用 Session Scope，Process
factory 不接收会话 Config。异步准备在 Host 生命周期保留区之外执行。宿主借入能力
仍由原所有者释放。上下文 factory、关闭顺序和兼容 `create` API 见
[Agent Session](AGENT_SESSION.md)。

替换能力时，装配层先准备显式 Descriptor 集合，删除/替换目标 Kernel Port 对应
的 Descriptor，并在 `validate`/`activate` 前加入替代 Descriptor。激活后的
Registry 再转换为 `KernelServices` 并传给 `AgentLoopKernel`；运行中的 Kernel
不会发生替换。这是内部装配接缝，不新增 CLI/ACP 公共插件配置键；托管服务通过
上文所述的内部可选参数向后传递。

当前版本明确不支持 Python entry-point 扫描、目录扫描、热加载/热卸载、公共
Plugin 配置或 `WorkflowPolicy`。本架构也不表示已完成动态插件发现或已部署打包
运行时。

## Session 持久化与恢复

`SessionLog` 是 Agent 会话持久化状态的唯一事实源。它记录并恢复消息、工具
调用与结果、Goal、Plan、Todo、活动 Skill、压缩记录和轮次边界等通用事实。

恢复活动 Skill 时使用当前 SkillLoader 提供的内容，历史内容哈希不同不会阻断
会话恢复。内存中的哈希同步为当前内容哈希，恢复过程不改写历史日志。
不可用的 Skill 直接跳过，其余可用 Skill 继续恢复；没有 SkillLoader 时，
会话直接继续，不恢复活动 Skill。
格式异常的可选 Skill/Todo 状态直接忽略，保留有效的对话历史。

ACP 在验证会话 ID 和工作目录一致后，可降级恢复不兼容的日志版本、事件或
无效消息记录。替换运行日志之前，原始字节完整保存为 `session.recovery-*.jsonl`。
不回放历史工具；空的新日志允许下一次请求注入匹配的宿主续聊历史，再次重启后
也保留这一机会。宿主没有提供历史时，新运行会话不会恢复原对话上下文。
日志缺失时允许复用残留目录，已有日志与真实并发写锁仍受保护。
创建 Agent 或准备恢复失败时，立即释放会话写锁。

模型配置旧修订缺失时，若同一 profileId 的服务商和地址没有歧义，使用本地最新
有效修订；仍存在的旧修订保持固定，并保留会话选择的模型。解析过程只记录配置
和修订标识，不改写注册表或历史绑定。没有有效修订或服务商地址存在冲突时，
仍需更新模型配置。

一个 Session 在整个生命周期内只拥有一个规范化 cwd。用不同 workspace 打开
同一 Session 时，会在修复或修改日志之前失败。语法等价路径可以接受；
symlink alias 被视为不同的 workspace identity。

旧 workflow-paused 日志只做降级恢复：保留通用对话状态和已有持久产物，过滤
旧 synthetic workflow state，不重建领域状态机。历史 checkpoint/owner 文件
不会被读取、改写或自动删除。

## 等待用户输入

受信任的交互 Tool 通过 `Tool.ends_turn_on_success` 明确声明成功后结束本轮。
成功请求产生通用 `StopReason.WAITING_FOR_USER`；内核不会继续执行同批兄弟
Tool，也不会额外调用模型。ACP 对外映射为协议 `end_turn`，并报告通用
`runStatus: waiting_for_user` 元数据。

## Skill 与领域策略

Skill 激活只由显式调用、当前 matcher、宿主明确选择或通用 capability metadata
驱动。宿主选择在本轮具有权威性，语义匹配不会再追加一个竞争的领域 Skill。

格式专用的创作阶段、validator、scaffold、finalizer、质量规则和恢复说明属于
对应 Skill 或插件，由 Session Log 上下文和持久文件事实推导进度。Core、CLI
和 ACP 不判断交付是否完成，不重建领域阶段，也不强制隐藏续跑。
`ArtifactEvent` 只报告产物事实，不认证任务完成。

## 一个需求应该放在哪里

| 需求 | 放置位置 |
| --- | --- |
| 新增工具或外部能力 | `Tool` 实现、Skill 或 MCP Server |
| 新增格式专用流程或校验器 | 对应 Skill 或插件 |
| 新增模型 Provider 或协议兼容 | `box_agent/llm/` |
| 修改 ACP 字段、会话元数据或宿主渲染 | `box_agent/acp/` |
| 修改终端命令或显示 | `box_agent/cli.py` |
| 修改通用会话持久化事实 | `session_log.py` 及其 replay 测试 |
| 新增宿主无关事件或 Run Option | 稳定 API / 内核，需要核心团队评审 |
| 修改调度、取消、Tool 闭合或安全不变量 | 内核，需要核心团队评审 |

如果产品功能看起来必须修改 Core，先判断能否通过 Tool、Skill、Hook、事件消费
者或 Run Option 表达。都不满足时，才增加最小、通用契约；不要把产品名、
产物格式或某个领域状态机写入内核。

## 自动边界

`tests/test_architecture_boundaries.py` 保护 runtime bridge，禁止 Core 依赖产品
适配器或已删除的 workflow 模块，并防止演示文稿状态重新进入稳定内核。聚焦
行为测试覆盖 Session Log 恢复、cwd 不可变化、通用等待、直接预算、Skill
预加载、ACP 翻译和旧文件不变。
