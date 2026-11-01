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

## 大表计算的前置上下文策略

CSV/TSV/Excel 计算通过共享、按能力渲染的指引，先取得表头、少量样例和数据概况，再用
`execute_code` 或通过 `bash` 调用已有 Python 解释器全量处理记录。可以分块解析，但采样
不能证明全量计算完成。须检查记录数、类型、缺失值、解析错误及任务要求的汇总勾稽，
不得静默跳过错误记录；对话只返回统计、校验结果和有界异常样例。保存后还须重新读取产物，
核对格式和关键汇总。

启用文件工具时，`validate_table` 为本轮用户明确引用且已存在的 CSV/TSV 输入提供程序级
完成门禁。它在权限检查后锁定源文件 SHA-256，用格式解析器扫描至 EOF，对每条原始记录的
每一列执行声明的字段规则；旧版本、取消记录和文件尾部不能提前过滤。字段契约必须覆盖
完整表头，支持必填性、文本、整数、有限十进制及范围/精度、日期、布尔、枚举。

执行途中用户补充的明确输入会追加到同一个门禁，保留已有回执及已锁定的任务用途。
引号和 Markdown 中的路径按整体识别，不把其中的尾部文件名重复识别为另一输入。
CSV 解析器至少支持 16 Mi 字符的单字段，并保留进程已有的更高额度；解析错误仍不能
证明已扫描至 EOF。

默认 `profile="declared_fields"` 使用用户实际字段规则，支持合法负金额、零版本和更高精度；
不能仅凭列名推断领域含义。仅明确符合订单/退款要求的任务选择 `profile="transaction_v1"`，
执行固定基线：标识必填，revision 为至少 1 的整数，amount 为有限、
非负、最多两位小数的金额；status 和存在的 region/currency 必须声明枚举，date 必须是
日期，is_test 必须是布尔，均不可设为可空或普通文本。模型只能声明满足此基线的契约。
profile 随字段契约锁定，后续省略则继承；发现坏数据后不能降级已选规则绕过门禁。

每次扫描记录源哈希、契约哈希、逻辑记录数、逐字段检查/缺失/错误计数与最多 20 个异常
样例。Session Log 的 `table.inputs`、`table.validation`、`completion.check` 提供审计证据；
模型回复和公开工具 JSON 不授予完成资格。缺少证据时最多要求补验两次；扫描不完整、
源文件变化、校验器异常或补验耗尽以 `COMPLETION_GATE_UNSATISFIED` 结束。
首次扫描后字段契约不能放宽；新一轮必须重新校验，不复用历史声明。

用户提问和计划审批的交互暂停会保留待完成门禁。`completion.paused` 把能力自有的输入、
哈希、已锁定契约、文件角色和扫描回执存入 Session Log；重开会话先恢复这些状态，再追加
新提供的输入。无日志的进程内会话按消息列表身份在既有工具上暂存，不能跨会话复用。
模型文字和公开工具 JSON 不能重放为校验证据；恢复后重新核对当前文件哈希，缺少能力或
不支持的检查点会阻止完成。成功、失败或取消终态通过 `completion.closed` 结束待完成任务，
之后独立新任务重新开始；只有 Session Log 提供持久状态。

任务用途在第一次扫描开始前通过 `purpose` 声明并按本轮锁定，同时记录 `table.purpose`：
仅检查并报告错误的任务使用 `diagnosis`；要求有效数据的计算使用 `computation`（首次省略
时的默认值，后续省略则继承已锁定用途）。不能发现坏数据后切换用途绕过计算门禁。
诊断用途仍要求每个原始输入、全部字段检查、EOF 和当前版本与校验哈希一致，也必须按任务要求如实报告
错误、保存及读回诊断产物；计算用途额外要求全部输入数据合格。

扫描调用的工具 `success` 和新增 `validation_complete` 只表示校验程序完整执行成功；`data_valid`
单独表示数据是否全部合格，扫描不完整时为 `null`。例如完整诊断发现六处错误时，工具可返回
`success=true, validation_complete=true, data_valid=false`，任务正常完成；相同坏数据用于
计算仍会被门禁阻断。兼容字段 `valid` 保持原有语义，不能用工具 `success` 代替数据有效性。
任务成功终态、`completion.check` 门禁结果和 `table.validation` 中的数据质量结果分别留痕。
用途声明须符合用户任务；它不替代业务交付验收或自动推断用户意图。

仅用户要求原地编辑/追加的文件，首次调用校验器且尚未写入时声明
`source_mode="edit_target"`；默认 `input` 仍要求源文件不变，开始扫描后不能切换角色。
修改目标必须先完成原版全量扫描并保留回执，每个修改后版本都要按声明的字段契约全量复验，
最终版本必须合格（诊断用途也一样）。写入后缺少新校验会要求补验，不将预期修改判为不可
修复的源损坏；其他计算输入仍保持哈希保护。角色声明须符合用户指令，它不授予写权限、
不证明修改意图；既有工具权限和生产者写入回执保持不变。改列时首次扫描前同时声明原版
`fields` 和新版 `output_fields`，两份契约均锁定；后续省略 `fields` 继承新版契约，前后都
必须覆盖每列和每条记录。

纯文件管理须在操作前声明角色：`delete_target` 不传字段契约，准备删除（无需解析坏 CSV），
完成时验证路径实际不存在；`replace_target` 传新版 `fields`，准备不处理旧记录的整文件
替换，写入后再次调用校验器全量验证新版。这两类准备调用返回 `operation_ready=true`、
`validation_complete=null`、`data_valid=null`，记录 `table.file_operation` 和原哈希，
不宣称旧数据已校验，也不授予实际写权限。未实际删除或未复验新版仍阻止完成；其他输入
不豁免，声明后不能切换角色或放宽契约。角色须符合用户指令，不能用于绕过计算输入校验。

门禁证明的是**按所声明契约全量校验原始数据**。字段含义、允许的枚举值和业务范围仍须从
任务规则确定；它不会推断业务契约，也不证明后续去重、计算或交付文件正确，仍须执行计算
及读回勾稽。当前只覆盖本轮明确引用的本地 CSV/TSV；Excel、仅在工具输出中发现的路径、
未启用该工具的会话不在此门禁范围内。计划待确认与等待用户输入也不表示数据任务完成。
`read_file` 分页、JSONL 查询、600 秒等待及 429 重试策略均不改变。

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

### 能力与提交边界

Kernel 通过 `CompactionInput` 调用 `CompactEnginePort`。默认引擎沿用现有压缩算法，
不替换请求 Context 接口，也不增加第二套 Skill 加载策略。输入分别保留用于恢复运行状态的
工具目录、用于估算的本次实际提供 schema、强制恢复标志及摘要请求输入上限。

Context 先投影有效历史。压缩引擎必须在摘要调用或确定性兜底前调用 Kernel 提供的
`before_summary`；Kernel 提交并 flush 返回的 surface 后才更新内存历史。请求交付及响应
确认回调、最多一次压缩重试的规则保持不变。普通及 managed 装配均保留调用方提供的压缩器，
包括布尔值为假的实例。`CompactionOutcome` 保留原 tuple 解包方式，超限标志必须与已有
blocked 模式一致。

### 触发阈值

```text
autoCompactThreshold = 0.9 * (context_window - max_output_tokens)
```

`LLMConfig.context_token_limit` 会先预留配置的最大输出预算，再从剩余输入预算中保留 10% 作为 token 估算误差和摘要请求的余量。

ACP 模型绑定可以通过当前所选模型的 `contextWindow` 和 `maxTokens` 覆盖这两个值。Agent 在创建会话时推导输入阈值，并在轮次之间切换模型绑定时重新计算。绑定没有提供能力数据时回退 `config.yaml`；用户自定义模型预设仍由这里的配置值提供能力声明。

自动选模也会在排序前检查候选输入预算。已知输出上限时复用 `derive_context_token_limit`，并计入本次辅助调用更小的输出额度；目录缺少输出上限时使用当前客户端的输出上限，两者均未知才保留窗口 80% 的旧预算。没有剩余输入预算的候选会被排除。候选池只要存在已知窗口，就只从已知且容量足够的候选中选择；全部不满足时返回 `AUTO_MODEL_CONTEXT_CAPACITY_EXCEEDED`，不再回退到仍然装不下的最大窗口，也不借容量未知的候选绕过约束。目录完全缺少窗口时保留兼容路由，并记录 `context_capacity=unknown`，这不代表容量安全；已知容量记录 `context_capacity=estimated` 和 `input_token_budget`。这里使用路由输入估算，不能替代下述完整请求计量与压缩。

已通过容量检查的模型若无法绑定，会返回 `AUTO_MODEL_BINDING_FAILED`，不再静默继承上一客户端未经本次校验的预算。

### 查看模型为什么被选中

任务标签和文字复杂度会区分明确的翻译指令与待译内容：例如 `把这句话翻译成英文：这个错误已经修复。` 不因内容里的“错误／修复”产生 `debug` 标签或升到能力 3。此规则支持中英文翻译指令及成对引号、冒号后单行正文；额外分析／修复动作、未闭合引号和不明确的边界保留原分类。它只影响词法信号，原始请求、完整长度和 token 估算不变；图片、显式工具、能力要求及桌面思考／专家等信号继续有效，手动绑定和会话沿用规则不变。

`resolve_model_client` 通过现有 Python INFO 日志输出 `model.routing`；处于会话 trace 范围内时，同时写入会话 JSONL。ACP 把 Python 日志发往 stderr，桌面宿主会收集到日志目录中的 `box-agent-stderr.log`。Trace 沿用 `BOX_AGENT_SESSION_TRACE_DIR` / `log/sessions` 路径和保留策略，设置 `BOX_AGENT_SESSION_TRACE_ENABLED=0` 可关闭该 trace 通道。其他宿主需启用 INFO 日志，才能保留 trace 范围外的路由记录。

按 `routing_id` 关联一次选模尝试。Agent 先记录 `decision`，随后记录 `bound`、`inherited` 或 `binding_failed`；候选筛选直接被拒绝时只有 `decision`。`strategy` 用于区分子任务和辅助请求；会话 trace 外层还包含 `session_id`、`turn_id`。桌面主模型选择写入 Electron 日志，搜索 `[local-agent] model routing`，按其中的 `routing_id`、`session_id` 查看 `decision` 和后续 `persisted` / `preparation_failed`。两端 ID 分别标识各自的尝试，并非同一个跨进程事务。选中候选不等于绑定保存成功，也不等于模型请求成功。

带版本的 `decision` 记录任务标签、所需能力、输入估算、容量是否已知，以及最多 64 条候选摘要。`candidate_count`、`candidates_truncated` 标明总数和省略数，最终被选候选始终保留。排查时看候选的 `input_token_budget`、`matched_tags`、`rejection_reasons`、`score_breakdown`：

| 字段或原因 | 含义 |
| --- | --- |
| `rejection_reasons` 中的 `context_capacity`、`context_unknown`、`ability`、`vision` | 分别表示容量不足、容量未知、能力不足、缺少视觉能力；`score=null` 表示未进入评分排序。 |
| `highest_score` | 满足约束后按既有评分胜出。 |
| `catalog_order_tiebreak` | 合格候选同分，按目录顺序选择。 |
| `previous_model_eligible`（桌面） | 当前模型仍满足约束，因此继续使用，即使其他模型得分更高；`minimum_ability_level` 包含当前模型的能力下限。 |
| `manual_model`、`no_auto_model_pool`、`task_context_not_supplied` | 手动指定、无自动候选池或未提供新任务输入，沿用绑定而不重新自动排序。 |
| `no_context_candidate`、`no_ability_candidate`、`no_vision_candidate` | 没有候选通过相应约束。 |

评分保持既有策略：每个命中标签加 100 分；非简单任务加能力等级 × 20，简单任务减能力等级 × 5；简单任务的 `fast` 模型加 25；桌面目录默认模型额外加 2。约束优先于评分。例如能力 3 且命中 `code` 的候选为 160 分，同能力但无标签命中的候选为 60 分；容量或能力不合格的模型不能靠标签加分胜出。

记录只摘取上述诊断字段，不复制任务正文、文件路径、凭据、完整模型绑定或异常正文；模型名、标签和候选数量均有界，日志失败不改变选模结果。这些记录解释本地策略及客户端准备过程，不能证明模型质量或网关最终使用的后端模型。

离线规则基线及固定模型／自动模型的真实结果对照入口见[自动路由评测基线](../test_workspace/README.md#自动路由评测基线2026-10-31)。评测报告会引用已采集的路由记录，并将其与实际模型请求和独立任务判定分开呈现。

### 估算下一次请求

Provider 会把真实 API 响应的 usage 附在对应 assistant message 上。压缩器找到最近一条带真实 usage 的响应，按以下方式得到当时完整上下文：

```text
input_tokens
+ cache_creation_input_tokens
+ cache_read_input_tokens
+ output_tokens
```

然后对这条响应之后新增的消息做保守估算。若没有真实 API usage，则对整个待发送请求（包括工具 schema）取 `字符数 / 4` 与 UTF-8 字节数 `/ 3` 中较大者，避免中文及其他多字节文本被严重低估。

压缩会创建新的历史 epoch。压缩结果中保留的 assistant 消息会清除旧的 provider usage，下一次 `prepare request` 从压缩后的消息面重新估算；loop 同时清除旧的 API token 锚点，直到新的 provider 响应提供新的 usage。

### 压缩后的消息组织

压缩会在原始 message 列表末尾临时追加一条 `user` 摘要指令，并调用一次摘要模型。历史不会被序列化进新 prompt，也不会分块或滚动摘要，因此摘要请求保留完整的 provider message 前缀，可以复用 KV cache。ACP 通过会话模型路由器解析该调用，并把输出上限限制为 8,192 tokens。自动模式只能在宿主下发的 `autoRouting.models` 模型池中，按摘要任务标签、能力和上下文适配度排序选择；上下文适配使用当前 Agent 的安全输入上限，避免把大段历史交给小窗口工具模型。显式选择模式始终锁定原模型，不再切换独立 lite 模型。`routingMode=manual` 时，ACP 忽略宿主误传的 `autoRouting`，包括过期或格式错误的候选池；该规则统一作用于会话、子任务和辅助调用。未声明路由模式的旧版 builtin 绑定仍兼容候选池路由。其他 host 可传入独立摘要客户端，未提供时回退主模型。这次调用不提供工具并关闭 thinking。指令要求按时间顺序列出全部 user message，把所有结构化分析放进唯一的 `<summary>...</summary>` 块，并内置九节输出结构示例。响应必须严格由一个非空 summary 块组成；写入 `Summary:` 后只取标签内部文本，标签本身会被丢弃。摘要请求和本地写回的上限均为 8,000 字符；若重建请求仍超过安全输入限制，同一份摘要会在不增加 provider 调用的前提下依次收紧到 4,000、2,000 字符。摘要调用失败、格式错误或返回空内容时，使用明确标注为有损的确定性有界摘要兜底。若重建请求仍超限，本轮返回普通上下文超限错误，不进入任何领域恢复状态机。

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

Goal、Todo 和 Plan 通过显式、无副作用的 `compaction_state` 契约读取；压缩不会执行普通工具调用，合并后的运行状态消息上限为 12,000 字符。SkillRuntime 管理选择及带版本的读取事实；Context 将显式选择或恢复的 Skill 正文放入普通请求资料，或接收真实读取工具的回复，并按最终可见文本核实覆盖。完整 Skill 正文不固定在 system message 中，也不会回放历史 `get_skill` 调用。分页、来源校验和请求提交边界见 [Skill Engine](design/skill-engine.md)。控制策略查询“最新用户文本”时会排除内部摘要与运行状态消息。

若重建后的请求仍超过安全阈值，结果会标记为 blocked，不会静默发送一个已知超限的请求。

## 相邻保护

write/edit 工具调用参数会保留原文，直到整段历史压缩摘要其所在轮次；当前实现不会再单独将这些参数替换成历史占位符。它与工具结果落盘是两套独立机制。旧会话或外部历史中的遗留占位符仍会被安全保护拦截，不能作为可执行文件或代码参数使用。

## 验证

- `tests/test_tool_result_storage.py`：类型处理、独占写入、预览、Read 单结果豁免、失败保留、去重和总预算排序；
- `tests/test_core.py`：请求前执行、usage 加增量估算、原始前缀单次摘要、回退估算、近期消息边界与运行状态恢复；
- `tests/test_auth.py`：阈值推导。
