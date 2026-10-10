# Memory System Integration Guide

## 后端选择与外部接入

默认 `memory_backend_type: local` 使用原有本地记忆。`memsense` 提供原生接入；
`generic` 提供可配置 HTTP 映射；`mem0`、`memu` 当前是使用 generic 的占位类型，
需要自己配置对应服务的操作，尚不支持它们的完整原生工作流。

```yaml
enable_memory: true
memory_backend_type: memsense
memory_tenant_id: default
memory_user_id: default
memory_external:
  base_url: "http://127.0.0.1:8787"
  timeout_seconds: 30
  max_retries: 1
  context_max_chars: 16000
  date_memory_load_days: 3 # 仅 MemSense；0 关闭日期预载
  save_queue_limit: 128
  shutdown_timeout_seconds: 45
  # headers: {} # 按部署要求配置认证头，凭证只保存在本地配置中
```

| 行为 | local | memsense |
| --- | --- | --- |
| 核心上下文 | 会话开始加载本地核心和目录摘要 | 每个真实用户轮次开始读取两个核心文件及近期日期摘要 |
| 搜索 | 本地 topic/关键词检索 | `/v1/memory/resource_search`，检索会话标题和历史记忆 |
| 保存 | 原有工具写入及本地 LLM 提取 | 整轮结束后，后台调用 `/v1/memory/save` 保存用户输入和最终回答 |
| 文件修改 | 保留 `memory_write` | `memory_write` / `memory_edit`，仅两个核心文件 |

MemSense 的 `memory_read`、`memory_write`、`memory_edit` 都要求必填 `path`，
只接受 `user.md`（用户画像）和 `memory.md`（长期记忆）。模型参数不使用
`memory://` 前缀，后端内部转换为服务端路径。三个工具不开放日期、会话、事实
或原始 QA 路径；`memory_search(query, limit=6)` 保留外部搜索协议和结果。
核心文件交给模型前保留 `<mem>...</mem>` 条目及正文，去掉 `time`、`priority`
等存储属性；自动注入和显式读取使用同一视图转换。
当前 MemSense 服务将 `qa_chunk` 搜索类型兼容映射到事实记忆；原始 QA 仍通过
save 保存，搜索结果保留服务实际返回的类型和资源路径。
`enable_memory_extraction` 和维护、晋升配置只作用于 local。纠错工具仍保存到
同身份的本地存储，不修改远端文件。

子代理按实际工具实例检查权限：外部 `memory_read/search` 要求网络权限，
MemSense `memory_write/edit` 还要求外部副作用权限。现有公开委派协议不授予
外部副作用权限，因此不会向子代理开放远端修改；本地记忆和纠错工具维持原策略。
`box-agent config --set` 默认隐藏 `memory_external.headers` 的所有值，包括
设置整个 headers 或后端对象时的回显；只有显式 `--show-secrets` 才展示原值。
校验和 YAML 错误不回显原始输入，配置写入失败仍回滚。

### MemSense 核心文件工具

修改前必须在同一会话显式 `memory_read` 目标文件；自动 system prompt
预载不解锁修改，读取另一文件也不能解锁目标文件。工具描述说明这一要求，
执行时同样强制校验。read 的完整 JSON 结构进入模型的工具消息，例如：

```json
{
  "path": "user.md",
  "exists": true,
  "content": "<mem>用户希望被称呼为刀客塔。</mem>",
  "memory_edit_write_rules": "条目格式和追加、替换、删除、完整重写规则",
  "user_profile_update_rules": "用户画像的内容更新规则"
}
```

读取 `memory.md` 时，最后一个字段是 `long_term_memory_update_rules`。
规则不混入 content，不写入远端文件，不加入 system prompt；只在显式 read
返回。raw_output 保留同一结构供宿主查看，规则也真实进入模型消息。

```json
{"name": "memory_read", "arguments": {"path": "user.md"}}
{"name": "memory_edit", "arguments": {"path": "user.md", "old_text": "", "new_text": "<mem>用户偏好先讨论方案，再确认修改。</mem>"}}
```

`memory_edit(path, old_text, new_text)` 的空 old_text 表示追加；空 new_text
表示删除；非空 old_text 必须是一个或多个连续完整条目并唯一匹配。重复
条目要用更多连续条目消除歧义。`memory_write(path, content)` 创建或完整
重写文件，content 必须包含全部条目，不能只提交新增部分。read 明确返回
exists=false 时只允许 write 创建，不允许 edit。

模型只能提交不带属性的完整 `<mem>...</mem>`；time/priority 由后端转换维护。
未变条目继承原属性，新条目生成时间和 priority=1。priority<1 的条目不能
修改或删除；含保护条目的文件拒绝完整重写，但可 edit 其他非保护条目。
核心 edit 转换后也通过 `/v1/memory/files/write` 提交完整存储内容。

现有文件携带内部保存的 base_revision，不暴露覆盖开关。成功修改更新内部
版本，后续编辑可继续使用；冲突、失败、取消或无法验证的响应清除读取记录，
必须重新 read。修改请求不自动重试；read/search/save 保留配置的有限重试。
读取记录仅保存在独立会话后端中，重开会话需要重新读取。

此版本收紧了 MemSense 模型工具接口：旧的省略 path、带 memory:// 前缀或读取
任意搜索资源的 memory_read 调用需改为两个必填短路径。后端内部资源读取
和日期自动预载保留，local/generic 的工具参数和行为不变。

### System prompt 的记忆位置

模板可以使用 `{MEMORY_CONTEXT}` 指定记忆位置；默认模板已预留该位置。
旧模板没有占位符时自动追加，禁用记忆和 utility 会话移除占位符。
local 保留原有内容和加载时机，generic 保留服务返回格式。
外部后端每个真实用户轮次原位替换一个 `--- MEMORY START ---` 块；
同轮工具调用和自动续跑不重复读取。读取失败清除旧数据、保留空块位置，
不影响后续恢复或其他系统提示。

MemSense 块包含 `User Profile`、`Long-term Memory`、`Recent Date Memory`
三个非空分区及使用规则，全部位于 system prompt 中。日期按旧到新排列，
自动注入只保留日期标题和摘要正文；模型工具不开放日期文件的显式读取或修改。
`date_memory_load_days` 仅用于 MemSense，默认 3，范围 0～31；读取
`memory://date-memory/YYYY-MM-DD.md`。日期依据本轮时间戳和 MemSense 服务端
的 UTC 日分区计算，包含当天和此前若干天，每个新 query 重新计算和读取。
服务端不存在或为空的文件不生成分区；失败文件单独记录日志，其他文件继续使用。
日期预载只读取已有摘要，不会等待后台保存或代替服务端生成摘要。
`context_max_chars` 限制后端上下文总长度；MemSense 截断保留完整 mem 条目
边界；核心文件截断时提示通过 memory_read 获取完整内容，日期只标记摘要截断。

MemSense 的会话文件路径要求 UUID。已有 UUID 会话标识保持不变，CLI/ACP 等
非 UUID 标识在后端内结合 tenant/user 稳定映射为 UUID；本地 Session Log 和
日志继续使用原始标识。generic 不执行此映射。切换前已用非 UUID 保存的远端
数据不会自动迁移。

tenant/user 为空时分别取 `default`。本地 `default/default` 使用旧目录；其余
身份使用 `memory_dir/identities/<身份摘要>/`。Python 管理会话可以通过
`SessionOptions(memory_tenant_id=..., memory_user_id=...)` 覆盖身份；ACP 可在
`session/new` 的 `_meta` 中指定：

```json
{"memory": {"tenant_id": "tenant-a", "user_id": "user-a"}}
```

未提供的会话字段继承配置，空字符串使用 default。身份在会话创建时绑定，
记忆工具、纠错存储和上下文均使用该身份。身份字段是宿主提供的路由上下文，
不是认证授权机制；远端服务仍需按实际部署执行访问控制。

通用协议示例（外部服务不需要 tenant/user 字段）：

```yaml
memory_backend_type: generic
memory_external:
  base_url: "http://memory-service.example"
  search:
    path: /lookup
    method: POST
    request:
      query: "${query}"
      count: "${limit}"
    response_path: results
  save:
    path: /conversations
    request:
      messages: "${messages}"
    response_path: ""
```

`recall`、`read`、`search`、`save` 都是可选操作；缺省的操作不发送请求，对应
工具不注册。每项操作支持 GET（query params）或 POST（JSON），默认 POST。
请求模板仅替换完整的 `${变量名}` 值，支持嵌套对象和数组，不执行代码。
可用变量包括 `tenant_id`、`user_id`、`agent_id`、`session_id`、`turn_id`，以及
操作相关的 `query`、`limit`、`path`、`user`、`assistant`、`messages`、`timestamp`。
`messages` 是 user/assistant 两条消息。generic 不默认发送任何身份字段；
需要跨身份隔离的服务，应在请求中映射其身份字段，或为不同身份使用独立实例
及连接配置。内部身份不会自动赋予不支持身份的外部协议隔离能力。

`response_path` 使用点分字段路径（如 `data.results`，数组下标也可用），空串
表示整个响应。HTTP 非成功状态始终视为错误；如服务有应用层状态，还可配置
`success_path: ok`、`success_value: true`。read/recall 将选定内容渲染为文本，
search 保留结果对象。不要把 MemSense 的 `{ok,data}` 格式当作 generic 默认。
未配置响应字段或成功标记时，也接受 HTTP 204 等无正文的成功响应。

CLI 和 ACP 的自动续跑共享外层用户轮次：开始时刷新一次，最后只调度一次 QA
保存。单次 Python `AgentService.start()` 对应一轮；需要组合多次运行时可用
`box_agent.memory.memory_user_turn(session, user_text=..., session_id=..., turn_id=...)`
作为异步上下文管理器。底层 `Agent.run_events`/`AgentSession.run_events` 不会
自行推断宿主的真实用户轮次，外部保存应通过共享服务边界调用。

取消、失败、等待用户或无最终回答的运行不保存。远端失败记录日志，
取消会中断正在等待的远端预载，并等待请求清理后沿用宿主的取消结果；下一轮可重新刷新。
不改变主任务结果；日志不记录 QA 正文和认证头。CLI 将 memory 日志写入
`~/.box-agent/log/memory_<进程号>.log`（设置 `BOX_AGENT_HOME` 时使用该目录下
的 `log/`），按 5 MiB 轮转，保留 3 个备份。重试、最终失败和退出
清理均不向终端显示提示。日志目录或磁盘不可写时无法保留相关记录，但仍不会
打断输入或主任务。ACP 和 Python 宿主继续使用其原有日志路由。
连接使用有限超时、重试，后台保存有队列上限，会话关闭时有限等待。进程崩溃
可能丢失尚未完成的保存，本期
没有额外持久队列；MemSense 的传输重试复用请求内容和时间戳，仍受服务端现有
去重语义约束。以下章节描述 `local` 的原有行为。

兼容既有 Python 宿主：显式通过 `HostBindings` 借用本地 manager 时，仍保留
宿主提供的能力；`enable_memory` 控制自动创建的本地记忆及外部后端访问。

Box-Agent provides persistent cross-session memory with core memory plus topic-sharded context memory:

| Type | Purpose | Recall behavior | Storage |
|------|---------|-----------------|---------|
| **Core memory** | User identity, explicit preferences, local defaults, durable behavioral rules | Automatically injected into the system prompt at session start | `~/.box-agent/memory/MEMORY.md` |
| **Memory summary** | Lightweight routing guide for deciding whether a request should search memory | Injected with core memory; does not contain full context entries | `~/.box-agent/memory/memory_summary.md` |
| **Context / experience memory** | Project context, task templates, historical notes, decisions, deadlines, prior pitfalls | Topic-routed search on demand via `memory_search`; weak auto-match may surface v2 hits | `~/.box-agent/memory/v2/experiences/<topic>.md` |

This split keeps high-signal user facts always available while giving the model a small Codex-style routing summary for deciding when `memory_search` is worth calling. Full project/history notes stay out of the prompt unless searched.

Compatibility policy: v2 is an overlay, not a migration. Pre-v2 `CONTEXT.md` and `context/<topic>.md` files remain on disk and are searched only as a read-only fallback for explicit `memory_search`; they are not auto-matched, not rewritten, and not eligible for promotion.

---

## 1. Configuration

Add these values to `config.yaml` if you need to override the defaults:

```yaml
enable_memory: true                    # Enable memory tools and startup core recall
memory_dir: "~/.box-agent/memory"      # Memory storage directory

enable_memory_extraction: true         # Auto-extract useful memory from agent lifecycle points
memory_extraction_cooldown: 300        # Seconds between extraction attempts
memory_extraction_step_interval: 10    # Extract every N agent steps
```

Set `enable_memory: false` to disable the memory manager and memory tools.

---

## 2. Tool interface

### `memory_write` — write persistent memory

```json
{
  "name": "memory_write",
  "arguments": {
    "content": "- 用户偏好中文回答\n- Q2 goal: launch data dashboard by 6/30",
    "category": "context",
    "mode": "append"
  }
}
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | Yes | Markdown bullet-style memory content |
| `category` | `core` or `context` | No | `core` for explicit user identity/preferences/rules; `context` for project/task history. Default: `core` |
| `mode` | `append` or `overwrite` | No | `append` merges/appends; `overwrite` replaces the target file |
| `topic` | string | No | Context bucket when `category="context"`, for example `preferences`, `project`, `feedback`, or `general` |

#### Core writes

`category="core"` writes to `MEMORY.md`.

Use core only when the user explicitly states durable personal information or preferences, for example:

```text
- User prefers concise Chinese responses
- User is a product manager in the data platform team
- 用户用于本地查询的默认城市是北京
- Do not add emoji in final answers
```

#### Context writes

`category="context"` writes to topic-sharded v2 experience memory under `v2/experiences/<topic>.md`.

When an LLM client is available, append-mode context writes are model-merged with existing memory:

1. Box-Agent sends the candidate memory plus current `MEMORY.md` and context memory to the LLM.
2. The LLM returns a structured operation plan: `add`, `replace`, `drop`, or `noop`.
3. Code applies the plan safely:
   - `replace` and `drop` require an exact full-line match.
   - `add` is line-deduped against both Core and Context.
   - Invalid model JSON falls back to append-with-dedup.

When no LLM is available, context writes use append-with-dedup directly.

### `memory_read` — read all persistent memory

```json
{
  "name": "memory_read",
  "arguments": {}
}
```

Returns both `MEMORY.md` and context memory when present.

### `memory_search` — search context memory

```json
{
  "name": "memory_search",
  "arguments": {
    "query": "weekly report"
  }
}
```

Search is case-insensitive and ranks exact matches first, then multi-term overlap for longer natural-language queries. Both explicit search and automatic matching require complete English/ASCII tokens, including compound names such as `box-agent` and `cache_store`: `user`/`use` do not match `/Users`, and `and` does not match `Understand`. Chinese phrases retain substring matching. Automatic matching uses the host-sanitized current request when supplied, excluding ACP-added UI-language instructions and wrapped history. When `topic` is omitted, Box-Agent first uses the topic sidecar index (`v2/experiences/_index.json`) to route the query to likely topic files, then falls back to all v2 topics if the routed search finds nothing. If v2 has no match, explicit `memory_search` falls back to legacy context read-only. Core memory is already present in the prompt, so `memory_search` only searches context / experience memory.

Models should call `memory_search` with short durable keys instead of whole action sentences. For example, `排产平台融资 ppt 的演讲稿发我` should become searches such as `排产平台`, `融资路演`, or `ppt`; `上次 EACCES 怎么修` should become `EACCES`, `npm cache`, or `runtime install`.

---

## 3. CLI integration

No additional integration code is needed. When `enable_memory: true`:

- **Startup**: `MEMORY.md` is recalled and injected into the system prompt if non-empty. `memory_summary.md` is also injected when searchable v2 or legacy context exists, so the model can decide whether to call `memory_search` without an extra routing model call.
- **During a session**: the agent can call `memory_write`, `memory_read`, and `memory_search`.
- **Lifecycle extraction**: when `enable_memory_extraction: true`, the agent loop asks the LLM to extract cross-session-useful memory at protected lifecycle points. Explicit user profile/preferences/local defaults can go to core; project and task history go to topic-sharded v2 experience memory.

Manual editing is also possible:

```bash
vim ~/.box-agent/memory/MEMORY.md
vim ~/.box-agent/memory/v2/experiences/preferences.md
```

`memory_summary.md` is generated from the v2 topic index and legacy-presence marker; inspect it when debugging routing, but do not treat manual edits as durable because the manager refreshes it.

---

## 4. ACP / Runtime integration

Memory tools are registered as normal tools and are available through standard ACP tool calls.

### 4.1 Writing memory

A host can prompt the agent to remember something:

```python
prompt_text = "请记住：用户偏好简洁的中文回答"
```

The agent may then call:

```text
memory_write(content="- 用户偏好简洁的中文回答", category="core", mode="append")
```

For project context:

```text
memory_write(content="- Weekly report format: progress/issues/next week", category="context", mode="append")
```

With an LLM-backed memory tool, context writes return a strategy label such as:

```text
Memory updated (context, applied). Current context memory: ...
Memory updated (context, no_change). Current context memory: ...
Memory updated (context, fallback_appended). Current context memory: ...
```

### 4.2 Automatic recall

On ACP `newSession`, Box-Agent:

1. Reads `MEMORY.md`.
2. Builds a memory block if core memory exists.
3. Appends the block to the session system prompt.

Format:

```text
--- MEMORY START ---

[Core Memory]
- 用户偏好中文回答
- 用户希望结果简洁

--- MEMORY END ---
```

Full context memory is not injected automatically. The model sees only `memory_summary.md` as a routing guide, and should call `memory_search` when the current request may depend on saved preferences, historical decisions, repo conventions, previous pitfalls, specific paths/errors, or recurring workflows.

---

## 5. Storage layout

```text
~/.box-agent/memory/
├── MEMORY.md              # Core memory, always recalled at session start
├── memory_summary.md      # Generated routing summary for memory_search decisions
├── v2/
│   ├── state.json         # No-migration cutover marker
│   └── experiences/       # Searchable v2 experience memory by topic
│       ├── _index.json    # Topic routing index
│       ├── general.md
│       ├── preferences.md
│       └── project.md
├── context/               # Legacy fallback only; not auto-matched/promoted
│   └── ...
├── CONTEXT.md             # Legacy fallback only, if present
└── .openclaw_imported # Marker for one-time OpenClaw import, when applicable
```

`MEMORY.md` and topic files under `v2/experiences/` are plain UTF-8 markdown files. Bullet points are recommended because model merge and line-level safety checks operate on full lines. The topic files are buckets such as `preferences`, `project`, `feedback`, and `general`; they are not intended to grow one file per project.

---

## 6. Automatic memory extraction

When `enable_memory_extraction` is enabled, `MemoryExtractor` analyzes recent conversation at lifecycle points:

- before context summarization (`pre_summarize`)
- every configured step interval (`step_interval`)
- at loop end (`loop_end`) only when the turn has high-signal evidence such as explicit preferences, "remember" instructions, tool-backed work, verified fixes, root cause notes, or enough multi-turn substance

The extractor can write explicit user-stated profile facts, preferences, and local defaults to `MEMORY.md`. For example, if the user says they are in Beijing while asking for weather, the extractor should save a cautious default such as `- 用户用于本地查询的默认城市是北京`, not infer a permanent residence.

Project context, task patterns, historical notes, decisions, deadlines, and behavioral feedback still go to topic-sharded v2 experience memory. This keeps one-off task details out of core memory, and avoids running a memory extraction pass after every trivial stop.

New v2 experience entries may include provenance metadata in the entry header:

```text
<!-- ctx id=... source=extractor topic=project session_id=chat-a turn_id=chat-a-turn-17 trigger=loop_end -->
```

`session_id` is the host-owned conversation id from ACP `_meta.session_id`; `turn_id` is the host-owned user-visible turn id from ACP prompt `_meta.turnId` / `_meta.turn_id`; `trigger` records the lifecycle point that wrote the entry. These fields are optional and older entries simply omit them. They are for audit/debugging and future trace-back flows, not for search ranking or promotion eligibility.

---

## 7. One-time OpenClaw import

At startup, if memory is enabled, Box-Agent attempts a one-time import from:

```text
~/.openclaw/**/USER.md
~/.openclaw/**/MEMORY.md
```

The LLM filters those files for durable user identity/preferences/habits and appends useful results to `MEMORY.md`. A `.openclaw_imported` marker prevents repeated imports.

---

## 8. Python API

```python
from box_agent.memory import MemoryManager

mgr = MemoryManager(memory_dir="~/.box-agent/memory")

# Core memory
mgr.append_core("- 用户偏好中文")
print(mgr.read_core())

# Context memory
mgr.append_context("- Weekly report format: progress/issues/next week", topic="preferences")
print(mgr.search("weekly report", topic="preferences"))

# Startup recall block for system prompt injection
block = mgr.recall()

# LLM-assisted context merge
await mgr.update_context_with_llm(
    "- Weekly report should include progress, issues, and next-week plan",
    llm_client,
)
```

Legacy aliases remain for compatibility:

```python
mgr.read_manual_memory()
mgr.write_manual_memory("- 用户偏好中文")
mgr.read_all()
mgr.write_all("- 用户偏好中文")
```


---

## 9. Verified correction memory

Correction memory learns concrete repairs and supplies relevant guidance **before**
the model selects its next operation. It is separate from always-injected core memory.

```mermaid
flowchart LR
    A[Repeated failures] --> B[Changed operation succeeds]
    B --> C[Runtime evidence receipt]
    C --> D[Specific remedy and applicability]
    D --> E[Validate and remember automatically]
    E --> F[Match current tools and Skill revisions]
    F --> G[Bounded guidance before the next model request]
```

### What users see

There is no draft approval task for the user. Once a specific reusable remedy is
saved with matching runtime evidence, the tool reports:

> 已记住这个解决办法，下次遇到相同情况会提前提醒。

Users may say “忘掉这条” or “这个办法不适用了”; the agent uses the existing delete or
supersede tool. A failure by itself never produces a “saved” message. Internal staging
is a storage implementation detail, not a user-facing workflow.

### Learning and evidence

- Runtime observation requires actually executed tool calls. Denied calls and fabricated
  model statements cannot issue evidence.
- Two distinct failures for the same fingerprint and subject within one run, followed by
  a successful call with changed arguments or an observed file repair, can issue an opaque `verification_id`.
  Observations and receipts are bounded in-memory data with a six-hour validation window.
- Success must match the original tool/version and operation identity. File/URL targets
  must agree. Simple shell calls must retain their executable/script/subcommand identity;
  unrelated successful commands and help/version/dry-run calls do not verify a repair.
  `uv run` and `npm run` retain the actual script identity. An unchanged command may
  verify a repair only after an executed edit/append or committed write to its script/input
  in the same run. Compound or dynamic shell/code
  calls conservatively receive no receipt.
- A successful call proves an observed execution outcome, **not universal correctness**.
  The model must still provide specific repair steps, the failure they address, and their
  applicability. It must not infer “a known fix” merely from repeated failures.
- `memory_write_correction` requires the receipt, `lesson`, `error_fingerprint`, and
  `subject_name`. It verifies identity and evidence before saving and activating the
  remedy in one call. No `confirm` or `draft_id` round-trip is exposed by the tool.
- Receipts bind the tool and source-validated active Skills at execution time. A Skill
  remedy is stored against that exact source revision. The tool supports `tool` and
  `skill` subjects; other historic subject kinds remain readable through the storage API
  but are not automatically inferred from arbitrary model claims.
- Failure strings, lessons, and scope metadata pass the preference/credential checks.
  Raw commands and tool output are not copied into durable evidence; its record contains
  the executed call identifier and an argument digest. One-shot setup repairs must not
  be proposed as reusable lessons.

### Using remembered repairs

When memory is enabled, the default Context engine checks current offered tools and
selected/read, source-valid Skills **on every request preparation**, including the first
request. Matching uses exact subject identity and version/revision, with task text used
only to rank matching records. A versioned record is not used when the current version
is unknown or different.

At most three complete records and 1,800 characters are supplied, further bounded by
available request budget. Oversize records are omitted, never truncated into a different
instruction. Guidance is request-only reference data; it does not change durable history,
tool arguments, permissions, or user instructions. Revoked records disappear from the
next prepared request. Custom Context implementations may opt into `bind_memory`; their
existing contracts remain unchanged.

General user-text auto-match excludes corrections so it cannot bypass subject/version
checks. Explicit `memory_search` still finds verified active corrections, including their
error fingerprint even when `symptom` was omitted. No vector database or full correction
file injection into the system prompt is introduced.

### Storage, maintenance, and compatibility

- Records remain in `~/.box-agent/memory/v2/experiences/corrections.md` with their existing
  lifecycle and a new `verification` evidence field.
- Unverified and legacy active records without evidence stay inspectable, but are excluded
  from default search and automatic recall. Revalidate and save them with fresh evidence;
  do not silently bless old template lessons.
- Generic decay, deduplication, conflict arbitration and compaction do not alter correction
  records. Generic context overwrite/add/replace/drop cannot mutate their reserved topic
  or records. Corrections are also excluded from promotion into always-injected core memory.
- An evidenced replacement supersedes the previous record and retains its history. Used
  receipts cannot reactivate deleted/superseded records or authorize different remedies. Repeated
  failures and direct active writes cannot overwrite an existing concrete remedy with a template.
- Receipt expiry is not a TTL on remembered repairs. Active records remain until superseded,
  deleted, or excluded by subject/version applicability.
- The development PR's former `confirm`/`draft_id` tool schema is replaced by the one-call
  evidence contract. Hosts must rebuild/install the runtime and restart to adopt it. Source
  tests do not prove that an installed desktop client is using these changes.
