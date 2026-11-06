# 本地会话切云对接

## 触发与职责

用户点击“切换到云端”后，客户端对当前本地会话发送一次普通 `session/prompt`，增加布尔值 `_meta.cloudHandoff: true`。等待当前请求结束后再发送；会话忙时沿用既有 busy 错误。其他值（包括字符串 `"true"` 和数字 `1`）不触发切云。

Box-Agent 在正常 user 消息写入 Session Log 前展开固定交接指令，基于当前会话生成独立任务说明和已知依赖文件列表。展开后的 user 指令及 assistant 回答沿原有路径持久化，可恢复并继续本地对话。切云生成轮不自动续跑已有 Goal。已有待审批计划保持原状，不阻止交接摘要生成，也不会因切云请求被批准；后续本地任务仍遵守原审批门禁。没有新 ACP 方法，也没有用于交接的工具调用。

切云生成轮通过现有 RunOptions 将模型工具调用额度设为 0；即使模型输出工具调用，执行入口也会拒绝，包括计划修改与普通业务工具。该限制只作用于本轮，不修改会话工具配置；后续本地任务沿用原额度及审批规则。

切云摘要不代表原任务已完成。本轮同时设置通用运行选项 `completion_checks_enabled=False`，不创建、恢复、扩展或关闭能力提供的完成校验，也不改写原任务在 Session Log 和内存中保存的暂停检查点。因此，等待表格校验的任务仍可生成摘要；摘要成功、JSON 无效、取消或模型失败均保留原检查点。后续本地任务（包括 session/load 后）恢复默认校验，未校验的原表格仍会阻止任务完成，执行 validate_table 后才可正常完成。

`AgentRunOptions` 与共享 `run_agent_loop` 的 `completion_checks_enabled` 默认为 `True`，现有调用方无需修改。该选项由可信宿主按轮次设置，不是模型参数或新的客户端元数据；禁用时不改变 Session Log 格式，也不修改普通零工具额度轮次的校验规则。

在当前有序输入链路中，固定指令作为第一个 text 块加入，后面保留客户端原有块及附件顺序；展开发生在 UserInput 校验、轮次指纹计算及历史编辑之前。Session Log 保存展开后的 input_facts，模型从这些事实派生输入；切云动作因此参与输入幂等判定。恢复沿用标准 session/load。

固定交接指令按本轮 `_meta.ui_language` 或 `_meta.uiLanguage` 选择：`zh` 使用中文，其他值（包括 `en`、`ja`）及未传时统一使用英文。沿用现有元数据大小写归一化。JSON 字段名保持英文；回答内容的语言仍遵循现有语言提示及用户明确要求。

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "session/prompt",
  "params": {
    "sessionId": "local-acp-session-id",
    "prompt": [{"type": "text", "text": "切换到云端"}],
    "_meta": {"cloudHandoff": true}
  }
}
```

## 输出与成功条件

正文通过既有 `session/update` 的 assistant 文本事件输出，最终正文严格为一个 JSON 对象，无 Markdown 围栏：

```json
{
  "instruction": "目标：分析销量。已有结论：已确认字段含义。约束：保留原始数据。进度：完成清洗。后续：生成趋势图和报告。",
  "dependency_paths": ["E:\\work\\销量.xlsx"]
}
```

`instruction` 必须为非空字符串；`dependency_paths` 必须为字符串数组，可为空。仅允许这两个字段；不提供 `name`。指令要求任务说明包含目标、结论、约束、进度和后续工作，且只列出上下文中已知的必要依赖，不扫描工作区或猜测路径。程序校验 JSON 结构及文件路径，不自动证明任务说明的完整性或文件与任务的相关性。

最终 `PromptResponse` 的成功示例（省略既有 usage 等字段）：

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "stopReason": "end_turn",
    "_meta": {
      "ok": true,
      "completed": true,
      "runStatus": "completed",
      "cloudHandoff": true,
      "messageDisplay": {
        "version": 1,
        "finalMessageIds": ["final-answer-message-id"],
        "durationMs": 1200
      }
    }
  }
}
```

只有本次请求正常结束、无执行或持久化失败、无取消，且最终结果校验通过时才回传 `cloudHandoff: true`。不得根据流中出现 JSON 或仅根据 `stopReason: end_turn` 提前切云。客户端应同时检查自己发送了标记、`cloudHandoff === true`、`ok === true`、`completed === true` 和正常 stopReason，再解析最终正文；正文不放在 PromptResponse 中。按现有 messageDisplay 的最终消息归属获取正文，不能拼接所有中间解释、重试快照和工具输出。

服务端使用同一最终消息归属校验完整正文：文本截断后的补写按消息 ID 拼接，每次输出的快照替代该输出的流式片段以避免重复；重试产生的新消息不混入旧残文。不会仅校验最后一次模型输出。

## 路径与上传

依赖采用运行 Box-Agent 的本机原生绝对文件路径。生成指令以当前会话 `session/new.cwd` 为本地工作目录，模型须将已知相对引用转换为绝对路径；回执校验不接受相对路径，也不使用进程 cwd 补全。校验逐项确认文件存在，拒绝目录、缺失文件、空值、NUL 和 URL，不读取文件内容、不枚举目录、不改写最终 JSON。

绝对路径可位于工作区之外，符号链接遵循本机文件系统的文件存在性检查；该检查不是上传授权。客户端上传链路必须按自己的授权和路径规则解析链接、核对实际目标，处理文件在回执后被删除或变化的情况。路径不发给云端当作可读地址。

收到成功回执并解析正文后，客户端通过既有独立上传链路上传 dependency_paths，拿到云端可访问地址，再将 instruction 和文件地址用于创建、启动独立云会话。全部必需文件准备成功后才启动；上传失败保留结果以便重试。未知依赖由用户后续主动补充。本功能不上传文件、不创建云会话，也不复制本地完整 Session Log。客户端与本地 Box-Agent 统一打包，本期不增加版本协商。

## 失败与取消

| 情况 | 回执及客户端动作 |
| --- | --- |
| JSON/字段/路径不合法 | `cloudHandoff: false`、`ok: false`、`completed: false`、`runStatus: error`、`errorCode: invalid_cloud_handoff`；任务登记为 error，保留本地回答，允许重试 |
| 执行失败或持久化失败 | 沿用既有失败信息；切云标记为 false；不上传、不启动云任务 |
| 取消、等待审批、步数或 token 耗尽 | 切云标记为 false；沿用原 stopReason/状态，保留本地会话 |
| 请求抛错、断连或被取消而无最终响应 | 没有成功回执；客户端保留本地会话并处理原请求错误 |
| 普通 prompt | 不增加 cloudHandoff 响应字段，沿用原行为 |

## raccoon-lab 接入位置

路径相对于 `raccoon-lab/apps/office-desktop/`，本次仅给出对接契约，未修改前端。

1. 在切云按钮对应发送链路、IPC 请求类型中携带 `_meta.cloudHandoff: true`，保持当前本地 sessionId。`runtime/electron/main/agent/boxAgentManager.ts` 的 prompt 元数据合并及 `connection.prompt` 发送入口已能透传该字段；`stripReservedLocalAgentMeta` 不会删除它。
2. 在同文件 `connection.prompt(...).then(...)` 中保留现有 cancelled、pause、failure 分支。成功分支当前只向 `kind: done` 事件透传 messageDisplay 等字段，需要增加 cloudHandoff 回执及相应类型，让消费端识别；不能把普通 done 视作切云成功。
3. 消费端按本轮 promptId/sessionId 和最终消息归属取 JSON，严格解析并再次核对字段，再调用上传及远端任务入口。重复 done/重连不得重复创建任务；创建与上传的去重由现有业务链路负责。
4. 取消和任何失败都留在本地；成功也保留本地会话。云端使用不同 session，不接续本地隐式上下文。

## 验证入口与边界

`tests/test_acp_cloud_handoff.py` 覆盖实际 prompt 展开、恢复持久历史、有效 JSON 成功回执、普通 prompt、严格布尔标记、无效输出、各非成功终态、异常、取消和继续本地对话。

飞书切云设计链接在本环境无法读取，尚未核对其内容与本契约是否有差异。标准附件、有序输入及 session/load 沿用 main 的现有实现；本项在这些入口之上接入切云动作，不重新实现附件上传或会话恢复。

源码测试不证明打包运行行为。交付后仍需统一构建并安装客户端和 Box-Agent、重启本地 ACP、在真实按钮入口验证最终消息解析、上传、云任务创建及失败恢复。

开发验证记录（2026-11-05）：

- 本地真实模型 sn-sensenova-6-8-flash：中英文交接 JSON、成功回执、依赖路径、持久化和后续本地对话均通过。
- 工作台云端 Box-Agent 0.9.8，分别使用 Claude Opus 47 和 GPT-5.5：将生成的交接说明与独立上传的 CSV 交给新云会话，两种模型均生成英文报告和趋势图，源文件保持不变。
- 两种云模型的缺必备文件场景：调用既有 request_user_input，runStatus 为 waiting_for_user，没有编造数据或生成分析产物。此时协议 stopReason 仍可为 end_turn，客户端须同时判断业务状态。
- 上述云端证据证明交接内容的可执行性及既有缺文件交互，不代表云端运行包包含本次本地切云标记改动。工作台使用接口提交，未验证客户端切云按钮。
- Review 修复覆盖请求开始时已有待审批计划、保留审批后继续本地任务、无效输出的任务持久状态及任务登记失败时撤销成功回执。
- 完整 Preflight、包构建和前后端打包联调状态见 PR 的 Proof 与 Risk。测试数据、日志和生成产物保留在本地，不纳入提交。
