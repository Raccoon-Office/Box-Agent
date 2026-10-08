# T12 显式的批内工具去重契约

## 修改方案

- 固定边界：工具参数、ACP 消息结构、权限协商、直接与委派硬预算、串并行调度顺序、Session Log 提交点不变；不增加自动重试、跨批缓存或 MCP 配置字段。
- 保持行为的改动：去重许可绑定模型请求时的 PreparedTools 快照；执行前仍检查工具身份、定义与 MCP generation。别名先规范化，显式允许合并的重复调用仍为每个 call_id 产生闭合结果，使用现有隐藏重复事件格式。
- 功能变化：Tool 增加 `deduplicate_within_batch=False`。只有可信本地实现显式设置为布尔 True，才允许同批同参数合并。未知工具、普通工具和 MCP 默认逐次执行，受现有权限和预算限制；两个同参 append_file 可实际追加两次。`parallel_safe`、只读标注和幂等性都不自动授予去重许可。
- 许可语义：工具作者保证同批重复调用可共享同一结果，包括失败；重复项不单独进入执行前 Hook 或消费执行额度。结果可能随环境变化的读取、追加写入、外部动作、交互与委派工具保持默认值。本项不贸然为内置工具批量开启去重。
- 重试边界：未开启合并的调用即使前一项失败也独立准入；开启合并后保留原失败引用行为。后续模型回合可重新调用，本项不引入失败自动重放或 exactly-once 承诺。
- 兼容影响：以前依赖隐式去重的自定义工具可能执行更多次并更早消耗预算。需要原行为的工具必须审查上述许可语义后显式开启；模型 schema 和客户端前端无需增加字段。
- 验收：真实文件两次追加；默认工具在串行/并行时分别执行；显式许可合并、别名合并、跨批重试、失败传播、预算计费、快照后许可变更拒绝、MCP 默认不合并。直接回归后运行相关 Core/Session/Tool Engine/ACP 及可行的完整门禁，编译和构建分别记录。
- 回退：独立回退本提交，无持久数据迁移；回退会恢复旧的隐式合并行为，不能据此撤销已发生的文件或外部副作用。

## 实施与验证

实现位于 `tools/base.py`、`tools/engine/contracts.py`、`tools/engine/engine.py`：新增默认关闭的许可，纳入 PreparedTools 请求快照与目标匹配，仅通过验证的显式许可参与签名合并。没有修改 Kernel、MCP loader、权限执行或预算账本。既有 web_search 的查询/结果专项去重保持原样。

更新原有 Core 与别名用例，使其明确声明允许合并；Hook 集成用例同时验证默认逐次执行与显式合并。新增 `tests/test_tool_deduplication.py`，通过真实 Kernel 验证默认串并行执行、真实文件重复追加、直接预算、失败与跨批重新调用、许可变更及 MCP 默认行为。新增属性不进入模型工具 schema。

直接回归：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_tool_deduplication.py tests/test_tool_aliases.py tests/test_tool_engine_service.py tests/test_core.py::test_explicitly_mergeable_tool_calls_in_one_response_execute_only_once -q --tb=short --basetemp workspace/t12-direct-complete`：**44 passed**。首轮新增测试曾触发已有空参数循环保护，以及误期望定义变更拒绝事件可见；改用合法非空参数并断言原拒绝结果，未更改这两项产品规则。

`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_hook_plugins.py tests/test_tool_deduplication.py -q --tb=short --basetemp workspace/t12-hooks`：**60 passed**。完整回归发现旧 Hook 用例仍依赖默认合并；将该用例更新为两种模式，保留执行次数与 Hook 回执断言。

最后补严许可布尔身份，以及修正上述旧 Hook 断言后，先前未完成的全量运行主动终止，不计入通过记录。最终源码和用例集执行：`PYTHONUTF8=1 .venv/Scripts/python.exe -u -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp workspace/t12-verified-full`：**5730 passed、318 skipped、1 deselected、3 warnings，586.25 秒，退出码 0**。覆盖架构、Core、Session、Tool Engine、共享委派预算、权限、ACP 和桌面 stdio 测试。

沿用 T9 已按锁文件准备的 Windows / Python 3.12.14 环境，无依赖声明或锁文件变更。318 项条件跳过及 1 项既有 preflight 排除的原因与 [T9](T9-test-gate.md) 一致；三个警告仍为 requests 依赖组合及 tarfile 未来默认过滤策略提示，没有新增跳过来规避本项失败。

`.venv/Scripts/python.exe -m compileall -q box_agent` 通过；`.venv/Scripts/uv.exe --cache-dir workspace/uv-cache build` 成功生成 `dist/box_agent-0.9.8-py3-none-any.whl` 和 `dist/box_agent-0.9.8.tar.gz`。实际核查 wheel 1099 项、sdist 1618 项，三份 T12 生产源码与工作区逐字节一致，未包含 workspace、pip、.venv、.box-agent 或字节码缓存。构建后的 wheel/sdist viewer 资源检查再次执行：**2 passed**。文档相对链接和 `git diff --check` 通过。

计划范围是 P0-4 的首个独立工作包：去重许可及重试边界；资源冲突调度和全面 effect 分类留待后续，不声称整个工具调度路线完成。没有给内置读取工具直接开启许可，因为只读不足以保证与同批写操作交错时结果可复用。方案无范围外实现。

## 运行交付边界

本项不修改前端或打包流程。不为 T12 安装 standalone runtime、重启开发客户端或发送真实模型任务；T10 的客户端联调证据属于此前实现，不能代替本项真实任务验收。发布时仍需更新实际 runtime，按原流程打包、安装、重启后检查；重点观察重复动作次数和预算耗尽时机。
