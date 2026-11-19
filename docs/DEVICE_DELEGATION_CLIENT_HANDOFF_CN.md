# 云端委派到本机 · raccoon-lab 客户端交接

**交付目标**　用户在云会话中把任务交给一台已授权的在线桌面。桌面创建本地 Box-Agent 子会话，在本地历史标记「云端委派」，向云端返回接单、等待和最终结果。用户可以在原桌面补充信息，也可以在有权访问父会话的端上批准一次受限文件操作。

**从哪里开始**　桌面改动在 `feat/device-subagent-cloud` 工作树，目标分支为 `feat/cloud-computer`，迁移基线 `da2127b10`；配套 Box-Agent 目标为 `main`，基线 `dcd65ffb`。交接时两端功能均为未提交源码。文中桌面路径以 `raccoon-lab/apps/office-desktop/` 为根。先用已有实现对接生产路由，再验收打包客户端。

## 1. 已有实现与接手事项

| 场景 | 当前实现 | 接手时看哪里 |
| --- | --- | --- |
| 登录、切账号、退出 | 主进程建立和回收 WS，以清洗后的本机主机名注册。路由凭据绑定登录账号，切换时先关闭旧连接。未配置或账号不匹配时保持普通客户端行为 | `runtime/electron/main/client/startClientHost.ts`、`runtime/electron/main/device/startDeviceRouter.ts`、`deviceRpcPeer.ts`。生产联调需与 2c 确定地址、设备凭据签发、更新和撤销 |
| 接单、派发、停止 | host 持久化接单后即可返回；driver 通过现有 BoxAgentManager 驱动本地 ACP。新子会话使用客户端默认工作区（默认目录名 `RaccoonWork`），相同会话后续 send 沿用绑定；没有必填 workspaceId | `runtime/electron/main/device/deviceSubagentHost.ts`、`deviceBoxAgentDriver.ts`、`runtime/electron/main/agent/boxAgentManager.ts` |
| 桌面历史、用户补答 | 本机已登录账号可见委派会话。入口保留只读历史，对待答请求复用底部 DecisionCard；允许回答或取消。云端查询只返回当前父会话有权访问的本地子会话 | `runtime/electron/main/device/deviceSessionHistory.ts`、`runtime/electron/main/client/conversation/registerConversationHandlers.ts`、`decisionResolution.ts`、`app/local-agent/composer/workbench-composer.tsx` |
| 远端用户审批 | 桌面保留正在等待的本地 ACP 权限请求，收到有权用户的决定后回填原请求。独立 mock 已模拟审批端；正式云会话界面的审批展示、用户身份和返回路径需与 2c 联调 | `runtime/electron/main/device/devicePermission.ts`、`deviceSubagentHost.ts`；[后端方法与路由](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md) |

## 2. 从注册到上报

桌面在已认证的 WS 连接上发送 JSON-RPC 2.0，生产使用 `wss://`。同账号下的另一台设备即使同名，也不能接管仍被占用的设备名。以下示例使用虚构主机名 `PC-A`，实际由 `resolveDeviceRouterId()` 从 `os.hostname()` 取得。客户端只上报当前可执行的操作；`list` 和 `subagent.wait` 交给 2c。

```json
{"jsonrpc":"2.0","id":1,"method":"_device/register","params":{"version":1,"deviceId":"PC-A","capabilities":["session.list","session.read","subagent.start","subagent.send","subagent.get","subagent.cancel"]}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"registered":true}}
```

1. **解析请求**。2c 转发 `_device/request`。`params.sessionId` 是云父会话，`params.parentTurnId` 是派发父轮，`params.deviceId` 必须指向本机。以 `operation` 选择实现；`session.list/read` 返回授权范围内的历史，`start/send/get/cancel` 操作子会话。线上的方法保留前导下划线。
2. **分配目标**。`start` 创建本地子会话 ID 和逻辑子轮 ID；`send` 复用已授权的会话，分配新子轮。有在途任务返回 `SESSION_BUSY`。将请求和回执写入按账号、设备隔离的 journal 后再接单。相同 `operationId`、相同请求重投时返回相同结果，写入失败时留给原请求重试。
3. **运行本地 ACP**。原生宿主在 `initialize.params._meta` 声明 `{"deviceDelegationReceiver":{"version":1}}`。用 `createExternalSession` 建立或复用实际 ACP 会话，透传下面的委派元数据。`remote-device-01` 是桌面对外 ID，`acp-child-01` 是本地 ACP ID。普通用户在本地自行发起的会话沿用原有路径。
4. **上报状态**。每个逻辑子轮独立分配递增 `seq`，从 1 起，初始接单尚未产生事件。`_device/event` 是带 ID 的请求；收到 `recorded:true` 后删除本地相应待投递项。`get` 返回本机快照；`cancel` 的 `accepted` 只是接收取消请求，最终以子轮事件为准。

例如，桌面收到的 start 请求、返回的接单结果和发给本地 ACP 的首轮请求如下。`operationId` 贯穿接单和事件；查询、取消用接单返回的 `deviceId/sessionId/turnId`。

```json
{"jsonrpc":"2.0","id":11,"method":"_device/request","params":{"version":1,"sessionId":"cloud-1","parentTurnId":"c1","deviceId":"PC-A","operation":"subagent.start","operationId":"start-01","args":{"instruction":"生成 report.txt；缺少项目编号时先询问"}}}
```

```json
{"jsonrpc":"2.0","id":11,"result":{"deviceId":"PC-A","sessionId":"remote-device-01","turnId":"t1","status":"accepted"}}
```

```json
{"jsonrpc":"2.0","id":31,"method":"session/prompt","params":{"sessionId":"acp-child-01","prompt":[{"type":"text","text":"生成 report.txt；缺少项目编号时先询问"}],"_meta":{"turn_id":"t1","deviceDelegation":{"parentSessionId":"cloud-1","operationId":"start-01","parentTurnId":"c1"}}}}
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

2c 鉴别审批人后将决定转发给桌面。桌面再次校验目标、时效和允许选项，向原权限请求返回结果并应答 `{"applied":true}`。拒绝、过期、已取消或目标错配时文件操作不能被放行。答复用户问题本身不会获得文件权限。可按 `waiting_input → running → waiting_approval → running → succeeded` 验证同一逻辑子轮。

## 4. 恢复与记录

| 情况 | 客户端可观察行为 |
| --- | --- |
| WS 临时断开，桌面进程仍在 | 正在运行的子任务可继续。本地保存事件，重连注册成功后按序重投。握手/回执超时有界；不要因为某次请求超时就合成子任务失败 |
| 桌面进程重启 | 从 journal 读取接单及待投递事件；此前仍活跃的子轮登记为 `failed` 并反馈原因，防止重复执行可能已有副作用的工具。用户切换账号时先退役旧 driver，旧会话历史和待审批不能交给新账号 |
| 用户点击云端 Stop | 云端以点击时的父轮 ID 发 `_device/cancel_turn`。桌面收到的取消是 `subagent.cancel`，只操作指定设备、会话和子轮。若正处于异步准备阶段，也须挡住随后发送的 ACP prompt |
| 产物和历史 | 文件仍在实际执行的工作区。上报结果可含 `summary`；本机路径不能直接作为云端下载地址，产物上传不在此工具组内。首轮本机委派在 Session Log 的 `source` 为 `runtime`，人工补答为 `user`，云父轮自动续跑为 `runtime`。读取或回放时保留这些来源 |

## 5. 联调与验收

1. **桌面基础响应**。在已安装该分支依赖的 `apps/office-desktop` 运行 `pnpm desktop:test:device`。核对账号切换、待答、拒绝/过期审批、取消期间无额外 ACP prompt。桌面显示名、通知和历史页用真实登录账号验收。
2. **连接独立 mock**。在 `device-delegation-mock` 按其 `README.md` 配置三个角色凭据并启动。启动桌面进程前设置 `RACCOON_DEVICE_ROUTER_URL`（默认调试地址 `ws://127.0.0.1:8787/`）、`RACCOON_DEVICE_ROUTER_TOKEN`（对应 mock 的 `LOCAL_DESKTOP_TOKEN`）、`RACCOON_DEVICE_ROUTER_ACCOUNT_ID`（当前登录资料 ID）。桌面 WS 首帧应为注册，设备 ID 应等于实际主机名。mock 的测试 token 只用于本机联调。
3. **无界面跨仓复现**。先在独立 mock 仓安装依赖；`DEVICE_TEST_BOX` 指向 Box-Agent 功能工作树，`DEVICE_TEST_PYTHON` 指向装有其依赖的 Python，`DEVICE_TEST_DESKTOP` 指向桌面的 `apps/office-desktop`。Windows 可选设 `DEVICE_TEST_BASE` 为已有短路径。运行 `node examples/box-desktop-smoke.mjs --input-approval`；同一入口还支持 `--write`、`--waiting-input`、`--approval`、`--cancel`。探针使用确定性模型和两个真实 ACP 源码进程。
4. **看两侧结果**。在探针输出的临时目录检查 `journal.json`、`local-profile/sessions/*/session.jsonl` 和 `cloud-profile/sessions/*/session.jsonl`。组合用例应看到人工答复保留用户来源、授权前文件不存在、授权后文件写成、父模型消费最终序号并自然结束。在桌面仓设置 `DEVICE_TEST_BOX`、`DEVICE_TEST_PYTHON`、`DEVICE_TEST_ROOT`（独立短目录）运行 `pnpm desktop:test:device-chain`，再核对双端回放、取消重试和 SQLite 历史。
5. **上线前联合验收**。使用生产路由凭据和打包桌面完成登录/注销、主机同名冲突、桌面断线、换设备审批、设备历史账号隔离及长任务期间的防空闲睡眠。已验证的源码、SQLite、ACP 和 mock 结果不能代替这一步。

父轮状态注入、续跑和旧客户端兼容规则见[Box-Agent 协议说明](DEVICE_DELEGATION_LOCAL.md)。
