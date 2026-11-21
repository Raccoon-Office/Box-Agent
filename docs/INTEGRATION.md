# Box-Agent ACP 对接索引

本目录下与 ACP 宿主对接相关的协议文档总索引。这些扩展从 Box-Agent 0.8.26
开始逐步加入；每份文档中的兼容性小节才是对应能力的版本边界。CLI 不消费 ACP
wire 格式，但底层共享 core/tool 行为仍可能一致。

> ACP 入口：`box-agent-acp` 走 stdio JSON-RPC。
> 公共扩展点：`session/new._meta`（一次性配置）、`session/prompt._meta`（每轮可变）、`update_tool_call.rawOutput`（结构化产物）。

SDK 0.12.1 与配套 Lab 的接口、生命周期约束和回归矩阵见
[ACP SDK 0.12 对接与验证](integration/acp-sdk-0.12.md)。

---

## 协议清单

| 协议                  | 方向        | 入口                                  | 文档                                                             | 用途                                                                              |
| --------------------- | ----------- | ------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Structured Input** | 前端 → 后端 | `session/prompt._meta` + `_inject._meta` | [ACP SDK 0.12 对接](integration/acp-sdk-0.12.md#2a输入事实与-agent-职责) | 结构化输入事实、选区策略、数据库上下文及幂等边界 |
| **Action Hint**       | 后端 → 前端 | 模型 markdown 围栏块                  | [ACTION_HINT_PROTOCOL.md](./ACTION_HINT_PROTOCOL.md)             | 模型在合适场景输出 `action_hint` 块，前端解析为可点击设置入口                     |
| **Memory Match**      | 后端 → 前端 | `update_tool_call.rawOutput`          | [MEMORY_MATCH_PROTOCOL.md](./MEMORY_MATCH_PROTOCOL.md)           | 宿主展示本轮 `memory_search` 或后端自动匹配到的上下文记忆                         |
| **Memory Proposal**   | 双向        | `session/memory_proposal` + extension methods | [MEMORY_PROPOSAL_PROTOCOL.md](./MEMORY_PROPOSAL_PROTOCOL.md) | 记忆晋升提案的 push、list、apply 流程 |
| **Env Context**       | 前端 → 后端 | `session/new._meta.env_context`       | [ENV_CONTEXT_PROTOCOL.md](./ENV_CONTEXT_PROTOCOL.md)             | 宿主把 CLI 路径 / 平台 / 浏览器工具状态等已知事实喂给模型，避免它否认已可用的工具 |
| **Connector Discovery** | 双向 | `session/new/prompt._meta` + `update_tool_call.rawOutput` | [按需发现连接器](#按需发现连接器与恢复入口) | 未连接能力发现、结构化设置请求及逐轮权限核验 |
| **Filesystem Policy** | 前端 → 后端 | `session/new._meta.filesystem_policy` | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | 宿主声明 session 工作区根 + 额外允许目录，避免反复触发 `permission/request` 协商  |
| **Permission Mode**   | 前端 → 后端 | `session/new._meta.permission_mode`   | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | `default` 显式受限；`full_access` 仅在服务端 `tools.allow_full_access` 已启用时生效 |
| **Artifact**          | 后端 → 前端 | `update_tool_call.rawOutput`          | [ARTIFACT_PROTOCOL.md](./ARTIFACT_PROTOCOL.md)                   | 宿主收集、解析并渲染 Agent 生成的文件产物                                          |
| **Session directory** | 前端 → 后端 | `session/new.params.cwd`              | [ARTIFACT_PROTOCOL.md](./ARTIFACT_PROTOCOL.md#5-session-directory-contract) | 宿主传用户选择的工作区；后端保持 cwd 稳定，任务子目录由模型按需创建 |
| **Host Progress**     | 后端 → 前端 | `update_tool_call.rawOutput`          | [integration/host-progress-events.md](./integration/host-progress-events.md) | 宿主分组渲染 sub-agent、plan、todo、goal、turn usage 等结构化执行状态 |
| **User Decision**     | 双向        | `update_tool_call.rawOutput` + `session/prompt._meta` | [USER_DECISION_PROTOCOL_CN.md](./USER_DECISION_PROTOCOL_CN.md) | Skill/模型发起结构化执行决策，宿主选择或按运行时批准的默认项超时续跑 |

> 仍然有效的扩展点：`_meta.session_mode`（会话模式）、`_meta.deep_think`（深度思考开关）。`artifact_mode`、`artifact_root` / `artifact_root_dir` 和 `session_workspace_dir` 已废弃：当前兼容版本记录 warning 后忽略，下一主版本删除。

---

## 典型对接场景

### 视频插件配套技能与安装恢复

支持此链路的 runtime manifest 声明 `plugin_skills_env_version: 1`。宿主通过进程环境
`BOX_AGENT_PLUGIN_SKILLS_DIR` 指定当前 `BOX_AGENT_HOME/plugins/skills`；未传时 CLI 和旧宿主保持原有来源。
这个目录是插件配套资源，不属于后台内置技能目录，也不属于普通用户技能。

宿主显式安装视频插件时，从应用分发的 Agent 资源复制 `hyperframes-video/SKILL.md`，
同时校验运行组件、模板、浏览器和技能。完整就绪后才设置 `BOX_AGENT_VIDEO_RUNTIME_READY=1`；
安装前的 `env_context.hyperframes.setup_supported=true` 允许模型请求安装引导。
未就绪时 `get_skill` 返回 `runtime_component_required` v1，组件为 `video-generation`，
代码为 `VIDEO_GENERATION_SETUP_REQUIRED`。模型解释后结束回合，宿主等待用户点击安装。
宿主刷新进程环境并在同一产品会话发送一次短继续消息；保留历史、产物及当前连接器选择，
不重放原请求或附件。`video` 仍仅表示 `read_video` 所需的 ffmpeg/ffprobe。
用户选择暂不安装时，宿主验证原回执并发送一次带有稳定 `request_id` 和
`skip_component_id: "video-generation"` 的 `session/prompt._meta.component_continuation`。
跳过连接或视频安装的续跑使用已经就绪的能力。若插件不可用或本次跳过了插件，
`get_skill` 返回当前限制，任务可以继续完成大纲、旁白或分镜。委派的 Skill 也沿用该回合的跳过选择。

### 按需发现连接器与恢复入口

这是可选的版本 1 扩展，只在新建的工具接口 v2 会话中协商启用：

1. `initialize` 返回 `_meta.connector_discovery = {"versions": [1]}`。
2. 宿主在 `session/new._meta.host_capabilities.connector_discovery` 传整数 `1`，
   并在 `_meta.connector_discovery` 提供下述快照。
3. 启用后的会话返回 `_meta.capabilities.connector_discovery_versions = [1]`。
   宿主以此回执决定后续 `session/prompt` 是否发送新快照。

会话模式以可忽略的 `connectors/discovery-contract` 事件保存在 Session Log；
完整连接器清单只保存在运行时。已有会话、工具接口 v1、旧宿主和 utility 会话
保持 legacy 行为，不因后续提供元数据自动升级。新模式会话恢复时需要宿主重新提供
能力声明和快照；旧宿主重绑时使用 legacy 行为，保留已存的模式供受支持宿主恢复。
`session/new` 可同时传新快照与旧 `connector_statuses`，以支持工具接口 v1 回退。
新模式不会把旧完整状态清单注入模型。

用户在任务暂停时选择暂不连接或使用，宿主验证原任务的可信回执后，在同一会话发送一次继续请求。
`session/prompt._meta.connector_continuation` 带有稳定 `request_id` 和 `skip_connector_id`。
续跑回合排除该连接器，只搜索已就绪的其余连接器，不产生新的连接准备回执。
任务可以使用现有工具或直接回答，可先完成独立部分并说明剩余限制。
下一条普通消息按该回合的连接器选择和实时状态处理。

```json
{
  "selected_connector_ids": [],
  "connector_discovery": {
    "schema_version": 1,
    "catalog_revision": "manifest-content-revision",
    "state_revision": 1,
    "available": true,
    "entries": [{
      "id": "tmeet",
      "name": "腾讯会议",
      "description": "管理会议、录制和纪要。",
      "kind": "cli",
      "auth_mode": "oauth",
      "skills": ["tmeet"],
      "keywords": ["meeting", "minutes", "会議"],
      "configured": false,
      "enabled": false,
      "status": "disconnected",
      "execution_ready": false
    }]
  }
}
```

宿主 manifest 是能力简介和可选关键词的单一来源。快照最多 256 项；ID 匹配
`^[a-z0-9][a-z0-9_-]{0,63}$` 且不可重复；name 最多 128 字符，description
1024，auth_mode 32，catalog_revision 128，reason_code 64。
skills 最多 16 项、每项 128 字符；keywords 最多 16 项、每项 64 字符。
所有长度按 Unicode 字符计，禁止控制换行；未知字段（包括凭据、地址和 schema）拒绝。
state_revision 是非负 JavaScript 安全整数，同一绑定内不可回退，同 revision 内容不可变化；
选择变化只通过 `selected_connector_ids` 表达。快照每轮更新，缺失时使已知能力变为
不可执行；无效或倒退快照使本轮失败并撤销 ready 状态。`available: false` 表示宿主
状态读取不可用，不把所有项目误报为断开连接。

原生 status 保留 `disconnected`、`configuring`、`authorizing`、`connecting`、
`connected`、`unauthorized`、`error`、`cancelling`、`disconnecting` 和 `unknown`。
建议的 reason_code 包括 `STATUS_UNAVAILABLE`、`SKILL_UNAVAILABLE`、`AUTH_REQUIRED`、
`CONNECTION_FAILED`、`EXECUTION_UNAVAILABLE`、`UNSUPPORTED_*`，不放底层错误或凭据。
模型只在 `tool_search(query/queries)` 时看到最多 3 个相关候选，候选用途最多
160 字符；候选、状态、action、CLI 方法名与附加 notice 的实际 JSON 文本总计最多
2400 字符（包括转义）。完整清单不进入模型消息或工具定义，固定引导不超过 600 字符。
已在当前请求可见的候选简介不重复发送；压缩、移除后可再次完整发现，状态仍逐次返回。

普通查询的 `connector_candidates` 仅说明能力，不激活工具，也不发起设置。
只有 `tool_search(connector="精确 ID 或唯一名称")` 单独指定连接器且该项未就绪时，
返回成功的发现结果和下述 `rawOutput`：

```json
{
  "type": "connector_action_required",
  "version": 1,
  "connector_id": "tmeet",
  "action": "connect",
  "state_revision": 1,
  "wait_for_user": true
}
```

action 为 `connect`（未配置）、`enable`（已配置但禁用）、`reauthorize`（授权失效）、
`retry`（配置后故障或执行能力缺失）、`select`（已就绪但当前会话未选）、`wait`
（正在配置/授权/连接/取消/断开），或 `recheck`（状态未知/不可读/平台不支持）。
未知和进行中状态优先于配置标志，授权失效优先于禁用。
server_name、tool_names、混合查询和不唯一的名称不会生成设置回执；现有 MCP 工具
的 count、top_k、精确 scope、schema 与 tool_ref 语义保持不变。

`wait_for_user: true` 表示 Agent 保存当前工具结果后以 `waiting_for_user` 停止本轮，
同批后续工具及下一轮模型调用都不执行。宿主可立即展示一个就绪操作入口，用户确认且所需能力
就绪后在原会话继续，暂缓时不发送继续消息。旧版回执可缺省此字段，沿用回合完成后的恢复入口。
当视频制作未指定服务或本地方案时，提示词建议先发现 SEKO；明确指定的方案优先。

发现不授予权限。`selected_connector_ids` 是选择状态的唯一来源；执行还要求当前
快照 configured/enabled/connected/execution_ready，并保留实际 MCP runtime 与工具
门禁检查。CLI 的 execution_ready 必须由宿主核验驱动/文件/owner/技能启用，
Agent 只允许加载真实 Skill loader 中属于该已选连接器且与快照 skills 名称一致的
启用方法；不会伪造 MCP schema 或仅凭已加载的历史技能恢复授权。
自定义 MCP 和 computer use 沿用原有独立发现、开关和权限流程。

设置、授权、取消和 UI 由宿主负责，Agent 没有静默连接/安装接口。用户完成设置后，
宿主确认执行能力、更新选择及快照，等待当前轮结束，再向同一会话发送普通续做输入，
使用稳定的 `turn_id` 保持原有幂等边界。不要重发原始业务请求、把运行时热更新当作
会话选择，或只凭旧回执重试写入操作。Agent 会重新搜索并在真实调用时再次检查权限。

### MCP 服务使用说明

内置和自定义 MCP 都可以在握手时返回 `instructions`。Box-Agent 将它作为该服务的外部使用说明。
普通工具随成功激活它的 `tool_search` 结果提供说明，`alwaysLoad` 工具随首次曝光提供说明。
模型当前可见的同一服务、同一说明只发送一次，重连后按新握手返回的说明处理。
未返回说明、工具不可用或只命中连接器候选时，不新增说明。调用仍需通过原有权限检查。

每个服务的说明最多保留 2,048 字符，超出时用省略号标记。每次发送的说明合计最多
4,096 个 JSON 字符，并受该次工具发现或曝光的剩余预算约束。超出预算时优先保留可调用的工具定义。

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
新会话通过 `$BOX_AGENT_SCRATCH_DIR` 使用工作区内的 `.box-agent/scratch/<会话唯一 ID>/`。
一次性脚本和临时预览放在会话子目录下，运行时清理可丢弃的文件并保留聊天中引用的预览。
已有的 `.box-agent-scratch/` 目录及其中的文件留在原位，现有聊天链接可继续使用。
工作区的 `.box-agent` 和 `scratch` 需为实际目录。工作区恰好是已重定向的全局 `.box-agent`
配置目录的上级时，新会话改用工作区内的 `.box-agent-scratch/<会话唯一 ID>/`。

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

### Playwright MCP 截图原图与本地路径

Agent 对 `browser_take_screenshot` 的 MCP arguments 注入
`_meta.box_agent_screenshot_original=true`。支持该协商的宿主适配器返回未经过
MCP 预览缩放的 image content，并附带
`structuredContent.raccoonScreenshot={version:1, inline:"original", mimeType:"image/png"}`
（JPEG 使用 `image/jpeg`）。此元数据由执行适配器添加，不在模型工具参数中公开。
未协商的普通 MCP 客户端仍获得上游原有的缩略图行为。
显式配置 `imageResponses=omit` 优先于原图协商：服务不返回 inline 图片或原图回执，
并说明禁用原因。Agent 没有收到图片时不宣称任务目录中的截图已保存。

MCP 进程 cwd 与任务 cwd 可以不同。Agent 消费任务目录内的 `filename`，将返回的
图像字节保存到该绝对路径；省略文件名时在任务 cwd 生成唯一文件名。保存结果的
`raw_output.browser_screenshot` 提供 `path`、`width`、`height` 与 `quality`，
供宿主检查；模型读取结果中明确给出的绝对路径，不自行拼接 MCP 缓存路径。
旧服务没有原图回执时，`quality` 标记为 `preview`，不宣称它是原图。
预览的像素限制不能作为改变页面版式、画布尺寸或重复相同截图的理由；细节检查
应读取保存图或裁剪图像，不改动页面来适配预览。

---

## 独立运行包资源布局

独立 runtime 把只读资源放在 `bin/` 同级，CLI 和 ACP 使用同一套定位规则：

```text
box-agent-runtime/
├── manifest.json
├── bin/
│   ├── box-agent-acp[.exe]
│   └── _internal/                 # Python 模块、库及 MCP 静态资源
├── skills/                        # 随包内置 Skill、脚本、引用文件和 _manifest.json
└── config/                        # 随包默认提示词、配置示例和 MCP 示例
```

定位以当前可执行文件所在的 `bin/` 为依据，不依赖进程工作目录或 `manifest.json`。
打包运行时，可执行文件未位于 `bin/` 会报告布局错误，不回退查找包内旧资源。
`get_skill` 返回新资源目录中的路径，后续读取和脚本执行沿用这套路径；内置
Skill 的写保护也覆盖新位置。显式的 `tools.skills_dir`、宿主
`BOX_AGENT_BUILTIN_SKILLS_DIR` 和用户配置优先级保持原有语义。
未设置 `BOX_AGENT_HOME` 时，显式相对 Skill 路径仍优先查找工作目录；
未显式配置 `tools.skills_dir` 时，独立 runtime 默认使用随包 `skills/`。

这里的 `config/` 是发行资源，不是用户状态目录。实际 `config.yaml`、认证、
MCP 配置及会话状态仍使用原有用户目录或显式 `BOX_AGENT_HOME`；启用独立
profile 后，不会从运行包回退读取认证或 MCP 配置。

源码和 pip 安装仍使用 `box_agent/skills`、`box_agent/config`，Python 包和
`mcp_servers` 的位置不变。更新独立 runtime 必须一起交付 `bin/`、`skills/`
和 `config/`；只替换旧二进制不足以启用新布局。Windows `--exe-only` 构建及
安装也会同步更新这两份资源。该布局不承诺缩短宿主自行选择的运行包父目录。

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
