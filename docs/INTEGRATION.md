# Box-Agent ACP 对接索引

本目录下与 ACP 宿主对接相关的协议文档总索引。这些扩展从 Box-Agent 0.8.26
开始逐步加入；每份文档中的兼容性小节才是对应能力的版本边界。CLI 不消费 ACP
wire 格式，但底层共享 core/tool 行为仍可能一致。

> ACP 入口：`box-agent-acp` 走 stdio JSON-RPC。
> 公共扩展点：`session/new._meta`（一次性配置）、`session/prompt._meta`（每轮可变）、`update_tool_call.rawOutput`（结构化产物）。

---

## 协议清单

| 协议                  | 方向        | 入口                                  | 文档                                                             | 用途                                                                              |
| --------------------- | ----------- | ------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Action Hint**       | 后端 → 前端 | 模型 markdown 围栏块                  | [ACTION_HINT_PROTOCOL.md](./ACTION_HINT_PROTOCOL.md)             | 模型在合适场景输出 `action_hint` 块，前端解析为可点击设置入口                     |
| **Memory Match**      | 后端 → 前端 | `update_tool_call.rawOutput`          | [MEMORY_MATCH_PROTOCOL.md](./MEMORY_MATCH_PROTOCOL.md)           | 宿主展示本轮 `memory_search` 或后端自动匹配到的上下文记忆                         |
| **Memory Proposal**   | 双向        | `session/memory_proposal` + extension methods | [MEMORY_PROPOSAL_PROTOCOL.md](./MEMORY_PROPOSAL_PROTOCOL.md) | 记忆晋升提案的 push、list、apply 流程 |
| **Env Context**       | 前端 → 后端 | `session/new._meta.env_context`       | [ENV_CONTEXT_PROTOCOL.md](./ENV_CONTEXT_PROTOCOL.md)             | 宿主把 CLI 路径 / 平台 / 浏览器工具状态等已知事实喂给模型，避免它否认已可用的工具 |
| **Filesystem Policy** | 前端 → 后端 | `session/new._meta.filesystem_policy` | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | 宿主声明 session 工作区根 + 额外允许目录，避免反复触发 `permission/request` 协商  |
| **Permission Mode**   | 前端 → 后端 | `session/new._meta.permission_mode`   | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | `default` 显式受限；`full_access` 仅在服务端 `tools.allow_full_access` 已启用时生效 |
| **Artifact**          | 后端 → 前端 | `update_tool_call.rawOutput`          | [ARTIFACT_PROTOCOL.md](./ARTIFACT_PROTOCOL.md)                   | 宿主收集、解析并渲染 Agent 生成的文件产物                                          |
| **Session directory** | 前端 → 后端 | `session/new.params.cwd`              | [ARTIFACT_PROTOCOL.md](./ARTIFACT_PROTOCOL.md#5-session-directory-contract) | 宿主传用户选择的工作区；后端保持 cwd 稳定，任务子目录由模型按需创建 |
| **Host Progress**     | 后端 → 前端 | `update_tool_call.rawOutput`          | [integration/host-progress-events.md](./integration/host-progress-events.md) | 宿主分组渲染 sub-agent、plan、todo、goal、turn usage 等结构化执行状态 |
| **User Decision**     | 双向        | `update_tool_call.rawOutput` + `session/prompt._meta` | [USER_DECISION_PROTOCOL_CN.md](./USER_DECISION_PROTOCOL_CN.md) | Skill/模型发起结构化执行决策，宿主选择或按运行时批准的默认项超时续跑 |

> 仍然有效的扩展点：`_meta.session_mode`（会话模式）、`_meta.deep_think`（深度思考开关）。`artifact_mode`、`artifact_root` / `artifact_root_dir` 和 `session_workspace_dir` 已废弃：当前兼容版本记录 warning 后忽略，下一主版本删除。

---

## 典型对接场景

### 运行时限前收尾

宿主可在当前 `session/prompt` 尚未结束时调用 `_request_wrap_up`：
参数为 `sessionId`、`remainingSeconds`（1–10800 的整数），以及可选的
`turnId`（必须与当前轮一致）。返回 `ok: true` 表示系统预算提醒已入队，
重复请求返回 `deduplicated: true`；不表示模型已收到或任务已完成。
提醒在下一安全边界进入模型上下文，不作为用户消息回显。它要求保存/导出、
在剩余预算内核验需求与交付物、列出未完成项，不绕过既有质量门。
本轮结束后不再自动续跑 Goal；下一次用户请求恢复正常策略。

最终回包可包含 `_meta.wrapUp: {requested: true, goalIncomplete: boolean}`。
未完成的 Goal 保留原状态，`completed` 为 false，正常收尾的 `runStatus`
为 `incomplete`，`stopReason` 为 `max_turn_requests`；宿主不能当作任务成功。
没有 Goal 时仍沿用原有完成与交付检查，预算提醒本身不是验收证明。

硬截止仍由宿主计时并发送 `session/cancel`，保留原请求等待取消清理后的回包。
宽限期内正常完成的回包应走原有交付校验；宽限期耗尽才能按原有恢复机制处理，
未确认进程退出前不能向用户宣称已安全停止。旧 runtime 不支持此扩展时，
宿主应继续执行取消/恢复流程，不把提醒降级成伪造的用户输入。

### 查询结果与可恢复的工具拦截

单条、无包装和控制语法的 POSIX `grep` / `rg` 无匹配以及 `diff` 有差异（结构识别忽略首尾空白），
退出码仍为 1，但工具结果为成功，`rawOutput.command_outcome` 分别为
`no_matches` / `differences`。该字段可选，旧宿主无需更改。PowerShell、复合命令、
管道、重定向及无法确定来源的非零退出码不自动放行；优先使用独立 `grep` 工具。
失败命令的 stdout 和 stderr 一起保留在模型错误诊断中，沿用 8,000 字符截断预算。

托管浏览器收到明确的会话终止或已关闭传输错误后，丢弃当前会话缓存并返回
`BROWSER_SESSION_RESET`；下一次调用重新建立浏览器上下文。其他会话不受影响。
失败动作不会自动重放，可能已发生副作用；恢复后先导航、检查页面并获取新元素引用。
普通页面错误和导航超时不触发会话重置。

`CHAT_REFERENCE_PROTECTED`、`CHAT_REFERENCE_DELETE_DENIED` 和
`BROAD_HOME_SEARCH_BLOCKED` 仍是失败结果，不能当作授权或执行成功。
宿主可分别展示文件保留、删除取消及缩小搜索范围提示；Agent 对相同诊断的重复拦截给出停止重试提示，
同一错误码下不同原因或目标的诊断仍完整保留。
一次性脚本使用已有 `$BOX_AGENT_SCRATCH_DIR`，不将其发布或链接为交付物。

### 同一产品会话的并发请求

`session/new._meta.session_id` 是宿主的稳定产品会话 ID，与返回的 ACP
`sessionId` 句柄不同。使用相同产品 ID 重建会话时，旧请求必须已彻底结束，
包括准备（如图片理解）、执行、取消清理和收尾阶段。

若仍有请求占用，`session/new` 与重复的 `session/prompt` 一样，立即返回
JSON-RPC 错误 `code=-32010`、`data.code="SESSION_BUSY"`，
`data.sessionId` 指向原 ACP 句柄。拒绝不会关闭原会话或清空其任务状态；
宿主应等待原请求返回后再重建，发送 `session/cancel` 本身不代表取消已完成。

会话创建/重绑尚未返回期间，同一产品 ID 的另一个 `session/new`，以及发给
正在被替换的旧句柄的 `session/prompt`，也会立即返回 `SESSION_BUSY`。
首次创建尚无旧句柄时，错误中不包含 `data.sessionId`。服务端不会排队或自动重试；
其他产品会话不受此限制。请求结束后可正常重建并恢复历史。

宿主关闭 stdin 或终止服务时，ACP 先停止接收新的 `session/new` 和
`session/prompt`，取消并等待在途会话创建/重绑完成回滚，再释放会话资源。
关闭期间的新请求返回 `code=-32000`、`data.code="AGENT_CLOSING"`；
宿主需要重新启动服务后再发起任务。已有任务的取消和最终状态发送仍在协议连接关闭前处理。

ACP 连接不保存已完成的入站 RPC 正文、响应或异常历史；单个请求的记录随其
处理任务释放。多会话状态及恢复历史仍由会话对象和 Session Log 管理。
权限请求等出站 RPC 仅保留仍在等待回复的记录；宿主回复、取消、超时、发送失败
或连接关闭后释放对应记录。清理按请求 ID 进行，不影响其他会话仍在等待的请求；
取消后的迟到回复会被忽略。该生命周期通过 SDK 的 `state_store` 和 `sender_factory`
扩展接口接入，不改变会话历史、权限等待时限或 ACP 消息格式。

### 场景 A：用户首次打开应用

1. 宿主侧检测：`MEMORY.md` 是否已存在、各个 CLI 是否已安装、Chromium 是否已通过 `box-agent install-browser` 装好。
2. 创建会话时，把这些事实通过 `_meta.env_context` 一次性喂给后端。
3. 用户问"你好/你是谁"时，后端发现 MEMORY.md 稀缺 → system prompt 注入 onboarding hint 规则 → 模型在回复末尾输出 `action_hint{tab:"onboarding"}` → 前端渲染为"点击完善个人记忆"链接，点击打开设置页 onboarding tab。

### 场景 B：用户问"帮我打开 example.com"

1. 后端发现 mcp.json 中 playwright 缺失或 `disabled=true` → system prompt 注入 browser-tools hint 规则。
2. 模型回复："我目前还没有可用的浏览器工具" + `action_hint{tab:"browser-tools"}`。
3. 前端剥离 hint 块，原位渲染为可点击链接，点击打开设置页 browser-tools tab。

### 场景 C：用户问"用飞书 CLI 给我发条消息"

1. 宿主在 `env_context.cli["lark-cli"]` 里传了 bundled 路径。
2. 后端 sanitize（绝对路径校验、长度限制等）后写入 system prompt 的"可用 CLI"清单。
3. 模型不再说"你机器上没装"，而是直接通过 bash 工具调用绝对路径。

---

## 安全约束（最低限度）

- **`_meta.env_context.extras`** 不会进入 system prompt（仅入后端日志），但仍然在 ACP 入站记录中可见。**不要传 token / API key / 用户隐私字段。**
- **CLI value 必须是绝对路径，不能含控制字符 / 反引号。** 后端会单条丢弃违规条目并 WARNING 日志。
- **action_hint 块由模型生成，前端必须做白名单校验**（`tab` 只接受协议定义的值），未知 tab 应当忽略不报错。

详细规则见各协议文档第 2 节。

---

## 实现位置（后端）

```
box_agent/acp/
├── __init__.py            # newSession / _build_session_prompt 主流程
├── action_hints.py        # MEMORY 稀缺检测 + playwright disabled 检测 + prompt 段
└── env_context.py         # 宿主环境注入 schema + sanitize + markdown 渲染

tests/
├── test_action_hints.py   # 18 个用例
└── test_env_context.py    # 23 个用例（含 11 条恶意输入回归）
```

---

## 版本与变更

| 版本   | 变更                                                                                                                                                                             |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.8.38 | 新增 Memory Match 协议：`memory_search.rawOutput` 返回本轮显式搜索或后端自动匹配到的 context memory；core memory 只注入模型，不返回前端                                      |
| 0.8.29 | `PermissionEngine` 把 `~/.box-agent/` 视为引擎自有数据，所有 scope 下都默认放行（skills / runtime-packages / browsers / log / trash 等子目录不再触发 `permission/request` 弹窗） |
| 0.8.28 | system prompt 注入 skills 源目录，限定模型只从 `~/.box-agent/skills/`（user）和 builtin 包内目录加载 skill，禁止扫描其它路径                                                     |
| 0.8.27 | 新增 `_meta.filesystem_policy`（宿主声明 session 工作区根 + 额外允许目录）；修复 bash 路径提取裸系统根误报（`cd /; ls` → `/`）；权限拒绝诊断日志增强                             |
| 0.8.26 | 首次引入 `action_hint` 协议、`_meta.env_context`、`enable_mcp` 防护；env_context 包含输入校验与 extras 不进 prompt                                                               |
| 0.8.25 | `context_window` / `max_output_tokens` 配置化（与 ACP 无关）                                                                                                                     |
| 0.8.24 | `max_tokens` 截断防护（与 ACP 无关）                                                                                                                                             |

后续协议变更会在本表追加，并在对应协议文档第 1 节注明适用版本。
