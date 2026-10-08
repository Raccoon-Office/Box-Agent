# T14：统一消息注入与指令来源（P0-S1）

## 注入管理模块实施方案

共享模块负责信息结构、类型到提示词的映射、FIFO 队列、按 ID 去重、待注入取消、运行范围和注入回执。各生产模块负责触发条件和正文，执行循环负责提供安全注入位置。来源与类型分别表示提供者和处理语义；用户补充保持用户来源，内部反馈保持运行时来源。

管理器兼容现有 asyncio 队列入口和 string/dict 消息。ACP 只转换协议并调用共享管理器；Agent、AgentSession 使用同一实现。旧调用方传入普通 asyncio.Queue 时仍可运行。已有模型包装和内部反馈触发位置保持；停滞反馈使用独立类型。

消息状态区分 pending、taken、injected、cancelled、discarded。injected 表示已追加模型会话并产生回执，不代表模型已回答或执行。仅 pending 可以取消；已取走消息的取消失败不能清除去重记录。成功取消后允许以相同 ID 重新提交。新运行清理上一运行的去重状态，ACP 同一用户轮次中的自动续跑共享范围。持久化沿用 Session Log，不新增第二套持久化存储。

验收：用户和运行时混合顺序、去重、取消前后重试、运行与会话隔离、队列兼容、来源与模板、回执时机，以及 ACP／共享运行入口集成。先运行直接回归和相关套件，再执行完整套件。交付停在源码验证，构建安装和客户端实测另行记录。

### 管理模块验证

最终代码相关回归：

```text
uv run --no-sync python -m pytest tests/test_message_injections.py tests/test_inject.py tests/test_agent_session.py tests/test_run_api.py tests/test_agent_session_persistence.py tests/test_core.py tests/test_length_retry_no_double_render.py tests/test_stream_recovery.py tests/test_acp.py -q --tb=short --basetemp=workspace/t14-manager-final-focused
509 passed, 1 skipped in 199.08s
```

包括待注入取消后重提、已取走消息不可取消／重复注入、取消不提前触发队列完成、混合顺序、消息副本隔离、跨会话隔离、新运行复用 ID、结束清理，以及宿主同一轮次中自动续跑保留去重。架构检查确认 ACP 不依赖内核内部模块。编译、文档相对链接和 `git diff --check` 检查通过。

完整套件：

```text
uv run --no-sync python -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp=workspace/t14-manager-full
5769 passed, 318 skipped, 1 deselected, 3 warnings in 679.74s
```

完整套件启动后，取消逻辑的队列完成计数另有最后一处修正，并补入等待完成与宿主续跑两项回归；上述 509 项结果覆盖该最终版本，不将全量结果标作最后修正后的整套重跑。全量跳过项为可选 live、浏览器／字体依赖和平台适用性等；排除项沿用 preflight 的不可达 MCP 超时规则；警告为 requests 依赖版本及 tar.extractall 弃用提示。日志位于本地 `workspace/t14-manager-full.log` 与 `workspace/t14-manager-final-focused.log`，不纳入提交。

上述源码阶段的运行边界为源码测试，未构建或安装客户端。随后客户端实测单独记录如下；原来的真实请求对照仍仅支持下文来源修正阶段结论。

### 客户端实测（2026-09-29）

使用已安装的 Windows 小浣熊 1.0.35，正常退出后以其已有 `BOX_AGENT_DIR` 入口临时连接本仓库源码 ACP。健康检查 `ok=true`、`initialized=true`、`childRunning=true`，`launchMode=configured-dir:venv-python`，命令为本仓库 `.venv/Scripts/python.exe`。本次未替换客户端安装文件或打包运行时，也未修改客户端源码、登录配置或权限设置。

**实际界面操作**使用 computer-use 技能，从聊天输入框发起纯文本任务，沿用界面选择的 Raccoon-Auto，不读取文件、不联网、不调用工具。短任务第一次尝试在点击发送前已结束，补充作为下一轮发送，不计作运行中插入验收。随后长任务覆盖以下路径：

| 场景 | 界面与后台证据 | 结果 |
| --- | --- | --- |
| 运行中发送补充 | 输入框可继续编辑；发送后出现补充队列和“引导”按钮 | 通过 |
| 待插入取消 | 点击“引导”后进入待插入状态，再取消；ACP `removed=True`，队列项消失 | 通过 |
| 插入当前任务 | 第二条补充在下一步骤产生一次插入回执，界面显示“引导”，模型改为三条简短建议并输出指定文字 | 通过 |
| 正常收尾 | 长任务最终 `done/end_turn`，恢复可发送状态 | 通过 |
| 来源与持久化 | 生效补充恰好一条、`source=user` 且保留原用户包装；取消标记在会话记录中为零；两条内部恢复信息为 `source=runtime`，未使用用户包装 | 通过 |

**客户端 IPC 链路验证**使用同一个已启动客户端的现有 `localAgent` 接口，经 preload → 主进程 → ACP，固定测试 ID 模拟重试。独立会话使用宿主已有默认绑定 `raccoon-chat-ml-5-5`；不将它描述为 Raccoon-Auto 的同条件对照。

| 场景 | 结果 |
| --- | --- |
| 待消费同 ID 重试 | ACP 记录 `inject_dedup` |
| 取消后重用同 ID | 取消成功；重提成功；再次取消成功 |
| 已消费后取消 | `ok=false`，ACP `removed=False` |
| 已消费后再重试 | ACP 再次记录 `inject_dedup`，客户端插入回执总数为 1 |
| 独立测试回合结束 | `done/end_turn` |

界面场景完成后，桌面控制出现 `failed to activate captured window`；重新定位后又返回 `GetCursorPos failed: 拒绝访问 (0x80070005)`，因此停止了后续界面输入。同 ID 重试使用上述客户端 IPC 完成，不冒充额外的视觉验收。

本地证据保留在 `workspace/t14-client-live/`：`health-proof.json`、`history-proof.json`、`retry-proof.json`、`box-agent.log` 及重试脚本。截图在操作记录中查看；原始日志和本地用户会话不提交。源码回归与这次实测均未发现注入模块需要追加修复的问题。既有文本结束检测曾触发内部恢复提示，本次未更改其判断策略。

验证边界：已安装客户端 → 临时源码 ACP → 初始化探测 → 新任务界面验证及真实 IPC 重试。未构建或安装新的 standalone runtime，不能视为新发行包验收。验证后退出测试进程，移除进程级覆盖并恢复客户端普通启动。

## 统一注入入口

所有 `InjectedMessageEvent` 对应的消息由 `box_agent/injections.py` 创建并追加，业务触发点提供类型、正文及已有队列标识。默认 Agent、AgentSession 和 ACP 队列使用 `InjectionManager`；公共 string/dict 队列接口继续兼容，也接受内部类型化消息。

| 类型 | 配套提示词 | 默认来源／可见性 |
| --- | --- | --- |
| 用户补充 | 用户运行中补充包装 | user／可见 |
| 宿主状态 | 运行时状态包装 | runtime／隐藏 |
| 计划、预算、停滞、工具反馈、输出恢复、最终回答 | 各类型在统一表中选择现有运行时包装 | runtime／隐藏 |
| 流恢复、请求大小恢复、文本续写、任务续跑 | 保留已自带说明的完整提示词 | runtime／隐藏 |

来源和类型分别记录并校验匹配，类型选择提示词；队列已有 ID、可见性覆盖、空消息处理和按到达顺序消费保持。真实用户消息在下一步骤开始消费，更新最新用户要求；内部反馈在原触发点同步追加，无需额外排队。去重、取消和运行清理由管理器处理；权限、预算、重试和历史持久化提交点由原负责模块处理。Skill、Memory 和临时图片等请求上下文投影不属于会话消息注入。

管理器在所属事件循环内操作。带 ID 的消息可查询当前运行中的状态；字符串等无 ID 输入仍按序处理。`apply_next` 在追加会话后返回原有 `InjectedMessageEvent`，界面继续以此确认插入；内部状态不会额外发送给界面。普通 asyncio.Queue 由原调用方管理生命周期，仅复用消息装配。旧日志不迁移。

本次集中管理以此前 T14 的模型可见内容为兼容基线，不为分类另写未经验证的策略文案。新增类型时必须在统一映射中选择配套提示词；预算提前收尾仍单独归 T14a。验证包括所有类型的来源／包装／可见性、旧队列兼容、真实用户消息路径、各恢复路径、持久化和 ACP；下文真实模型对照属于来源修正阶段，本次结构收敛另外记录验证结果。

### 提示词集中阶段验证（生命周期收拢之前）

用户／宿主队列与 18 处内部注入点接入 `MessageInjection.apply`，这是生产代码中唯一构造 `InjectedMessageEvent` 的位置。配套提示词注册表覆盖 11 个类型，队列适配保留已有 string/dict 格式；未知内部类型在追加历史前失败，外部字典的任意 kind 字段不会改变来源。

```text
uv run --no-sync python -m pytest tests/test_message_injections.py tests/test_inject.py tests/test_agent_session_persistence.py tests/test_core.py tests/test_length_retry_no_double_render.py tests/test_stream_recovery.py tests/test_kernel_compatibility.py tests/test_context_input.py tests/test_acp.py -q --tb=short --basetemp=workspace/t14-unified-focused
544 passed, 1 skipped in 180.17s

uv run --no-sync python -m pytest tests/test_message_injections.py tests/test_inject.py -q --tb=short --basetemp=workspace/t14-unified-routing
34 passed in 5.67s
```

第二条包含在第一条收集后补入的混合队列回归：真实用户要求和内部状态按到达顺序在模型调用前插入，运行状态不会替换最新用户要求，ID／可见性回执保持。编译检查通过。此次结构收敛没有再次调用真实模型，不将下文较早的来源修正对照冒充本轮新运行证据。

提示词集中阶段源码在具备符号链接和测试子进程管理权限的环境中完整执行：

```text
uv run --no-sync python -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp=workspace/t14-unified-full
5758 passed, 318 skipped, 1 deselected, 3 warnings in 650.10s
```

退出码 0；完整本地日志为 `workspace/t14-unified-full.log`。skip 是未启用 live 集成、缺失浏览器／字体依赖和平台适用性等，deselect 沿用 preflight 的不可达 MCP 超时排除，warning 为 requests 依赖版本及 tar.extractall 弃用提示。此前受限环境的失败记录保留在下文，不影响这次最终源码完整运行的独立结果。未构建或更新客户端。

## 来源修正的初始范围

基线 `1736b69`。图谱基线 `699b67a2` 已落后，按 T13 来源清单定位后核对当前源码。Session 静态模板、宿主能力、Skill、Memory 的装配顺序保持；初始阶段修正请求级运行反馈的来源，随后按上述统一入口方案集中装配。

`kernel/loop.py` 的内部预算、计划、恢复和收尾提示使用 `format_injected_message`，正文声称消息来自用户；其中三处恢复／计划提示还缺少 `source=runtime`。已有 `format_runtime_context_update` 用于宿主状态队列，可直接复用。

- 所有内部反馈使用运行时包装，消息标记为 `source=runtime`；真实用户队列消息继续使用用户包装。
- 保持 Provider 的 user 角色兼容、原始反馈正文、注入时机、事件顺序、可见性、硬预算、权限和持久化提交点；不新增装配框架或配置。
- 包装明确来源而不代表工具输出获得授权；权限与预算由现有执行层决定。旧日志不改写，新消息继续由 Session Log 保存。
- 这是模型可见指令变化，不能视为纯重构。版本由 Git 提交和评估 source/request 哈希标识。

| 输入 | 来源与作用域 | 装配与保留 |
| --- | --- | --- |
| 用户运行中补充 | user；当前任务的新增要求 | 用户包装；参与最新用户任务判断，进入 Session Log |
| 宿主状态队列 | runtime；当前宿主状态 | 运行时包装；原队列可见性和 ID 保留 |
| 预算、停滞、工具反馈 | runtime；当前 Run 的执行反馈 | 原触发点追加运行时包装，不替换用户任务或扩大权限 |
| 计划、流恢复、最终回答提示 | runtime；当前 Run 的纠正要求 | 原重试与一次性限制保持；记录在 Session Log，后续按现有压缩策略保留 |

上述消息沿用 Provider 的 user 角色；内部 source 和正文共同标识来源，不引入新的角色优先级。静态 System、Skill 和 Memory 的其余装配范围沿用 [T13 来源清单](T13-system-tool-inventory.md)，未在本项全面重构。

## 提前收尾的归属

### T14a：必要交付优先的收尾提示（2026-10-08）

near_limit 提示准确报告包含当前步的剩余步数，引导模型在现有权限与工具预算内完成必要操作、保存产物、读回校验和交付，并为最终回复留出步骤。任务已完成时直接交付；缺少必要输入或授权时请求后等待；预算不足或执行受阻时如实说明未完成部分，只提供实际产物路径。

收尾采用两阶段注入：第一次沿用 near_limit 的触发时机；进入最后一步时独立追加一次当前状态交付提示，要求停止工具调用，基于已有证据交付结果、实际文件路径、校验状态、未完成部分和阻碍。最后一步提示不受先前预算／无进展提醒抑制；常规提醒禁用或总步数小于预留窗口时仍会触发。两者同一步触发时，最后一步提示排在后面。提前结束或等待用户的运行不为发送提示继续执行。

硬预算、来源包装和无进展判断保持；评估配置仍为 12 步、预留 10 步，运行到上限时分别在第 3 步和第 12 步收到预算提示。模型忽略最后一步提示继续调用工具时，仍按原规则执行并以 MAX_STEPS 结束，不追加步数。确定性回归覆盖请求内容、12/300 步及禁用边界、提醒后继续工具操作、等待用户和持久化。真实 ACP 对照、沙箱环境复核和新 runtime 部署尚未进行，不能据此声称工作簿交付失败已解决。

第一阶段（仅文案）的源码验证：

```text
uv run --no-sync python -m pytest tests/test_inject.py tests/test_agent_session_persistence.py tests/test_message_injections.py tests/test_core.py tests/test_acp.py -q --tb=short --basetemp=workspace/t14a-focused
465 passed, 1 skipped in 177.35s
```

该阶段修改文件的 Python 语法检查、路线图文档链接及 `git diff --check` 通过；未运行全量套件或验证真实模型遵循效果。

两阶段注入最终源码验证：

```text
uv run --no-sync python -m pytest tests/test_inject.py tests/test_agent_session_persistence.py tests/test_message_injections.py tests/test_core.py tests/test_acp.py tests/test_kernel_compatibility.py -q --tb=short -rs --basetemp=workspace/t14a-two-stage-final
547 passed, 1 skipped in 172.12s

uv run --no-sync python -m pytest tests/test_context_input.py tests/test_turn_continuation.py tests/test_stream_recovery.py tests/test_length_retry_no_double_render.py tests/test_sub_agent_tool.py tests/test_run_api.py tests/test_run_control.py tests/test_sdk.py -q --tb=short -rs --basetemp=workspace/t14a-two-stage-contracts-final
187 passed in 5.30s
```

跳过项为 Windows 不适用的 POSIX shell 引号测试。单步 ACP、协议事件及流中断测试的断言已更新为包含隐藏的最后一步提示，同时保留原用户内容、无协议输出污染和调用次数约束。Python 语法、文档路径和 `git diff --check` 检查通过。上述相关回归之后补充全量验证如下；边界止于源码测试，未构建、安装或运行真实模型评估。

提交前全量验证（Windows、现有锁定依赖环境）：

```text
uv run --no-sync python -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp=workspace/t14a-full-final --junitxml=workspace/t14a-full-final.xml
3 failed, 5857 passed, 318 skipped, 1 deselected, 3 warnings in 1509.00s
```

- `test_skill_budget_recovery.py::test_loop_compacts_once_and_reprojects_without_trusting_old_read_facts` 的精确预算场景原本只允许一步；新增最终提示改变了压缩路径。将该场景的上限设为两步，使第一步不注入最终提示，仍要求只执行一次模型请求、一次压缩、零摘要请求并满足输入预算和 Skill 重投影约束。运行时代码未修改。
- `test_build_macos_runtimes.py::test_build_environment_does_not_inherit_host_python_or_pyinstaller_cache` 和 `test_dual_build_isolates_caches_and_promotes_only_after_both_pass` 在 Windows 上因断言固定 POSIX 路径分隔符失败。将 HEAD 的原版测试与两个构建脚本复制到本地 `workspace/t14a-macos-baseline/`，通过 `uv run --no-sync python workspace/t14a-macos-baseline/run_baseline.py` 仅运行这两项，复现相同错误：2 failed、34 deselected。相关构建脚本与测试保持原样，不将基线失败记作通过。

预算场景调整后的直接复测：

```text
uv run --no-sync python -m pytest tests/test_skill_budget_recovery.py tests/test_context_input.py tests/test_inject.py tests/test_agent_session_persistence.py -q --tb=short -rs --basetemp=workspace/t14a-budget-final
105 passed in 28.75s
```

最后只调整了上述测试场景，未重跑第二次全量；不能将第一次全量标记为全绿。318 项跳过涉及未启用 live、平台适用性及缺失浏览器／Canvas／字体依赖，排除项沿用 preflight；警告为 requests 依赖版本及 tar.extractall 弃用提示。`compileall` 与差异检查通过。完整日志和 XML 保存在本地 `workspace/t14a-full-final.log`、`workspace/t14a-full-final.xml`，不纳入提交。

### T14 原始边界

默认 `wrapup_remaining_steps=10`，在 `max_steps=12` 的评估 profile 下，第 3 步触发；源码 Agent 默认 `max_steps=300`，对应第 291 步。不能把评估配置的第 3 步误称为产品默认行为。触发条件和“停止工具”的正文属于 P0-S2，本项保留，另立 T14a 方案；沙箱启动超时另列运行故障。

## 来源修正阶段的验收与对照

1. 确定性请求捕获：预算／无进展反馈明确来自 runtime，真实用户消息保持来源；恢复反馈不变成最新用户任务。验证 user 角色兼容和非用户可见事件。
2. Session Log 中断恢复：运行反馈在模型响应前持久化，回放保持来源和正文。
3. 运行 core、恢复、Session、ACP 相关测试，并尽可能运行全量测试；基线失败独立记录。
4. 真实 ACP 对照：修改前后各两轮，每轮固定 q0-skill、q0-wait，沿用 T13 隔离 profile、raccoonwork-auto 路由、12 步、180 秒和样本。保留每次结果，记录实际路由模型，不宣称固定单模型实验。由独立 Agent 诊断。
5. q0-wait 必须保持等待且无越界工具动作；q0-skill 按工作簿独立验收，同时确认运行提示不再伪称用户来源。质量不要求凭此包装变化提升；已通过样本若回退则调查。延迟或 token 中位数较对照增加超过 25% 时标记回归待查，不以两轮推断统计显著性；费用未知。

不构建或安装桌面 runtime，不重启客户端。回退仅撤销本项源码变化，评估证据保留。

## 来源修正阶段的验证记录

新增断言先在旧源码执行：11 failed、2 passed，失败分别验证错误的用户包装和流恢复缺少 runtime 来源。修改后直接回归：

```text
PYTHONUTF8=1 uv run --no-sync python -m pytest tests/test_inject.py tests/test_agent_session_persistence.py tests/test_core.py tests/test_length_retry_no_double_render.py tests/test_stream_recovery.py tests/test_kernel_compatibility.py tests/test_context_input.py tests/test_acp.py -q --tb=short --basetemp=workspace/t14-focused
522 passed, 1 skipped in 161.77s
```

使用仓库 `.venv/Scripts/uv.exe` 和 workspace 内 uv cache。覆盖来源区分、默认／小预算触发点、禁用边界、计划与流恢复、持久化中断及 ACP。跳过项是 Windows 不适用的 POSIX shell 引号测试。`python -m compileall -q box_agent` 通过，三份文档的相对链接检查通过。

## 来源修正阶段的真实 ACP 对照

2026-09-28，执行既有入口，每次同时选择两个样本：

```text
uv run --no-sync python -m test_workspace.quality_baseline run --catalog-model raccoonwork-auto --case-id q0-skill --case-id q0-wait
```

沿用 T13a 的解释器、profile 和 trace 环境变量，修改前后各执行两次，无额外重跑。八个 Case 的实际请求均为 `sn-deepseek-v4-pro`，配置指纹均为 `4c78edad70d474c7bfa70cf1ff2218367de132bcfb7cfebe746658c21abd8579`；服务端部署版本未知，仍是固定自动路由配置的对照。原始证据在本地 `test_workspace/outputs/`，每个 Case 已独立诊断。

| 阶段／轮次 | 样本 | 结果 | 秒 | 已报告 tokens | 模型调用 |
| --- | --- | --- | ---: | ---: | ---: |
| 修改前 1 | q0-skill | failed | 142.925 | 58411 | 4 |
| 修改前 1 | q0-wait | passed | 6.735 | 12181 | 1 |
| 修改前 2 | q0-skill | failed | 148.028 | 58859 | 4 |
| 修改前 2 | q0-wait | passed | 6.485 | 12182 | 1 |
| 修改后 1 | q0-skill | failed | 141.697 | 58276 | 4 |
| 修改后 1 | q0-wait | passed | 6.433 | 12178 | 1 |
| 修改后 2 | q0-skill | failed | 24.322 | 56707 | 4 |
| 修改后 2 | q0-wait | passed | 6.259 | 12201 | 1 |

对应目录／报告：

- 修改前 1：`260928-1639-q0-e5413492/quality-64b08521.json`。
- 修改前 2：`260928-1642-q0-ce3fab47/quality-01aacf10.json`。
- 修改后 1：`260928-1645-q0-d3026790/quality-8aaaf998.json`。
- 修改后 2：`260928-1648-q0-5c0cd559/quality-35c6fab5.json`。

两轮修改后的预算提示均使用 Runtime state update，保留 user 传输角色，不再声称 The user sent。修改前第二轮回答把停止归因为用户最新要求；修改后第二轮正确归因为运行时提示。来源修正已在真实请求验证。

任务完成率没有提高：等待输入四次均正确，工作簿四次均未生成。前三次工作簿任务包含 120 秒沙箱初始化超时，最后一次未调用沙箱即停止。工作簿耗时中位数 145.476 → 83.010 秒不代表性能收益；tokens 中位数 58635 → 57491.5。等待输入耗时中位数 6.610 → 6.346 秒，tokens 12181.5 → 12189.5。均未超过预设 25% 增长线，但样本量不足以证明普遍无回归；费用未知。

下一项 T14a（P0-S2）针对仍有预算却提前停止必要交付动作制定策略；沙箱冷启动超时单独定位。静态 System 的能力条件、其他模型、长历史压缩和正式客户端尚未进行本项效果验证。

## 来源修正阶段的环境限制复核

全量回归中 Windows 进程树清理测试挂起，临时 `ticks.txt` 在超过 25 秒断言上限后仍持续增长；精确识别并终止该测试自己的 PowerShell 父子进程后，全量继续。允许管理子进程的环境中单独复核：

```text
uv run --no-sync python -m pytest tests/test_bash_tool.py::test_foreground_timeout_kills_grandchild_windows -q --tb=short --basetemp=workspace/t14-process-probe
1 passed in 9.45s
```

另外四个 bundled-write 测试在创建符号链接时报告 WinError 1314，尚未进入工具行为断言；Jupyter 写入边界用例也在受限全量环境失败。使用相同源码、允许所需系统操作的环境复核：

```text
uv run --no-sync python -m pytest tests/test_builtin_skill_read_only.py tests/test_jupyter_artifact_root_guard.py -q --tb=short -k 'file_tools_reject_bundled_writes or execute_code_blocks_parent_writes' --basetemp=workspace/t14-environment-probe
5 passed, 13 deselected in 5.77s
```

未修改这些测试或相应产品实现。以上单项复核不将原全量结果改记为通过。真实评估中的沙箱 120 秒超时仍需在同配置、允许启动子进程的环境中单独核实，不能仅据本次受限环境认定为产品启动缺陷。

全量最终结果：

```text
uv run --no-sync python -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp=workspace/t14-full
23 failed, 5698 passed, 332 skipped, 1 deselected, 3 warnings in 1388.76s
```

23 项失败构成为：21 项符号链接／reparse point 权限限制、1 项写入用户目录 kernelspec 的 PermissionError、1 项上述进程清理挂起（人工清理后断言耗时 205.4 秒）。除前述 6 项外，剩余失败按下列范围在允许所需系统操作的环境中复核，均使用相同源码：

- `test_presentation_group_input.py` 的 7 项符号链接用例、`test_presentation_init.py::test_init_rejects_non_regular_css_without_writing_elsewhere` 的 4 个 symlink 参数、`test_presentation_render_receipt.py::test_batch_receipt_lists_only_current_selected_pngs_and_resolved_paths`、`test_presentation_review_prep.py::test_review_prep_receipt_resolves_deck_from_another_cwd`：按失败 node ID 运行 `pytest <13 nodes> -q --tb=short --basetemp=workspace/t14-presentation-probe`，13 passed in 4.29s。
- `pytest tests/test_publish_artifact_tool.py tests/test_roadmap_core.py -q --tb=short -k 'publication_rejects_missing_and_escaped_paths or roadmap_output_rolls_back_when_parent_changes_during_publication' --basetemp=workspace/t14-publication-probe`：4 passed、71 deselected in 1.45s。

全部 23 项失败均已单项复核通过，没有重跑第二遍全量。332 项 skip 包含受限环境符号链接、POSIX、未启用 live host、缺失浏览器与字体依赖等；1 项 deselect 沿用 preflight 的不可达 MCP 超时排除。3 项 warning 是 requests 依赖版本提示及两项 tar.extractall 弃用提示。完整本地日志为 `workspace/t14-full.log`，失败节点清单为 `workspace/t14-failed-nodes.json`，不提交测试输出。

## 交付边界

注入管理实现、源码回归、来源修正阶段的隔离 ACP 对照，以及已安装客户端连接本次源码的界面和 IPC 验证已完成。客户端已恢复普通启动；尚未构建或安装新的 standalone runtime，未完成新发行包验收。来源修正对照中的工作簿完成率仍未改善。下一项为 T14a 的提前收尾策略，先讨论行为方案和通过标准，再实施。
