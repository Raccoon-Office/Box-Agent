# 云端委派到本机 · raccoon-lab 客户端交接

**交付目标**　用户在云会话中把任务交给一台已授权的在线桌面。桌面创建本地 Box-Agent 子会话，在本地历史标记「云端委派」，向云端返回接单、等待和最终结果。用户可以在原桌面补充信息，也可以在有权访问父会话的端上批准一次受限文件操作。

**从哪里开始**　桌面实现见 raccoon-lab MR !1276（`feat/device-subagent-cloud` → `feat/cloud-computer`），Box-Agent 实现见 PR #291（`feat/device-subagent-main` → `main`）。本轮 session 操作需使用包含相同协议修订的两份源码和独立 mock。文中桌面路径相对 `raccoon-lab/apps/office-desktop/`。

## 1. 已有实现与接手事项

| 场景 | 当前实现 | 接手时看哪里 |
| --- | --- | --- |
| 登录、切账号、退出 | 主进程建立和回收 WS，以清洗后的本机主机名注册。路由凭据绑定登录账号，切换时先关闭旧连接。未配置或账号不匹配时保持普通客户端行为 | `runtime/electron/main/client/startClientHost.ts`、`runtime/electron/main/device/startDeviceRouter.ts`、`deviceRpcPeer.ts`。生产联调需与 2c 确定地址、设备凭据签发、更新和撤销 |
| 接单、派发、停止 | host 持久化接单后即可返回；driver 通过现有 BoxAgentManager 驱动本地 ACP。新子会话使用客户端默认工作区（默认目录名 `RaccoonWork`），相同会话后续 send 沿用绑定；没有必填 workspaceId | `runtime/electron/main/device/deviceSubagentHost.ts`、`deviceBoxAgentDriver.ts`、`runtime/electron/main/agent/boxAgentManager.ts` |
| 桌面历史、用户补答 | 本机已登录账号可见委派会话。入口保留只读历史，底部 DecisionCard 支持回答或取消。补答之后的流式消息和普通本地续跑共用历史写入 | `registerConversationHandlers.ts`、`decisionResolution.ts`、`decisionContinuation.ts` 均在 `runtime/electron/main/client/conversation/` 下 |
| 云端查询本地历史 | `session.list`、`session.read` 限于当前父云会话委派给本机的子会话。账号切换后重新校验 | `runtime/electron/main/device/deviceSessionHistory.ts`、`runtime/electron/main/localChatStorage/repository.ts` |
| 子轮产物取件 | 从本地 ACP 产物登记信息生成取件 ID。需要云端文件时复用桌面云文件服务上传，在文件服务确认后上报云端引用 | `runtime/electron/main/device/deviceArtifacts.ts`、`runtime/electron/main/client/cloud-files/service.ts` |
| 远端用户审批 | 桌面保留正在等待的本地 ACP 权限请求，收到有权用户的决定后回填原请求。B 复用云会话审批卡，云 Harness 将决定发回 A。正式 2c 部署需验证身份与路由 | `runtime/electron/main/device/devicePermission.ts`、`deviceSubagentHost.ts`；[后端方法与路由](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md) |

## 2. 从注册到上报

桌面在已认证的 WS 连接上发送 JSON-RPC 2.0，生产使用 `wss://`。同账号下的另一台设备即使同名，也不能接管仍被占用的设备名。以下示例使用虚构主机名 `PC-A`，实际由 `resolveDeviceRouterId()` 从 `os.hostname()` 取得。注册报文在 capabilities 中列出能力组。跨端工具中只有 `list` 由 2c 生成结果，其他业务结果由桌面生成。

```json
{"jsonrpc":"2.0","id":1,"method":"_device/register","params":{"version":1,"deviceId":"PC-A","capabilities":["session.history","session.execute","artifact.upload"]}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"registered":true}}
```

上例为已接入文件服务的桌面。注册时由桌面按 driver 实际可执行的能力生成完整快照：同时支持列举和读取授权历史才报 `session.history`；支持接单、续发、查轮次、等待、取消和上报才报 `session.execute`；接入上传服务才报 `artifact.upload`。三个组独立。每次重连先等本次注册回执，再投递 journal 中未确认的事件。

1. **解析请求**。2c 转发 `_device/request`。`params.sessionId` 是云父会话，`params.parentTurnId` 是派发父轮，`params.deviceId` 必须指向本机。按 `params.operation` 精确分派。`session.list`、`session.read` 返回授权历史；`session.start`、`session.send` 发起子轮；`session.get_turn`、`session.wait`、`session.cancel` 作用于明确指定的轮次。线上的方法保留前导下划线。
2. **分配目标**。`start` 创建本地子会话 ID 和逻辑子轮 ID；`send` 复用已授权的会话，分配新子轮。有在途任务返回 `SESSION_BUSY`。将请求和回执写入按账号、设备隔离的 journal 后再接单。相同 `operationId`、相同请求重投时返回相同结果，写入失败时留给原请求重试。
3. **运行本地 ACP**。原生宿主在 `initialize.params._meta` 声明 `{"deviceDelegationReceiver":{"version":1}}`。用 `createExternalSession` 建立或复用实际 ACP 会话，透传下面的委派元数据。`remote-device-01` 是桌面对外 ID，`acp-child-01` 是本地 ACP ID。普通用户在本地自行发起的会话沿用原有路径。
4. **上报状态**。每个逻辑子轮独立分配递增 `seq`，从 1 起，初始接单尚未产生事件。`_device/event` 是带 ID 的请求；收到 `recorded:true` 后删除本地相应待投递项。`get` 返回本机快照；`cancel` 的 `accepted` 只是接收取消请求，最终以子轮事件为准。

例如，桌面收到的 start 请求、返回的接单结果和发给本地 ACP 的首轮请求如下。`operationId` 贯穿接单和事件；查询、取消用接单返回的 `deviceId/sessionId/turnId`。

```json
{"jsonrpc":"2.0","id":11,"method":"_device/request","params":{"version":1,"sessionId":"cloud-1","parentTurnId":"c1","deviceId":"PC-A","operation":"session.start","operationId":"start-01","args":{"instruction":"生成 report.txt；缺少项目编号时先询问"}}}
```

```json
{"jsonrpc":"2.0","id":11,"result":{"deviceId":"PC-A","sessionId":"remote-device-01","turnId":"t1","status":"accepted"}}
```

```json
{"jsonrpc":"2.0","id":31,"method":"session/prompt","params":{"sessionId":"acp-child-01","prompt":[{"type":"text","text":"生成 report.txt；缺少项目编号时先询问"}],"_meta":{"turn_id":"t1","deviceDelegation":{"parentSessionId":"cloud-1","operationId":"start-01","parentTurnId":"c1"}}}}
```

### 客户端响应设备请求

- **桌面路由和执行宿主**（`deviceRpcPeer.ts`、`deviceSubagentHost.ts`）：按[协议映射表](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md#本轮-acp-报文差异)切换五个执行操作的分派、接单去重和取消处理。同机与跨端共用分派入口。新增桌面侧 `session.wait` 处理。每次只接收本机的一组 targets；校验设备一致、父会话归属及子轮身份，失败时拒绝整组。get 与 wait 复用同一个本机快照读取路径，返回 `source:"device"`。
- **ACP 错误回传**。桌面 RPC 适配层保留 JSON-RPC 错误的 `code/message/data`。已经收到并处理过的指令继续按本机实际结果回包。2c 在桌面收件前确认离线或在转发后失去回执时，分别报告该次投递的状态。
- **wait 的生命周期**：对 journal 的已提交状态检查就绪，订阅变化时防止检查与订阅之间漏事件（例如先订阅再复查）。终态、等待输入或审批立即返回；业务超时前再读一次完整快照。断线、宿主关闭、超时或响应后释放订阅和定时器，只结束等待。恢复连接时重新注册并补投未确认事件。
- **异常验收**：模拟客户端仍在线但从不回包、接单后回包丢失、上传中断和 Stop 遇到离线。核对父 loop 能收到工具错误、同一 operationId 只执行一次、子轮终态始终基于桌面证据。再跑同机断开 2c 的用例。报文及截止规则见[ACP 投递错误与发起端截止时间](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md#acp-投递错误与发起端截止时间)。

### 同机会话协作

同机 A 操作 B 的路径为：A 通过 ACP 反向发出 `_device/request` → 桌面主进程校验来源、设备和目标归属 → 复用设备执行宿主。启动和续发由 BoxAgentManager 在 B 的 ACP 会话中投递，取消定位到 B 的目标子轮。授权历史查询直接复用桌面会话库。桌面将 `_device/event` 投给来源 A，由其 Harness 处理状态与结果。本地开发时设置 `RACCOON_DEVICE_LOCAL_DELEGATION=1` 并使用已登录的桌面账号。独立 mock 仓的 `node examples/box-desktop-smoke.mjs --local` 用两个真实 ACP 进程验证设备发现、派发、等待、读取结果及父子 Session Log；该用例无需启动 backend。

同机沿用同一份模型工具参数。本地宿主处理 A 的 `device_list` 时可直接提供本机设备及其实际能力，无需连接 2c。选定本机后传清洗后的真实 `deviceId`；若同时展示远端设备，由宿主标识哪一项是当前设备。模型调用 `session_send` 的示例：

```json
{"deviceId":"PC-A","sessionId":"remote-device-01","instruction":"沿用上一轮的口径再算一次"}
```

桌面路由使用当前登录账号及设备绑定核对目标；同机与跨端均沿用桌面对外的子会话 ID。ACP 反向请求外层 `params.sessionId` 属于 A，`params.args.sessionId` 指向 B，桌面负责映射 B 的实际 ACP 会话。查轮次、等待和取消使用接单回执中的真实设备、子会话和子轮 ID。查询能力与执行授权分别校验；同机也只开放当前授权范围。验收时断开 2c，验证同机接单、等待和事件回执仍可完成；指定其他设备时不得误投本机。

**父轮自动续跑。** 桌面按 `_device/turn started` 的 `sessionId,turnId` 登记续跑轮。上一轮仍在归档时，新轮也可能立即发出 `session.send`。两轮须分别归属，旧轮收尾只能清理自己的状态。接到同一 turnId 的 `finished` 后，通过桌面既有的终态事件发布结果。订阅方按续跑自己的 `promptId` 接收，错过通知时可在当前会话的内存缓存中通过 `getPromptOutcome` 查询最近的终态。成功、暂停和模型报告的失败按普通 PromptResponse 解析，取消使用 `result.stopReason:"cancelled"`，请求异常使用 `params.error`。

```json
{"jsonrpc":"2.0","method":"_device/turn","params":{"version":1,"sessionId":"parent-A","turnId":"device-wake-01","phase":"finished","result":{"stopReason":"end_turn","_meta":{"usage":{"totalTokens":120}}}}}
```

## 3. 子轮等答复，与子轮等授权

**等待用户输入**　解析本地 ACP `session/update` 中的有效输入或决策工具结果，结合该轮的完成状态判断；普通 checkpoint 暂停不能靠一张没有请求 ID 的空卡继续。可取到问题时，按原逻辑子轮报 `waiting_input`。用户在原桌面子会话答复；云父会话可读状态和问题。二者使用不同的交付入口。

```json
{"jsonrpc":"2.0","id":23,"method":"_device/event","params":{"version":1,"sessionId":"cloud-1","event":{"deviceId":"PC-A","sessionId":"remote-device-01","turnId":"t1","operationId":"start-01","seq":2,"status":"waiting_input","inputRequest":{"question":"项目编号是什么？"}}}}
```

补答使用新的 ACP `turn_id`、同一个本地 ACP `sessionId`，并由 `DecisionResolutionService` 生成原请求的决策元数据。`continuationOfTurnId` 指向**紧邻的上一段已等待的 ACP 轮次**。多次提问时该指针逐段前移，向 2c 上报的 `turnId` 始终是根逻辑子轮 `t1`。下例只展示 `session/prompt.params._meta`；正文通过现有决策服务构造。没有单独 request ID 的输入请求使用真实 toolCallId 作为 request ID。

```json
{
  "turn_id":"t2",
  "deviceDelegation":{"parentSessionId":"cloud-1","operationId":"start-01","parentTurnId":"c1","continuationOfTurnId":"t1"},
  "userDecision":{"request_id":"call-4","tool_call_id":"call-4","decision_kind":"missing_input","request_type":"input","missing_fields":["project_code"],"custom_text":"RC12","trigger":"user"},
  "user_decision":{"request_id":"call-4","tool_call_id":"call-4","decision_kind":"missing_input","request_type":"input","missing_fields":["project_code"],"custom_text":"RC12","trigger":"user"}
}
```

**等待文件审批**　当本地 ACP 发起 `session/request_permission` 时，核对本地 ACP 会话、在途请求，以及 `toolCall.rawInput.deviceApprovalTurnId` 等于根子轮 `t1`。当前可远端审批的是符合本地策略的用户主目录内单个具体文件的读或写。发 `waiting_approval` 时携带 `approval.{approvalRequestId,title,expiresAt,decisions}`，其中 `expiresAt` 带时区、`decisions` 为 `["allow_once","deny"]`。在父会话端展示具体文件和操作，仅在用户明确选择后提交决定。普通命令、目录授权及其他本机策略沿现有权限路径。

```json
{"jsonrpc":"2.0","id":42,"method":"_device/permission_decision","params":{"deviceId":"PC-A","cloudSessionId":"cloud-1","sessionId":"remote-device-01","turnId":"t1","approvalRequestId":"approval-7","decision":"allow_once"}}
```

B 在 ACP `initialize` 的 `params._meta.deviceApprovalRelay` 传 `true` 开启审批中继。收到 `waiting_approval` 后云 Harness 在父会话发起标准 `session/request_permission`，携带 `params._meta.deviceApproval.{approvalRequestId,expiresAt}`、含设备和文件操作的标题，以及一次允许和拒绝选项。B 用原 RPC ID 向云 Harness 回答，审批卡持续到用户选择或到期，可跨父轮自然结束。云 Harness 关联原子轮后发出上例请求，2c 校验身份与归属后路由至 A。桌面再次校验目标、时效和允许选项，向原权限请求返回结果并应答 `{"applied":true}`。拒绝、过期、已取消或目标错配时文件操作不能被放行。答复用户问题本身不会获得文件权限。可按 `waiting_input → running → waiting_approval → running → succeeded` 验证同一逻辑子轮。

同一会话重新 `session/resume` 时，B 移除上一次绑定的审批卡。恢复成功后，同一 `approvalRequestId` 可以对应新的 JSON-RPC ID，B 仅向当前请求应答。已记录选择且仍待交付时，Harness 沿用原选择。使用稳定 ID 的 `session/new` 重开时也恢复待办；只读 `session/load` 期间仅记录审批事件，直到会话进入执行状态。

独立 mock 的 `--approval-running`、`--approval` 和 `--approval-resume` 分别覆盖父轮运行中、父轮自然结束、旧卡未点即恢复的路径。

### 补答后的本地历史

用户提交答复后，`DecisionResolutionService` 认领原问题，设备 driver 通过 `sendDeviceDelegatedPrompt` 保留委派来源和根子轮审批绑定。新 ACP 段复用 `openDecisionContinuationHistory` 和 `subscribePromptEvents`。先建 assistant 占位，本地 BoxAgentManager 返回 `promptId` 后，按 `sessionId` 和 `promptId` 消费事件，包括回执前已到达的流式片段。阶段快照按顺序写入本地会话库。终态事件携带的 `changeSet` 作为 `change_set` 阶段保留。

验收时，让子轮先提问，用户回答后输出一段正文，再等待文件审批。审批卡仍挂起时，本地历史应已保存这段正文，消息记录的 `status` 为 `"streaming"`，`content.status` 为 `"running"`。批准后检查同一条续跑消息的最终快照和文件变更。云端通过 `get/wait` 查看根子轮状态、待办问题或审批描述，完整阶段历史留在桌面。

### 历史可见范围与分页

`DeviceSubagentHost` 从本机 journal 取出该父云会话名下的子会话 ID。`session.list` 每页只调用一次存储层 `listSessions`，通过 `deviceSessionScope: { ids, accountId }` 传入授权范围；SQLite 对可用性、`source:"device"` 和所属账号过滤，再按授权 ID 清单顺序分页。`deviceSessionScope` 是桌面内部参数。线上只传 `args.cursor` 和 `args.limit`。验收应覆盖清单里混有其他账号、不可用记录，以及第一页过滤后仍有下一页的情况。

`session.read` 先校验父云会话归属，再读本地记录。委派首条消息通过 `inputSource:"runtime"`、`inputKind:"device_delegation"` 及 `deviceDelegation` 标识来源，返回的 `turnId` 取该委派的 `runId`。普通消息和缺少来源信息的旧记录沿用存储的 `turn_id`。本地会话访问入口执行基础鉴权后追加设备账号检查；Agent Bridge 的独立入口执行完整鉴权。

## 4. 恢复与记录

| 情况 | 客户端可观察行为 |
| --- | --- |
| WS 临时断开，桌面进程仍在 | 正在运行的子任务可继续。本地保存事件，重连注册成功后按序重投。握手/回执超时有界；不要因为某次请求超时就合成子任务失败 |
| 桌面进程重启 | 从 journal 恢复接单和待投递事件。`running` 且 `deliveryStatus:"pending"` 的子轮继续交付已登记的产物。其他仍活跃的子轮登记为 `failed`，避免重跑可能已有副作用的工具。切账号时先退役旧 driver，账号与设备隔离 journal，存在 Office 身份时也纳入隔离键 |
| 本地执行或上传期间 | 本地 ACP prompt 使用 BoxAgentManager 已有的防空闲挂起机制；文件上传通过 `withPowerSaveBlocker('device-upload', …)` 覆盖传输期。并发上传应在最后一项结束后释放，失败路径也要释放。在打包客户端验收操作系统的实际休眠行为 |
| 用户点击云端 Stop | 云端以点击时的父轮 ID 发 `_device/cancel_turn`。桌面收到的取消是 `session.cancel`，只操作指定设备、会话和子轮。若正处于异步准备阶段，也须挡住随后发送的 ACP prompt |
| 产物和历史 | 文件上报使用 `result.artifactRefs`，跨端取件及云端引用见后文“设备产物按需交付”。首轮本机委派在 Session Log 的 `source` 为 `runtime`，人工补答为 `user`，云父轮自动续跑为 `runtime`。读取或回放时保留这些来源 |

## 5. 联调与验收

1. **桌面基础响应**。在已安装该分支依赖的 `apps/office-desktop` 运行 `pnpm desktop:test:device`。核对账号切换、待答、拒绝/过期审批、取消期间无额外 ACP prompt。桌面显示名、通知和历史页用真实登录账号验收。
2. **连接独立 mock**。在 `device-delegation-mock` 按其 `README.md` 配置三个角色的凭据并启动。启动桌面进程前，显式设置 `RACCOON_DEVICE_ROUTER_URL`（mock 默认监听 `ws://127.0.0.1:8787/`）、`RACCOON_DEVICE_ROUTER_TOKEN`（对应 mock 的 `LOCAL_DESKTOP_TOKEN`）、`RACCOON_DEVICE_ROUTER_ACCOUNT_ID`（当前登录资料 ID）。缺少任一项或账号不匹配时，桌面跳过注册。连接成功后，首帧应为注册，设备 ID 应等于清洗后的主机名。
3. **无界面跨仓复现**。先在独立 mock 仓安装依赖；`DEVICE_TEST_BOX` 指向 Box-Agent 功能工作树，`DEVICE_TEST_PYTHON` 指向装有其依赖的 Python，`DEVICE_TEST_DESKTOP` 指向桌面的 `apps/office-desktop`。Windows 可选设 `DEVICE_TEST_BASE` 为已有短路径。运行 `node examples/box-desktop-smoke.mjs --input-approval`；同一入口还支持 `--write`、`--waiting-input`、`--approval`、`--cancel`、`--artifact-required` 和 `--artifact-later`。文件场景还校验实际 PUT 的字节及 Session Log 中的补报。探针使用确定性模型和两个真实 ACP 源码进程。
4. **看两侧结果**。在独立 mock 的跨仓探针输出目录检查 `journal.sqlite3`（`entries` 保存子轮，`events` 保存待确认的上报）、`local-profile/sessions/*/session.jsonl` 和 `cloud-profile/sessions/*/session.jsonl`。组合用例应看到人工答复保留用户来源、授权前文件不存在、授权后文件写成、父模型消费最终序号并自然结束。
5. **上线前联合验收**。使用生产路由凭据和打包桌面完成登录/注销、主机同名冲突、桌面断线、换设备审批、设备历史账号隔离及长任务期间的防空闲睡眠。已验证的源码、SQLite、ACP 和 mock 结果不能代替这一步。

父轮状态注入、续跑和旧客户端兼容规则见[Box-Agent 协议说明](DEVICE_DELEGATION_LOCAL.md)。

### 桌面整链入口

在桌面工作树的 `apps/office-desktop` 运行 `pnpm desktop:test:device-chain`。先安装桌面及独立 mock 的依赖，并设置下列路径。测试入口自行启动独立仓的 `server.mjs`，分配回环端口和临时角色凭据。手工调试安装版客户端时才需要第 2 步的常驻 mock。

独立 mock 由单独项目提供。跨机复现需要先取得该项目源码和 `package-lock.json`，再在 mock 目录执行 `npm ci`。

| 环境变量 | 指向 |
| --- | --- |
| `DEVICE_TEST_BOX` | 含本期改动的 Box-Agent 工作树根目录 |
| `DEVICE_TEST_PYTHON` | 已装该工作树依赖的解释器可执行文件，ACP SDK 为 `0.12.1` |
| `DEVICE_TEST_MOCK` | 含 `server.mjs` 的独立 mock 根目录。默认查找桌面工作树的同级目录 `device-delegation-mock` |
| `DEVICE_TEST_ROOT` | 本次独用的短路径空目录，保存 journal、聊天库、两侧 Session Log 和 `evidence.json` |

读取 `evidence.json` 时，检查 `passed`、`ownedProcessesCleanedUp` 以及 `sourceEvidence` 中父子两项的 `result._meta.replayed`。改造前的源码联调覆盖八项基础操作、丢回执重投、跨端审批、取消和双端回放，记录了 9 次子轮执行及 26 条父会话事件回执。次数用于定位这次记录；验收依据是断言和回放内容。整链使用确定性模型和实际 ACP 进程，桌面的原生续跑写入另由 `tests/device/device-subagent.test.mjs` 验证。

需要验证真实模型审批时，在同一目录运行 `node scripts/test/device-approval-live.mjs allow`，复用上述 Box、Python 和 mock 路径。此入口读取已配置的模型认证并产生模型调用，默认模型为 `sn-deepseek-v4-1-flash-1017-p`。最近一次整链为确定性用例，打包客户端、真实模型和生产 2c 的验收记录应分别留存。

## 设备产物按需交付

父会话由 2c 在 `session/new` 的 `_meta.device` 设置 `{"version":1,"artifactUpload":true}`。设备注册时通过 `capabilities` 上报 `artifact.upload`。两个条件就绪后才能要求云端取文件。仅设置 `version:1` 的父会话沿用原有八个工具和参数。

| 时点 | 请求或回执 | 接收方的动作 |
| --- | --- | --- |
| 委派时只需摘要 | `session.start` 或 `session.send` 省略 `args.artifactDelivery` | 桌面回传摘要和已登记文件的 `result.artifactRefs`。每项含 `deviceId, artifactId, name`，可以在源桌面保留原件 |
| 委派时已知需要云端文件 | `session.start` 或 `session.send` 增加 `args.artifactDelivery:"cloud"` | 桌面上传该子轮发布的交付文件。传输时返回 `status:"running", deliveryStatus:"pending"`，所有文件服务回执就绪后才报 `succeeded`。没有可交付文件或上传失败时以 `failed` 交付具体错误 |
| 事后需要一个文件 | 父 Agent 的 `device_artifact_upload` 或经身份验证的客户端取件请求，使用下方同一条 `_device/request` | 2c 校验账号、父会话和子轮归属。新取件 RPC 转源桌面；写入前已离线则回 `DEVICE_OFFLINE`。调用方已收到云端引用时可以直接访问文件 |
| 云端确认收件 | 文件服务完成导入后提供 `file_uri` | 桌面把它作为 `cloudFileRef` 随取件回执和 `_device/event` 上报。2c 可靠转交事件，云 Box-Agent 更新 Session Log。get、wait 可看到新增引用。已经消费过的完成结果不会再唤醒父轮 |

例如，需要桌面生成的报告直接进入云工作区时，2c 发给桌面以下报文。`workspaceId` 由 2c 写在外层，`artifactDelivery` 位于 `args` 中。

```json
{"jsonrpc":"2.0","id":16,"method":"_device/request","params":{"version":1,"sessionId":"cloud-parent","parentTurnId":"c1","deviceId":"OfficePC","operation":"session.start","operationId":"start-report","workspaceId":"cloud-work-42","args":{"instruction":"生成并发布 report.csv","artifactDelivery":"cloud"}}}
```

事后取件时，外层 `sessionId` 为云父会话，`args.sessionId` 和 `args.turnId` 为本地委派子轮。下例假定该子轮已经上报这个取件 ID。调用方从原清单原样取值，桌面沿用派发时绑定的云工作区。

```json
{"jsonrpc":"2.0","id":17,"method":"_device/request","params":{"version":1,"operation":"artifact.upload","operationId":"fetch-17","sessionId":"cloud-parent","deviceId":"OfficePC","args":{"sessionId":"local-child","turnId":"child-turn","artifactId":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}}
```

成功回执的 `result` 示例（地址仅示意，真实引用原样取自文件服务）。

```json
{"deviceId":"OfficePC","sessionId":"local-child","turnId":"child-turn","artifactId":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","name":"report.csv","cloudFileRef":"file:///sandbox/device-artifacts/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef/report.csv"}
```

2c 在派发到支持取件的桌面时，将父会话已绑定的云工作区 ID 写入请求外层 `workspaceId`，供日后取件沿用。此值由 2c 解析，独立 mock 使用 `RaccoonCloudWork`。桌面根据当前账号和子轮记录找到文件，校验目录、文件身份与发布时的 SHA-256，通过已有云文件服务申请上传、发送字节并确认导入。目标相对路径为 `device-artifacts/<artifactId>/<name>`。网关认证和短期 PUT 地址留在桌面文件服务内部。

桌面在子轮 journal 内保存 `artifactId`（对外取件 ID）、发布时的 SHA-256。Box 提供修订信息时，同时保存 `sourceArtifactId`、`sourceArtifactRevisionId` 及可选的 `sourceTaskId`。有修订信息时，即使路径和内容相同，新修订也有独立取件 ID。旧记录继续使用已发出的取件 ID。本地绝对路径、哈希和原始修订关联留在桌面，对外提供的字段以 `artifactRef()` 为准。一次成功子轮最多发布 64 项；超限返回 `DEVICE_ARTIFACT_LIMIT`。

桌面通过 `uploadDeviceArtifact` 按云工作区和取件 ID 复用上传操作。调用文件服务 `POST /uploads` 申请上传，用签名地址 PUT 字节，再调用 `POST /uploads/{uploadId}/complete` 确认。只有该文件返回 `status:"ready"` 和 `file_uri` 后才对外提供 `cloudFileRef`。这里的路径相对现有云文件服务。

首次打开 journal 时，桌面在一个 SQLite 事务内迁移旧 JSON。后续状态更新和对应的待投递事件在同一事务中写入，收到回执后删除该事件。打包时确认主进程能加载 `deviceJournalWorker.js`，用安装版完成一次接单、重启和事件补投。

上传超时允许重试同一文件版本。并发取件共享一次传输，已完成取件复用原引用。上传前发现文件内容与登记的 SHA-256 不一致时，返回 `SOURCE_CHANGED`。终态之后只允许为已登记的同版产物补入云端引用，子轮原有摘要和结论保留。
