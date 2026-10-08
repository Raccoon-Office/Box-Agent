# T11 共享核心与桌面宿主兼容审查

## 审查方案

- 固定边界：本项只审查和记录，不修改产品源码、ACP 消息结构、模型配置或打包脚本。
- 保持行为的改动：建立 T1–T6 方案、源码入口、测试和客户端调用的对应关系。
- 功能变化：无；发现必须修复的问题时先补充独立方案，不将审查意见直接混入实现。
- 兼容影响：分别核对 ACP 前端、Python SDK、自定义委派工具、运行包与用户配置，不能以单一正常回合替代全部契约验收。
- 验收：核实消费登记、终止分类、容量异常、取消权限等待、委派预算和构建入口；以 T9/T10 验证作为证据并注明范围。
- 回退：删除本审查记录，不影响运行行为。

## 审查记录

## 结论

本轮共享核心修改不要求 officev3 前端新增 ACP 字段或修改正常流式消费方式。实际开发客户端已能处理容量错误和权限取消，见 [T10](T10-desktop-scenarios.md)。外部 Python SDK 与自定义委派实现有明确迁移要求，不能将“前端无需改动”推广为所有调用方完全无感。

| 对象 | 源码核对 | 兼容结论 |
| --- | --- | --- |
| CLI / ACP | cli 的 `_run_session_turn`、ACP 的 `AgentService().start` 与 `protocol_handle.events()` | 均使用同一交付机制，ACP 继续映射 payload，内部 envelope 不直接成为新线协议 |
| ACP 容量异常 | 既有请求错误响应；BoxAgentManager 的 prompt error 路径 | 实测客户端收到 error 而不是 done；前端无强制适配项 |
| Python SDK 结果消费 | AgentClient.run、AgentRunHandle.events/result | result 先登记后不再支持补读全部事件；调用方需要按 [SDK](SDK.md) 选择模式 |
| 运行结果 | RunResult.termination_kind、RunResultCollector | 旧 status/stop_reason 保留，新序列化字段可能影响严格字段白名单；正常回合并非用户目标验收 |
| 权限与取消 | PermissionBroker、CancellablePermissionNegotiator、Session.cancel | 沿用 ACP 反向请求；仅结果宿主必须提供有效权限处理，否则明确拒绝 |
| 自定义委派工具 | engine/execution 的 supports_delegated_budget 与调用上下文检查 | 配置硬委派预算时必须接入共享账本；否则明确拒绝。内置实现已支持 |
| 模型提供方 | ACP binding / SessionBoundLLM 入口未由本轮修改 | 第三方模型配置方式保留；本轮没有新增云端依赖 |
| 前端源码 | officev3 client-v2 工作区保持无改动 | 当前无必须随本轮提交的前端功能修改 |
| 打包 | scripts/build_runtime.py；客户端 install-box-agent-runtime、check-electron-build-resources | 沿用原流程，但必须产出包含本分支代码的 runtime 并更新打包输入，不能只打前端继续携带旧 runtime |

## 证据与仍然存在的边界

- T1–T6 的直接测试覆盖首次消费者登记竞争、等待者取消、权限处理、队列取消/关闭、超限、并发发布和共享预算。T9 记录最终完整门禁；T10补充真实客户端 IPC/ACP 链路。
- 前端此前检查结果为 181 passed、17 failed、1 skipped，其中 manager 108 passed；事件处理器和生命周期合计 17 项旧契约失败。它们不能计入 Box-Agent 门禁通过，也没有在本项擅自改写为绿色。未改客户端源码，本轮不重新宣称该前端全套测试通过。
- 浏览器/Canvas/字体等可选真实渲染环境和非 Windows 平台由 T9 单独列出；没有把平台跳过当作已验证。
- Python wheel/sdist 可构建不等于 standalone runtime 已安装。仍需按原流程构建目标平台 runtime、安装进客户端构建资源、构建/安装应用、重启并完成新的真实用户任务。当前仍停在开发版源码联调边界。
- 自动续跑判断的额外模型等待属于既有续跑策略，本轮未修改；正常目标完成判断和提供方等待优化应另立功能方案。

未发现需要为本轮共享运行契约强制修改前端线协议的证据；这不是对客户端其他既有缺陷或全部真实任务的无条件兼容保证。

### ACP 终态后的清理失败

ACP 收到 `DoneEvent` 后先关闭事件消费，再读取共享运行的最终结果，避免将终态后已发生的清理异常误报为成功。失败沿用现有 `session/prompt` 元数据：`ok=false`、`completed=false`、`runStatus=error` 和 `error` 消息；`stopReason` 仍使用协议允许的值，不要求宿主增加字段。

宿主关闭终态后的待清理句柄所产生的正常取消，仍保留原完成或等待原因。缺少 `DoneEvent` 的运行使用共享层的失败结果。相关回归覆盖立即清理失败、关闭期间清理失败、正常关闭、缺失终态及失败后同会话继续运行；这些检查证明源码 ACP 行为，不代表桌面安装包已经更新。

### 宿主断开 stdio 后退出

ACP 的 stdio reader 消费完缓冲帧并读到 stdin EOF 后，通知服务进入与 SIGINT/SIGTERM 相同的收尾路径。先取消并关闭会话、释放插件，再关闭 ACP 连接及其请求任务，随后释放后台任务、模型客户端与其他资源。会话收尾时保持协议发送器可用，避免取消流程的最终消息等待已经停止的发送队列。

空闲、等待权限和等待模型流均有真实子进程回归：只关闭 stdin 就应正常退出，不能依赖测试清理中的 TERM/KILL 才通过；信号退出及 EOF/信号竞争也检查资源只关闭一次。普通任务完成、空行和一次读取取消均不会被当作 stdin EOF；POSIX 与 Windows stdio 使用同一通知 reader，保留 32 MiB 帧读取上限。

超过 32 MiB 的帧或 stdin I/O 错误会终止 SDK 的读取循环，因此同样触发一次关闭通知，并保留原读取异常。不能等待读取循环再次消费 EOF。回归覆盖有无末尾换行、有无提前收到 EOF 和重复读取错误；一次读取取消仍不触发关闭。

会话收尾完成后，ACP 连接最多等待 1 秒发送剩余消息。若 stdout 持续回压，则取消 SDK 关闭中的发送等待，并中止输出管道，随后释放其请求任务及后续资源；此时允许丢弃断开宿主无法接收的协议尾部消息，不进行重试循环。正常完成发送时不强制中止管道。SDK 聚焦回归检查正常排空、超时中止和关闭操作自身被取消时的任务回收；POSIX 真实子进程通过管道进入回压的标记建立测试前提，不读取 stdout、不发送补救信号也必须退出并完成会话与模型清理。

提供方流的读取任务如果已经结束，而外层消费者在取结果前被取消，共享流控制器仍需回收已完成任务的异常；否则正常耗尽产生的 `StopAsyncIteration` 会被事件循环报告为未处理任务异常。未完成的读取仍取消并等待清理，正常消费路径的提供方错误和取消期间的清理错误仍按原语义传播。同步屏障回归覆盖任务耗尽、失败和成功与取消交错的情形。

此行为不增加宿主协议字段，不改变正常 `session/cancel` 的响应时限。验证区分源码进程回归与 Windows feeder 的单元测试；没有将其表述为已安装新的 standalone runtime 或完成 OfficeV3 桌面验收。

### 同会话重叠任务的拒绝与隔离

同一个 ACP 会话的 `session/prompt` 从准备开始到收尾结束只允许一个请求占用。重叠请求在修改历史、权限、模型绑定、取消状态或注入队列前返回 JSON-RPC 错误；拒绝不会清除原任务的活动标记或执行其清理。不同会话仍可并发，原任务结束后可继续提交新任务。准备阶段异常或取消也会释放占用。

此前重叠请求通常返回 `-32603 / Internal error`，原因仅在 `data.details` 中；准备阶段还可能未被内层运行检查拦截。现在固定返回 `code=-32010`、`data.code="SESSION_BUSY"` 和 `data.sessionId`，message 为：

> This session already has an active task. Wait for it to finish, or cancel it and wait for cancellation to complete before retrying.

含义是“当前会话已有任务，请等待它结束，或取消并等待取消完成后再试”。继续使用标准 JSON-RPC error 响应；只重试当前错误码的宿主需关注错误码变化。运行中补充消息仍使用 `_inject`。没有向原任务插入错误消息或伪造终态，也不自动排队或重复执行被拒绝的任务。

回归覆盖准备、执行、收尾和取消期间的重叠请求，证明原历史、授权、注入去重与取消状态保留；真实 stdio 子进程验证错误文字、原权限请求继续批准/取消、后续任务恢复和 EOF 资源关闭。此证据止于源码 ACP 进程；OfficeV3 的界面错误展示和已安装 runtime 未在此验证。
