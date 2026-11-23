# MCP Apps：Lab / Box-Agent 对接契约

Lab 与 Box-Agent 配套升级。ACP 宿主固定启用 MCP Apps，不使用内部能力协商，
`initialize` 不要求或返回 `_meta.mcp_apps`。CLI 保持非 UI 模式。
Box-Agent 连接 MCP Server 时仍声明标准 `io.modelcontextprotocol/ui` extension，
支持的 MIME 为 `text/html;profile=mcp-app`。

## 生图服务选择

CLI、ACP 和 Python SDK 共用提示层的 Image Generation Routing：普通生图、改图及
HTML/PPT 位图配图默认使用 Box-Agent 内置工具及当前生图配置。仅当用户明确指定
Seko，或继续该指定任务的图片修改时使用 Seko；无关新任务恢复默认。连接、授权、
发现工具及此前调用均不代表用户指定；提示模型在失败或拒绝费用确认后不得自动切换服务。

内置工具描述声明默认用途；Seko 生图工具的模型描述和搜索目录在原描述前补充使用条件。
匹配连接器 ID `seko`；无连接器 ID 的手动配置兼容服务名 `seko`、`seko-mcp`、
`seko-canvas`，工具原名为 `generate_image` 或旧代理别名 `seko_generate_image`。
原 descriptor、参数 schema、UI 元数据、可见性和调用路径保持不变；此规则不增加执行层限制，
也不修改 Seko 服务端。更新 runtime 并重启后需在新任务中验证真实模型选路效果。

## 消息标识与完整快照

可信 MCP 工具执行捕获成功后，ACP `tool_call_update._meta.mcpApp` 为：

```json
{"version":1,"resourceUri":"ui://flight-fixture/search.html","title":"Flight search"}
```

`title` 可省略。标识随原 `toolCallId` 发送，历史重放也恢复它。
消息 stage 只携带此小型标识；不能从经过脱敏、截断的 `rawOutput` 重建组件数据。

原工具 descriptor、原始 arguments、完整 `CallToolResult`（包含其 `_meta`）
写入原 Session Log 的 `mcp-app/snapshot` 事件，设置 `ignorable: true`。
这是宿主数据，不作为模型消息或通用工具 stage 内容；模型继续接收工具文本结果。
历史恢复读取已保存事实，不重新执行原业务工具。已撤销历史或清空 surface 的调用不可打开。

## ACP 方法

以下是 wire 方法名。Python `ext_method` 接收到的方法名不带开头的 `_`。
所有 `sessionId` 都是现存 ACP session ID；`toolCallId` 是产生该 UI 的原工具调用 ID。

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `_mcp/apps/snapshot` | `{sessionId, toolCallId}` | `{version:1, resourceUri, tool, arguments, result}` |
| `_mcp/apps/resource` | `{sessionId, toolCallId, uri}` | `ReadResourceResult`：`{contents:[...]}`；读取历史入口 UI 时另返回 `resolvedResourceUri` |
| `_mcp/apps/call` | `{sessionId, toolCallId, interactionId, name, arguments}` | 标准 `CallToolResult` |
| `_mcp/apps/cancel` | `{sessionId, interactionId}` | `{cancelled:boolean}` |

快照中的 `tool` 保留原 descriptor，至少包含 `name`、`inputSchema`，可包含
`description`、`_meta` 等字段；`arguments` 是对象，`result` 是完整 MCP 工具结果。
`name` 使用该 MCP Server 的原始工具名，不能填宿主内置工具名或其他服务的路由名。

### 请求级连接器状态

`snapshot`、`resource`、`call` 可携带 `connectorContext`，字段复用宿主元数据：
`selected_connector_ids`、`connector_statuses`（`id`、`name`、`status`）及
`connector_status_unavailable`。Lab 每次请求只从前端接收选择 ID，连接状态由主进程
连接器服务重新读取，第三方 iframe 不能声明授权状态。

提供此字段时，仅本次请求中已选且原生状态为 `connected` 的 connector 可用；状态缺失、
不可用或未连接时不回退到会话旧授权，格式错误直接拒绝。省略字段的旧宿主仍使用原会话选择。
实际 MCP 连接、单次请求的连接一致性及工具可见性校验继续执行。该上下文不写回
`selected_connector_ids` / 模型状态，不触发 `session/resume`，不改变思考配置或已有卡片；
正常会话恢复仍只在 Agent 会话不存在时执行。客户端与 Box-Agent 需配套升级。

### 页面升级后的历史恢复

`snapshot.resourceUri` 始终是原调用保存的地址，`tool`、`arguments`、`result` 也保持原样。
打开时优先读取该历史 URI，不要求它仍出现在当前工具声明或资源列表中。
ACP 根据有效 Session Log 中的原调用定位当前已选服务。首次打开最多等待目标服务 15 秒，
遇到已续期的托管登录时通过现有连接管理器重连。等待后重新核对会话、历史和连接。
桌面若在等待前读到了断开的连接器状态，会重新读取状态；确认已连接后最多再取一次快照。
原工具改名、下线或移除 UI 声明时，原页面仍可由当前 MCP 提供。

`resources/read` 返回资源不存在时，从同一工具的当前 descriptor 取得新 URI，
重新检查本次请求的连接和历史后最多读取一次。兼容旧代码 `-32002`；对 `-32602`，
按官方 TypeScript SDK 的 `ResourceNotFoundError` 约定识别精确的 `data: {"uri": 请求 URI}`，
同时保留已有精确英文 missing-resource 消息的兼容。
对于没有 `data` 或仅有空对象的 `-32602`，历史入口也允许尝试同一工具当前声明的新 URI，
不依赖错误消息的语言。只有入口地址确实变化才重试一次，不将普通资源读取的参数错误
统一认定为缺页；新地址仍失败时不再回退。URI 不匹配或带有其他诊断字段的错误不因此回退。
参见 [MCP Resources 错误约定](https://modelcontextprotocol.io/specification/2026-07-28/server/resources#error-handling)
及 [SDK ResourceNotFoundError](https://ts.sdk.modelcontextprotocol.io/v2/api/index/@modelcontextprotocol/client/#resourcenotfounderror)。
鉴权失败、传输 HTTP 404、超时、空内容、MIME/正文不合法和普通内部错误不触发回退。
缺页返回 `APP_RESOURCE_NOT_FOUND`；可识别的 HTTP 401/403 返回 `APP_AUTH_REQUIRED`，
超时返回 `MCP_APP_REQUEST_TIMEOUT`。服务端任意诊断文本不透传到宿主错误提示。

资源读取失败时，stderr 日志输出 `MCP App resource read failed`，包含 session/tool call ID、
服务和工具名称、资源 URI 的 SHA-256、是否入口 UI/回退读取、异常链的类型与栈位置，
以及可用的 MCP 原始错误码、HTTP 状态码和响应结构校验错误类型。
URI 只记录摘要，不记录路径、查询参数或片段；异常消息、响应正文、校验输入、
凭证和栈中的源码/局部变量不写入诊断。成功和主动取消不产生失败日志，原有错误返回、
页面回退与业务调用行为不变。这些日志用于区分协议错误、传输错误及响应解析错误。

入口读取成功时，ACP 覆盖返回 `resolvedResourceUri` 为实际选定地址，不能由服务端正文
伪造。Lab 使用它校验、加载 HTML 和其 CSP，并在视图内单独记录；历史快照地址不改写。
AppBridge 仍回放原输入和完整结果，恢复不会重跑原业务工具或代确认。
新页面解释旧数据的兼容性由 MCP 服务负责；宿主不猜版本号或迁移服务业务字段。

卡片不再持有跨请求的连接 `bindingId`。每次资源读取或工具调用都由
`sessionId` 和 `toolCallId` 定位有效历史中的原调用，再使用当前同名 MCP 连接。
重连或工具目录重新发布后，已打开的卡片可以继续交互，无需重新读取快照或重建页面。
历史授权签名、旧连接器 ID、HTTP origin 和 UI 命名空间均不参与匹配，HTML 不落盘。
AppBridge 回放快照中的输入和完整结果；恢复不会重跑原工具。

单次请求仍固定其开始时解析的连接，执行前继续校验连接器授权、工具可见性和取消状态。
宿主更新或清除凭据后，尚未被替换的旧连接仍拒绝访问；完成重连后的新请求使用新连接。
连接在确认或执行期间变化时，不自动转移或重放该操作。
工具调用回包前再次核对本次请求开始时的连接身份与目录代次；当前连接已被替换或目录
重新发布时返回 `APP_BINDING_EXPIRED`。已完成的远端交互仍保留结果用于去重，
后续使用当前连接的同一 `interactionId` 请求可读取完成回执，不重新执行业务工具。
会话关闭、历史删除和客户端窗口/view 生命周期校验保持有效。

协议变更：`snapshot` 响应以及 `resource` / `call` 请求移除 `bindingId`，
客户端与 Box-Agent 必须配套升级；旧宿主要求该字段时无法直接使用新版响应。

## 执行与授权边界

- 沿用 Box-Agent 已有 MCP 连接及认证，不由 renderer 新建连接或获取凭据。
- App 调用限定原服务、单次请求解析的当前连接及当前请求获准的 connector（未提供请求级状态时使用会话选择）。
  `visibility` 必须包含 `app`；仅含 `app` 的工具不进入模型发现/调用目录。
- App 调用使用会话配置的调用前 Hook 和现有 invocation guards。会话正在运行时使用
  当前能力门，暂停时读取已保存的能力门。Hook 拒绝返回 `MCP_APP_TOOL_DENIED`，
  Hook 调整后的参数继续经过 schema 和能力门校验。`readOnlyHint` 仅供界面呈现参考。
  会话已经开始准备新回合且授权上下文还未就绪时，返回 `MCP_APP_SESSION_BUSY`。
  打开组件不会授予跨服务、跨会话或内置工具执行权限。
- UI 操作不创建 LLM turn，也不追加没有模型 tool-call 配对的 `role=tool` 消息。
  `mcp-app/interaction` 以独立、可忽略事件记录来源、参数、状态及结果。
- `interactionId` 在会话内幂等：相同调用返回已保存结果；改变工具、原调用或参数报冲突。
  幂等记录从完整 Session Log 查询，不随历史编辑回退；已完成或结果未知的远端操作
  不会因编辑后续消息或恢复会话而再次执行。卡片入口仍受当前有效历史约束。
  在途操作可单独取消；会话重绑定和关闭会取消其操作，不借用 prompt 的取消身份。
- 初版接口不提供宿主计费授权桥接。内置收费工具不在同服务 MCP 目录中；
  第三方 MCP 自身的收费规则仍属于服务端/连接器授权，不能通过工具名称推断。

## 模型的状态判断与卡片交接

支持 MCP Apps 的宿主加载带有效 UI 资源的模型可见工具时，在模型工具说明中附加
通用处理指引。识别仅依赖 MCP UI 元数据，不按服务名或业务字段做专用适配。
搜索匹配和简短目录仍使用原始能力说明，避免通用指引干扰状态查询工具的发现。
远端原始 descriptor、工具结果和 App 快照保持原样；App-only 工具仍不进入模型目录。

模型结合实际返回结果区分最终结果、失败、等待用户交互、已提交执行中和状态不明：
需要用户操作时，邀请用户在卡片中完成操作并交接。
不得代确认、开放 App-only 工具或另起重复确认。已提交执行中的任务沿用现有 Agent
循环按需查询，遵守服务端查询间隔及全局步数、超时、取消和权限边界，
没有新增轮询框架或每个 App 的固定查询次数保证，也不自动重提业务操作。
普通交接只用简短操作引导说明卡片入口及下一步，不复述“尚未生成／未完成／待处理”、
报价、费用、积分、请求参数或提示词细节，相关信息以卡片当前显示为准。
已有最终业务结果或失败、用户明确询问状态／费用／参数时，仍以最新证据如实回复；
状态查询失败不能直接当作业务失败。交接不等于业务完成，不承诺回复结束后的后台监控。

这项指引不会把普通 MCP 改成 UI 工具，不证明 iframe 已加载，也不自动同步后续卡片
交互或唤醒模型。实际模型是否选择查询以及最终措辞仍需真实模型验收。

## App 模型上下文的生命周期

`ui/update-model-context` 由桌面宿主在实例内存中保存最新文本/结构化值，
不调用 ACP，不写 Session Log，也不在重启、重新打开 App 时自动恢复。
实例关闭、离屏回收或工作台卸载后清理；显式空更新清空当前实例的值。
已发送或已入队消息仍保留当时的 `inputContext.appshot` 快照，不受后续清理影响。

普通任务打开、发送和入队不依赖任何 App 上下文恢复接口。
合并当前 App 上下文超限时仅省略本轮可选 App 数据，保留原输入与引用。
旧版本曾写入的 `mcp-app/model-context` 属于 ignorable 事件，保留原文件且不再投影；
已移除 `_mcp/apps/context`、`_mcp/apps/contexts`，调用返回不支持。
这不改变原工具结果快照或 App 工具交互记录的持久化，也不改变工具调用授权。

## 失败、历史与限制

协议/授权失败返回 JSON-RPC error，`data.code` 是稳定的 `MCP_APP_*` 或 `APP_*` 代码；
工具业务错误可返回 `CallToolResult.isError: true`。ACP 适配层的本地异常仅返回固定代码；
服务端返回的工具正文仍按业务结果处理。

`cancelled: true` 仅确认本地发出了取消，不能证明远端未执行。
超时、断线、取消或执行后无法交付完整结果都可能表示结果未知。
宿主不得自动重试业务调用；同一 `interactionId` 的未知结果也不会重新执行。

历史默认展示已保存的结果/入口，由用户显式打开组件后才运行 HTML/JS。
仅禁用 `tools/call` 不能保证已运行脚本没有网络副作用；宿主负责沙箱、CSP、
消息来源校验和视图生命周期。会话恢复和读取快照本身不会重跑业务工具。

入口 UI 必须是 `ui://` URI，MIME 必须精确匹配上述类型；同时兼容旧的
`_meta["ui/resourceUri"]`，存在新版字段时以新版为准。
资源正文未提供 UI 元数据时，在同一读取超时内沿 `resources/list.nextCursor` 查找
目标资源的静态 CSP；找到目标、分页结束或游标重复即停止，正文 UI 元数据保持优先。
App 可通过原服务的已认证连接读取其他 MCP 资源，保留标准文本、base64、MIME、
多内容项及元数据；所有读取仍校验会话、binding、连接代次和 connector 权限。
URI 始终交给该 MCP Server 处理，不在宿主解释为本地文件或另建网络连接。
资源读取和 App 的 `tools/call` 均不启动模型轮次；`ui/update-model-context`
是另一条由 App 显式发起、供宿主未来对话引用的可选协议能力。
当前支持固定 MCP session，
不为专用 session-lease 浏览器池增加 UI 支持。
完整快照、App 工具结果及资源的序列化上限为 1 MiB；App 调用参数上限为 256 KiB。
MCP 层按 UTF-8 JSON 计量；ACP 快照、调用参数及调用结果另按 ASCII-safe JSON 计量，
以先达到的上限为准。
超限时不把截断数据当完整结果；保留文本降级或明确失败，不重跑工具。

## 离线联调与验证边界

在本仓库执行：

```sh
uv run --no-sync python tests/fixtures/mcp_apps_agent.py --state-dir /tmp/mcp-apps-acceptance
```

该入口使用真实 `BoxACPAgent`、ACP stdio 及 `mcp_apps_server.py` 的 MCP stdio，
模型为固定脚本，不读取用户配置、登录 token 或调用模型网络端点。
正常 `initialize` 后轮询 `_mcp/status`，等待 `flight-fixture.state == "connected"`；
`session/new` 的 cwd 使用上述目录内的 `workspace`，再发送一次普通 `session/prompt`。
原调用 ID 固定为 `flights-original`；`refresh_flights` 是 App-only 工具，
`calls.jsonl` 记录实际执行，可用来核对刷新及历史恢复没有重复调用。

直接回归为 `tests/test_mcp_apps.py`、`tests/test_acp_mcp_apps.py`。
源码测试和离线 fixture 只证明对应源码及协议链路；不证明正式 runtime 已构建、
安装或发布。客户端原生验收、正式打包与部署状态应分别记录。
