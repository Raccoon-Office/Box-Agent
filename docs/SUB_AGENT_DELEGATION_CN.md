# 子 Agent 委派

本文是当前 `0.8.79` 开发树中 `sub_agent` 工具的契约文档，覆盖显式能力解析、
执行策略、限制、兼容行为和宿主诊断。UI 进度渲染还需结合
[宿主进度事件对接](integration/host-progress-events.md)。

## 执行模型

子 Agent 拥有独立消息历史，但复用父会话的实时 LLM client 和已解析工具实例。
因此 Jupyter kernel 等运行态可以共享，同时子 Agent 看不到无关的父会话历史。
父 Agent 只接收子 Agent 最终结果；结构化进度也可转发给 ACP 宿主。

父 Agent 仍负责判断委派收益、处理冲突、写最终交付物并执行最终验证。
运行时始终禁止子 Agent 递归调用 `sub_agent`。

## 新式请求

只要出现 `capabilities` 字段，就进入严格的新式委派。最小有效请求为：

```json
{
  "task": "读取给定文件并总结。",
  "capabilities": {
    "required_tools": ["read_file"]
  }
}
```

完整结构为：

```json
{
  "title": "API 文档",
  "task": "比较给定 API 文档并报告不兼容变更。",
  "execution": {"strategy": "batch_files"},
  "capabilities": {
    "required_tools": ["read_file"],
    "optional_tools": [],
    "skills": []
  },
  "inputs": {"files": ["docs/api-v1.md", "docs/api-v2.md"]},
  "constraints": {
    "read_only": true,
    "network": true,
    "write_scope": null,
    "external_side_effect": false
  },
  "budget": {"max_steps": 1, "max_tool_calls": 2}
}
```

`required_tools` 必须非空。未知字段或无效值会在调用子 LLM 前返回
`INVALID_DELEGATION_SPEC`。调用方最多修正一次；严格请求绝不会静默回退到 legacy。

## 能力解析

运行时会归一化请求名称，展开所选 Skills 及其 `required_skills`，加入
`allowed-tools` 路由元数据，再与父会话实时工具和声明约束取交集。
`related_skills` 只是推荐，不会自动加载。

默认保持只读且禁止外部副作用，同时允许网络访问：

| 约束 | 默认值 | 效果 |
| --- | --- | --- |
| `read_only` | `true` | 禁止写工具和进程工具。 |
| `network` | `true` | 允许标记为可访问网络的工具；设为 `false` 时禁止。 |
| `write_scope` | `null` | 默认只读策略下不允许委派写入。 |
| `external_side_effect` | `false` | 禁止修改外部系统的工具。 |

委派文件写入时，需要设置 `read_only: false` 并提供非空 `write_scope`。
运行时会包装 `write_file`、`append_file`、`edit_file`，在调用实时工具前拒绝范围外
路径。其它写工具因为无法通过这条路径强制限定范围而会被拒绝。现有
`PermissionEngine` 仍是资源级最终权限闸门。

必需能力解析失败会在执行前终止；可选工具可以缺失，并记录在 `denied_tools`。
未知必需 MCP 工具在 MCP 仍加载时返回 `REQUIRED_TOOL_NOT_READY`，加载完成后仍不存在
则返回 `REQUIRED_TOOL_NOT_FOUND`。

## 策略与硬限制

### `general_loop`

用于异构工作、独立网络研究或需要迭代工具循环的任务。

- 默认和最大预算：12 次模型 step、16 次工具调用总量。
- 子 Agent 只接收解析成功的工具和所选 Skill 指令。
- 单工具循环保护与总 `max_tool_calls` 预算都会执行。
- 解析后的工具若标记为 `parallel_safe` 仍可并发；子循环当前使用 core 默认值：
  最多并发 8 个调用，单批超时 900 秒。

### `batch_files`

用于多个已知本地文本文件执行相同的只读总结、比较、评估或抽取。应优先使用一个
批次，而不是每个文件创建一个子 Agent。

- `required_tools` 必须严格等于 `["read_file"]`。
- `inputs.files` 必须包含 1–32 个唯一文件路径。
- 文件并发读取；每个文件必须通过 `read_file` 结构化元数据证明读取完整。
- 单文件上限：选中内容 64,000 字符。
- 聚合上限：200,000 字符。
- 固定为一次综合 step；`max_tool_calls` 必须覆盖全部文件。
- 预读成功后只发起一次无工具、关闭 thinking 的综合调用。
- `sub_agent_batch_synthesis_timeout_seconds` 为综合调用增加 wall-clock 上限
  （默认 `300`；设为 `0` 关闭额外上限，仅由 provider request timeout 控制）。

任一文件读取失败、被截断、无法证明完整或超过限制时，返回
`BATCH_FILES_PREFETCH_FAILED`，且不调用综合模型。综合超时返回
`BATCH_SYNTHESIS_TIMEOUT`。

## Legacy 兼容

只传 `task` 且完全没有 `capabilities` 字段时，使用 legacy 子循环。它继承父会话
可用工具与父 system prompt，保留 legacy 40 step 循环和配置中的
`sub_agent_token_limit`，不执行新声明 schema。
`capabilities: null` 属于无效严格请求，不是 legacy 请求。

新调用方应始终使用显式能力声明。Legacy 路径只服务旧 prompt 和旧宿主。

## 诊断与宿主对接

严格执行成功时，`ToolResult.raw_output` 包含：

- `type: "sub_agent_delegation"`
- 策略、请求/解析后的工具与 Skills、被拒绝的可选工具
- 归一化约束、预算和已应用默认项
- 模型/工具调用次数与 token usage
- 成功 `batch_files` 的 `aggregate_chars`

执行前失败和批处理失败使用 `type: "sub_agent_delegation_error"`，并提供稳定的
`code`、`message` 和 `retryable`。ACP 进度仍按父工具调用分组，使用
`rawOutput.type: "sub_agent_progress"`；最终委派诊断属于父 `sub_agent` 结果，宿主
不应依赖标题或文本启发式判断。

## 配置

```yaml
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
sub_agent_token_limit: 50000
sub_agent_batch_synthesis_timeout_seconds: 600
```

这两个子 Agent 配置都以注释形式列在 `box_agent/config/config-example.yaml` 中，
作为高级覆盖项。保持注释状态可避免新生成的用户配置固定旧值，使 runtime 升级能够
更新默认值。

## 实现与验证

- Schema 与能力解析：`box_agent/tools/sub_agent_capabilities.py`
- 执行与诊断：`box_agent/tools/sub_agent_tool.py`
- 实时工具/Skill/MCP 状态接线：`box_agent/tools/setup.py`、
  `box_agent/agent.py`、`box_agent/cli.py`、`box_agent/acp/__init__.py`
- 工具调用总量保护：`box_agent/core.py`、`box_agent/loop_guards.py`
- 回归覆盖：`tests/test_sub_agent_capabilities.py`、
  `tests/test_sub_agent_tool.py`、`tests/test_core.py`、`tests/test_acp.py`
