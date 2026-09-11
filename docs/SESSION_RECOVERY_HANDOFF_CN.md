# Session 重建与上下文持久化 Handoff

> 状态：设计与实施交接文档，尚未实现。
> 涉及仓库：`box-agent`、`officev3`。
> 目标宿主：officev3 本地 Agent（ACP stdio）。
> 本文描述目标契约、改动位置、迁移、验证和风险；当前源码与测试仍是已实现行为的事实来源。

## 1. 执行摘要

当前 officev3 会把一个稳定的产品会话映射到 Box-Agent 进程内的 ACP Session。ACP Session
被取消超时强拆、运行时重启、配置切换或进程异常清理后，映射随内存状态一起消失。下一次发送会
调用 `session/new`，Box-Agent 创建全新的 `Agent`，最多再从宿主提供的最近 12 条
user/assistant 文本构造语义 continuation。这不是完整恢复：Goal、工具协议、Plan/Todo、Skill、
工作流和中断状态均可能丢失。

本方案不把 ACP handle 持久化，也不引入前端可见的 epoch。它采用以下最小模型：

- officev3 的产品 `sessionId` 是稳定的逻辑 Session 身份；
- ACP `sessionId` 是可丢弃的运行时 handle；
- `turnId` 标识 Session 下的一次用户请求，并用于幂等；
- Box-Agent 按稳定产品 Session 身份持久化模型执行状态；
- ACP handle 重建时，先按当前配置创建运行时对象，再恢复持久化语义状态；
- cancel 只终止当前 Turn，不删除逻辑 Session；
- 无法恢复时明确失败，不静默创建空会话；
- 固定 12 条 continuation 只作为旧会话迁移 Adapter，不再承担正常恢复。

第一版不要求实现标准 ACP `session/load`。officev3 已经在 `session/new._meta.session_id`
传递稳定产品 Session 身份，因此新的 ACP handle 可以通过相同产品 Session 身份自动恢复。
未来若支持其他 ACP Host，可把 `session/load` 做成复用同一持久化 Module 的新 Adapter。

## 2. 要解决的问题

### 2.1 用户主动取消后的续聊

用户取消的语义应该是“停止当前请求”，而不是“删除会话”。取消完成后：

- 已完成的历史上下文仍然存在；
- 当前 Turn 标记为 `cancelled`；
- 当前 Turn 不会被自动重跑；
- 用户下一条消息仍在同一个逻辑 Session 中执行。

### 2.2 ACP handle 被强拆后的恢复

当模型或工具不及时响应取消，officev3 会丢弃当前 ACP handle 以释放前端锁。下一次发送可以
创建新的 ACP handle，但必须恢复相同逻辑 Session 的执行状态，不能只注入最近 12 条文本。

### 2.3 Box-Agent 进程重启后的恢复

运行时升级、配置变化、应用退出或崩溃都会清空 Box-Agent 内存。进程重启后必须从磁盘恢复，
而不是依赖旧进程内的 `_sessions`。

### 2.4 中断工具调用的安全性

进程可能在工具已经产生副作用、但工具结果尚未写入消息历史时终止。恢复后不能盲目重跑未知
状态的副作用工具，否则可能重复写文件、发送消息、创建外部资源或执行交易。

### 2.5 重试幂等

宿主、IPC 或传输层可能重试同一个 Turn。相同 `turnId` 不能重复追加用户消息、重复调用模型或
重复执行工具。

### 2.6 生命周期和隐私

持久化状态必须随用户明确删除会话而删除。cancel、强拆、进程退出和普通缓存淘汰均不得删除
逻辑 Session；业务会话删除则必须清理执行快照和关联运行时文件。

## 3. 当前行为与已确认根因

### 3.1 Box-Agent Session 只在内存中

`box_agent/acp/__init__.py` 中的 `_sessions` 是进程内字典。`newSession()` 每次生成新的 ACP
Session ID，并创建新的 `Agent`。初始化能力当前声明 `loadSession=False`。

结果是：只要 ACP 进程退出或内存映射被清理，完整 Agent 状态就不可恢复。

### 3.2 officev3 会主动清理运行时映射

`electron/main/boxAgentManager.ts` 中维护：

```text
officeSessions: product session id -> OfficeSessionState
acpToOffice: ACP session id -> product session id
```

取消等待超时、运行时 dispose、进程失败和 renderer 销毁等路径会清理这些映射。清理本身是合理
的运行时资源释放；问题在于系统把这些缓存误当成了唯一会话状态来源。

### 3.3 当前 continuation 不是完整恢复

officev3 的 `src/lib/agent/localAgentContinuation.ts` 与 Box-Agent 的
`box_agent/session_continuation.py` 只传递有界的 user/assistant 语义文本：

- 最多 12 条消息；
- 单条最多 12,000 字符；
- 总计最多 48,000 字符；
- 不包含工具调用和工具结果；
- 不包含 Goal、Plan、Todo、Skill 和 ACP 运行状态。

Box-Agent 的 `Agent.seed_continuation_messages()` 也明确只向新 Agent 注入语义消息，不恢复工具
协议状态。

### 3.4 取消超时会放大重建概率

officev3 的取消等待上限为 8 秒；Box-Agent 静默 provider 流的活动心跳为 15 秒，取消检查位于
流消费点。因此 provider 在一段时间内不返回 chunk 时，普通取消也可能被宿主误判为卡死，进而
强拆 ACP handle。

### 3.5 前端存在重复 cancel 调用

`src/lib/agent/localAgentStream.ts` 的 abort handler 会调用本地 Agent cancel；
`src/components/AI/hooks/useChatSession.ts` 的 `handleCancel` 在触发 abort 后又调用一次 cancel。
这不是上下文丢失的唯一根因，但会增加取消竞态和诊断噪音。

### 3.6 未知 ACP handle 会被静默替换

Box-Agent 当前在 `prompt()` 找不到 ACP Session 时，会自动调用 `newSession()` 并使用新 ID。
该路径没有稳定产品 Session 元数据，无法正确恢复原会话，而且会掩盖状态丢失。目标实现必须改为
明确返回 `ACP_SESSION_NOT_FOUND`。

## 4. 第一性原理与目标不变量

### 4.1 最小概念

| 概念 | 含义 | 是否持久化 | 是否对宿主可见 |
| --- | --- | --- | --- |
| 逻辑 Session | 一个产品会话的长期执行上下文 | 是 | 是，使用产品 `sessionId` |
| ACP handle | 当前 ACP 连接中的运行时对象句柄 | 否 | 是，ACP `sessionId` |
| Turn | Session 下的一次用户请求 | 是 | 是，使用 `turnId` |
| generation | 防止旧实例迟到写回的新旧代数 | 仅内部需要 | 否 |

`generation` 不是新的协议 ID。当前 officev3 已经通过对象身份守卫忽略迟到的旧请求；目标实现
只需把这一思想延伸到持久化 checkpoint，防止旧 ACP handle 在新 handle 建立后覆盖新状态。

### 4.2 必须满足的不变量

1. 稳定产品 `sessionId` 在 ACP handle、进程和配置重建之间不变。
2. 一个 Session 可以包含多个 Turn；一个 Turn 只能有一个终态。
3. 相同 `turnId` 不得重复产生模型调用或外部副作用。
4. cancel 终止 Turn，不删除 Session。
5. 进程内对象和 Map 都是缓存，不是持久化事实来源。
6. 当前运行时配置、system prompt、权限和工具实例必须重新构造，不能从快照反序列化。
7. 进入模型历史的 assistant tool call 必须有匹配的 tool result。
8. 状态未知的副作用工具不得自动重跑。
9. 恢复失败必须显式可见，不能静默切换为空会话。
10. 用户删除业务会话后，持久化执行状态最终必须删除。

## 5. 设计决策

### 5.1 使用产品 Session ID 作为恢复键

officev3 已在 `session/new._meta.session_id` 传递稳定的产品 `sessionId`，Box-Agent 已保存为
`SessionState.upstream_session_id`。目标实现直接以该值作为逻辑 Session 键；内部文件名使用
安全规范化或 hash，避免路径穿越和跨用户冲突。

如果一个 Box-Agent 数据目录可能被多个用户或多个 Host 共用，内部键应至少包含：

```text
hash(client identity + product session id)
```

第一版不增加独立 `agentSessionId`。

### 5.2 ACP handle 保持临时

ACP `session/new` 仍可返回 `sess-N-xxxx` 一类当前连接内 handle。强拆或进程重启后 handle 可以
变化；只要新的 `session/new` 带有相同稳定产品 Session ID，Box-Agent 就恢复同一个逻辑 Session。

### 5.3 统一恢复，不设正常恢复等级

正常 Interface 只有以下结果：

```text
成功恢复
不存在（新 Session 或旧版本待迁移）
快照损坏
schema 不兼容
当前 writer 已失效
```

“重新构造 system prompt、工具和 Skill，再注入语义状态”是所有恢复都会执行的标准过程，不是
一个恢复等级。语义 continuation 是无快照旧会话的一次性迁移，不是正常运行时自动降级。

### 5.4 Box-Agent 执行快照是模型执行状态的权威来源

officev3 SQLite 继续是用户可见聊天记录的权威来源；Box-Agent 快照是模型执行状态的权威来源。
两者不是同一个数据模型：

- officev3 保存 UI stage、可见 partial、文件行和展示状态；
- Box-Agent 保存 provider-neutral 的模型消息、Goal、工具协议和执行状态。

恢复时不从 officev3 UI transcript 重建正常执行状态。只有旧会话迁移时才使用 continuation。

### 5.5 持久化逻辑必须是深 Module

新增 `box_agent/session_state.py`，将 schema、校验、原子 I/O、revision、generation fencing、
消息清洗、状态捕获和恢复放在一个 Module 内。ACP 是 Adapter，只在稳定 Seam 上调用：

```python
load(session_id) -> SessionSnapshot | None
save(session_id, snapshot, *, expected_generation) -> int
delete(session_id) -> None
```

测试通过同一 Interface 使用临时目录 Adapter，不为测试暴露额外生产接口。

### 5.6 第一版采用原子 JSON

建议路径：

```text
~/.box-agent/sessions/<safe-session-key>/session-state.json
```

要求：

- schema version；
- payload SHA256；
- 临时文件独占创建；
- `flush + fsync + os.replace`；
- 目录权限 `0700`，文件权限 `0600`；
- 单进程内每个 Session 串行保存；
- 新 ACP handle 激活后，旧 generation 的保存请求被拒绝。

第一版假设 officev3 对一个用户只维护一个 Box-Agent 进程。若未来允许多个进程同时写同一数据
目录，应在不改变 Interface 的前提下切换到支持跨进程事务/锁的 Implementation，例如 SQLite。

## 6. 目标结构

```text
officev3 产品 sessionId（稳定）
        |
        | session/new._meta.session_id
        v
Box-Agent ACP Adapter
        |
        | load/save/delete
        v
Durable Session Module --------------------+
        |                                   |
        | 恢复模型执行状态                    | 原子 checkpoint
        v                                   v
当前 Agent + 当前工具 + 当前 system prompt   ~/.box-agent/sessions/.../session-state.json
        |
        | session/prompt._meta.turn_id
        v
Turn: running -> completed/cancelled/interrupted
```

ACP handle 只指向当前内存 `SessionState`。它消失时，持久化逻辑 Session 不受影响。

## 7. 持久化数据模型

建议 schema v1 的逻辑形状如下。实际字段命名可调整，但语义和校验要求不可省略。

```json
{
  "schema_version": 1,
  "session_id": "office-session-id",
  "revision": 17,
  "generation": 3,
  "created_at": "2026-08-26T10:00:00Z",
  "updated_at": "2026-08-26T10:05:00Z",
  "runtime_compatibility": {
    "workspace_identity": "...",
    "working_directory": "/absolute/session/cwd",
    "session_mode": "general"
  },
  "messages": [],
  "goal": null,
  "active_skills": [
    {"name": "skill-name", "sha256": "..."}
  ],
  "stateful_tools": {
    "plan": {},
    "todo": {}
  },
  "session_state": {
    "turn_counter": 4,
    "current_task_id": "task-1",
    "source_text": "...",
    "pending_plan_approval": null,
    "pending_completion_gate": null,
    "waiting_for_user_input": false,
    "last_checkpoint": null
  },
  "current_turn": {
    "turn_id": "local_...",
    "status": "completed",
    "partial_assistant_text": "",
    "stop_reason": "end_turn",
    "started_at": "...",
    "updated_at": "...",
    "tool_calls": []
  },
  "recent_terminal_turns": [],
  "payload_sha256": "..."
}
```

恢复旧记录时沿用该会话原有 cwd；历史 `artifact_mode`、`artifact_root_dir`
和 `session_workspace_dir` 只作为未知/废弃字段忽略，不得重新解释输出目录或
移动已有文件。

### 7.1 Messages

保存 `Agent.messages` 中除 system message 之外的 provider-neutral `Message`：

- user；
- assistant；
- tool；
- assistant thinking（如果当前策略允许其进入持久历史）；
- tool calls、tool call ID、usage 和 `request_only_input_tokens`。

不得保存：

- 当前 system prompt；
- `trace_redact_content=True` 的 request-only 内容；
- 原始图片字节或 data URL；
- 临时权限文本和运行时密钥；
- 仅为一次 provider 请求构造的覆盖层。

恢复时始终保留新 Agent 当前生成的 system message，并把快照中的非 system 消息追加到其后。

### 7.2 Goal

复用 `agent.py` 中现有 `goal_payload()` 和 `restore_goal()`。Goal 的 objective、status、progress、
evidence、blocked reason 和 completion metadata 均应恢复。

### 7.3 Active Skills

只保存 Skill 名称、加载顺序和内容 hash，不把旧 Skill 指令文本当作可执行配置反序列化。恢复时：

1. 使用当前 SkillLoader 按名称重新加载；
2. 重新计算 hash；
3. 一致则激活；
4. 不存在或发生变化时记录明确诊断，并使用当前可信版本或拒绝不兼容恢复。

### 7.4 有状态工具

PlanStore、TodoStore 等工具通过统一内部 Interface 暴露：

```python
export_session_state() -> dict
restore_session_state(payload: object) -> None
```

Durable Session Module 只处理实现该 Interface 的工具，不针对工具名称维护分支。运行时连接类工具
不实现该 Interface，恢复时重新创建。

### 7.5 Turn 状态

允许的持久状态：

```text
running
completed
cancel_requested
cancelled
interrupted
error
```

加载快照时，如果最后状态为 `running` 或 `cancel_requested`，说明上一个 writer 没有正常落终态，
应转换为 `interrupted`，不得直接继续执行该 Turn。

`recent_terminal_turns` 保存有界的最近 Turn 幂等收据，至少包含 `turn_id`、终态、stop reason 和
终态 revision。相同 `turnId` 再次到达时返回已有终态，不重新执行。

## 8. Checkpoint 时机

### 8.1 Turn 开始

ACP 完成输入校验并调用 `state.agent.add_user_message(user_text)` 后，执行第一次 checkpoint：

```text
current_turn.status = running
保存用户消息
保存 turn_id/task_id
revision + 1
```

在写入用户消息前必须先检查 `turnId` 幂等收据，防止重复追加。

### 8.2 Assistant/工具安全边界

ACP 已在 `_run_turn()` 中消费共享 Agent events。以下事件发生后保存：

- `ToolCallStartEvent`：保存 assistant tool call 意图；
- `ToolCallResultEvent`：保存匹配 tool result；
- `ContextCheckpointEvent`：保存工作流 checkpoint；
- Goal、Plan、Todo 或 active Skill 状态发生变化；
- `DoneEvent`：保存 Turn 终态。

### 8.3 流式 partial

流式文本不应每 token 写盘。ACP 可以在内存累计当前 Turn 的 partial，并按时间或字符阈值刷新，
例如每秒或每 4 KiB。partial 单独保存在 `current_turn.partial_assistant_text`，不能伪装成已经完成的
assistant message。

用户主动取消后，partial 可用于 UI/诊断，但下一轮模型上下文不能把它当作完整答案。进程异常时，
partial 与 `interrupted` 状态一起保留。

对模型流在输出部分内容后发生的暂时性连接中断，共享执行循环在步数预算内最多自动恢复
一次（每个 Turn 合计一次）。恢复请求保留已完成的工具结果与残文，明确要求从未完成的
动作继续，不重播已有文本或重新执行成功的工具；中断响应中的不完整工具调用不执行。
用户取消优先于恢复。再次中断或已无剩余步数时，保留 `interrupted` 终态并提示任务未完成。
ACP 的协议字段仍使用有效的 `end_turn`，但 `_meta` 返回 `ok=false`、`completed=false`、
`runStatus=error` 和 `lastStopReason=interrupted`，任务登记也记录为失败。

### 8.4 保存失败

保存失败不能被当成普通日志后继续承诺“可恢复”。至少应：

- 记录结构化错误；
- 在 prompt response `_meta` 标记持久化失败；
- 对即将执行有副作用工具但尚未保存调用意图的情况 fail closed；
- 不覆盖最后一份有效快照。

## 9. 工具调用安全

### 9.1 协议完整性

恢复前验证消息历史。任何 assistant tool call 必须存在相同 ID 的 tool result。若进程在 tool result
写入前终止，恢复逻辑为缺失结果生成受控的 interrupted stub，保持 provider 协议合法，并同时把
调用标记为 `unknown`。

现有 `core.py` 已有 dangling tool call 清理逻辑，但 Durable Session Module 不能依赖私有 helper
的偶然行为；应提取或实现稳定、可直接测试的恢复校验。

### 9.2 副作用分类

恢复时按保守策略处理：

| 状态 | 行为 |
| --- | --- |
| tool result 已持久化 | 直接恢复，不重跑 |
| 调用未开始 | 不自动执行，等待新 Turn |
| 只读工具，状态 unknown | 第一版仍不自动续跑；以后可显式加入安全重试策略 |
| 有副作用工具，状态 unknown | 禁止自动重跑；检查 artifact/workspace 或等待用户决定 |

第一版的简单且安全策略是：任何异常终止的 Turn 都不自动续跑。恢复 Session 后，由用户下一条消息
决定是否继续；模型会看到受控 interrupted receipt，而不是重复执行旧工具。

## 10. 生命周期流程

### 10.1 首次创建

```text
officev3 session/new（携带稳定 product session_id）
  -> Box-Agent 创建当前运行时 Agent
  -> 快照不存在
  -> 返回 resumed=false
  -> 第一个 prompt 正常运行并生成正式快照
```

### 10.2 正常下一轮

```text
officev3 复用 ACP handle
  -> prompt 携带新 turn_id
  -> Box-Agent 校验幂等
  -> checkpoint running
  -> 执行
  -> checkpoint completed/error/cancelled
```

### 10.3 用户主动取消

```text
session/cancel
  -> 设置当前 Turn 的取消信号
  -> checkpoint cancel_requested
  -> 执行循环尽快退出
  -> checkpoint cancelled
  -> 保留 ACP handle（若运行时健康）
  -> 下一条消息使用同一逻辑 Session
```

不得自动重跑被取消 Turn。

### 10.4 取消超时强拆

```text
officev3 等待超时
  -> 只删除 live ACP 映射
  -> 下一次发送调用 session/new
  -> 携带相同产品 session_id
  -> Box-Agent 激活新 generation
  -> 旧 handle 的迟到 checkpoint 被拒绝
  -> 加载最后有效快照
  -> 未正常结束的 Turn 标记 interrupted
```

### 10.5 Box-Agent 进程重启

```text
旧进程退出，内存缓存消失
  -> 新进程收到 session/new
  -> 重新构造当前 LLM/工具/system prompt/权限
  -> 加载稳定产品 session_id 的快照
  -> 返回 resumed=true + revision
```

### 10.6 配置、模型或 workspace 变化

配置变化可以创建新的 ACP handle，但仍恢复同一逻辑 Session。当前宿主配置优先，旧快照中的运行时
配置只用于兼容性校验和诊断。不得从快照恢复旧权限、旧连接或旧 system prompt。

若 workspace identity 变化导致工具结果路径或工作流 checkpoint 无法安全解释，应返回明确的
`SESSION_INCOMPATIBLE` 或将相关状态标记为不可用；不能在新 workspace 静默执行旧副作用。

### 10.7 未知 ACP handle

`session/prompt` 找不到当前 ACP handle 时返回 `ACP_SESSION_NOT_FOUND`。宿主重新调用
`session/new` 并携带稳定产品 Session ID。Box-Agent 不在 prompt 路径自动创建空 Session。

### 10.8 损坏或不兼容快照

```text
checksum 错误 -> SESSION_CORRUPTED
schema 不支持 -> SESSION_INCOMPATIBLE
```

不得把损坏文件覆盖成新空快照，也不得静默回退 continuation。可保留原文件用于诊断，并向宿主
提供明确错误和人工恢复选项。

### 10.9 用户明确新建会话

officev3 创建新的产品 `sessionId`。由于恢复键变化，Box-Agent 创建全新逻辑 Session。无需额外
`forceFresh` 标志。

### 10.10 用户删除会话

officev3 删除本地会话记录时调用 Box-Agent 的受控 `_session/purge` 扩展方法。Box-Agent：

- 删除持久化 snapshot；
- 删除该 Session 的 tool-results 等执行状态；
- 清理相关 live ACP handle；
- 不删除用户 project workspace 或产物，除非现有产品删除契约另有明确规定。

如果 Box-Agent 当时不可用，officev3 必须记录 pending deletion，并在运行时恢复后重试。恢复功能
本身不需要修改现有 `sessions` 表；可靠删除队列可以增加独立 tombstone 表。

## 11. 取消链路修复

### 11.1 Box-Agent

`core._stream_with_activity()` 应同时等待 provider 下一 chunk、取消信号和原有活动心跳。取消轮询
应明显短于 officev3 的 8 秒超时，但不能通过把 15 秒活动 heartbeat 降到高频来实现，否则会
制造无意义事件。

建议由 `asyncio.Event` 触发取消，并在等待 provider `__anext__()` 时与该 Event 竞争。取消后关闭
provider stream，并通过现有 `DoneEvent(CANCELLED)` 落终态。

### 11.2 officev3

stream abort 和 UI 停止按钮必须共享同一个 cancellation promise，确保只发一次 IPC cancel，
同时允许 UI 等待主进程确认 Turn 已停止。不能简单删除 abort handler，因为页面销毁和会话切换也
可能通过 abort 触发后端取消。

8 秒超时继续作为卡死工具的最后保护，但正常静默 provider cancel 应远早于该阈值完成。

## 12. ACP/Host 契约变化

### 12.1 `session/new` 入站

沿用现有：

```json
{
  "cwd": "...",
  "mcpServers": [],
  "_meta": {
    "session_id": "stable-office-session-id"
  }
}
```

`_meta.session_id` 是恢复所需的稳定逻辑身份。utility/one-shot Session 不应参与持久恢复，除非产品
以后明确要求。

### 12.2 `session/new` 出站

在现有 `_meta` 中增加 additive 字段：

```json
{
  "resumed": true,
  "session_revision": 17
}
```

`resumed=false` 只表示没有找到已有快照，不代表恢复失败。损坏和不兼容必须返回明确错误，而不是
`resumed=false`。

### 12.3 `session/prompt`

沿用现有 `_meta.turn_id`。宿主生成的 `turnId` 在一次请求的所有重试中必须保持不变。

### 12.4 删除扩展

增加受控扩展方法，例如：

```text
_session/purge { session_id }
```

该方法只用于业务删除，不用于 cancel、dispose 或强拆。

### 12.5 `session/load`

第一版保持 `AgentCapabilities(loadSession=False)`。未来实现标准 ACP load 时，必须复用同一个
Durable Session Module，不能维护第二份恢复逻辑。

## 13. 兼容与迁移

### 13.1 已有但尚无快照的会话

部署后的旧会话首次重建时：

1. `session/new` 返回 `resumed=false`；
2. officev3 继续发送现有 `session_continuation/v1`；
3. Box-Agent 只在新 Agent 且没有快照时应用 continuation；
4. 应用后立即或在 Turn start 时写入正式 snapshot；
5. 后续恢复走 snapshot，不再依赖 continuation。

### 13.2 已成功恢复的会话

officev3 看到 `resumed=true` 后不再发送 continuation，避免把 UI 历史重复注入模型上下文。

### 13.3 continuation 保留范围

以下文件继续保留，但定位调整为迁移 Adapter：

- officev3 `src/lib/agent/localAgentContinuation.ts`；
- Box-Agent `box_agent/session_continuation.py`。

### 13.4 回滚

回滚旧运行时时，新版本快照不会被旧代码读取。旧代码仍按当前 continuation 行为运行。快照 schema
必须使用新文件名并保持未知文件无害；回滚不得删除用户快照。再次升级后继续恢复。

## 14. Box-Agent 改动位置

### 14.1 新增文件

| 文件 | 改动 |
| --- | --- |
| `box_agent/session_state.py` | Durable Session Module：schema、原子 I/O、校验、revision、generation、capture/restore/delete |
| `tests/test_session_state.py` | Module Interface 的持久化、损坏、迁移、安全和删除测试 |

### 14.2 修改文件

| 文件 | 改动 |
| --- | --- |
| `box_agent/acp/__init__.py` | 注入 store；`newSession` 恢复；prompt 幂等/checkpoint；cancel 状态；删除 prompt auto-create；`resumed` metadata；purge 扩展 |
| `box_agent/agent.py` | 增加受控 `restore_history()`；复用 Goal 恢复；确保当前 system prompt 保留 |
| `box_agent/tools/plan_tool.py` | PlanStore 导出/恢复 Session 状态 |
| `box_agent/tools/todo_tool.py` | TodoStore 导出/恢复 Session 状态并保持现有校验 |
| `box_agent/tools/setup.py` | 保持 Plan/Todo Store 可被 Agent Session 捕获；不复制持久化策略 |
| `box_agent/core.py` | provider 静默期间可立即响应取消；必要时提取稳定的 dangling tool history 校验 |
| `tests/test_acp.py` | ACP 重建、取消、幂等、metadata、未知 handle、迁移和 purge 回归 |
| `tests/test_core.py` | 静默 provider 快速取消及协议完整性回归 |
| `tests/test_session_continuation.py` | continuation 只作为无快照迁移输入的兼容测试 |

不应把完整持久化策略直接写进 `acp/__init__.py`，也不应把 officev3 产品字段写入 `core.py`。

## 15. officev3 改动位置

| 文件 | 改动 |
| --- | --- |
| `electron/main/boxAgentManager.ts` | 读取 `resumed/session_revision`；保留稳定 `session_id`；强拆只丢 live handle；增加 purge；更新日志 |
| `src/lib/agent/localAgentStream.ts` | `resumed=true` 时不构造 continuation；统一 stream cancel promise |
| `src/components/AI/hooks/useChatSession.ts` | UI 停止复用 stream cancellation promise，不再次发送 cancel IPC |
| `electron/main/localApi.ts` | 删除本地 Session 时触发 Box-Agent purge；失败时记录 pending deletion |
| `electron/main/localChatStorage/schema.ts` | 仅在实现可靠 pending deletion queue 时增加 tombstone 表；恢复本身不修改 Session schema |
| `electron/main/boxAgentManager.spec.ts` | 强拆后新 handle、稳定产品 ID、resumed metadata、Map 重建、单次 cancel |
| `src/lib/agent/localAgentStream.spec.ts` | resumed 时跳过 continuation、未恢复时保留迁移、取消只调用一次 |
| `src/lib/agent/localAgentContinuation.spec.ts` | 保留旧会话迁移兼容测试 |
| `electron/main/localApi.spec.ts` | 业务删除触发 purge 和 pending deletion 重试 |

officev3 不需要新增 `agentSessionId`，也不需要持久化 ACP handle。现有 SQLite `sessions.id` 和
消息 `turn_id/run_id/external_idempotency_key` 已足以承载产品身份与 UI 幂等。

## 16. 实施顺序

### 阶段 A：持久化 Module 与回合边界恢复

1. 新增 `session_state.py` 和直接测试；
2. 增加 Agent history、Plan、Todo 的受控恢复；
3. ACP `newSession` 按 `upstream_session_id` 加载；
4. Turn start、tool result 和 Done 后 checkpoint；
5. 未知 ACP handle 明确失败；
6. 添加 `resumed/revision` metadata；
7. 运行 Box-Agent focused tests。

阶段 A 完成后，正常取消结束、ACP handle 重建和进程重启后的已完成上下文应可恢复。

### 阶段 B：中断安全与取消时延

1. provider 静默流和 cancel event 竞争；
2. ToolCallStart 时保存调用意图；
3. dangling tool call 恢复为 interrupted receipt；
4. generation fencing 拒绝旧 ACP handle 的迟到保存；
5. partial assistant 有界刷新；
6. officev3 统一 cancel 所有者；
7. 运行 Core/ACP/officev3 取消测试与真实静默流 probe。

### 阶段 C：迁移、删除和宿主收尾

1. officev3 根据 `resumed` 决定是否发送 continuation；
2. continuation 首次迁移后生成正式快照；
3. 实现 `_session/purge`；
4. 删除本地 Session 时调用 purge；
5. 实现 pending deletion 重试或等价的可靠清理；
6. 完成跨仓库集成测试和打包运行时验证。

建议三个阶段分别形成可审查的提交或 PR，避免持久化、取消和删除生命周期混在一个巨大 diff 中。

## 17. 测试矩阵

### 17.1 Durable Session Module

- 新 Session 不存在快照；
- 保存后完整加载；
- 原子替换不会留下半文件；
- checksum 损坏明确失败；
- schema 不兼容明确失败；
- system prompt 不从快照恢复；
- request-only 图片/脱敏内容不落盘；
- Goal、Plan、Todo、Skill 名称和 Session 状态恢复；
- 旧 generation 不能覆盖新 generation；
- delete 幂等并只删除 Session 自有状态。

### 17.2 ACP

- 同一产品 Session 创建两个不同 ACP handle，第二个恢复第一个上下文；
- 不同产品 Session 不共享上下文；
- 正常 cancel 后下一 Turn 保留历史；
- 强拆后重建保留历史；
- 进程重建模拟后恢复 Goal、Plan、Todo 和工具结果；
- 相同 `turnId` 重试不重复模型/工具调用；
- unknown ACP handle 不自动新建；
- resumed Session 不应用 continuation；
- 无快照 Session 可应用一次 continuation 并转成快照；
- corrupted/incompatible snapshot 不静默新建；
- purge 清理快照和 live handle。

### 17.3 Core/工具安全

- provider 永不产出新 chunk 时，cancel 在 officev3 超时前完成；
- cancel 后 provider stream 被关闭；
- tool call 后、tool result 前中断，恢复历史仍满足 provider 协议；
- unknown 副作用工具不会自动重跑；
- Plan/Todo 恢复后 read 与中断前一致；
- 大工具结果引用在恢复后仍可读取。

### 17.4 officev3

- `session/new` 始终携带稳定产品 `session_id`；
- `session/prompt` 携带稳定 `turn_id`；
- `resumed=true` 不发送 continuation；
- `resumed=false` 允许旧会话迁移；
- 强拆删除 live Map，下一次 send 自动重建 handle；
- abort/UI stop 只发一次 cancel；
- 删除会话触发 purge；
- Box-Agent 不可用时删除请求进入可靠重试队列。

### 17.5 故障注入

至少在以下位置强制终止并恢复：

1. 用户消息持久化之前；
2. 用户消息持久化之后、模型调用之前；
3. assistant tool call 持久化之后、工具执行之前；
4. 工具产生副作用之后、tool result 持久化之前；
5. tool result 持久化之后、下一次模型调用之前；
6. 流式 partial 生成期间；
7. Turn terminal checkpoint 期间；
8. 新 ACP handle 激活后旧 handle 迟到返回。

每个测试都要证明：快照不损坏、不会重复副作用、下一 Turn 行为明确。

## 18. 验收标准

以下全部满足才算完成：

1. 用户正常取消后，下一条消息仍使用同一逻辑 Session 和完整已完成上下文。
2. officev3 强拆 ACP handle 后，新 handle 能恢复消息、Goal、Plan、Todo 和已完成工具结果。
3. Box-Agent 进程重启后恢复行为与 handle 强拆一致。
4. 相同 `turnId` 不会导致第二次模型调用或副作用工具执行。
5. 异常中断的工具调用不会形成非法 provider 消息序列。
6. 状态未知的副作用工具不会自动重跑。
7. 损坏或不兼容快照返回明确错误，不静默开新会话。
8. 已恢复 Session 不再注入 12 条 continuation；旧会话仍可一次性迁移。
9. 正常静默 provider cancel 在 officev3 8 秒强拆阈值前完成。
10. UI 停止动作只发送一次 cancel IPC。
11. 用户删除业务会话后，Box-Agent 执行状态最终被清除。
12. stdout 仍只输出 ACP JSON-RPC，持久化诊断走 stderr/log。

## 19. 验证和运行时交付

源码验证至少包括：

```bash
uv run pytest tests/test_session_state.py -v
uv run pytest tests/test_acp.py -v
uv run pytest tests/test_core.py -v
```

根据实际改动范围补充 Plan/Todo、workflow checkpoint、tool result storage 和 full suite。

officev3 运行对应 Vitest focused tests，并做一个真实 stdio ACP probe：

```text
创建 Session -> 完成一轮 -> 取消静默流 -> 强拆 handle
-> 新 handle 使用同一产品 sessionId -> 下一轮验证上下文
```

运行时交付必须分开报告：

```text
source changed
-> source tests passed
-> runtime built
-> runtime installed into officev3 development environment
-> ACP probe passed
-> officev3 restarted
-> fresh live task verified
```

源码测试不能替代打包、安装、宿主重启和真实任务验证。

## 20. 安全、隐私和保留策略

- Session ID 只能用于安全 hash/规范化后的路径段；禁止直接拼接未经验证的用户输入。
- snapshot 不得包含 token、API key、权限 promise、原始图片字节或 request-only overlay。
- 文件权限默认 `0600`，目录 `0700`。
- 日志只记录 session hash、revision、状态和错误，不打印完整消息或工具结果。
- purge 只能删除 Box-Agent 自有 Session 状态，不递归删除 workspace/project root。
- 需要定义孤儿 snapshot 的保留/清理策略；在策略明确前不能把自动 TTL 当作业务删除替代品。
- schema 迁移必须保留原快照直到新快照原子写入成功。

## 21. 主要风险和缓解

| 风险 | 缓解 |
| --- | --- |
| 旧 handle 迟到覆盖新状态 | generation fencing；每次保存校验当前 writer |
| 快照包含不应持久化的数据 | allowlist serializer；redacted/request-only 回归测试 |
| tool call/result 不完整 | 恢复校验和 interrupted stub；禁止自动续跑旧 Turn |
| 相同 turn 重复副作用 | 持久化 Turn 收据；执行前幂等检查 |
| JSON 全量重写影响性能 | 先测真实 Session 大小；Interface 保持不变，必要时换 SQLite |
| Skill 更新后旧指令不一致 | 只保存名称/hash；重新加载当前可信 Skill 并显式诊断 |
| workspace 变化导致旧路径失效 | 当前 host 配置优先；workspace identity 校验；fail closed |
| 删除时 Box-Agent 不在线 | officev3 pending deletion tombstone 和重试 |
| continuation 与快照重复注入 | `resumed=true` 时宿主不发送；Box-Agent 恢复后 turn_counter 非零 |
| 取消仍超过宿主超时 | cancel event 与 provider stream 竞争；真实静默流 probe |

## 22. 非目标

第一版不包括：

- 把 ACP handle 设计成全局永久 ID；
- 对外暴露 epoch/generation；
- 正常恢复时在多个“恢复等级”间静默降级；
- 序列化 MCP/浏览器/Jupyter/asyncio 等运行时对象；
- 自动续跑异常中断的旧 Turn；
- 从 UI transcript 完整重建 Box-Agent 工具协议；
- 同时支持多个 Box-Agent 进程写同一 Session 数据目录；
- 修改用户 workspace/project 产物的删除语义。

## 23. 实施者快速检查清单

- [ ] 使用稳定 `upstream_session_id`，不使用 ACP handle 作为磁盘恢复键。
- [ ] 当前 system prompt 和 runtime 配置重新构造，不从快照覆盖。
- [ ] Turn start 在副作用发生前持久化。
- [ ] ToolCallStart 和 ToolCallResult 都有 checkpoint。
- [ ] 相同 `turnId` 在执行前去重。
- [ ] cancel 只结束 Turn。
- [ ] unknown ACP handle 明确失败。
- [ ] 旧 generation 无法写入。
- [ ] snapshot 损坏不被新空状态覆盖。
- [ ] resumed Session 不注入 continuation。
- [ ] 业务删除调用 purge，并有离线重试。
- [ ] focused tests、full suite 和真实 ACP/officev3 probe 分别记录。

## 24. 参考实现位置

Box-Agent：

- `box_agent/acp/__init__.py`：SessionState、newSession、prompt、cancel、Agent event Adapter；
- `box_agent/agent.py`：Agent messages、Goal、active Skill 和运行入口；
- `box_agent/schema/schema.py`：provider-neutral Message schema；
- `box_agent/core.py`：共享取消和 tool history 不变量；
- `box_agent/workflow_owner_store.py`：稳定产品 Session 下的原子 JSON 模式参考；
- `box_agent/workflow_checkpoint_store.py`：schema/hash/原子 checkpoint 模式参考；
- `box_agent/tool_result_storage.py`：Session 级大工具结果引用；
- `box_agent/session_continuation.py`：旧会话语义迁移 Adapter。

officev3：

- `electron/main/boxAgentManager.ts`：产品 Session 与 ACP handle 的运行时映射、强拆和 `session/new`；
- `src/lib/agent/localAgentStream.ts`：turnId、continuation 和 abort cancel；
- `src/components/AI/hooks/useChatSession.ts`：UI 停止行为；
- `electron/main/localChatStorage/schema.ts`：产品 Session/UI 消息持久化；
- `electron/main/localApi.ts`：本地 Session 删除；
- `src/lib/agent/localAgentContinuation.ts`：最近 12 条语义迁移。

## 25. 代码图谱说明

本次分析使用 `.understand-anything/knowledge-graph.json` 作为导航索引，但其基线 commit
`bb52addbae8a77a0e032a4f8f9d2c6ecedaae500` 早于当前源码 commit
`e4167a98734ec2abf466510ad94f414dc2b17113`。本文的现状结论已通过当前源码直接核对；图谱不作为
实现真相。若后续改动范围继续扩大，建议在代码落地后统一刷新 graph、meta 和 fingerprints。
