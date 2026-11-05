# ACP SDK 0.12 对接与验证

依赖固定为 `agent-client-protocol==0.12.1`，Python 范围为 `>=3.10,<3.15`。
SDK 包版本与 ACP 线协议版本是两回事：`initialize.protocolVersion` 仍使用整数 `1`。
本次按 Lab / Box-Agent 配套版本验收，不提供与旧组件混装的兼容层。

## 接口与职责

- Agent Python 接口为 `initialize`、`new_session`、`prompt`、`cancel`、`ext_method`，
  接收 SDK 展开的参数；反向调用使用 `session_update`、`request_permission`、`ext_method`。
- 标准 JSON 字段仍为 `protocolVersion`、`sessionId`、`stopReason`、`toolCallId`、`optionId` 等。
  私有方法在 wire 上只有一个 `_` 前缀；传给 SDK `ext_method` 时不加此前缀。
- 宿主身份扩展改为 `_meta.host_client_info`。标准 `clientInfo` 保持原义。
  SDK 将 `_meta` 合并到方法参数，旧扩展名 `client_info` 会覆盖标准参数，因此两端同时改名。
  每轮 `turn_id`、历史编辑、模型绑定、宿主能力和会话恢复等现有元数据继续传递。
- 实际能力声明仍为图片输入及现有产品能力；没有新增 `session/load`、音频、
  resource_link 业务消费或远程文件上传。普通文件仍沿用既有路径语义。
  当界面与 Agent 不在同一机器时，本地 `file://` 不会自动成为云电脑可读文件；
  下一阶段须先准备 Agent 可读资源，再发送引用。

## 2A：输入事实与 Agent 职责

Lab 的普通发送、历史编辑重发和 `_inject` 使用同一组 `_meta` 字段。宿主不再追加通用回答规则、选区编辑流程、截图分析指导或数据库指导语；文件附件仍沿用既有正文包装，结构化文件输入留给 2B。

| 字段 | 数据合同 | 消费边界 |
| --- | --- | --- |
| `input_context` | `schemaVersion: 1`、`userRequest`；可选 `appshot: {text}`、`referencedConversations: {text, sources: [{sessionId, title}]}`、`teamTask: {text}` | 原始请求决定本轮意图；截图辅助文本、主动引用对话及团队任务保留独立来源。纯 Appshot 延续分析当前窗口的默认请求。 |
| `selection_context` | 既有 v1 `document_selection` 合同：相对源文件路径、预览策略、PDF 页码及选中文本 | 文档能力规定问答/不落盘请求不写文件；明确局部编辑保存原文件，保留其余内容；定位不唯一或写入校验失败时不得覆盖。 |
| `database_context` | `schemaVersion: 1`、`connections: [{connection, allowedTables}]`、`truncated` | 只携带本轮显式引用的连接及选表事实；不授予权限，数据库凭据和工具仍执行实际访问控制。 |

共享 `input_context.py` 在接受轮次、回退历史或入补充队列前校验以上字段，并统一投影到模型消息。未知字段、非整数版本、非法选区路径/页码等返回 `INVALID_INPUT_CONTEXT:<field>`。Appshot 和主动引用对话的单段文本最多 200,000 字符，引用来源最多 3 个，选区文本最多 4,000 字符；数据库最多 8 个连接、每连接 16 张表。`teamTask.text` 是聚合多份项目资源、协作资料与任务要求的完整快照，要求非空字符串，但不套用单段文本的字符上限；与 `userRequest` 一样受 32 MiB ACP 总输入帧及 Agent 模型上下文预算约束，不能为满足单段限制静默截掉末尾任务要求。Lab 另外保留自己的采集/展示大小限制。

投影保留用户原文中的标签和代码；资料以有来源的 JSON 呈现，不作为行为指令或工具授权。投影后的用户消息沿用 Session Log 持久化与恢复，`input/context` 是可忽略的诊断事件；不批量重写已有历史。Lab 的消息配置另存本轮输入事实，用于重试、历史编辑及界面恢复。

幂等指纹纳入规范化后的语义上下文，忽略 trace ID、时间戳等传输元数据。相同 ID/相同输入保留既有去重行为；同一轮次或补充 ID 携带不同语义输入分别返回 `TURN_IDEMPOTENCY_CONFLICT` 或 `INJECTION_IDEMPOTENCY_CONFLICT`。没有新上下文的旧请求沿用原指纹。

直接回归见 `tests/test_input_context.py`、`tests/test_message_injections.py` 和历史编辑测试。配套 Lab 的 `boxAgentSdk.spec.ts` 使用真实 SDK 子进程及受控模型，记录普通发送与运行中补充实际交给模型的消息；它证明合同贯通，不代替真实模型和原生界面验收。

## 2B：已准备文件的 resource_link

Lab 的普通发送和编辑重发保留用户 text 原文，另发标准 ACP `resource_link`
（`uri`、`name`、`mimeType`、`size`，数据源名称/相对路径放在 `title`）。
运行中 `_inject` 的 `resources` 使用同样的 link 对象列表，`text` 仍是用户原文。
不再拼接宿主文件指导，也不把数据源清单裁成前 6 个文件。

共享 `input_resources.py` 只接受当前会话工作区内、已存在且可读的普通文件。
仅支持本机 `file:` URI，拒绝远程 authority、query/fragment、非法百分号/UTF-8 编码、
父路径、跨工作区、符号链接逃逸、硬链接及非普通文件；同时检查既有 PermissionEngine。
资源 URI 不触发下载，不授予新权限。文件工具实际读取时继续执行原访问检查。
同一路径只投影一次；完整名称、规范路径、类型和大小进入有来源的 JSON 事实，
资源名/内容不成为新指令。原图片 block/`image_attachment_paths` 通路保持，
普通图片附件不再同时作为文件 link 触发另一条自动分析路径。

校验发生在轮次绑定、历史回退和补充入队之前；失败返回
`INVALID_INPUT_RESOURCE:<reason>`，不会占用该轮次/补充 ID，可在准备完成后重试。
纯文件输入的默认分析请求由 Agent 生成。资源事实参与既有语义指纹和持久消息，
因此补充去重、历史编辑和恢复仍使用同一套路径。协议最多 520 个 link，
对应宿主原有 20 个上传附件和 500 个数据源选择；上传 100 MiB 限制仍由原准备入口执行。
预检查不等于不可变文件快照；检查后删除、替换或访问权限变化仍由实际工具读错误处理。

直接回归：`tests/test_input_resources.py` 覆盖编码、完整性、归属、权限、失败重试和历史保护。
Lab 真实 SDK 子进程测试使用 9 个中文/空格/特殊字符同名文件与 1 个运行中补充，
受控模型调用既有 `read_file`，核对 10 个随机 token 和数值总和；不是仅回显 URI。
该测试的文件工具通过 `BOX_AGENT_DESKTOP_TEST_FILE_INPUTS=1` 显式启用，不影响生产配置。
这只证明本地合同；云端上传、身份绑定及资源/产物传输仍属于 2C。

## 长驻进程约束

`stdio_compat.StdioTransport` 实现 SDK 的消息级 transport，SDK 负责 JSON-RPC 路由和回包关联。
保留 32 MiB 输入帧上限、大帧、EOF 通知、发送背压和有界退出。
SDK 默认 NDJSON transport 会拼接 `readuntil` 超限分段，故 reader 的 `limit` 本身不足以限制整帧。
Windows 输出在 transport 创建时绑定原 stdout，避免 stdout 重定向后协议写入 stderr；
写失败向上抛出，由 transport 通知断连。

旧 SDK 的请求 state store / sender 适配已删除。0.12.1 只保存未完成回包，
但接收循环会在等待下一帧时保留上一条消息。`request_state.release_received_frames`
使用单实例的 `_process_message` 钩子：浅拷贝消息封套交给 SDK，然后清空接收循环持有的封套。
处理任务和回包 future 仍持有各自需要的正文，完成后释放。
这是依赖私有 SDK 接口的局部适配；升级依赖时必须重跑生命周期测试，不能仅放宽版本范围。

## 能力覆盖矩阵

路由覆盖、业务行为和宿主消费分别验证，三者组合才构成升级证据。

| 能力 | 直接验证入口 | 关键断言 |
| --- | --- | --- |
| 4 个标准入口 | Lab `boxAgentSdk.spec.ts`、Agent `test_desktop_runtime_fixture.py` | 真 stdio initialize/new/prompt/cancel，整数协议版本，标准字段，成功/取消/错误回包 |
| 18 个入站扩展 | Lab `boxAgentSdk.spec.ts`；Agent ACP、轻量调用、Memory 测试 | 真实 SDK 路由进入全部方法；业务层保留有效、无效及不可用结果 |
| 4 类宿主扩展 | Lab `boxAgentSdk.spec.ts`、`boxAgentManager.spec.ts`；Agent `test_acp.py` | runtime_environment、memory_proposal、skillhub_search、skillhub_install 经真实 SDK 发出并由 Lab 处理 |
| 文本、既有附件、图片 | `test_acp.py`、`test_acp_history_edit.py` | 大文本、路径附件、标准内联图片、图片权限重试、历史图片分支与推理年龄 |
| 工具、思考、计划、子代理、产物、用量 | `test_acp.py`、`test_desktop_run_delivery.py`、Lab manager 测试 | 事件顺序、rawOutput/字段、交付失败与错误分类；真实 stdio 工具与子代理执行 |
| 权限与并发 | `test_permission_negotiation.py`、`test_acp_request_state.py`、跨仓库测试 | 允许/拒绝/超时/取消、共享等待者、迟到/重复回包、另一会话继续、SESSION_BUSY |
| 补充、撤回、Goal、收尾 | `test_acp.py`、`test_acp_wrap_up.py`、跨仓库测试 | 注入去重、撤回、状态操作、收尾预算与未完成状态 |
| 历史与隔离 | `test_acp_history_edit.py`、`test_acp_session_continuity.py`、`test_session_restore.py`、`test_isolated_profile.py` | 重绑/重启恢复、编辑幂等、图文分支、临时 profile 不触及用户状态 |
| 模型、Skills、Memory、MCP | `test_lite_llm_routing.py`、`test_model_profiles.py`、`test_acp_memory_proposal.py`、`test_acp.py` | 模型绑定、轻量用途、凭据变更、目录更新及能力发现 |
| 请求生命周期 | `test_acp_request_state.py` | 成功/错误后空闲时正文与结果释放，未启动/已启动取消、发送/序列化失败、回包关联与 Future 清理 |
| stdio 与关闭 | `test_acp_stdio.py`、`test_desktop_runtime_fixture.py` | >64 KiB、32 MiB 边界、无换行超限、EOF/I/O 失败、背压、关闭超时、子进程退出；Windows stdout 模拟 |

18 个入站扩展为：`request_wrap_up`、`inject`、`cancel_inject`、`list_skills`、`goal`、
`memory_proposal_list`、`memory_proposal_apply`、`llm/prompt`、`workspace/list`、`workspace/get`、
`workspace/set`、`mcp/status`、`mcp/credential/set`、`mcp/credential/clear`、`mcp/reconcile`、
`mcp/source/replace`、`mcp/reconnect`、`mcp/disconnect`。

## 可复现检查

在 Box-Agent checkout 中：

```sh
uv sync --inexact --frozen
uv run --no-sync python -c 'from importlib.metadata import version; print(version("agent-client-protocol"))'
uv run --no-sync pytest tests/test_acp_request_state.py tests/test_acp_stdio.py tests/test_acp_history_edit.py tests/test_desktop_runtime_fixture.py -q
BOX_AGENT_ACP_HOST_LIVE=0 uv run --no-sync pytest tests/ -q --tb=short -n 4 --dist=loadfile --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server
```

最后一条沿用仓库 CI 的已知外网超时排除项；`BOX_AGENT_ACP_HOST_LIVE=0` 不调用真实模型。
跳过项和平台限制应按实际报告记录，不能算作通过。

在配套 Lab checkout 的 `apps/office-desktop` 中，显式指定当前 Agent 源码位置：

```sh
BOX_AGENT_SOURCE_ROOT=/absolute/path/to/Box-Agent pnpm exec vitest run --config vitest.runtime.config.ts tests/unit/agent/boxAgentManager.spec.ts tests/unit/agent/boxAgentSdk.spec.ts
pnpm desktop:typecheck:runtime
```

跨仓库测试使用指定 checkout 的 `.venv`（可用 `BOX_AGENT_TEST_PYTHON` 指定解释器），
真实启动 `tests.desktop_runtime_fixture`，临时 profile 位于 Agent `workspace/` 下。
模型、外部 SkillHub / runtime 安装服务和 UI 被控制，协议收发与 SDK 路由保持真实。
未设置 `BOX_AGENT_SOURCE_ROOT` 时此跨仓库用例跳过，因此独立 Lab 单测成功不能替代配套验收。

这些检查验证源码组合，不证明已安装客户端、正式 runtime、远程传输或目标云电脑可用。
Windows 模拟与 Windows 原生进程检查分别报告；正式平台验证需使用实际目标环境。
