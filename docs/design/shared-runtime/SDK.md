# 共享 Run 接口与迁移

宿主拥有 Session 的创建和清理；桌面保持独立 Agent 和第三方模型能力。以下调用均使用现有 Session。

## 只取结果

```python
from box_agent.api import RunRequest, RunDeliveryOptions
from box_agent.sdk import AgentClient

client = AgentClient(session)
result = await client.run(
    RunRequest("run-1", "session-1", "执行任务"),
    delivery_options=RunDeliveryOptions(),
)
```

`run()` 使用仅结果模式并返回 `RunResult`；执行/交付失败通过 `status="failed"` 和 `error` 返回。`start()` 返回 handle 后直接等待 `result()` 也选择此模式。内部消费者持续排空事件，不保留供稍后重放的副本。

## 流式消费

```python
handle = await client.start(RunRequest("run-2", "session-1", "执行任务"))
async with handle:
    async for envelope in handle.events():
        await render(envelope.payload)
    result = await handle.result()
```

首次实际迭代 `events()` 登记唯一流式消费者。登记后可并发等待 `result()`，它不会抢走事件；重复订阅报错。若 `result()` 先登记，仅结果模式不可切回流式。只创建异步生成器对象不算登记。

取消单个 `handle.result()` 等待者不取消运行；显式使用 `cancel()` 或 `aclose()` 结束运行。提前退出流式消费应关闭迭代器或使用 handle 的上下文管理器。

## 终止与错误

`status` 和 `stop_reason` 保留旧映射。新增只读派生字段 `termination_kind` 随 `to_dict()` 输出：

| stop_reason | termination_kind |
| --- | --- |
| end_turn | normal |
| max_steps、max_tokens | budget_exhausted |
| interrupted | interrupted |
| cancelled | cancelled |
| waiting_for_user | waiting_for_user |
| error | failed |
| 未知原因 | unknown |

`COMPLETED` 或 `normal` 只代表该回合结束，不证明用户目标已经验收。宿主需要自己的目标验收逻辑。

流式交付失败会在 `events()` 抛出 `RunDeliveryError` 子类，同时结果中保留结构化错误；仅结果模式检查 `result.error`。稳定错误码：`RUN_EVENT_TOO_LARGE`、`RUN_EVENT_CONSUMER_TIMEOUT`、`RUN_EVENT_DELIVERY_FAILED`。ACP 保留现有线协议，交付异常进入现有请求错误路径。

## ACP 会话与后台服务

ACP 后台 Bash 服务的归属以当前 adapter 实例和宿主 `_meta.session_id` 为边界。
在同一 runtime 内，以相同产品会话 ID、相同工作目录重新调用 `session/new` 后，
新 ACP 句柄仍可通过原 `bash_id` 查看或停止 `lifetime=runtime` 的服务；重绑创建
失败或取消后重试也沿用该归属。没有产品会话 ID 的会话仍各自独立，不同 adapter
实例不会因产品 ID 相同而共享服务。既有工作目录校验和跨会话访问校验继续生效。
稳定归属仅适用于 Bash；Python 沙箱继续使用 ACP 句柄隔离内核。
`lifetime=turn` 仍在轮次结束时回收，runtime 服务仍在显式停止或 runtime 退出时
回收。本归属不持久化，不承诺 runtime 重启后恢复进程，也不增加 ACP 字段或要求
宿主修改接口；更新已安装客户端的行为仍需要替换并重启其实际运行的 runtime。

后台输出按块读取，单行超过 64 KiB 也不会丢失。`bash_output` 消费过的内容立即
释放；未读内容每个进程最多在缓冲区内保留 256 KiB，超出后转存私有临时文件，
读完或回收进程时关闭文件。未换行的尾部在下一次换行或输出结束后才可读取。
`bash_kill` 先停止进程并排空输出，再返回最后一批内容。两种读取都保留已有的
显示裁剪和完整结果持久化约定；读取失败会明确报告失败。
停止后的排空等待最多 2 秒；即使监视任务已失败、排空阶段超时或取消，也会主动关闭
子进程管道，不依赖垃圾回收。正常停止仍先保留尾部输出再关闭管道；单任务停止、
会话级清理和 runtime 退出共用此收尾路径。

每个 runtime 最多保留 128 条已退出且输出已消费完的普通历史记录，超限先清理
较旧的记录，之后访问该 ID 返回 `Shell not found`。运行中、有未读内容、读取
失败或仍有 POSIX 子进程组的记录不会因此被删除，会话重绑也不会清理这些记录。
这不是总内存或磁盘的硬上限：未读输出仍会占用磁盘，读取一批完整输出及持久化
结果时仍需要相应的临时内存；Session Log 的保留策略不在此修改范围内。

## 容量与权限

`RunDeliveryOptions` 默认 `max_events=1024`、`max_bytes=4194304`、`congestion_timeout_seconds=30`。统计完整事件 envelope 的紧凑 JSON UTF-8 大小，包括编号字段；每个入队事件计算一次。两项积压均降到 50% 且待写事件可放入时恢复。上限只覆盖通道积压，不是进程总内存上限；序列化临时对象、模型内部缓冲和结果聚合另外占用内存。

超大事件立即失败，不自动裁剪工具结果。调用方可通过宿主交付选项提高额度，或使用已有工具结果引用机制。参数不进入 ACP 协议或用户配置文件。

使用 `PermissionBroker` 时，流式消费者通过 `handle.send(ControlCommand.permission_response(...))` 回复。仅结果模式调用配置的 `on_request`，回调必须在返回前回复请求，可以异步等待宿主；没有回复即拒绝。普通宿主 negotiator 的等待也受共享 Run 取消控制。ACP 继续使用现有权限反向 RPC。

## 委派硬预算

配置 `max_delegated_tool_calls` 后，内置子 Agent 的工具执行共享祖先账本。该限额独立于父级直接调用和子任务自身额度。已进入执行的失败/取消不退款，权限请求等明确未执行尝试释放扣费。完成回报只作统计。

自定义同名 `sub_agent` 必须支持调用上下文、声明 `supports_delegated_budget=True`，并在子作用域绑定 `context.child_budgets`、使用共享工具调用入口。声明属于可信插件契约，不是安全隔离；未接入的实现会被明确拒绝。未配置父级委派限制时保留旧行为。

## 自定义工具的批内去重

`Tool.deduplicate_within_batch` 默认为 `False`。同一模型响应中的同参调用默认分别准入、执行和计费；其中一次失败不会仅因参数相同而吞掉另一调用。既有权限、预算、交互终止、搜索专项策略和无进展保护仍然生效。

可信工具可显式声明 `deduplicate_within_batch = True`，允许同批同参调用共享一次执行与结果（包括失败）。重复项保留独立 call_id 的隐藏结果，不单独运行执行前 Hook 或消费执行额度。许可在请求准备时固定，执行前发生变化会拒绝调用；别名按规范名称判断，不跨模型响应缓存结果，也不会自动重试失败。

只读、幂等或 `parallel_safe=True` 均不等于可合并：文件读取可能夹在写操作之间，重复写可能表达用户要求，外部查询可能产生计费或审计副作用。MCP annotations 不授予许可。内置工具本轮保持默认值；插件作者确认可共享结果与上述 Hook 语义后再开启。模型 schema、ACP 字段及前端接入方式不变，调用次数和预算耗尽时机可能变化。方案和验证见 [T12](T12-explicit-tool-deduplication.md)。
