# Agent 工具接口、代码编排与协作任务设计

整理日期：2026-09-28。本文记录本次讨论中的工具组成、调用方式与设计取舍，作为 Box-Agent 工具设计的参考。

证据范围：工具名称、参数和限制来自当前 Codex 会话向模型暴露的接口定义；不代表所有 Codex 版本或其他 Agent 的通用配置，也不披露或推断不可见的内部实现。本文将接口事实与设计建议分开，示例中的简化类型不是完整机器可读 schema。

关联文档：[长期演进方案](shared-runtime/LONG_TERM_PLAN.md)、[优化任务总览](shared-runtime/ROADMAP.md)、[Tool 设计](tool-refactor/design.md)。本文不改变已有开发优先级，不等于批准新的产品接口、框架或部署方式。

## 1. 工具名、命名空间和底层协议

`collaboration` 意为“协作”，在当前环境中是多 Agent 工具的命名空间。`命名空间.操作名` 用于组织能力和避免重名，例如 `collaboration.spawn_agent`、`clock.sleep`、`functions.exec`。

另一些名称采用扁平形式，例如 `mcp__codex_app__create_thread`，分别标识 MCP 来源、服务名和操作名。名称表达分组和来源，不足以确定实际网络协议、进程位置或权限。

当前 `collaboration.*` 是运行环境直接暴露的协作接口；`mcp__codex_app__*` 是 codex-app-tools 插件提供的 MCP 应用接口。只能据此确认模型可见接口层，不能断言协作接口的内部通信实现。

## 2. 当前非 MCP 工具组成

### 2.1 顶层命名空间

| 命名空间 | 工具 | 作用 |
| --- | --- | --- |
| `functions` | `exec` | 执行 JavaScript 编排代码，调用注入的工具接口 |
| `functions` | `wait` | 继续等待尚未完成的 exec cell，获取新增输出或完成状态 |
| `functions` | `request_user_input` | 提问并等待回答；当前仅在 Plan 模式可用 |
| `functions` | `request_user_input_async` | 发出问题后立即返回，回答随后以消息送达 |
| `clock` | `sleep` | 按时间暂停；新用户输入可提前结束等待 |
| `collaboration` | `spawn_agent`、`send_message`、`followup_task`、`list_agents`、`wait_agent`、`interrupt_agent` | 当前任务内部的多 Agent 协作 |

工具被列出不代表在所有模式下都可调用。模式、任务授权、环境权限和工具自身前置条件共同决定是否可用。

### 2.2 通过 exec 可调用的非 MCP 能力

以下名称是注入对象 `tools` 的方法名，不是 `functions.exec` 的子命名空间：

| 能力 | 方法名 | 用途 |
| --- | --- | --- |
| 命令执行 | `exec_command`、`write_stdin` | 启动命令、写入标准输入和读取持续输出 |
| 文件修改／查看 | `apply_patch`、`view_image` | 应用文件补丁、查看本地图片 |
| 目标管理 | `create_goal`、`get_goal`、`update_goal` | 创建、读取、更新显式目标；不把普通请求自动转为目标 |
| 时间查询 | `clock__curr_time` | 查询当前 UTC 时间 |
| 网络检索 | `web__run` | 搜索、打开页面及其他受支持查询 |
| 图像生成 | `image_gen__imagegen` | 生成或编辑图像 |
| 插件安装请求 | `request_plugin_install` | 满足指定条件时建议安装插件，不是任意安装入口 |

exec 还可以编排环境提供的部分 MCP 工具；“使用 exec”与“底层工具是否来自 MCP”是两个独立维度。资源发现等其他接口也可能被提供，本表聚焦本次讨论涉及的能力，不承诺完整或永久不变的工具目录。

## 3. exec 的真实调用结构

当前模型调用的外层工具是 `functions.exec`，输入为 JavaScript 源码，而不是普通 JSON 参数对象。运行环境预先注入 `tools` 对象以及 `text()` 等辅助函数，不需要 `import`。

```javascript
const result = await tools.exec_command({
  cmd: "git status --short --branch"
});
text(result);
```

调用关系如下：

```text
模型
  → functions.exec（输入：JavaScript）
      → tools.exec_command（输入：结构化命令参数）
          → shell／命令进程
      → text（将结果输出给模型）
```

因此，工具名不是 `functions.exec.exec_command`。外层 exec 负责执行编排，底层 exec_command 仍然是有独立定义的工具。

当前环境没有单独暴露给模型直接调用的 Bash 工具。执行终端命令通过上述路径；本仓库宿主的默认 shell 是 PowerShell。只有环境中存在 Bash 且选择相应 shell 时，命令才按 Bash 语义执行。

### 3.1 tools 是定制接口，不是通用 JavaScript 库

模型仍须知道每个方法的名称、参数类型、语义、返回结构、错误、副作用、权限与取消规则。将调用写成 JavaScript，不会免除提供工具定义的成本。

当前环境会提供部分完整工具定义；还提供 `ALL_TOOLS` 名称与描述元数据辅助发现。部分条目的 description 包含完整函数声明，可以按需读取；只有简述时仍需获取准确的参数说明。未掌握调用契约时不应猜测。

编排代码运行在受限 JavaScript 环境中，不是完整 Node.js：不能直接使用任意文件系统或网络库，相关能力通过工具接口获得。当前 exec 每次在新的 V8 isolate 中运行；需要跨次保留数据时使用显式提供的存储辅助接口，不能假定局部变量会自动延续。

当前 `collaboration.*` 必须直接调用，不能塞进 `functions.exec`；并非所有工具都属于这个 `tools` 对象。

### 3.2 编排输出与生命周期

- `text()` 返回文本或可序列化结果；图片、音频等有各自输出辅助接口，不能将大段编码数据直接当日志输出。
- `await` 确保编排等待所需结果。独立操作可以组合并发；有依赖、顺序约束或副作用冲突的操作应顺序执行。
- exec 结束时未等待的 Promise 会被丢弃，不应把这种写法当作可靠后台任务机制，也不能据此认定外部副作用已取消。
- 长时间命令应使用命令工具的进程会话；exec 的运行 cell 与命令进程会话是不同对象。

## 4. 提问和各种等待接口

| 接口 | 等待对象 | 关键行为与约束 |
| --- | --- | --- |
| `functions.request_user_input` | 用户回答 | 一次 1–3 个简短问题，支持选项；当前只在 Plan 模式使用 |
| `functions.request_user_input_async` | 不在调用处阻塞 | 发出问题后可继续不依赖回答的工作；答案作为后续消息到达 |
| `functions.wait` | 某个 exec cell | exec 明确返回仍在运行的 `cell_id` 后才能调用；读取新增输出，也支持终止该 cell |
| `clock.sleep` | 时间 | 按指定毫秒数暂停，可能被新用户输入提前结束；不检查任务完成条件 |
| `collaboration.wait_agent` | 子 Agent 的协作消息／通知 | 有更新、用户输入或超时后返回；通知不等于子 Agent 已完成，也不直接承载完整结果正文 |
| `tools.write_stdin` | 命令进程会话 | 使用 exec_command 返回的 `session_id`，可发送输入；不发送字符时可继续取输出或等待退出 |
| `mcp__codex_app__wait_threads` | 独立任务 | 等待目标任务完成或需要关注，支持游标和立即状态快照；不等于 wait_agent |

这些接口应按“等待什么”选用，不能互相替代。终止 exec cell 不应被视作已保证所有外部命令或远程操作停止；必须按相应执行工具的契约核实。

异步提问允许继续独立工作，不表示可以把用户沉默视作批准；普通问题接口也不应代替需要专门处理的权限批准流程。

## 5. 独立 thread：用户拥有的任务

本次通过 `mcp__codex_app__create_thread` 创建了“T13 System 与 Tool 最小任务基线”独立任务。这里的 thread 是 Codex 应用中的聊天任务，不是 Box-Agent 的 AgentSession，也不是 collaboration 子 Agent。

### 5.1 创建与交接

工具来自 codex-app-tools。创建前通过 `mcp__codex_app__list_projects` 获取项目 ID；用户明确要求创建新任务后，调用 create_thread。

本次使用的接口部分如下；工具还支持其他目标及可选参数：

```typescript
create_thread({
  title?: string,
  prompt: string,
  target: {
    type: "project",
    projectId: string,
    environment: { type: "local" }
  }
})
```

返回可供后续工具使用的 threadId 和 hostId。创建非阻塞，需再查状态才能确认任务已开始。某些需要准备工作区的创建会先返回 clientThreadId；不能将它冒充 threadId 调用后续接口。

本次新任务使用同一项目的本地检出，没有创建 worktree，没有自动复制全部聊天历史，也没有覆盖默认模型。它通过初始交接消息、方案文档、源码及 Git 提交获得必要上下文。

可靠交接应包含目标、非目标、分支与提交、关键文档、验收、提交权限、环境限制、已知失败及运行时验证边界。历史证据注明所属版本，不能充当新任务的测试结果。

### 5.2 当前会话可对其他 thread 执行的操作

以下工具均带 `mcp__codex_app__` 前缀：

| 操作名 | 能力 |
| --- | --- |
| `list_threads` | 列出任务与状态 |
| `read_thread` | 读取近期状态、消息与执行摘要，按需读取部分输出 |
| `wait_threads` | 等待任务完成／需要关注，或获取立即快照 |
| `send_message_to_thread` | 发后续要求，需用户明确授权消息或持续协调工作流 |
| `navigate_to_codex_page` | 在应用中显示任务 |
| `set_thread_title` | 重命名 |
| `move_thread_to_sidebar_section` | 置顶或移动到侧边栏分组 |
| `set_thread_archived` | 归档／恢复 |
| `fork_thread` | 基于已有历史创建分叉任务 |
| `share_thread` | 创建不可变分享链接 |

有这些能力不代表可无条件操作：分享、创建、消息和状态变更仍遵循各自授权要求。当前没有与 `interrupt_agent` 对应的专用 thread 强制中断接口；发送消息不能被承诺为立即停止按钮。

独立任务的对话不会自动同步；本次共用的仓库文件会互相可见，因此多任务同时修改同一文件或操作暂存区有冲突风险。新 thread 本身不带来文件隔离；需要时必须显式使用隔离检出。当前轮次结束后也不应承诺自动持续监控，持续跟进需要相应执行或调度机制。

## 6. Subagent：当前任务内部的协作单元

当前可用的是运行环境提供的 `collaboration` 工具组：

| 工具 | 主要参数 | 行为 |
| --- | --- | --- |
| `spawn_agent` | `task_name`、`message`、`fork_turns` | 创建子 Agent 并分配工作；可按规则选择是否继承上下文 |
| `send_message` | `target`、`message` | 向已有子 Agent 发消息，不启动空闲 Agent 的新一轮执行 |
| `followup_task` | `target`、`message` | 分配后续工作；空闲时启动执行，运行中递送任务 |
| `list_agents` | 可选 `path_prefix` | 列出当前协作树中的 Agent 与状态 |
| `wait_agent` | 可选 `timeout_ms` | 等待协作通知 |
| `interrupt_agent` | `target` | 中断子 Agent 当前执行轮次，Agent 仍可接收后续任务 |

例如直接调用 `collaboration.spawn_agent`，参数可以是：

```json
{
  "task_name": "inspect_system_prompt",
  "message": "只读调查系统指令装配链路，返回源码位置和问题，不修改文件。",
  "fork_turns": "none"
}
```

`fork_turns` 可以选择全部、最近若干轮或不继承父上下文。Agent 通过任务路径标识，结果和消息回到协作树；并不是通过独立应用 thread ID 进行管理。

当前会话共 4 个并发槽位，包含主 Agent，通常可同时运行 3 个子 Agent；这不是永久产品上限。当前规则还要求用户或适用指令明确授权并行 Agent 工作。子 Agent 共用工作目录，不自动获得 worktree 隔离。

不能用 `collaboration.interrupt_agent` 去控制 create_thread 创建的 T13；这两类标识和生命周期属于不同工具体系。

## 7. Thread 与 Subagent 的取舍

| 维度 | 独立 thread | Subagent |
| --- | --- | --- |
| 主要目标 | 独立推进、交付和后续追踪 | 当前任务内拆分、并行和整合 |
| 管理关系 | 用户可直接打开和继续的任务 | 父任务管理的协作单元 |
| 上下文 | 精简初始说明加文档；分叉时可继承历史 | 可配置继承范围或精简启动 |
| 协调 | 读取记录、发消息、等待状态 | 消息、等待、后续任务和直接中断 |
| 审阅 | 方案、讨论和结果有独立入口 | 主 Agent 汇总子任务成果 |
| 适合粒度 | 可独立验收的优化工作包 | 有明确输入输出的调查、实现或验证步骤 |

独立 thread 的核心优势是用户可管理、交付边界明确、适合长期阶段推进；subagent 的优势是主 Agent 能紧密调度和整合内部并行工作。

两者都不天然提高智力、降低上下文成本或隔离代码。效果取决于任务边界、交接、协调与验证。建议以 thread 承载可独立交付的工作包，必要且已授权时在内部使用 subagent；不要把多 Agent 数量当作质量指标。

同一任务的实现与修复通常可留在同一会话；独立工作包使用精简交接启动新任务。上下文压缩用于保留有用工作状态，不等于删除可见聊天记录，也不能保证完整保留所有细节。关键决策与验收应持续落盘，不能只依赖会话记忆。参见 [OpenAI 对上下文管理的说明](https://developers.openai.com/blog/mastering-codex-remote-for-engineering)。

## 8. 为什么引入代码编排层

以下是从接口能力推导的设计分析，不是对内部产品决策过程的陈述。

### 8.1 单次操作：直接结构化工具更简单

如果只需要执行一个命令，直接工具接收 `{"command":"git status"}` 即可。再包一层 JavaScript 没有必然收益，却增加语法、await、输出处理和异常处理的出错机会。

模型已经能生成结构化工具调用时，不能仅以“统一成代码”为理由强制所有简单动作经过脚本。

### 8.2 多次操作：编排能减少确定性中间步骤

```javascript
// query_a/query_b 是示意方法，不是当前工具清单中的真实函数。
const results = await Promise.all([
  tools.query_a({ key: "a" }),
  tools.query_b({ key: "b" })
]);
text(results.map(result => ({
  status: result.status,
  count: result.items.length
})));
```

可能的收益包括：并行独立调用；在代码中完成确定性的条件、循环和数据传递；筛选或聚合返回值，减少进入模型上下文的数据；减少逐步重新采样模型的往返。

原生并行工具调用也能获得其中部分收益。需要模型判断的步骤不能因为有代码编排就自动省去；是否更省 token、更快或更准确必须实测。

### 8.3 成本与失效模式

| 风险 | 需要处理的设计问题 |
| --- | --- |
| 接口知识没有减少 | 提供可靠 schema／文档与按需发现；代码语法不替代参数语义 |
| 语法、await 或结果处理错误 | 明确运行环境、错误位置和生命周期，不能将未等待 Promise 当后台服务 |
| 误并发有依赖或冲突的动作 | 区分独立调用、前后依赖和共享资源副作用 |
| 部分成功后抛错 | 记录每个调用的实际结果，不盲重跑整段脚本 |
| 超时和取消层次增加 | 分清编排 cell、命令进程、远程动作，报告已取消与结果未知 |
| 输出过滤遗漏证据 | 按需压缩观察数据，保留审计和恢复所需事实 |
| 工具外层掩盖执行行为 | 权限、预算和日志仍应作用于每个底层调用，不能只审计一条 exec |

## 9. 对 Box-Agent 的设计建议

建议保留直接结构化调用与受限代码编排两种形态的选择空间，先由任务评估证明是否需要新增编排入口。本节不是立即开发该入口的计划。

1. 简单的命令、文件或单个能力调用优先使用直接工具；组合查询、大结果筛选或明确的批处理才考虑代码编排。
2. 两种入口复用同一工具注册、参数验证、权限、预算、取消和结果提交机制，避免脚本成为绕过执行契约的旁路。
3. 工具定义同时面向模型理解与机器校验：用途、参数、返回状态、副作用、可重试条件和取消语义都需要明确。
4. 长操作返回可追踪的运行标识；等待、取结果与取消围绕同一执行对象设计，避免混淆 cell_id、进程 session_id、Agent Run 和应用 thread。
5. 协作工具区分用户拥有的独立任务和父 Agent 管理的子任务；文件隔离、授权范围和结果归属都显式描述。
6. 先比较直接工具、原生并行调用和代码编排在同一任务集上的完成率、错误率、模型往返、输入输出 token、延迟与恢复表现；不要仅比较工具列表长度。

当前最高优先级仍是 [System／Tool 质量工作包](shared-runtime/LONG_TERM_PLAN.md)：先基线，再优化定义与装配。是否新增编排层、协作接口或跨语言协议，应由这些测量结果和具体需求决定。

## 10. 当前已提供完整定义的工具清单

快照日期：2026-09-28。本节的“已提供完整定义”指当前会话在发现操作之前已经可见的调用声明和用途说明，不代表产品的全局默认配置，也不表示工具实现已启动、连接可用或操作已获授权。无法从这些接口判断底层预加载、缓存和 token 计费细节。

当前清单共 68 个接口：13 个顶层接口、55 个通过 exec 调用的接口。编排注册表 `ALL_TOOLS` 查询到 97 个条目，包含其他按需发现的方法；该数值不是顶层工具数，也不是完整定义提前加载的数量。下表是功能索引，不代替执行时的完整参数契约。

### 10.1 顶层接口：13 个

| 完整工具名 | 功能 |
| --- | --- |
| `functions.exec` | 执行受限 JavaScript，编排注入的工具调用 |
| `functions.wait` | 等待已返回运行 cell ID 的 exec，获取后续输出或终止 cell |
| `functions.request_user_input` | 结构化提问并等待回答；受 Plan 模式限制 |
| `functions.request_user_input_async` | 异步提问，允许继续不依赖答案的工作 |
| `clock.sleep` | 暂停指定时长，可被新输入提前唤醒 |
| `collaboration.spawn_agent` | 创建当前任务内的子 Agent |
| `collaboration.send_message` | 向已有子 Agent 递送消息 |
| `collaboration.followup_task` | 追加任务，必要时启动空闲子 Agent 的新一轮执行 |
| `collaboration.list_agents` | 查看协作树及 Agent 状态 |
| `collaboration.wait_agent` | 等待子 Agent 消息或状态通知 |
| `collaboration.interrupt_agent` | 中断子 Agent 当前轮次 |
| `mcp__cua_repl.js` | 通过持久 JavaScript 会话使用当前启用的浏览器控制 API；本环境原生桌面控制未启用 |
| `mcp__cua_repl.js_reset` | 重置上述 JavaScript 会话及变量，不关闭浏览器或清除其状态 |

### 10.2 exec 中的基础与专项接口：14 个

本表名称均按 `tools.<方法名>(参数)` 调用。辅助函数 `text`、`image`、`store`、`load` 和注册表 `ALL_TOOLS` 不计为独立工具。

| 方法名 | 功能 |
| --- | --- |
| `exec_command` | 启动 shell 命令，返回结果或持续运行的进程会话 |
| `write_stdin` | 向进程会话写入输入，或读取后续输出 |
| `apply_patch` | 使用补丁创建、修改或删除文件 |
| `view_image` | 查看本地图片 |
| `create_goal` | 创建用户明确要求的目标 |
| `get_goal` | 查询目标状态和预算使用情况 |
| `update_goal` | 按接口条件将目标标记完成、暂停或阻塞 |
| `list_mcp_resources` | 列举 MCP 资源，不是列举函数 schema |
| `list_mcp_resource_templates` | 列举可参数化 MCP 资源模板 |
| `read_mcp_resource` | 读取已发现的 MCP 资源 |
| `request_plugin_install` | 对满足条件的明确插件请求展示安装建议 |
| `clock__curr_time` | 查询 UTC 时间 |
| `image_gen__imagegen` | 根据描述生成图像或修改输入图像 |
| `web__run` | 网页搜索、打开、查找、PDF 截图，以及天气、行情等结构化查询 |

### 10.3 exec 中的 Codex 应用接口：41 个

以下方法统一加前缀 `mcp__codex_app__`，再通过 `tools` 调用。例如 `tools.mcp__codex_app__read_thread(...)`。为便于查阅，相关方法合并列出，每个方法仍是独立接口。

| 方法名 | 功能 |
| --- | --- |
| `list_projects` | 列出可用于创建任务的项目 |
| `create_thread` | 创建用户拥有的独立任务 |
| `fork_thread` | 从已有 Codex 任务历史创建分叉 |
| `list_threads` | 列出任务、置顶项目和相关状态 |
| `list_archived_threads` | 分页列出归档任务 |
| `read_thread` | 读取一个任务的状态和近期轮次摘要 |
| `wait_threads` | 等待任务完成／需要关注，或获取即时快照 |
| `send_message_to_thread` | 向明确获授权的其他任务发送后续消息 |
| `set_thread_title` | 修改任务标题 |
| `set_thread_archived` | 归档或恢复任务 |
| `set_thread_read_state` | 标记已读或未读 |
| `share_thread` | 创建任务的不可变分享链接 |
| `navigate_to_codex_page` | 在主窗口显示指定任务 |
| `handoff_thread` | 移动其他任务及 Git 状态到目标检出或主机 |
| `get_handoff_status` | 查询或等待任务转移状态变化 |
| `list_artifacts` | 查询当前任务附带的 PR、工作树等产物 |
| `attach_artifact`、`remove_artifact` | 关联或移除 PR 附件，不修改远端 PR 本身 |
| `create_worktree` | 创建并附加受管理的 Git 工作树 |
| `get_worktree_creation_status` | 查询工作树创建进度 |
| `archive_worktree` | 保存可恢复快照并归档工作树 |
| `restore_worktree` | 恢复当前任务的已归档工作树 |
| `create_sidebar_section`、`rename_sidebar_section`、`delete_sidebar_section` | 创建、重命名和删除侧边栏分组 |
| `move_thread_to_sidebar_section`、`move_project_to_sidebar_section` | 移动或置顶任务、项目 |
| `reorder_section` | 调整分组内任务顺序 |
| `reorder_sidebar_projects` | 调整未置顶项目顺序 |
| `reorder_sidebar_sections` | 调整侧边栏分组顺序 |
| `update_sidebar_preferences` | 修改分组方式和排序偏好 |
| `open_in_codex` | 在应用面板打开文件、浏览器、终端或代码审阅 |
| `read_thread_terminal` | 读取当前任务的应用终端输出 |
| `compile_latex_document` | 编译已保存的独立 LaTeX 文档并返回诊断 |
| `load_workspace_dependencies` | 查询内置工作区依赖的运行时和库路径 |
| `automation_update` | 创建、查看、修改和删除周期任务或当前任务的后续调度 |
| `check_app_update` | 检查当前应用的可用更新，不安装或重启 |
| `get_usage_limits` | 查询当前账户的 Codex 使用限额 |
| `uninstall_plugin` | 按用户明确要求卸载插件 |
| `capture_screen_context` | 仅在当前任务的活动语音通话中读取 Codex 页面上下文 |
| `end_realtime_voice_call` | 按用户明确要求结束语音通话 |

“MCP 工具”与“延迟加载工具”不是同义词：上述 MCP 应用接口已经提供完整声明，其他 MCP 接口可以按需发现。Skills 的入口摘要与按需读取的 SKILL.md 属于另一层操作指导，不计入本表的函数数量。

## 11. 工具发现与上下文控制

### 11.1 当前机制和证据边界

工具存在于运行环境的注册表，并不要求将其完整定义全部发送给模型。当前同时存在提前提供定义、在 `ALL_TOOLS` 中查询、读取选中条目完整说明，以及特定业务提供 schema 查询入口四种情况。

当前注册表未发现名为 `tool_search`、`search_tools` 等的通用专用工具搜索接口。在 JavaScript 中使用关键词匹配是当前可确认的发现方式；不能将其描述成已确认存在的语义检索或向量索引。

文档控制提供更细的发现链路：`mcp__codex_apps__codex_document_control_list_document_sessions` 返回会话支持的工具名称和版本，再用 `mcp__codex_apps__codex_document_control_get_document_tool_schemas` 获取所选定义，最后通过该服务的执行入口调用。这些属于查询注册表后发现的能力，不纳入第 10 节的提前提供清单。

OpenAI API 另有正式的 `tool_search` 和 `defer_loading` 机制，用于按需加载函数或 MCP 工具定义；这份公开文档不能证明当前会话采用相同的底层实现。参见 [官方 Tool search 文档](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### 11.2 可复用的发现操作说明

下面是根据可观察接口整理的操作说明，可作为 Box-Agent 设计讨论的输入；不是隐藏系统提示的原文，也不是 Box-Agent 已实现的工具协议。

**输入：** 当前任务、已知工具声明、可访问的能力注册表，以及当前权限和连接状态。

**输出：** 选中的工具及完整调用契约，或说明尚缺少的能力、连接、权限或参数信息。

1. 判断当前动作需要的能力。已有完整且适用的工具定义时直接使用，不为已知接口重复搜索。
2. 缺少合适定义时，在可用注册表中按能力、服务名或动作搜索。第一次只返回候选名称和简短说明，限制结果数量。
3. 根据用途、适用条件和可用状态选择候选项。没有匹配时扩大关键词或按能力分组浏览；一次匹配失败不足以证明能力不存在。
4. 读取选中工具的完整声明。确认必填参数、类型、返回结构、前置条件、副作用和失败处理；只有简述时继续取得 schema，不猜测参数。
5. 工具集合依赖当前会话或应用版本时，先取得会话支持的名称和版本，再请求对应 schema。
6. 检查任务授权和执行条件。发现工具不授予执行权限，也不自动建立服务连接。
7. 按声明调用。只将下一步判断需要的结果输出给模型；保留必要的错误、执行标识和验证证据。
8. 遇到工具不可用或 schema 不匹配时，刷新对应定义或连接状态；存在副作用的调用先确认是否已经执行，再决定是否重试。

### 11.3 当前环境中的查询示例

第一步，在 `functions.exec` 中返回小范围候选项：

```javascript
const candidates = ALL_TOOLS
  .filter(t => /document.*schemas/.test(t.name))
  .slice(0, 5)
  .map(t => ({
    name: t.name,
    summary: t.description.slice(0, 180)
  }));
text(candidates);
```

第二步，读取已发现工具的完整说明；摘要截断只用于第一步筛选，不能用于确定参数契约：

```javascript
const selected = ALL_TOOLS.find(t => t.name ===
  "mcp__codex_apps__codex_document_control_get_document_tool_schemas"
);
if (selected) {
  text(selected.description);
} else {
  text({ found: false });
}
```

此时选中方法可能已经可调用，发现操作只是让模型获得准确的接口知识。查询失败不应通过拼接猜测的方法名尝试调用。

### 11.4 分开控制三类开销

| 开销 | 控制方式 | 代价与验收 |
| --- | --- | --- |
| 工具定义占用上下文 | 高频工具提前提供，低频工具按需发现 | 衡量检索召回、选择正确率、schema 加载量和发现延迟 |
| 模型与工具反复往返 | 对独立调用并行，对确定性步骤使用编排 | 衡量总耗时、调用成功率和部分失败恢复能力 |
| 工具结果过大 | 分页、筛选、摘要或引用外部产物 | 检查是否保留决策、验证与恢复所需的事实 |

`exec` 本身不会自动节省工具定义。注册表留在执行环境且只输出匹配结果，才能避免将整个目录重复传入上下文。已加载定义及发现记录仍会增加上下文；当前无法确认其自动淘汰策略，也不据此给出具体 token 节省比例。

对 Box-Agent 的建议是将注册表、权限过滤和定义版本管理置于宿主无关的工具层；基础能力提前提供，专业能力按需发现。模型支持原生工具搜索时可适配该机制，第三方模型可通过普通发现工具获取定义。具体加载策略需与全量定义基线比较任务完成率、首次调用成功率、token 和端到端耗时，再决定是否采用。

## 12. 本次落盘边界

本次只新增并补充讨论文档，不修改工具实现、运行时配置或开发队列，也不代表已实现上述建议。检查相对链接、文档格式、工具清单和示例边界；没有运行产品测试、模型对照或重新构建 runtime。其他任务的工作区和暂存改动不纳入本次文档操作。
