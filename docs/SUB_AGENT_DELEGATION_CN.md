# 子 Agent 委派

本文是 `sub_agent` 工具的契约文档，覆盖扁平公开请求、运行时派生策略、批量文件
快速路径、预算和宿主诊断。UI 进度渲染还需结合
[宿主进度事件对接](integration/host-progress-events.md)。

## 执行模型

子 Agent 拥有独立消息历史，但复用父会话已解析的 LLM client 和实时工具实例。
`PermissionEngine` 等资源级检查仍是最终权限闸门。父 Agent 负责判断是否值得委派、
处理冲突、写最终交付物并执行最终验证。运行时始终禁止递归调用 `sub_agent`。

子 Agent 不拥有独立授权能力。它复用父会话的权限协商器：受限工具访问会话范围外
的目录时，由父会话向宿主发起审批；允许后原工具调用自动重试，拒绝或超时则保持
失败。并发的相同文件权限请求合并为一个宿主审批，不同请求串行展示；危险命令的
一次性安全审批不会合并。

## 公开请求

普通请求保持扁平：

```json
{
  "title": "API 审查",
  "task": "比较 API 文档并报告不兼容变更。",
  "required_tools": ["read_file"],
  "skills": ["code-review"],
  "budget": {"max_steps": 12, "max_tool_calls": 25}
}
```

除 `task` 外均为可选字段。未知顶层字段返回 `INVALID_DELEGATION_SPEC`，调用方可按
命名字段修正一次。旧的 `execution`、`capabilities`、`inputs`、`constraints` 嵌套
对象不再接受。

### 安全默认

省略 `required_tools` 时，子 Agent 取得父会话当前实际可用的以下读取和搜索工具：

- `read_file`
- `query_jsonl`
- `search_files`
- `web_search`、`web_extract`

声明 `skills` 时，还会配备可用的 `get_skill`、`list_skills`，范围限于已分配 Skill
及其依赖，并受父会话允许范围约束。图片、进程和未知 MCP 工具不属于默认工具。

省略 `required_tools` 并提供合法非空 `write_scope` 时，还会自动配备父会话当前
可用的 `write_file`、`edit_file`、`append_file`，全部受指定范围及原资源权限约束。
这种请求即使带有 `files` 也进入普通子任务循环；没有可用写工具时，在启动子任务前
返回 `REQUIRED_TOOL_NOT_FOUND`，不退化为只读任务。

显式空数组表示无工具子 Agent。Skill 元数据不能增加
工具或扩大运行时策略。显式工具列表不会被默认规则扩展。

子 Agent 会继承父会话稳定的安全与工作区约束，但不会重复继承父会话自动加载或按需
加载的完整 Skill 正文。子任务需要的 Skill 指导必须显式选择或作为任务输入提供，避免
大型父工作流在子 Agent 开始有效工作前就耗尽更小的子上下文预算。

## 运行时派生策略

运行时根据明确选择或默认补齐的工具派生策略，不再要求模型填写权限布尔值：

- `bash` 只有被显式点名且父会话提供权限协商器时才委派，每条子 Agent 命令都走父会话一次性审批；
- `execute_code` 及其他进程工具不委派；
- 具有外部副作用的工具不委派；
- 未知 MCP 工具 fail closed；
- 已知只读网络工具只有被显式点名时才开放；
- 路径写工具必须提供精确 `write_scope`；
- Skill 不能扩展解析后的工具集。

已知只读网络工具包括 `web_search`、`web_extract`、`inspect_images`，以及根据可信
服务器元数据识别的受管 Playwright 导航/检查工具。`generate_image` 是必须显式
选择的可信网络能力。
浏览器交互和任意浏览器代码仍属于外部副作用能力，默认拒绝。

### 写入范围

`write_file`、`append_file`、`edit_file` 必须同时提供非空、相对产物根目录的
`write_scope`：

```json
{
  "task": "把已核验的发现写入指定文件。",
  "required_tools": ["web_search", "write_file"],
  "write_scope": ["research/dim01.md"]
}
```

运行时会包装这些工具，在调用父会话实时工具之前拒绝超出范围的路径。并行子 Agent
必须使用互斥范围。子 Agent 可以传相对产物根目录的路径，也可以传它解析后的绝对路径；
两者都会基于同一个实时文件工具根目录校验。省略 `required_tools` 时，输出范围会
启用可用的文件写工具；显式工具列表没有路径写工具却传入 `write_scope` 则属于冲突请求。
`write_scope` 不约束 shell 语义；显式委派的 `bash` 会被包装为逐条精确命令的一次性父会话审批。

## 本地文件批量快速路径

传入 `files` 只是声明本地任务输入，general-loop 与批处理都可以使用。只有解析后的工具集
恰好为 `read_file` 时，运行时才选择内部批处理；有其他工具时保留普通 Agent Loop，
同时仍把文件路径传给子 Agent。可以这样明确选择批处理：

```json
{
  "task": "比较文档并总结差异。",
  "files": ["docs/a.md", "docs/b.md"],
  "required_tools": ["read_file"]
}
```

省略工具列表时，`files` 要求父会话具有 `read_file`，但不会移除其他默认工具。
为兼容原来的一次文件综合：没有输出范围，且请求预算受宿主上限约束后的有效步数为 1 时，
缺省工具仍只取 `read_file`，保留批处理。显式工具列表始终精确，预算不会自动增加。

批处理路径满足：

- 有效工具集只能为 `read_file`；
- `files` 包含 1-32 个唯一的本地路径；
- 文件并发读取，并通过结构化元数据证明完整；
- 单文件选中内容上限为 64,000 字符；
- 聚合内容上限为 200,000 字符；
- 综合阶段只调用一次无工具模型；
- `sub_agent_batch_synthesis_timeout_seconds` 限制综合调用时间。

输入缺失、失败、截断、无法证明完整或超限时，在综合前返回
`BATCH_FILES_PREFETCH_FAILED`；综合超时返回 `BATCH_SYNTHESIS_TIMEOUT`。

## 预算

通用循环的默认值和上限来自 `tool_limits.sub_agent`：

```yaml
tool_limits:
  sub_agent:
    general_max_steps: 80
    general_max_tool_calls: 48
    no_progress_steps: 8
```

调用方可以通过 `budget.max_steps` 和 `budget.max_tool_calls` 请求更小预算，超过配置
上限的值会被截断。`budget` 必须是 JSON 对象，不能是序列化 JSON 文本。
`sub_agent_token_limit` 独立限制子上下文。

可恢复的产物工作流使用两套计数。父 Agent 的一次 `sub_agent` 调用只在主工作流
`max_tool_calls` 中计一次；子 Agent 内部工具仅计入独立的
`max_delegated_tool_calls` 聚合预算。子任务预算耗尽后只阻止继续启动子 Agent，主 Agent
仍保留合并、QA 和最终交付额度。

## 诊断

成功的 `ToolResult.raw_output` 包含：

- `type: sub_agent_delegation`
- 推导出的 `strategy`
- 请求与解析后的工具和 Skill
- 内部派生约束和应用的默认值
- 归一化 `files` 与有效预算
- 模型/工具调用次数、usage 和模型路由诊断

执行前失败使用 `type: sub_agent_delegation_error`，并返回稳定 `code`、
`retryable`、`invalid_fields` 及适用的修正信息。子级进度使用
`rawOutput.type: sub_agent_progress`。

## 归属与验证

- 请求归一化与策略：`box_agent/tools/sub_agent_capabilities.py`
- 执行、批处理和写入包装：`box_agent/tools/sub_agent_tool.py`
- 会话工具组装：`box_agent/tools/setup.py`
- 回归覆盖：`tests/test_sub_agent_capabilities.py`、
  `tests/test_sub_agent_tool.py`、Core、ACP 和配置测试
