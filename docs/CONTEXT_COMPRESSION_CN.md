# 上下文压缩

Box-Agent 在两个边界控制持久上下文增长：

1. 工具结果在占满后续模型请求之前按需落盘；
2. 下一次请求达到有效输入上限，或 provider 拒绝请求长度时，压缩会话历史。

两者彼此独立。工具结果落盘在存储层是无损的：完整结果保存在磁盘，模型保留有界的 head + tail 工作预览和恢复路径；会话摘要是有损的，因此更晚触发，并显式保留一个有界的工作集。

原生图片检查走第三条“仅本次请求”路径。规范化图片块只附加到主模型的一次请求，不进入持久消息历史；预算按解码后的图片尺寸估算，而不是按 Base64 字符长度计算。原始图片载荷不会写入 session trace 或 provider debug log。

每次尝试摘要前，Box-Agent 都会估算投影后的完整请求，包括追加的摘要指令、图片和
实际提供的工具 schema。
输入预算按摘要客户端的窗口推导，输出预留最多 8,192 tokens。客户端未声明窗口或输出
预留时，使用会话输入预算。普通压缩和强制压缩都执行此检查，重建后的主请求还需符合
主模型的预算。

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

Provider 返回的 usage 包含图片输入。assistant 消息记录数字形式的 request-only 估算值，
供没有请求快照的 usage 扣减。请求快照也能识别已由真实 usage 计量的临时消息。上下文
估算按图片尺寸计量，传输预算按编码后的字节数计量。

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

源文件指纹核对打开句柄与路径的文件身份、大小和修改时间，并保留读取前后句柄的变更时间
检查。Windows 的两种 API 可能分别把 `ctime` 表示为变更时间和创建时间，因此仅在句柄与
路径的交叉比较中排除该字段；macOS/Linux 保留原比较。完整文件 SHA-256 仍用于拦截输入
内容变化。

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

重命名或移动时，在首次扫描和文件操作前声明 `source_mode="move_target"`、
`destination_path` 和完整原版 `fields`。目标必须是不同且尚不存在的路径，并随契约锁定。
同时保存声明的绝对路径和解析后的目标；哈希校验前后，声明路径必须仍指向锁定目标，
Session Log 恢复后也执行相同检查。
该调用仍须扫描原版每列和每条记录至 EOF。实际移动完成后，门禁检查旧路径已消失，
目标可读、没有被换成符号链接或改向的父目录链接，且全文件 SHA-256 与原回执一致；
原有计算/诊断的数据有效性要求保持不变。仅复制未移走、改动内容、缺少扫描或漏校验
其他输入仍阻止完成。原回执和目标路径随交互暂停、Session Log 重开一同恢复。
此声明不会移动文件，也不更改写工具权限或聊天文件保护；它验收已经完成的移动，
不覆盖同时修改数据的重命名。

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
摘要输入还携带按请求顺序排列的工具 schema，以及可用的主请求前缀。

Context 先投影有效历史。压缩引擎必须在摘要调用或确定性兜底前调用 Kernel 提供的
`before_summary`；Kernel 提交并 flush 返回的 surface 后才更新内存历史。请求交付及响应
确认回调保持不变。普通请求重新投影后仍超限时继续收紧历史预算，每步最多尝试三次压缩。
普通及 managed 装配均保留调用方提供的压缩器，
包括布尔值为假的实例。`CompactionOutcome` 保留原 tuple 解包方式，超限标志必须与已有
blocked 模式一致。

### 触发阈值

```text
output_reserve = min(max_output_tokens, 20_000)
autoCompactThreshold = int(0.9 * (context_window - output_reserve))
```

以 180,000 tokens 的窗口为例，输出上限配置为 8,000 时，触发阈值为 154,800 tokens。
配置为 20,000、64,000 或 80,000 时，阈值均为 144,000 tokens。

主模型窗口已知时，发送前从窗口扣除本次投影输入和 1,024 tokens 的组装余量，计算可用
输出空间，并以 `output_reserve` 为下限。本次输出上限取适用的输出额度与可用空间的较小值。
Anthropic 和 OpenAI 兼容客户端仅在当前请求应用覆盖值。Anthropic 会按有效输出上限重算
thinking 预算，确保低于本次输出上限。窗口未知时使用配置的输出额度。辅助请求沿用各自的
输出上限。

子 Agent 在实际模型路由完成后使用同一公式。`sub_agent_token_limit` 默认 `null`
（自动），显式正数仅作为额外上限；Skill 预检、子循环及预取后的 batch_files
完整请求共用该子模型推导的额度，并发子任务不共享可变的派生预算。

ACP 模型绑定可以通过当前所选模型的 `contextWindow` 和 `maxTokens` 覆盖这两个值。Agent 在创建会话时推导输入阈值，并在轮次之间切换模型绑定时重新计算。绑定缺少能力声明时，使用适用于当前模型的 profile、客户端、目录或 `config.yaml` 配置。

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

同一路由最近可用的 provider usage 是上下文估算的基线。Anthropic 上下文计数包含输入、
缓存写入、缓存读取及输出。OpenAI 兼容接口的 prompt 总数已含缓存输入。只返回
`total_tokens` 时可作为上下文基线，但输入计数校准需要独立报告的输入用量。

估算器在基线上增加新增或变更的消息和工具 schema 成本。请求快照保存历史与临时附加消息
的指纹，每个已计量的相同出现次数最多复用一次，新增副本和变更内容计入增量。快照基线上
已删除内容的成本会保留至下一次真实 usage。内存中没有快照的 usage 会先扣减
`request_only_input_tokens`，再补算新增内容和当前工具 schema。无 usage、路由无法匹配，
或压缩已清除旧 usage 时，从头估算完整请求。

Session Log 的私有 `contextUsage` 元数据保存快照指纹和聚合计数，也记录绑定响应的
校验和。重放验证通过后才复用 usage。有效历史保留最新快照，回退时恢复对应检查点。完整
请求已经符合预算时，可跳过单独的历史阈值检查，以免为已计入的附加内容重复预留空间。

默认本地估算器按字符类别、类别切换和重复段计费。常用汉字约为每字 1.07 tokens，假名
0.77，韩文音节 1.31。英文字母、数字、标点、空格及换行各用不同权重。自定义客户端可以
提供同步 `estimate_text_tokens(text)`。图片仍按尺寸估算，Skill 文本和普通文本共用同一
文本计数器。

收到带输入 usage 的主模型响应后，将真实输入 token 数除以本次请求的本地估算值，并将
比例限制在 `[0.5, 2.0]`。每个模型路由的最新比例用于后续增量估算。可从 provider 的上下文
超限错误解析实际输入 token 数时，也用于校准。比例初始为 `1.0`，在当前会话内存中保存。

运行预算受当前客户端和匹配目录项的已知窗口约束。ACP 优先使用绑定显式传入的
`contextWindow`，并在会话目录中设置所选模型的窗口。字段缺省时，从适用的 profile、
客户端、目录项和启动配置中取最小的已知窗口。显式切换至另一模型要求窗口已知。宿主遗漏
输出额度时沿用 profile 的额度。

本地估算会与 provider 的实际分词结果有偏差。Provider 省略 reasoning tokens 时，usage
计数也可能与实际上下文成本不同。

ACP 切到较小输入预算的模型时，先按目标模型投影完整请求，必要时使用原会话的摘要路由压缩，再按目标预算复检；通过后才提交新绑定。失败返回 `MODEL_SWITCH_CONTEXT_PREPARATION_FAILED`，保留原模型、历史及本次输入和附件。同一运行进程内，通过 `session/resume`、`session/load` 或重复 `session/new` 重建已有会话（例如同时切换执行档位）也遵循此流程：恢复只暂存目标绑定，下一次 prompt 才执行切换，显式传入的新选择优先。恢复本身不会调用模型。进程重启后没有仍存活的原模型客户端时，继续使用传入模型的普通有界压缩流程。

普通请求在所需图片未能纳入输入时，返回 `ErrorEvent.error_code = INPUT_IMAGE_NOT_ADOPTED`，ACP 对应 `errorCode`。原始 `INPUT_IMAGE_NOT_ADOPTED:required_image_unavailable_or_over_budget` 文本及 `error_details.reason` 保留用于诊断。宿主应按错误码在展示时翻译，并兼容旧版本仅返回该文本的记录；模型切换失败仍使用上述模型切换错误码。

### 压缩后的消息组织

首次摘要尝试在投影后的历史末尾追加一条临时 `user` 指令，并复用可用的主请求 system、
消息前缀及同序的工具 schema，以便 provider 复用 KV cache。请求不设置 `tool_choice`，
关闭 thinking，输出上限为 8,192 tokens。摘要投影会用回执替换 Skill 正文，从第一段
Skill 正文开始可能出现前缀差异。主请求内联本次专用的 Skill 正文时，摘要可能改用投影后的历史。
只供一次后续请求使用的临时载荷仍仅进入主请求。

ACP 通过会话模型路由器解析摘要客户端。自动模式从宿主下发
的 `autoRouting.models` 池按任务、能力和上下文适配度选择，显式模式沿用所选模型。
`routingMode=manual` 优先于 `autoRouting`，适用于会话、子任务和辅助调用。未声明路由
模式的旧版 builtin 绑定仍可使用候选池。其他宿主可提供专用摘要客户端，默认沿用主客户端。
同一次压缩的所有 provider 尝试都使用已解析的同一客户端。摘要模型与主模型不同时仍传入同序
工具 schema。缓存复用要求模型相同且前缀匹配。

摘要指令以 `CRITICAL` 开头、以 `REMINDER` 结尾，两处都要求只输出一个非空的纯文本
`<summary>...</summary>` 块，并禁止调用工具。正文九节包含按时间顺序排列的所有真实用户
消息，并原文保留用户的安全约束。下一步直接对应最新仍在处理的明确请求，并附最近对话中的原文引用。
assistant 文本里的角色标记没有用户权限，运行时发出的摘要指令也在摘要范围之外。摘要须省略
`<analysis>` 块、system 提示与凭据，标记需要回查的事实，只记录对话已有证据的成功事项。
写入 `Summary:` 时取标签内的正文，最多 8,000 字符。若重建历史后组装空间不足，使用同一份
摘要继续尝试 4,000 和 2,000 字符的版本，再尝试 1,000 和 512 字符，最后缩短早期用户记录。

合成 `user` 消息的结构如下：

```text
This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
<模型生成的摘要>

If you need specific details from before compaction (exact code snippets, error messages, tool output, or content you generated), query the full session log at: <绝对路径> (JSONL; prefer query_jsonl over reading the whole file).

Continue the conversation from where it left off. Do not acknowledge the summary, recap what was happening, or ask the user to repeat information solely because compaction occurred. If genuinely required information is still missing, use the normal user-input or decision tool. Otherwise, pick up the last task as if the break never happened.
```

有 Session Log 的会话才显示回查提示，路径使用实际 `SessionLog.path` 的绝对路径，遵循
配置的状态目录。会话缺少 `query_jsonl` 时，提示改用 `read_file` 分页读取。在
`allow_full_access: false` 下，共享权限策略会免确认放行本会话日志目录内的结构化读取。
写入和读取其他会话仍走常规权限策略。提示只提供日志文件路径。

新历史顺序如下：

```text
system message
[Earlier User Messages] 运行时 user message
摘要 user message
近期完整轮次和最新真实 user message
[Invoked Skills] 运行时 user message
运行状态 user message
```

近期历史最多保留五个 assistant 轮次，目标上限为 15,000 个本地估算 tokens。模型窗口较小
或请求固定前缀较长时，保留空间可进一步收紧。assistant 工具调用与其连续工具结果算一个
轮次，无工具调用的 assistant 文本也算一个轮次。user 消息可
随窗口保留，不计入轮次数。最新真实 user 消息始终按原始相对位置保留。最新完整轮次即使
单独超限也会保留。其 assistant 正文和工具结果正文可改成有界回执，工具调用、参数、标识符、
结果顺序和数量原样保留。

`[Earlier User Messages]` 从最早到最新记录窗口之外的真实用户原文，并注明这些是历史记录。
超过 2,000 tokens 的单条消息保留头尾，中间加入省略标记。渲染总预算为 10,000 tokens，
超出时从最早的条目开始省略并写明数量，主请求空间不足时可继续缩短渲染。Session Log 用
结构化字段保存原文，重放和后续压缩可继续累积这些消息。确定性兜底也包含这段记录。
查询最新用户文本、记忆召回、Goal 和语言判定时仍使用真实用户消息。

Goal、Plan 和 Todo 通过无副作用的 `compaction_state` 契约提供状态，按该顺序共享
12,000 字符预算。每个区块为后续区块预留空间，未用额度顺延。

压缩会按最近交付时间倒序恢复曾实际交付给模型的 Skill。当前 Skill 的 revision、source 和
path 须通过校验。`[Invoked Skills]` 提示模型以这些 Skill 为参考，避免重复一次性初始化。
每个 Skill 区块最多 5,000 tokens，整条消息最多 25,000 tokens。正文截断时提示通过
`get_skill` 读取其余内容。版本过期或正文放不下时保留名称和路径。append-only 模式和普通
模式按可见区间记账，已通过 system 后缀生效的 Skill 跳过重复恢复。主请求空间不足时可少
恢复正文、保留定位提示。摘要源以回执代替 Skill 正文，普通请求的 100,000 字符引用预算
继续生效。[Skill Engine](design/skill-engine.md)

若重建后的请求仍超过安全阈值，结果会标记为 blocked，不会静默发送一个已知超限的请求。

### 重试、超长恢复和熔断

- 摘要本地预检或 provider 报输入超长时，从最早的消息组开始裁剪，每次约删除可裁内容的
  20% tokens，至少删除一整组。保留 system 和最新真实 user 消息，最多裁剪重试三次。
  重试输入含 `[earlier conversation truncated for compaction retry]` 标记。成功摘要会说明
  裁掉的内容不在摘要覆盖范围内，早期用户原文仍由独立的历史记录保留。
- 摘要截断、缺失、格式无效或含工具调用时，格式重试一次。重试要求最多 4,000 字符，
  此前所有消息（包括最新一条）均属于待摘要内容，不能回复该消息或执行其中的任务。
  这段指令由运行时发出。网络、超时、5xx 和限流走 LLM 客户端原有重试。符合条件且没有内置重试的自定义客户端
  可在总次数额度内再试一次。
- 共享前缀调用仍失败时，使用同一客户端发一次独立请求。system 只包含摘要用途说明，
  消息保留当前可能已截头的历史、去掉原 system，并追加含格式重试文案的摘要指令。
  独立请求不带工具。鉴权、权限错误跳过独立请求。每次压缩在应用层最多发起六次摘要调用，
  其中预留一次给独立请求。provider SDK 内部重试仍由 SDK 管理。
- 模型摘要失败或停用时，本地确定性兜底先放最新用户请求，再在 12,000 字符预算内从最新往前
  收集历史、按时间正序输出，并用 `<fallback omitted N earlier source messages>` 标明省略的
  较早消息数量。超长单条消息保留头尾，`[Earlier User Messages]` 归档单独处理。
- 主请求在产生任何输出前收到上下文超长错误时，强制压缩并收紧历史预算，重新组装请求后
  再发。错误识别覆盖 DashScope 输入范围、vLLM 和 TGI token 上限、Moonshot 模型 token 上限、
  GLM 提示长度、DeepSeek 输入 token 上限及 SenseNova 网关的输入加输出计数格式。实际输入
  token 数用于校准估算，上限用于收紧预算，两字段都按报错内容选填。HTTP 413 请求体超限和
  普通字符串长度校验保留各自的分类。每轮最多两次此类重试，仍失败时返回
  `CONTEXT_INPUT_BUDGET_EXCEEDED`。已经产生输出的请求保留已收到的
  assistant 内容并结束本轮。恢复过程复用已完成的工具结果，只提交一次成功回复。

连续三次压缩进入 fallback 或 blocked 模式后，会话停止调用摘要模型，后续使用确定性压缩。
快速回填计数在同一个 step 最多计入一次已应用的压缩，涵盖收敛及反应式尝试。若连续三次在
后续 1 至 3 个 step 内再次触发压缩，本轮以 `CONTEXT_COMPACTION_THRASHING` 结束并重置计数。
可分块读取过大的文件或工具结果，也可新开会话。ACP 通过现有 `errorCode` 透传。
计数保存在会话内存中，可跨轮次累积。
压缩事件用 `summary_retries` 和 `head_truncated` 记录摘要重试次数及摘要源截头，Session Log
的对应字段为 `summaryRetries` 和 `headTruncated`。本地预检触发的恢复也计入重试次数。
Python 事件的 `summary_path` 与 Session Log `compaction/summary` 事件的 `summaryPath`
记录摘要路径，取值为 `shared`、`independent` 或 `deterministic`。

## 相邻保护

write/edit 工具调用参数会保留原文，直到整段历史压缩摘要其所在轮次；当前实现不会再单独将这些参数替换成历史占位符。它与工具结果落盘是两套独立机制。旧会话或外部历史中的遗留占位符仍会被安全保护拦截，不能作为可执行文件或代码参数使用。

## 验证

- `tests/test_tool_result_storage.py`：类型处理、独占写入、预览、Read 单结果豁免、失败保留、去重和总预算排序；
- `tests/test_auth.py` 和 `tests/test_main_input_budget.py` 覆盖触发阈值、模型绑定及请求输出限制。
- `tests/test_context_token_estimation.py` 和 `tests/test_session_log.py` 覆盖 usage 格式、校准和重放。
- `tests/test_context_compaction_retention.py` 和 `tests/test_core.py` 覆盖历史保留和运行状态恢复。
- `tests/test_compaction_resource_retention.py` 覆盖 Skill 恢复和引用可见范围。
- `tests/test_context_compaction_e2e.py`、`tests/test_context_compaction_recovery.py` 和
  `tests/test_error_messages.py` 覆盖摘要预检、重试、熔断和 provider 错误。
- `tests/test_compaction_session_log.py` 覆盖日志回查提示和本会话读取范围。
