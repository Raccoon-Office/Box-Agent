# HookBus

HookBus 在每个 Run 中调度插件提供的 Handler。运行开始、模型步骤、工具执行前后等事件由公共执行链触发；总线按优先级执行匹配的 Handler，并返回最终参数、文本或拒绝结论。

## 静态插件接入

插件使用 `PluginDescriptor` 声明 `HookProviderPort` 能力，Provider 的 `get_hooks()` 返回有序的 `HookSpec`。工厂函数在每个 Run 激活时调用，可以通过闭包接收宿主从 Config 解析出的插件参数。

```python
from box_agent.kernel.hook_types import BeforeToolDecision, HookContext
from box_agent.plugins.descriptors import PluginDescriptor
from box_agent.plugins.hooks import HookProviderPort, HookSpec


class TextPrefixHooks:
    """给 echo 工具的输入添加宿主配置的前缀。"""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    async def matches(self, context: HookContext) -> bool:
        return context.payload["tool_name"] == "echo"

    async def handle(self, context: HookContext) -> BeforeToolDecision:
        text = context.payload["arguments"].get("text", "")
        return BeforeToolDecision.modify({"text": self.prefix + text})

    def get_hooks(self) -> tuple[HookSpec, ...]:
        return (
            HookSpec(
                hook_id="prefix",
                events=("tool.before_execution",),
                kind="before_tool",
                handler=self,
                matcher=self,
                priority=10,
                timeout_ms=1000,
            ),
        )


def build_plugin(prefix: str) -> PluginDescriptor:
    """把已解析的配置绑定到每次运行的插件工厂。"""
    return PluginDescriptor(
        plugin_id="example.text-prefix",
        version="1.0.0",
        capabilities=(HookProviderPort,),
        factory=lambda: TextPrefixHooks(prefix),
    )
```

将描述符集合传给 `Agent(..., plugins=(descriptor,))`、`AgentSession.create(..., plugins=(descriptor,))`、`await AgentSession.open(..., plugins=(descriptor,))`，或者 `runtime.run_agent_loop(..., plugins=(descriptor,))`。也可以通过 `dataclasses.replace(agent.default_run_options(), plugins=(descriptor,))` 为单次运行指定插件。

`PluginDescriptor.disposer` 接收对应的 Provider 实例，可以是同步或异步释放函数。默认运行入口只接受 `PluginScope.RUN`；插件实例的创建和释放发生在本次 Run 内。旧的 `hooks=[...]` 参数和已有类路径配置继续可用，调用方传入的旧 Hook 对象仍由调用方持有。

Managed Session 先校验已有 KernelServices 与运行选项的一致性，然后只绑定新的 `hook_bus`、`hook_dispatch` 和 `hook_context`，保持模型、工具及其他能力对象身份。没有额外插件时不创建第二个 PluginHost；有插件时使用独立的 RUN 扩展宿主，借用已有能力，仅拥有新创建的 Provider。运行插件试图替换其他 managed capability 会在模型调用前被拒绝。这里的 `plugins` 是每轮扩展入口，不会把 HookProvider 自动加入共享 `PluginRuntime` 的固定 catalog。

Managed Run 传播取消或结束之前，必须等待 HookBus 排空和扩展 Provider 清理完成，之后 Session 才能释放自身资源。受保护的清理任务以返回值传递异常对象，保留 Python 3.10 上原始取消异常及其清理失败原因；不会依赖 `Task.result()` 对取消异常的版本差异。

每轮独立的扩展宿主没有后续 Session 来代为重试。若 Provider 清理再次被取消，Run owner 会保留清理任务并继续关闭尚存的实例，清理完成后再传播原始取消；持续中断的 Provider 会推迟该 Run 的清理完成。

超时或取消后，Handler 收尾中迟到的 `SessionLogDurabilityError` 仍按原异常对象保留。
该错误在下次分发或关闭时继续向宿主传播，不会因任务已经结束而静默消费；
运行收尾仍会先排空、注销并释放 Provider，避免错误传播跳过资源释放。

## 装配与调用

```text
AgentSession.build_run_options() / Agent.default_run_options()
    ↓ 传递 plugins 描述符与运行选项
Agent.run_events() → runtime.run_agent_loop() → core.run_agent_loop()
    ↓
Composition 创建 HookBus 和 PluginHost
    ↓
PluginHost.activate()
    ↓
register_run_hooks()
    ├─ 接入旧 Hook
    ├─ activation.contributions(HookProviderPort)：查询能力实例及归属
    ├─ provider.get_hooks()：读取声明
    ├─ bus.register(spec, owner)：绑定来源并登记
    └─ bus.freeze()：固定注册集合
    ↓
KernelServices 注入运行接口和 HookContext 基础身份
    ↓
AgentLoopKernel 触发生命周期通知
DefaultToolEngine 调用 before_tool / after_tool / observe
    ↓
运行结束：关闭工具事件流 → HookBus.close() → 释放插件实例
```

`HookBusPort` 保留已有 `fire_*()` 入口。默认总线同时实现结构化的 `HookDispatchPort`；工具引擎优先使用结构化接口，每个执行阶段调用一次。总线将旧回调与新 Handler 放入同一注册集合，按 `(priority, registration_order)` 排序。旧 Hook 使用优先级 0，并按原列表顺序先注册。

## Handler 与返回值

| kind | 事件 | Handler 返回值 |
| --- | --- | --- |
| `observer` | `run.started`、`step.started`、`llm.responded`、`tool.finished`、`step.finished`、`run.error`、`run.finished` | `None` |
| `before_tool` | `tool.before_execution` | `BeforeToolDecision.allow()`、`modify(arguments)`、`deny(reason, code)` |
| `result_text` | `tool.after_execution` | `ResultTextDecision.keep()`、`replace(content, error)`、`suppress(reason)` |

Handler 可以是异步函数，也可以是提供异步 `handle(context)` 的对象。matcher 可以是异步函数或提供异步 `matches(context)` 的对象，返回 bool；未声明 matcher 时直接执行 Handler。

`modify()` 使用完整对象替换参数。下一个 matcher 和 Handler 读取更新后的参数；后续 `allow()` 保留已修改的值。工具引擎对最终参数重新校验路径、执行限制与参数 Schema，权限协商使用同一份最终参数。权限重试不会重复执行前置 Hook。

`replace()` 和 `suppress()` 影响可见的 content/error，处理结果进入模型历史和宿主结果事件。总线保留整条链的 `modified` 标识，使显式替换的文本优先于工具原来的 model_context 或历史资源回执。工具的 success、原始输出、独立持久化内容和产物引用仍属于各自的数据出口；文本处理不会撤销已经发生的操作。

结果文本 Hook 不是所有模型输入的统一审查屏障。在仍使用 `SkillResultAdapter` 的旧 Skill
管线中，`get_skill` 可以在结果 Handler 前激活 system 指导；抑制工具文本不会撤销该状态。
Skill 正文和 Context 输入的策略由其所属模块负责，不能仅靠 `result_text` 推断它们已被清除。

被 Hook、参数校验或权限流程拒绝的调用跳过新式结果 Handler，通过 `tool.finished` 的 `executed=false` 表达未执行。旧结果 Hook 继续保留原来的可见失败结果回调范围。

## 调用信息

`HookContext` 包含 event、session_id、run_id、step、tool_call_id、只读 payload、取消检查函数、截止时间和日志接口。它表达当前 Hook 调用的数据与运行环境。

工具事件的 payload 使用 `tool_name`、`tool_call_id` 和 `arguments`；结果事件增加 success、executed、content、error 等数据。`tool.finished` 还可以携带抑制状态、Hook 来源、拒绝信息或 duplicate_of。

新 Handler 接收到的字典和列表被递归复制为只读结构，通过返回决策表达修改。旧生命周期回调保留原始参数语义，后续新 Observer 根据当前数据重新构建快照。

`KernelServices.hook_context` 保存装配层生成的本次 Run 身份，`ToolRunContext` 借用它构造各个工具调用的上下文。不同调用只共享固定的注册索引，各自保存处理进度、参数、结果和期限。

## 期限与清理

新 Observer 默认预算为 1 秒，新 Interceptor 为 5 秒，单条处理链为 10 秒。matcher 和 Handler 共用单 Hook 预算；有效截止时间取单 Hook、整链和 Run 期限的最早值。`HookSpec.timeout_ms` 可以进一步缩短预算。

Observer 的普通异常和超时记录告警后继续；前置 Handler 的异常、超时或非法返回导致拒绝；文本 Handler 对应情况导致抑制。取消和 `SessionLogDurabilityError` 继续向外传播。旧回调保留原有普通异常告警和期限语义。

旧 `on_tool_result` 返回错误长度、字段类型或不可解包的值时，保留告警并继续使用此前文本；
返回值转换也属于 legacy 异常边界。

```text
Registering → freeze() → Ready → close() → Draining → Closed
```

`close()` 停止接收新分发，取消并等待在途调用及 Handler 任务，然后逆序释放注册令牌。全部调用结束后，Composition 才释放插件实例。关闭过程中再次收到取消时，装配层保留正在进行的清理任务，使其继续持有需要清理的资源。

Handler 需要协作式响应取消。忽略取消的协程会使清理等待它结束，同步阻塞代码也不能依靠异步期限强制终止。关闭操作和令牌释放支持重复调用。

## 代码位置

| 文件 | 内容 |
| --- | --- |
| `box_agent/hooks.py` | 保留 BaseHook、HookManager 与加载器，统一导出新总线 |
| `box_agent/hook_bus.py` | HookBus、LegacyHookAdapter、注册令牌、分发与清理 |
| `box_agent/kernel/hook_types.py` | 上下文、只读数据、Handler 协议和决策类型 |
| `box_agent/kernel/ports.py` | HookDispatchPort 与 KernelServices 的能力引用 |
| `box_agent/plugins/hooks.py` | HookSpec、HookOwner、HookProviderPort |
| `box_agent/plugins/host.py` | PluginContribution 与激活结果的归属查询 |
| `box_agent/composition.py` | 每次 Run 的注册、冻结、注入和清理 |
| `box_agent/tools/engine/engine.py` | 工具执行前后 Hook 调用和完成通知 |
| `box_agent/tools/engine/results.py` | 将处理后的可见文本交给历史与结果发布管线 |
