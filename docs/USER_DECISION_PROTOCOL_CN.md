# 用户决策协议

`request_user_decision` 是内置工作流和用户 Skill 共用的公共声明式决策能力。
Skill 只提供文本与稳定选项 ID；宿主负责渲染，Box-Agent 运行时负责超时策略。
Skill 不能通过该协议注入自定义 UI。

只有 2-6 个选项会实质改变用户可见的交付范围、格式或内容方向时才使用该工具。
内部实现与恢复细节由模型自行决定；缺少必要事实使用 `request_user_input`；任务结束
后的输入框推荐继续使用 `follow_up_suggestions`。

## 请求事件

工具成功后通过 `update_tool_call.rawOutput` 发送：

```json
{
  "type": "user_decision_request",
  "schemaVersion": 1,
  "requestId": "decision_...",
  "status": "waiting",
  "question": "请选择交付范围。",
  "decisionKind": "delivery_scope",
  "options": [
    { "id": "keep_full", "label": "保持完整版本" },
    { "id": "prototype", "label": "先交付原型" }
  ],
  "defaultOptionId": "keep_full",
  "autoSubmit": {
    "allowed": true,
    "requestedSeconds": 30,
    "effectiveSeconds": 30,
    "behavior": "submit_default"
  },
  "resumeBehavior": "continue_existing_task"
}
```

模型可以申请超时，但只有调用声明低风险、可回退并保持用户明确意图时，运行时才会
启用。认证、授权、删除、付费、购买、发布和外部消息决策永不自动提交。无效或宿主
不支持的超时数据降级为人工选择。

## 宿主回传

宿主通过同一个 ACP session 发送隐藏 prompt，并在
`session/prompt._meta.user_decision` 中回传以下数据；也接受 `userDecision` 别名：

```json
{
  "request_id": "decision_...",
  "tool_call_id": "call_...",
  "decision_kind": "delivery_scope",
  "selected_option_id": "keep_full",
  "selected_option_label": "保持完整版本",
  "trigger": "user"
}
```

`trigger` 为 `user` 或 `timeout`。自由输入使用 `custom_text`，不传
`selected_option_id`。旧宿主可以显示工具文本降级并等待人工回复，但不得自行构造或
静默提交默认项。

宿主应在 Skill 声明的业务选项之外固定提供“取消并直接对话”。取消只关闭卡片、停止
倒计时并将焦点交还输入框，不得伪造成业务选项，也不得自动发送隐藏续跑消息。用户的
下一条普通消息解除等待，并在同一任务中继续。

## Skill 编写规则

Skill 可以要求模型调用 `request_user_decision`，但工具调用后不得再用 Markdown 重复
同一组选项。选项使用稳定 ASCII ID，并说明用户可感知的差异；超时字段只是申请，不
是保证。协议 v1 不要求 Skill manifest 声明能力；未实现卡片的宿主保留人工降级路径。
