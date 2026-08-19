# Sub-agent 委托契约

`sub_agent` 在独立消息上下文中执行一个自包含任务。是否值得委托、结果合并、
冲突处理、最终交付物和最终验证仍由主 agent 负责。

## 请求结构

只有 `task` 必填：

```json
{
  "task": "比较所给 API 文档并报告不兼容变更。",
  "title": "API 文档",
  "skills": [],
  "required_tools": ["read_file"],
  "budget": {"max_steps": 60, "max_tool_calls": 100}
}
```

工具 schema 为所有保留的可选字段提供机器可读默认值：

| 字段 | 默认值 |
| --- | --- |
| `title` | `""` |
| `skills` | `[]` |
| `required_tools` | 父 agent 当前全部可继承工具 |
| `budget` | `{"max_steps": 60, "max_tool_calls": 100}` |

`execution`、`capabilities`、`inputs` 和 `constraints` 不属于请求字段；schema
会在启动子模型之前拒绝它们。显式传空 `required_tools` 数组表示不给子 agent
任何工具。budget 会受已配置的子 agent 上限约束。

## 单一通用 loop

每次调用都运行相同的迭代式通用 agent loop，不存在调用方选择或运行时自动选择
的执行策略，也没有独立的文件批处理路径。文件路径和其他上下文应写入自包含的
`task`，子 agent 按需使用已解析的工具读取。

## 继承工具、Skills 和约束

调用时，若省略 `required_tools`，子 agent 会获得父 agent 当前实时工具映射中的
全部工具，但排除 `sub_agent` 自身和仅由父 agent 管理的延迟发现能力。显式列表
会解析为严格子集；任一名称不可用都会在启动子模型前失败。复用原工具实例意味着
权限引擎、工作区策略、会话和其他运行状态也被原样继承。

子 agent 继承最终父 system prompt，其中仅移除父级专用的 MCP 发现指引。顶层
`skills` 选择的 Skill 及其 required Skill 依赖会加入子 agent system prompt。
Skill 的 `allowed-tools` 元数据既不会增加工具，也不会改变显式
`required_tools` 边界；related Skills 不会自动加载。

## 诊断信息

成功调用的 `ToolResult.raw_output` 包含：

- `type: "sub_agent_delegation"`；
- `capability_source: "parent"`、`requested_tools` 和 `resolved_tools`；
- 请求和解析后的 Skills；
- 生效 budget 和 `defaults_applied`；
- 模型/工具调用次数及 token 使用量。

无效请求和能力解析失败返回稳定的结构化错误。ACP 进度事件分组和模型路由行为
保持不变。
