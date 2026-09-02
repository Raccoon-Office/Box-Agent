# 上下文压缩

Box-Agent 在两个边界控制持久上下文增长：

1. 工具结果在占满后续模型请求之前按需落盘；
2. 只有下一次请求接近模型有效输入上限时，才压缩会话历史。

两者彼此独立。工具结果落盘在存储层是无损的：完整结果保存在磁盘，模型保留有界的 head + tail 工作预览和恢复路径；会话摘要是有损的，因此更晚触发，并显式保留一个有界的工作集。

原生图片检查走第三条“仅本次请求”路径。规范化图片块只附加到主模型的一次请求，不进入持久消息历史；预算按解码后的图片尺寸估算，而不是按 Base64 字符长度计算。原始图片载荷不会写入 session trace 或 provider debug log。

## 请求生命周期

```text
工具执行完成
  -> 检查单个结果
  -> 追加 tool messages
  -> 下一次 LLM 请求前检查 fresh 结果总预算
  -> 从文本历史上限中预留本次图片预算
  -> 估算下一次请求大小
  -> 达到阈值才压缩历史
  -> 附加临时图片覆盖层
  -> 调用 LLM
  -> provider 成功响应后释放覆盖层
```

## 仅本次请求的原生图片输入

只有当前主模型已知或预期支持图片输入时，才允许调用 `inspect_images(..., strategy="native")`。默认 `proxy` 策略保持原有工具模型路径并返回文本。

原生策略沿用 proxy 路径的 PNG/JPEG 数量、尺寸、方向校正和降采样限制，然后通过 ToolResult 内部字段返回 provider-neutral 的 `input_image` 块。Core 只接受显式 opt-in 工具提供的这类内容，同时验证主模型能力，并对整批临时内容实施不超过安全输入上限 30% 的预算。图片成本按宽高保守估算，每张最少 512 tokens、最多 4,096 tokens；Base64 传输长度不会再被当作会话文本。

覆盖层只追加到下一次 provider 调用的内存消息列表，不参与 ToolResult 序列化、会话持久化、普通 AgentLogger 历史、cache fingerprint 或摘要输入。Session trace 只保留文字以及图片的媒体类型、宽高、源字节数和摘要；provider debug log 会始终脱敏 Anthropic Base64 source 与 OpenAI-compatible data URL，即使显式开启 full-payload logging 也不会记录原图。空的 provider-stale 重试会暂时保留覆盖层；一旦收到实际内容或正常结束就立即释放。能力或预算校验失败时，工具结果会变成有界错误，提示改用 `strategy="proxy"` 或减少图片数量。

Provider 返回的 usage 仍会包含本次图片输入。因此对应 assistant message 只持久化一个纯数字的 request-only 估算值；下一次上下文计算会先扣除它，再累加后续持久消息。这样已释放图片不会导致提前压缩，重启恢复后也能继续正确估算，同时不保存任何图片字节。

## 超长工具结果落盘

`box_agent/tool_result_storage.py` 中的 `ToolResultStorage` 统一负责持久化、预览生成和会话内去重。共享执行循环同时用于串行和并行工具；CLI 与 ACP 不复制这套策略。

### 单结果即时检查

模型可见单结果下限为 20,000 字符，并随当前模型的安全输入上限扩大：`max(20,000, context_token_limit * 0.25)`。未声明 `max_result_size_chars` 的工具使用这个共享动态值；工具可以声明更低的明确上限。只有尚未处理的普通结果进入这条通用策略。

以下结果不会再做二次压缩：

- 已实际采用工具 `model_context` 的结果会按 `tool_use_id` 冻结；
- `read_file`、`query_jsonl`、`search_files` 通过 Infinity 明确退出，它们分别依赖行/字符分页、cursor/结构化摘要、结果数/字符分页；
- `bash`、`bash_output` 也通过 Infinity 退出，因为工具内部已经执行一次 50,000 字符的 40% head + 60% tail 截断。

读取类工具不会被单结果即时检查外置，避免每个普通分页结果都落盘后又诱导模型重新读取。Infinity 只退出这条即时检查；当多个并行结果的合计内容超过请求总预算时，聚合检查仍可外置其中最大的页面。工具也可通过 `ToolResult.persistence_content` 请求统一落盘完整内容。

符合条件且超过上限时：

- 字符串保存为 `.txt`；
- 只含 text block 的数组格式化序列化后保存为 `.json`；
- 文件路径为 `~/.box-agent/sessions/<session>/tool-results/<tool_use_id>.<ext>`；
- 使用独占创建模式 `x`（等价于 `wx`），已有文件不会被覆盖；
- 模型侧结果替换为稳定的 `<persisted-output>` head + tail 预览。

以下情况保留原结果：未超过阈值、包含图片或任何非 text block、持久化失败。空输出规范化为 `(<工具名> completed with no output)`，Bash 对应 `(Bash completed with no output)`。

每个 `tool_use_id` 只做一次决策。成功落盘后的替换文本会被缓存，后续循环直接复用，不会重复写文件。恢复会话时已经存在的结果会被冻结，不会被追溯外置。

工具仍可自行保留有界输出；此时通过 `ToolResult.persistence_content` 把完整可落盘文本交给统一边界，真正的写入仍只由 `ToolResultStorage` 完成。Bash 的成功和失败命令都使用这条路径：完整输出保存到磁盘，模型继续看到工具已经生成的 head/tail，并额外得到完整输出路径，不会再被替换成通用的 2,000 字符 head 预览。工具提供的语义化 `model_context` 属于另一层职责，一旦采用也不会被即时检查或 fresh 总预算重复处理。

### 预览策略

1. 总计最多保留 2,000 个字符；
2. 同时保留开头和结尾，中间插入明确的 omitted 标记；
3. 在合适时优先沿换行边界裁剪，避免制造半行；
4. 单行文本也保留其精确 head 和 tail。

对尚未处理的普通结果，模型看到的形式为：

```text
<persisted-output>
Output too large (...). Full output saved to: ...

Preview (head + tail, up to 2.0KB):
...
</persisted-output>
```

对提供 `persistence_content` 的自截断工具，标签内改为 `Tool-bounded output`，内容是工具已经生成的有界结果，而不是再次生成的通用预览。

### fresh 结果总预算

每次 LLM 请求前，对本会话首次出现的工具结果执行动态总预算检查：`max(50,000, context_token_limit * 0.50)` 字符。

1. 只处理 fresh `tool_use_id`；
2. 排除已经采用 `model_context` 的结果，但仍统计单结果声明为 Infinity 的自处理工具；Infinity 只关闭即时单结果落盘，不代表并行批次可绕过聚合预算；
3. 按可落盘结果大小从大到小排序；
4. 总预算路径保留可按结果份数自动收紧的 head + tail 预览和恢复路径，并按包装后的模型侧实际长度记账；
5. 依次持久化并替换最大结果，直到实际剩余 fresh 内容不超过预算。

不支持的 block 和落盘失败结果保持不变。检查时 ID 会被标记为已见，因此后续请求不会反复处理。这条路径专门覆盖并行工具调用：单个结果都没有超限，但合计内容过大。

## 上下文限制压缩

### 触发阈值

```text
autoCompactThreshold = 0.9 * (context_window - max_output_tokens)
```

`LLMConfig.context_token_limit` 会先预留配置的最大输出预算，再从剩余输入预算中保留 10% 作为 token 估算误差和摘要请求的余量。

ACP 模型绑定可以通过当前所选模型的 `contextWindow` 和 `maxTokens` 覆盖这两个值。Agent 在创建会话时推导输入阈值，并在轮次之间切换模型绑定时重新计算。绑定没有提供能力数据时回退 `config.yaml`；用户自定义模型预设仍由这里的配置值提供能力声明。

### 估算下一次请求

Provider 会把真实 API 响应的 usage 附在对应 assistant message 上。压缩器找到最近一条带真实 usage 的响应，按以下方式得到当时完整上下文：

```text
input_tokens
+ cache_creation_input_tokens
+ cache_read_input_tokens
+ output_tokens
```

然后对这条响应之后新增的消息做保守估算。若没有真实 API usage，则对整个待发送请求（包括工具 schema）取 `字符数 / 4` 与 UTF-8 字节数 `/ 3` 中较大者，避免中文及其他多字节文本被严重低估。

### 压缩后的消息组织

压缩会在原始 message 列表末尾临时追加一条 `user` 摘要指令，并调用一次摘要模型。历史不会被序列化进新 prompt，也不会分块或滚动摘要，因此摘要请求保留完整的 provider message 前缀，可以复用 KV cache。ACP 通过会话模型路由器解析该调用，并把输出上限限制为 4,096 tokens。自动模式只能在宿主下发的 `autoRouting.models` 模型池中，按摘要任务标签、能力和上下文适配度排序选择；上下文适配使用当前 Agent 的安全输入上限，避免把大段历史交给小窗口工具模型。显式选择模式始终锁定原模型，不再切换独立 lite 模型。其他 host 可传入独立摘要客户端，未提供时回退主模型。这次调用不提供工具并关闭 thinking。指令要求按时间顺序列出全部 user message，把所有结构化分析放进唯一的 `<summary>...</summary>` 块，并内置九节输出结构示例。响应必须严格由一个非空 summary 块组成；写入 `Summary:` 后只取标签内部文本，标签本身会被丢弃。摘要请求和本地写回的上限均为 8,000 字符；若重建请求仍超过安全输入限制，同一份摘要会在不增加 provider 调用的前提下依次收紧到 4,000、2,000 字符。摘要调用失败、格式错误或返回空内容时，使用明确标注为有损的确定性有界摘要兜底。若重建请求仍超限，本轮返回普通上下文超限错误，不进入任何领域恢复状态机。

模型输出包装成以下合成 `user` message：

```text
This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
<模型生成的摘要>

Continue the conversation from where it left off. Do not acknowledge the summary, recap what was happening, or ask the user to repeat information solely because compaction occurred. If genuinely required information is still missing, use the normal user-input or decision tool. Otherwise, pick up the last task as if the break never happened.
```

新历史顺序如下：

```text
system message
摘要 user message
按规则保留的近期 messages
运行状态 user message
```

recent 选择统一覆盖 user、assistant 和 tool message；assistant 工具调用与连续 tool results 按组保留。目标上限为 5 条消息、合计 20,000 字符，并始终原样保留最新真实 user message。如果最新完整协议组本身超过字符上限，压缩会保留精确的 assistant 工具调用、参数、result ID、顺序和数量，同时将已经进入 continuation summary 的 assistant reasoning 与 tool-result 正文替换为有界 receipt。只有剩余的精确用户文本、工具调用参数、system 指令、工具 schema、有界摘要和运行状态仍无法放入安全窗口时，才标记为 blocked。

上下文压缩不再发现、重新读取或重放近期文件。

Goal、Todo 和 Plan 通过显式、无副作用的 `compaction_state` 契约读取；压缩不会执行普通工具调用，合并后的运行状态消息上限为 12,000 字符。完整 active skill 指令继续固定在 system message 中，不再通过回放历史 `get_skill` 调用重建。控制策略查询“最新用户文本”时会排除内部摘要与运行状态消息。

若重建后的请求仍超过安全阈值，结果会标记为 blocked，不会静默发送一个已知超限的请求。

## 相邻保护

write/edit 工具调用参数会保留原文，直到整段历史压缩摘要其所在轮次；当前实现不会再单独将这些参数替换成历史占位符。它与工具结果落盘是两套独立机制。旧会话或外部历史中的遗留占位符仍会被安全保护拦截，不能作为可执行文件或代码参数使用。

## 验证

- `tests/test_tool_result_storage.py`：类型处理、独占写入、预览、Read 单结果豁免、失败保留、去重和总预算排序；
- `tests/test_core.py`：请求前执行、usage 加增量估算、原始前缀单次摘要、回退估算、近期消息边界与运行状态恢复；
- `tests/test_auth.py`：阈值推导。
