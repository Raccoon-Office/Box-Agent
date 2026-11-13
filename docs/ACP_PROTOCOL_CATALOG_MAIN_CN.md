# Box-Agent ACP 双向协议总表（PR #250 固定版本）

核对日期：2026-11-06。基线由 main `a8a87c5c` 更新至 PR #250 **`9f0db4746fd9fba08adbe5580811bab946b6f069`**；客户端核对 MR !1170 `d8b36a01235574cfda750e9209a8ddada3354fe3`。此处不声明 PR 已合入 main。SDK 声明为 `agent-client-protocol==0.12.1`，ACP `protocolVersion` 为整数 `1`。

本文覆盖当前 Box-Agent 实际注册/调用的协议入口、回包、通知、元数据及内置工具结构化输出。使用 Git 中的固定 PR 提交内容，不含当前工作区未提交的切云实现。参数表以代码消费为准；SDK 接受但业务没有消费的字段另行标注。

后续 PR #254 新增 `session/resume`，其能力声明、无回放恢复及 Lab 按需对账合同见[ACP SDK 对接文档](integration/acp-sdk-0.12.md)。下表仍保留上述 PR #250 固定版本，不把新入口追写为旧版本已有能力。

配套材料：

- [源码字段及载荷核对附录](ACP_PROTOCOL_SOURCE_APPENDIX_CN.md)：精确默认值、兼容别名、嵌套结构、静态载荷结构示例及动态输出写入点。
- 原交接合同及交付差异为本地讨论材料；本文已独立列出当前合同与剩余差异，不依赖未提交文件。
- [固定版本 ACP 入口](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/acp/__init__.py)、[当前结构化事实 Schema](https://github.com/Raccoon-Office/Box-Agent/blob/9f0db4746fd9fba08adbe5580811bab946b6f069/box_agent/input_context.py)。

## 1. 阅读约定与覆盖边界

**上行 U：客户端/执行宿主 → Box-Agent；下行 D：Box-Agent → 客户端/执行宿主。** 请求的响应方向与请求相反。反向 RPC 的响应也是上行，不是新的 method。

- `?` 表示业务可选；“保留当前”表示本次无已定参数修改，不等于所有长期治理已经完成。
- 表内 JSON 通常展示 `params`、`result` 或 `update/rawOutput`，列标题标明位置；不是每个例子都包含 JSON-RPC 外壳。
- 示例值为虚构。路径必须替换成执行端真实授权路径；资源示例不代表云端 URI 已接通。
- `rawInput` 是实际工具参数，`rawOutput` 是开放结果，内置工具、动态 MCP、Skill/plugin 可以产生任意业务 JSON。本文枚举固定提交内已知载荷和通用开放合同，不把动态结果误称为有限枚举。
- 源码中 Python 的 `session_id`、`field_meta`、`raw_output` 等 SDK 属性，在线上分别为 `sessionId`、`_meta`、`rawOutput`；业务字典里的 snake_case 不自动转换。
- “应使用”分为 **已约定待实现** 与 **后续治理、尚无新 wire 合同**。后者不能直接让客户端发送臆造字段。

### 1.1 JSON-RPC 外壳

| 方向 | 功能 | 协议参数 | 示例 |
|---|---|---|---|
| U 或 D | 请求 | `jsonrpc:"2.0", id:string/number, method:string, params:object` | `{"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"/workspace","mcpServers":[]}}` |
| 与请求相反 | 成功响应 | 相同 `id`；`result`；不含 method | `{"jsonrpc":"2.0","id":1,"result":{"sessionId":"acp-v1-Y2hhdC1h"}}` |
| 与请求相反 | JSON-RPC 错误 | 相同 `id`；`error:{code,message,data?}` | `{"jsonrpc":"2.0","id":2,"error":{"code":-32010,"message":"Session busy","data":{"code":"SESSION_BUSY","sessionId":"acp-v1-Y2hhdC1h"}}}` |
| U 或 D | 通知 | 无 `id`，不回响应 | `{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"acp-v1-Y2hhdC1h"}}` |

主进程为 stdio NDJSON，每条消息一行；stdout 只承载协议，日志走 stderr。输入帧上限 32 MiB，Base64 开销计算在内。当前并非原生 WebSocket 服务；云端 transport 包装与 ACP JSON-RPC 本体分开。

私有方法在线上只加一个 `_`：代码 `ext_method("inject", ...)` 对应 `_inject`；`ext_method("session/runtime_environment", ...)` 对应 `_session/runtime_environment`。

## 2. 标准上行入口及下行回包

| 方向 | 方法 / 功能 | 当前协议参数 | 当前结果 / 示例 | 应使用参数 |
|---|---|---|---|---|
| U→D | `initialize` 初始化 | `protocolVersion:int`，`clientCapabilities?`，`clientInfo?:{name,version,title?}`，`_meta?` 见第 3 节 | 请求 `{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"raccoon-lab","version":"1.0.0"}}`；结果含 `protocolVersion:1,agentInfo,agentCapabilities,_meta.skills?` | 保留当前。不要把 SDK 版本 `0.12.1` 放进 protocolVersion |
| U→D | `session/new` 创建或旧式重绑 | `cwd:string,mcpServers:array,additionalDirectories?:string[],_meta?` | `{"cwd":"/workspace/project","mcpServers":[],"_meta":{"session_id":"chat-a"}}` → `{"sessionId":"acp-v1-Y2hhdC1h","_meta":{"history_revision_id":"initial","capabilities":{"session_continuation_versions":[1],"history_revision_versions":[1],"managed_mcp_config_versions":[1]}}}` | 新建保留；重启后恢复使用已实现的 `session/load`，见2.2 |
| U→D | `session/prompt` 执行一轮 | `sessionId,prompt:ContentBlock[],_meta?` | `{"sessionId":"acp-v1-Y2hhdC1h","prompt":[{"type":"text","text":"你好"}],"_meta":{"turn_id":"t-1"}}`；最终回包见第 5 节 | 共享有序 text/resource_link/image 输入与持久化已实现，见2.3 |
| U→D | `session/load` 恢复既有会话 | `sessionId,cwd,mcpServers,additionalDirectories?,_meta?`；meta 不重复传 session_id | `{"sessionId":"acp-v1-Y2hhdC1h","cwd":"/workspace/project","mcpServers":[]}` → `{"_meta":{"replayed":true}}`（结果可另含 new 的元数据） | 所有回放先于成功响应；失败不降级 new，详见2.2 |
| U 通知 | `session/cancel` 取消 | `sessionId`；额外 meta 当前未形成业务控制合同 | `{"sessionId":"acp-v1-Y2hhdC1h"}`；无独立响应，等待原 prompt 终结 | 保留当前，不把通知发出当作取消完成 |

初始化明确 `loadSession:true`、`promptCapabilities.image:true`。`mcpServers` 和 `additionalDirectories` 进入 SDK 请求对象，但当前会话装配并未据此完成标准会话级 MCP 或额外目录授权；实际使用既有 MCP 配置和权限策略。不能据 SDK 参数存在推断能力已实现。

`session/new` 的 `_meta` 结果还可能包含 `skills`、`expert_context`、`goal`；按宿主能力加入 `skillhub_search_versions:[1]`、`skillhub_install_versions:[1]`、`runtime_environment_versions:[1]`。skills 为元数据列表，实际项由技能加载器产生，不是工具 Schema 列表。

**当前特殊行为：** 对未知 `sessionId` 的 prompt，代码仍有自动创建会话兼容路径；相同产品 `_meta.session_id` 的 new 会重绑，忙时拒绝。load 已拒绝未知会话；prompt 的自动 new 是仍待修改的独立兼容行为。

### 2.1 prompt 内容块

| 方向 / 功能 | 当前字段和限制 | 示例块 | 应使用参数 |
|---|---|---|---|
| U / 用户文字 | `type:"text",text:string`；有序块保存在 input_facts；纯文字视图以换行连接 text | `{"type":"text","text":"分析报表"}` | 保留 text 原文；统一有序块持久化，不将派生资料写回原文 |
| U / 已准备文件 | `type:"resource_link",uri,name,mimeType?,title?,size?`；只读取当前工作区可读普通文件；最多 520 links；大小以实际 stat 为准 | `{"type":"resource_link","uri":"file:///workspace/sales.csv","name":"销售.csv","mimeType":"text/csv","size":128}` | 保留标准 link；共享层生成 SHA-256 版本；云资源联调暂缓 |
| U / 内联图片 | `type:"image",data:Base64,mimeType,uri?`；当前转换读取 data 与 MIME，不能将 uri-only 当作已支持 | `{"type":"image","mimeType":"image/png","data":"<替换为有效PNG的Base64>"}` | 保留标准 image，共享资源准备已实现；示例 data 是说明占位，不能直接发送 |
| U / 旧图片路径 | `_meta.image_attachment_paths`，别名 `imageAttachmentPaths`，最多取 6 条 | `{"image_attachment_paths":["/workspace/a.png"]}` | 新生产者迁移到 image 或图片 resource_link，避免重复发送；图片 link 已复用共享视觉准备，Lab 每个图片附件仅发送一个 link |
| U / 其他 SDK 内容 | `resource`、`audio` 等不是本实现已完成的业务输入 | 不提供可执行示例 | 本期不把它们列为支持能力，后续按需接入 |

文件拒绝远程 authority、非 file scheme、query/fragment、非法编码、父目录、工作区外路径、符号链接逃逸、硬链接及非普通文件；检查 PermissionEngine。URI/name 长度上限分别 16384/1024，title≤1024，MIME≤256。资源不是上传指令或额外授权。原始跨类型块顺序保存在 input_facts.blocks；模型投影是派生视图，不承诺与原始块一一同形。

### 2.2 稳定会话身份与 load 消费规则

`session/new` 返回 `acp-v1-` 加产品会话 ID 的 UTF-8 base64url 编码（去除 `=`）。未提供产品 ID 时先生成 UUID，再编码。客户端保存返回 ID 与产品 ID、工作区的绑定；旧 `sess-*` 不能直接 load，Lab 从既有产品身份派生稳定 ID 后通过 load 核验。不要把产品 ID 当作 ACP sessionId。

load 只打开已有日志，不创建缺失日志，也不以空日志替换损坏历史。公共读取器仍会截掉末尾无换行的残余记录；完整换行的损坏记录按错误处理。请求中的 `_meta` 使用既有会话配置；SDK 会展开 `_meta`，不得在其中重复 `session_id` 覆盖顶层 sessionId。回放使用有效历史的 user/assistant/tool 消息；不运行模型、工具或上传。图片引用读既有图片存储，缺失时显示 `[Image unavailable in saved history]`；图片预处理的工具回执也从已保存输入事实回放。

| 方向 / 功能 | 参数 | 示例 / 消费规则 |
|---|---|---|
| D / 回放身份 | `update._meta.replay:true,eventId:string,turnId:string或null` | `eventId:"7:0"` 来自日志序号及该消息更新索引，只在对应会话回放中去重；旧历史可能缺 turnId |
| D / 回放轮次状态 | `turnStatus:complete/cancelled/error/interrupted` | completed/aborted/error 结束原因分别映射前三种；无对应结束记录默认 interrupted |
| D / 已提交补充 | `inputKind:"user_supplement",inputId` | `{"inputKind":"user_supplement","inputId":"i-1"}`；对应 user_message_chunk，不是新发起输入 |
| D / 完成 | load result 的 `_meta.replayed:true` | 全部更新发送后返回，不新建 sessionId |
| D / 非法或未知句柄 | JSON-RPC `-32602,UNKNOWN_SESSION_ID`；日志不存在为 `-32012` | 非规范编码、长度超过4096、空身份拒绝；日志缺失 error.data.code 为 UNKNOWN_SESSION_ID |
| D / 日志不可加载 | `-32012`，data.code 为 `SESSION_WORKSPACE_MISMATCH/SESSION_LOG_CORRUPTED/SESSION_BUSY` | 不回退新建；活跃会话重绑的既有 busy guard 仍可返回 `-32010` |

Lab 在本次 load 内缓冲、按 eventId 去重，成功后以事务提交助手历史视图和绑定；保留原生用户附件、只读展示工具卡片。无法对应历史 turnId 时回滚，保留 SQLite 原历史。回放不得经过实时宿主动作执行链路；回放失败后不自动开始新轮次。

```json
{"jsonrpc":"2.0","id":101,"method":"session/load","params":{"sessionId":"acp-v1-Y2hhdC1h","cwd":"/workspace/project","mcpServers":[]}}
```

### 2.3 输入限制、持久事实与资源版本

普通 prompt 与结构化补充统一校验 `text/resource_link/image`，其他块返回 `INVALID_INPUT_BLOCK`。最多1040块、520个文件 link；内联图片与按 MIME 识别的图片文件合计最多6张。image 要求 data 为合法 Base64、mimeType 以 image/ 开头，解码后非空且单张≤20 MiB，并通过图片尺寸解析；32 MiB 总输入帧约束同时适用。旧 image_attachment_paths 是兼容路径，不可依赖它绕过新标准块的限制。

Session Log 的有效 user/message 同时保存原文字段和 `input_facts`：schemaVersion、id、fingerprint、有序 blocks、context、workspace、policy、resourceImages，以及准备后的图片引用/分析回执。内联图片块保存摘要和 imageIndex，图片字节走既有图片存储。`input/context` 仅诊断；恢复不依赖这条诊断记录。语言、资料指导、连接器和图片分析提示在模型投影时派生，不改写用户原文。这里的内部字段不是客户端应发送的新 ACP 参数。

文件事实生成 `version:{algorithm:"sha256",digest}`，准备候选及提交请求前复核；有效响应前重试继续核验原版本。变化返回 `INPUT_RESOURCE_VERSION_CHANGED`，无法读取或验证返回 `INPUT_RESOURCE_VERSION_UNAVAILABLE`。这不是整个工作区的不可变快照；有效响应后的普通工具访问仍遵循共享工作区语义。候选 `request/context.inputContext.phase:candidate` 不等于已采用，提交时 `input/adopted` 才记录采用。

新结构化日志包含不可忽略的 `input/contract`，旧运行包不能读取这种新日志；旧纯文字日志保持可读，不猜拆历史提示词。回滚运行包前必须考虑此持久格式约束。

## 3. 上行 `_meta` 字段台账

入口缩写：I=initialize，N=session/new，P=session/prompt，J=_inject，L=_llm/prompt。session/load 复用会话配置读取，但不在 _meta 传 session_id（见2.2）。私有请求的 `_meta` 是 params 内显式对象。下面是当前业务使用范围，不代表把所有字段塞到所有入口都有效。精确读取位置、别名和默认值见[附录第 1 节](ACP_PROTOCOL_SOURCE_APPENDIX_CN.md)。

| 方向 / 入口 | 功能 | 当前参数 | 示例 `_meta` 片段 | 应使用参数 / 状态 |
|---|---|---|---|---|
| U / I,N,L | 宿主身份 | `host_client_info:{name,platform,version,os_version/osVersion,channel,device_id/deviceId}` | `{"host_client_info":{"name":"raccoon-lab","platform":"windows","version":"1.0.0"}}` | 保留新名；旧 `client_info` 已被替换，标准 `clientInfo` 是另一字段 |
| U / I,N,P,L | 模型绑定 | `llm_binding/llmBinding`：`source:builtin/profile,model,version?,profileId/profile_id?,profileRevision/profile_revision?,routingMode/routing_mode?,maxTokens/max_tokens?,contextWindow/context_window?,autoRouting/auto_routing?` | `{"llm_binding":{"source":"builtin","model":"model-name"}}` | 保留；profile 要求 version=2、profileId、profileRevision 和 routingMode:auto/manual。不得改用未实现的标准 set_config_option |
| U / N | 产品会话身份 | `session_id:string` | `{"session_id":"chat-a"}` | 迁移为稳定 ACP ID 映射及 load；这是日志身份，不是 prompt 的 ACP句柄 |
| U / N,P,L | 展示标题 | `title/session_title/sessionTitle` | `{"title":"季度分析"}` | 保留 |
| U / N,P | 任务身份 | `task_id/taskId` | `{"task_id":"task-a"}` | 保留 |
| U / P,L | 轮次身份 | `turn_id/turnId` | `{"turn_id":"t-1"}` | 保留稳定 ID；重试不得换 ID 掩盖不确定状态 |
| U / N | 会话模式 | `session_mode`、`deep_think:boolean`、`execution_profile:fast/standard/deep` | `{"session_mode":"code_agent","deep_think":true,"execution_profile":"standard"}` | 当前私有配置；值以配置归一化为准，后续再评估标准配置映射 |
| U / N | 轻量会话 | `utility:boolean` | `{"utility":true}` | 保留；与独立 `_llm/prompt` 区分 |
| U / N | 权限模式 | `permission_mode`、`officev3_permissions_override` | `{"permission_mode":"default"}` | 由权限配置校验；不能用事实类 metadata 替代授权 |
| U / N | 文件系统政策 | `filesystem_policy:{session_workspace_root,allowed_directories,filesystem_scope}` | `{"filesystem_policy":{"session_workspace_root":"/workspace","allowed_directories":["/workspace"]}}` | 保留实际策略合同；与 SDK additionalDirectories 不等价 |
| U / N | 环境事实 | `env_context`：cli、platform、browser_tools、browser_connector、hyperframes、image_service、memory_configured、runtimes、obsidian | `{"env_context":{"platform":"windows","cli":{}}}` | 后续完善未提供/清空/替换语义；当前嵌套字段见附录解析器 |
| U / N | 记忆隔离 | `memory:{tenant_id?,user_id?}` | `{"memory":{"tenant_id":"team-a","user_id":"u-a"}}` | 保留，实际后端决定可用性 |
| U / N | 工作区布局 | `workspace_layout/workspaceLayout` | `{"workspace_layout":{}}` | 当前仅按已有解析器处理；废弃产物根字段不授予访问权限 |
| U / N | 专家/专家团队 | `expert`、`expert_team/expertTeam`，profile/team/packageSnapshot/skillBindings 结构见附录 | `{"expert":{"id":"e-a","name":"分析专家","role":"分析"}}` | 保留当前；不能仅靠 name 推断权限 |
| U / N | 宿主反向能力 | `host_capabilities/hostCapabilities`：runtime_environment=1；skillhub_search/skillhubSearch、skillhub_install/skillhubInstall 为1或 `{version:1}` | `{"host_capabilities":{"runtime_environment":1,"skillhub_search":1,"skillhub_install":1}}` | 保留；install 还依赖 search 能力 |
| U / N | 后续问题推荐 | `follow_up_suggestions/followUpSuggestions:boolean` | `{"follow_up_suggestions":true}` | 保留 |
| U / N,P | 强制计划 | `force_plan_start/forcePlanStart:boolean` | `{"forcePlanStart":true}` | 保留显式动作语义 |
| U / N | 计划审批默认 | `require_plan_approval/requirePlanApproval:boolean` | `{"requirePlanApproval":true}` | 保留，后续执行控制单列治理 |
| U / P | 计划审批回传 | `planApproval/plan_approval:{decision,request_id?,plan_id?,source?}`；兼容 planApprovalDecision/plan_approval_decision、planApprovalRequestId/plan_approval_request_id | `{"planApproval":{"decision":"approved","request_id":"p-1","plan_id":"1"}}` | 当前仍随 prompt；独立执行控制的新 wire 方法尚未定，不应臆造 |
| U / P | 自动审批开关 | `auto_approve_plan/autoApprovePlan/skip_plan_approval/skipPlanApproval` | `{"autoApprovePlan":false}` | 保留已有语义；不能拿“继续”文本代替权限回包 |
| U / N,P | Goal | `goal:string/object`，兼容 goal_objective/goalObjective；对象与 `_goal` 字段相同 | `{"goal":{"action":"set","objective":"完成分析"}}` | 保留 |
| U / N,P | 连接器选择 | `selected_connector_ids/selectedConnectorIds:string[]` | `{"selected_connector_ids":["drive"]}` | 保留结构化事实 |
| U / N,P | 连接器快照 | `connector_statuses/connectorStatuses:[{id,name,status:connected/disconnected}]`，最多256 | `{"connector_statuses":[{"id":"drive","name":"Drive","status":"connected"}]}` | 保留；权限由连接器执行，不由此字段授予 |
| U / N,P | 快照获取失败 | `connector_status_unavailable/connectorStatusUnavailable:boolean` | `{"connector_status_unavailable":true}` | 保留；缺字段与空列表不可混为一谈 |
| U / P | UI 语言 | `ui_language/uiLanguage:zh/en/ja` | `{"uiLanguage":"zh"}` | 事实字段保留；语言指导作为策略事实保存，模型投影与用户原文分离 |
| U / P | 显式技能选择 | `selected_skill_names/selectedSkillNames:string[]`，最多取8 | `{"selected_skill_names":["spreadsheet"]}` | 保留，需实际加载器中的名称 |
| U / P | 用户决策 | `user_decision/userDecision`，字段见3.2 | `{"user_decision":{"request_id":"d-1","decision_kind":"delivery_scope","selected_option_id":"full","trigger":"user"}}` | 当前仍随隐藏 prompt；后续由执行控制维护请求有效性，尚无替代 wire 方法 |
| U / P | 历史编辑 | `history_edit:{target_turn_id,expected_revision_id}`，同时提供新的 turn_id | `{"turn_id":"t-2","history_edit":{"target_turn_id":"t-1","expected_revision_id":"initial"}}` | 保留严格锚点，不用展示顺序猜测 Agent 历史 |
| U / P | 旧续接历史 | `session_continuation/sessionContinuation`，schema_version v1，详见3.3 | `{"session_continuation":{"schema_version":"officev3-session-continuation/v1","product_session_id":"chat-a","messages":[{"role":"user","content":"请分析销售报表。"},{"role":"assistant","content":"已确认按月份汇总。"}],"reason":"restart"}}` | 已有会话恢复应迁移 load；新任务交接与历史恢复分开 |
| U / P,J | 输入事实 | `input_context`、`selection_context`、`database_context` | 见3.1 | 字段保留；有效输入事实持久化与共享模型投影已分层 |
| U / L | 轻量用途 | `purpose`，params.purpose 为兼容来源；session_id、turn_id、title、host_client_info、llm_binding 可辅助关联 | `{"purpose":"title_generate","session_id":"chat-a"}` | 保留 |
| U / P（目标） | 切云信号 | **固定提交尚未消费 cloudHandoff** | 目标 `{"cloudHandoff":true}` | 独立工作包待合入；不能按固定提交已支持使用 |

### 3.1 结构化事实的嵌套参数

全部位于 `_meta`；三个对象均 `schemaVersion:1` 且必须是真正整数。未知字段拒绝。文件由标准内容块进入，调用方不能任意伪造服务器归一化后的 files。

| 方向 / 功能 | 当前参数 | 示例 | 应使用参数 |
|---|---|---|---|
| U / 原文及资料 | `input_context:{schemaVersion,userRequest,appshot?:{text},referencedConversations?:{text,sources?:[{sessionId,title}]},teamTask?:{text}}` | `{"input_context":{"schemaVersion":1,"userRequest":"总结","teamTask":{"text":"目标与要求"}}}` | 保留字段；userRequest 是原始意图，不能把所有辅助资料并回原文 |
| U / Appshot | `appshot.text` 非空、≤200000字符 | `{"input_context":{"schemaVersion":1,"userRequest":"分析窗口","appshot":{"text":"窗口辅助信息"}}}` | 与图片关联的共享资源语义待统一 |
| U / 引用会话 | text非空≤200000；sources≤3；sessionId≤512，title≤1024 | `{"input_context":{"schemaVersion":1,"userRequest":"对比","referencedConversations":{"text":"历史摘录","sources":[{"sessionId":"chat-b","title":"调研"}]}}}` | 保留资料来源，不当作当前会话执行历史 |
| U / TeamTask | text非空；无单段200000上限，仍受输入帧/模型预算约束 | `{"input_context":{"schemaVersion":1,"userRequest":"执行任务","teamTask":{"text":"完整任务快照"}}}` | 保留；这是 PR249 对此前问题的修复 |
| U / 文档选区 | `selection_context:{schemaVersion,kind:"document_selection",scope:"selection",source:{fileName,relativePath},preview:{strategy:regenerate_cached/direct_pdf},selection:{format:"pdf",pageStart,pageEnd,text}}` | `{"selection_context":{"schemaVersion":1,"kind":"document_selection","scope":"selection","source":{"fileName":"a.docx","relativePath":"a.docx"},"preview":{"strategy":"regenerate_cached"},"selection":{"format":"pdf","pageStart":1,"pageEnd":1,"text":"选区"}}}` | 保留；页码≥1且有序，text≤4000；源文件与预览PDF不能混淆 |
| U / 数据库范围 | `database_context:{schemaVersion,connections:[{connection,allowedTables:string[]}],truncated:boolean}`；≤8连接、每连接≤16表 | `{"database_context":{"schemaVersion":1,"connections":[{"connection":"db-a","allowedTables":["sales"]}],"truncated":false}}` | 保留；仅选择提示，不是SQL授权 |

### 3.2 用户决策与计划审批

用户决策归一化读取 `request_id/requestId`、`decision_kind/decisionKind`、`selected_option_id/selectedOptionId`、`selected_option_label/selectedOptionLabel`、`custom_text/customText`、`trigger`。request_id 和 decision_kind 必须非空，选项 ID 与自由文字至少一个；ID/类别截到128字符、标签500、自由文字2000。trigger只保留 user/timeout，否则归一user。旧文档示例包含 tool_call_id，但该归一化函数当前不保留它，不能声称它参与请求定位校验。

计划审批认可 approve/approved/accept/accepted/confirm/confirmed/execute/proceed/yes 等决策文本，且存在从简短自然语言和 pending plan 推断审批的兼容路径。该路径与 `session/request_permission` 的严格请求应答是两套机制，不可混用。

history_edit当前对象只允许 target_turn_id、expected_revision_id 两个键，不能增加version；历史能力版本通过new响应capabilities声明。

### 3.3 历史续接

v1字段：`schema_version,product_session_id,reason,messages:[{role:user/assistant,content,image_refs?}],truncated?,source_task_id?,target_task_id?,source_session_id?`。仅在首次轮次且产品会话匹配时导入。解析器按下列顺序归一化，不能把这些限制理解为统一拒绝超限请求：

1. 仅检查原始数组的前12项，超出的项不处理，并设置 `truncated:true`。前12项中的无效项不会由后续项补位。
2. 跳过非对象、非user/assistant角色、非字符串content，以及去除首尾空白后为空的消息。没有有效消息时返回 `None`，按没有续接内容处理；`messages:[]` 不是恢复成功示例。
3. 每条内容截到12000字符，同时受48000字符总预算约束；发生截断或预算耗尽时设置 `truncated:true`，不是返回“超限”错误。
4. 相邻同角色且双方均无图片引用的有效消息，用两个换行合并；合并后仍截到12000字符。含图片引用的消息不走这个合并分支。

以下为可解析出两条有效历史的 `_meta` 示例。实际导入还要求当前产品会话为chat-a且满足首轮条件：

```json
{"session_continuation":{"schema_version":"officev3-session-continuation/v1","product_session_id":"chat-a","reason":"restart","messages":[{"role":"user","content":"请分析销售报表。"},{"role":"assistant","content":"已确认按月份汇总。"}]}}
```

归一化示例：`[{"role":"user","content":"  "},{"role":"user","content":"甲"},{"role":"user","content":"乙"}]` 得到一条user消息，正文为 `甲\n\n乙`。图片引用另有严格校验，不能把文本过滤行为推广为所有错误都会静默跳过。

image_refs 的字段：`imageId` 必填，`contentRef,sha256,media_type,messageSeq` 可选；含图片必须提供 source_session_id；图片来源及资源必须通过校验。v2 当前明确拒绝 `HISTORY_EDIT_UNSUPPORTED_CONTINUATION`。标准恢复流程见2.2，不能把 v1 快照导入当作标准 load 回放。

## 4. 全部上行私有请求（18个）

下列每行方向均为 **U 请求 → D result**。result里的 `error`/`success:false` 是应用结果，不一定是 JSON-RPC error。示例为 params → result，条件字段可省略。

| 方法 / 功能 | 当前协议参数 | 示例 | 应使用参数 |
|---|---|---|---|
| `_request_wrap_up` 请求收尾 | `sessionId,turnId?,remainingSeconds:int(1..10800)` | `{"sessionId":"s","remainingSeconds":60}` → `{"ok":true}`；重复可有 deduplicated:true | 保留；要求活跃且未取消，不等于强制成功完成 |
| `_inject` 运行中补充 | `sessionId,injectionId,prompt:ContentBlock[],_meta?`；旧 `text + resources` 兼容；按字段存在性要求 text/prompt 恰好一个，prompt 禁止同时带 resources | `{"sessionId":"s","injectionId":"i-1","prompt":[{"type":"text","text":"也检查退款"}]}` → `{"ok":true,"injectionId":"i-1","status":"pending"}` | 结构化请求必传稳定非空 injectionId；接收不等于生效，详见4.1 |
| `_cancel_inject` 撤回未提交补充 | `sessionId,injectionId` | `{"sessionId":"s","injectionId":"i-1"}` → `{"ok":true}`；pending/taken 可取消；其他状态为false | 保留请求；false不等于已生效，需结合有效提交 |
| `_list_skills` 技能元数据 | 无必需参数 | `{}` → `{"skills":[]}` | 保留 |
| `_goal` Goal控制 | `sessionId,action?:get/set/pause/resume/complete/clear/progress/block,objective?,status?,evidence?,progress?,blockedReason/blocked_reason?,completedBy/completed_by?` | `{"sessionId":"s","action":"set","objective":"完成报告"}` → `{"ok":true,"goal":{"objective":"完成报告","status":"active"}}`（goal其余字段见下文） | 保留；block需原因；get默认 |
| `_memory_proposal_list` 记忆提案列表 | `sessionId?`（空用服务级）、`includeCooldown?,includePlan?,_meta?` | `{"sessionId":"s","includePlan":true}` → `{"candidates":[]}` | 保留 |
| `_memory_proposal_apply` 应用记忆提案 | 旧：`sessionId?,decisions:{id:pin/skip/reject}`；计划：`sessionId?,plan,decision:apply/reject/skip` | `{"sessionId":"s","decisions":{"m1":"pin"}}` → `{"pinned":1,"rejected":0,"skipped":0,"core":"记忆正文"}` | 保留两种现合同，不混用 |
| `_llm/prompt` 无工具单次生成 | `prompt:string,systemPrompt?,timeoutMs?,purpose?,workspaceLabel?,_meta?` | `{"prompt":"生成标题","purpose":"title_generate","timeoutMs":30000}` → `{"text":"分析报告","finishReason":"stop","usage":{"inputTokens":10,"outputTokens":4},"durationMs":100}` | 保留；失败为 `error:{code,message}` |
| `_workspace/list` 工作区列表 | `{}` | `{}` → `{"workspaces":[],"configPath":"/config/workspaces.json"}` | 保留 |
| `_workspace/get` 工作区查询 | `path:string` | `{"path":"/workspace"}` → `{"workspace":null,"configPath":"/config/workspaces.json"}` | 保留；配置查询，不切换ACP会话 cwd |
| `_workspace/set` 工作区分类 | `path:非空string,taskType/task_type:general或code（必填）` | `{"path":"/workspace","taskType":"code"}` → `{"workspace":{"path":"/workspace","task_type":"code","updated_at":"2026-11-06T00:00:00+00:00"},"configPath":"/config/workspaces.json"}`（实际profile附加字段由registry返回） | 保留 |
| `_mcp/status` MCP状态 | `{}` | `{}` → `{"servers":[],"loading":false,"configPath":"/config/mcp.json","configPaths":[]}`；browser可选 | 保留；这是ACP扩展，不是向MCP服务器直接发请求 |
| `_mcp/credential/set` 设置运行时凭据 | `credentialRef,headers:object,target?:{owner,name}` | `{"credentialRef":"ref-a","headers":{"Authorization":"Bearer <redacted>"}}` → `{"success":true,"affectedServers":["srv"]}` | 保留；示例凭据不真实；目标和owner按loader校验 |
| `_mcp/credential/clear` 清除凭据 | `credentialRef,target?` | `{"credentialRef":"ref-a"}` → `{"success":true,"affectedServers":["srv"]}` | 保留 |
| `_mcp/reconcile` 同步配置源 | `source?:string` | `{}` → `{"success":true,"results":[]}`（结果附加字段由loader返回） | 保留 |
| `_mcp/source/replace` 替换宿主连接器源 | `source:"connector",config:{mcpServers:{name:serverConfig}},connectorIds?:string[]`；提供ID时非空 | `{"source":"connector","config":{"mcpServers":{}},"connectorIds":["drive"]}` → `{"success":true,"results":[]}` | 保留；空配置可表达范围内移除，不是增量追加 |
| `_mcp/reconnect` 重连服务器 | `name:string` | `{"name":"srv"}` → `{"success":true}`（带loader诊断字段） | 保留 |
| `_mcp/disconnect` 断开服务器 | `name:string` | `{"name":"srv"}` → `{"success":true,"removedTools":[]}` | 保留 |

### 4.1 工作区分类的有效值与失败结果

`_workspace/set` 的分类只接受精确字符串 `general` 或 `code`，不自动转小写或去除分类值的空白。取值表达式为 `params.get("taskType") or params.get("task_type")`：非空taskType优先，其为空或缺失时才取task_type。两者均未提供，或最终分类为其他值时，返回应用层error；外层仍是JSON-RPC result。

| 方向 | 功能 | params示例 | result |
|---|---|---|---|
| U→D | 缺少分类 | `{"path":"/workspace"}` | `{"error":"task_type must be 'general' or 'code'"}` |
| U→D | 非法分类 | `{"path":"/workspace","taskType":"analysis"}` | `{"error":"task_type must be 'general' or 'code'"}` |
| U→D | 空路径 | `{"path":"","taskType":"general"}` | `{"error":"path is required"}` |

合法请求可使用 `{"path":"/workspace","taskType":"general"}` 或 `{"path":"/workspace","task_type":"code"}`。成功结果字段为 `workspace.path/task_type/updated_at` 与 `configPath`，请求别名不会改变结果字段名。精确校验函数及两端命名见附录第10节。

### 4.2 私有结果的嵌套字段

- Goal：`objective,status,createdAt,updatedAt,evidence,progress,blockedReason,completedBy`。status为 active/paused/blocked/complete。`_goal` errors包括 session_not_found、goal_not_found、empty_objective、empty_blocked_reason、invalid_status、unknown_action。
- 记忆候选：`id,content,hits,confidence,created,last_used,last_proposed`；计划：`currentCore,newCore,consumedEntryIds,rationale`。计划apply结果 `{applied,consumed,core}`；reject结果 `{rejected,core}`；skip结果 `{skipped,core}`；无本地记忆列表为空、apply可返回 memory_unavailable。
- MCP服务器状态：`name,owner,configId,connectorId,connectorName,sourcePath,state,transport,toolCount,tools,error,authStatus,browser?`。MCP config是既有服务器配置开放对象，可包含command/args/env或url/headers等；凭据不能写入示例/日志。reconcile/replace/reconnect/disconnect的详细结果以loader为准，见附录结果构造点。
- inject只表示入队，重复同内容回 `{ok:true,injectionId,deduplicated:true}`；不同语义内容为 `INJECTION_IDEMPOTENCY_CONFLICT`。无会话/无活跃轮次/空内容及资源错误均明确返回error。

### 4.1 补充输入身份与终态

同 injectionId 的回执查询先于文件准备：相同输入返回 `{ok:true,injectionId,deduplicated:true,status}`；不同块或规范化语义上下文返回 `INJECTION_IDEMPOTENCY_CONFLICT`。ID 不属于指纹内容；传输追踪字段不参与输入语义。结构化请求必须提供非空字符串 injectionId，旧 text 入口可省略并自动生成。纯附件允许，空文字且无非文字块返回 empty_text。

| 方向 / 功能 | 当前参数或状态 | 示例 / 含义 |
|---|---|---|
| D / 初次接收 | `status:"pending"` | `{"ok":true,"injectionId":"i-1","status":"pending"}`；只接受入队 |
| D / 重试查询 | `pending/taken/injected/cancelled/discarded` | taken 表示已取出准备，仍不等于提交；deduplicated:true 不等于生效 |
| D / 实时成功 | `[Injected:i-1] ...` 文本标记 | 有效输入提交后发出；尚未统一为 inputStatus 通知 |
| D / 取消或丢弃 | `update._meta.inputStatus:{injectionId,status}` | `{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":""},"_meta":{"inputStatus":{"injectionId":"i-1","status":"cancelled"}}}`；先读 meta，不能因空正文丢弃通知 |
| U→D / 取消 | `_cancel_inject {sessionId,injectionId}` → `{ok:boolean}` | pending/taken 可取消；false 不能据此判断已提交。轮次结束仍可能重复发送终态回执，客户端按补充身份更新状态 |
| D / 参数失败 | `result.error` | `INVALID_INJECTION_ID`、`INVALID_INJECTION_INPUT:provide_exactly_one_of_text_and_prompt`、`INVALID_INPUT_BLOCKS`，以及共享块/资源校验错误 |

进程重启后只从有效日志确认已提交的 user_supplement；pending/taken/cancelled/discarded 不是跨重启持久回执。Lab 从回放 inputKind/inputId 确认已提交项，其余未确认草稿保留为错误草稿，不自动重发。

**已知限制（9f0db474）：** 图片补充在准备阶段注册进 ImageContext 后被取消，可能没有有效 user/message，却仍被后续模型请求消费。取消回执已实现，但不能宣称取消后图片一定不会进入模型；需将图片关联发布与输入提交绑定或回滚。

## 5. prompt 最终下行结果

方向 **D 响应 U 的 session/prompt**。回答正文通常经 session/update 流式发送，最终 result不是回答全文。

| 功能 | 当前参数 | 示例 / 解释 | 应使用参数 |
|---|---|---|---|
| ACP终止原因 | `stopReason` | `end_turn/cancelled/max_tokens/max_turn_requests/refusal` | 保留标准枚举；不要只凭 end_turn 判定业务成功 |
| 业务结果 | `_meta.ok,error,lastStopReason,runStatus,completed,paused` | `{"ok":true,"error":null,"lastStopReason":"end_turn","runStatus":"completed","completed":true,"paused":false}` | 当前实际口径；等待、失败、取消和预算耗尽要联合判读 |
| 历史版本 | `_meta.history_revision_id` | `"initial"`或修订ID | 保留，编辑下一轮带expected_revision_id |
| 最终展示回执 | `_meta.messageDisplay:{version:1,finalMessageIds:string[],durationMs}` | `{"version":1,"finalMessageIds":["m1"],"durationMs":1000}` | 保留；失败/取消/等待/预算耗尽不据此隐藏部分回答 |
| 用量关联 | `_meta.usage:{totalTokens,sessionId,session_id,taskId,task_id,turnId,turn_id}` | `{"totalTokens":120,"sessionId":"chat-a","session_id":"chat-a","taskId":"task-a","task_id":"task-a","turnId":"t-1","turn_id":"t-1"}` | 保留当前双命名兼容；后续不能未经迁移删除别名 |
| 收尾 | `_meta.wrapUp?:{requested,goalIncomplete}` | `{"requested":true,"goalIncomplete":true}` | 保留 |
| Goal自动续跑 | `_meta.goalAutopilot?:{enabled,continuations,budgetExhausted,noProgressExhausted,noProgressTurns,lastStopReason}` | `{"enabled":true,"continuations":1,"budgetExhausted":false,"noProgressExhausted":false,"noProgressTurns":0,"lastStopReason":"end_turn"}` | 保留 |
| 错误细节 | `_meta.taskRegistryError?,errorCode?,errorCategory?,errorDetails?` | `{"ok":false,"error":"执行失败","runStatus":"error"}` | 保留；通用错误统一属后续治理 |
| 切云回执（目标） | 固定提交无此字段 | 目标 `_meta.cloudHandoff:true` | 待独立功能合入；有效指令与依赖JSON才可触发客户端后续流程 |

注意当前 `waiting_for_user`、内部error可能映射为标准 `end_turn`，必须读取 `_meta`；不能把 `completed` 单字段当作取消/预算完成证明。模型单次finish_reason也不是最外层轮次终态。

## 6. 标准下行通知：session/update

统一外壳：

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"结果"},"_meta":{"messageDisplay":{"version":1,"messageId":"m1","outputId":"o1"}}}}}
```

| 方向 | 功能 / sessionUpdate | 当前协议参数 | 示例 update | 应使用参数 |
|---|---|---|---|---|
| D通知 | 助手正文 `agent_message_chunk` | `content:TextContent,_meta.messageDisplay?` | `{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"结果"}}` | 同一outputId的片段按到达顺序追加；仅对快照与流式正文的重复展示做对齐，见6.1 |
| D通知 | 思考 `agent_thought_chunk` | `content:TextContent` | `{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"正在分析"}}` | 保留，不能当最终回答 |
| D通知 | 工具开始 `tool_call` | `toolCallId,title,kind,status?,rawInput,_meta?`；常用kind=execute | `{"sessionUpdate":"tool_call","toolCallId":"c1","title":"read_file","kind":"execute","rawInput":{"path":"a.txt"},"_meta":{"tool_name":"read_file","task_id":"task-a","turn_id":"t-1"}}` | 保留，工具参数随工具Schema变化 |
| D通知 | 工具/事件更新 `tool_call_update` | `toolCallId,status?,content?,rawOutput?`，字段允许部分更新 | `{"sessionUpdate":"tool_call_update","toolCallId":"c1","status":"completed","content":[{"type":"content","content":{"type":"text","text":"[OK] 内容"}}],"rawOutput":"[OK] 内容"}` | 保留；rawOutput按第7节分派，不能按title猜类型 |
| D通知 | load 历史回放 | `user_message_chunk/agent_message_chunk/tool_call/tool_call_update`；update._meta 见2.2 | `{"sessionUpdate":"user_message_chunk","content":{"type":"text","text":"原提问"},"_meta":{"replay":true,"eventId":"7:0","turnId":"t1","turnStatus":"complete"}}` | 缓冲后提交；不触发实时动作，不混用流式正文去重规则 |

有些结构化更新使用合成 toolCallId，并没有对应真实工具开始事件，例如 llm-output、agent-progress、turn-usage。客户端应允许独立更新；工具 `user_visible=false` 不直接展示，但可参与用量/技能事件。InjectedMessageEvent不是新的标准上行消息，实际展示由下面的文本marker承担。

### 6.1 正文片段、完整快照与补写的消费规则

`messageId` 标识逻辑回答，`outputId` 标识单次模型输出，**它们都不是单个流式片段的唯一ID**。同一输出的所有正文片段共享这两个ID。

- 同一outputId的 `agent_message_chunk` 按接收顺序追加，即使两个片段正文相同，也不能按ID或正文去重。当前字段不足以识别传输层重复投递的单个delta；需要重放去重时，应由transport另行提供可验证身份，不能拿outputId代替。
- `llm_output.content` 是对应outputId的完整快照。已经消费该输出的流式正文时，不把完整快照再追加一次；未收到该输出的delta时，可用完整快照补回一次。重复快照不重复展示。如宿主要处理“只收到部分delta”，可用快照核对/替换**该outputId对应的片段范围**，不能覆盖整个messageId下其他输出，也不能直接追加全文。
- 显式文字补写（TEXT_CONTINUATION）可保留messageId，但会创建新的outputId。新输出的delta或无delta时的快照继续接到同一逻辑回答之后，不自动添加换行；补写可能从词、表格行或代码围栏中间开始。
- 普通任务续跑、恢复或用户补充不因此复用旧messageId。身份由服务端返回值决定，不能根据文本相似度合并。
- 最外层prompt的messageDisplay回执确认哪些messageId属于最终回答；单个快照或模型finish_reason不能提前完成分类。元数据缺失时保留内容并走兼容展示，不因缺ID丢弃正文。

下面是消费语义样例。ID为示意值，实际由运行时生成；“快照”指 `llm_output.content`：

| 顺序 | 下行内容 | messageId / outputId | 累积展示 |
|---|---|---|---|
| 1 | delta：`你` | m1 / o1 | `你` |
| 2 | delta：`好` | m1 / o1 | `你好`，不能因ID相同丢弃本片段 |
| 3 | 快照：`你好` | m1 / o1 | 仍为 `你好`，不重复追加快照 |
| 4 | 文字补写delta：`世界` | m1 / o2 | `你好世界` |
| 5 | 快照：`世界` | m1 / o2 | 仍为 `你好世界` |
| 6 | 重复快照：`世界` | m1 / o2 | 仍为 `你好世界` |

另一条仅收到快照的输出 `m2/o3 → "独立回答"` 应展示一次。相同outputId依次收到两个 `哈` delta应展示 `哈哈`。完整服务端状态逻辑见附录第10节的 `box_agent/acp/message_display.py`。

## 7. 下行结构化载荷清单

除另有说明，方向为 **D，位于 session/update.update.rawOutput**。表中参数为业务Schema，完整动态表达式和各构造分支见[附录第3、4节](ACP_PROTOCOL_SOURCE_APPENDIX_CN.md)。示例为形状示意，不能据示例中空数组推断实际允许缺少必填内容。

### 7.1 Agent与宿主展示事件

| 功能 / type | 当前参数 | 示例 rawOutput | 应使用参数 |
|---|---|---|---|
| 主循环进度 `agent_progress` | step,content | `{"type":"agent_progress","step":1,"content":"检查资料"}` | 保留 |
| 模型输出快照 `llm_output` | messageDisplay,step,content,thinking,tool_calls,finish_reason,usage,provider_request_id | `{"type":"llm_output","step":1,"content":"结果","thinking":"","tool_calls":[],"finish_reason":"stop","usage":{},"provider_request_id":null,"messageDisplay":{"version":1,"messageId":"m1","outputId":"o1"}}` | 按outputId对齐完整快照与已接收正文，不将快照再追加一遍；补写规则见6.1 |
| 模型活动 `agent_activity_v1` | type,step,protocol:agent_activity_v1,phase:provider_stream/tool_arguments；tool_arguments另含tool_name,argument_chars | `{"type":"agent_activity_v1","step":1,"protocol":"agent_activity_v1","phase":"provider_stream"}` | 保留开放活动字段，不虚构固定枚举 |
| 轮次用量 `turn_usage` | **version:3**；sessionId/session_id,acpSessionId,taskId/task_id,turnId/turn_id,skills,skillInvocations,tools,mcp,tokenUsage,current? | `{"type":"turn_usage","version":3,"sessionId":"chat-a","session_id":"chat-a","acpSessionId":"s","taskId":"task-a","task_id":"task-a","turnId":"t-1","turn_id":"t-1","skills":[],"skillInvocations":[],"tools":[],"mcp":[],"tokenUsage":{"promptTokens":10,"completionTokens":5,"totalTokens":15,"calls":1}}` | 保留v3；旧文档v1示例不是当前完整合同 |
| 技能使用 `skills_usage` | current,skills；显式技能分支附activationSource:explicit | `{"type":"skills_usage","current":"spreadsheet","skills":["spreadsheet"]}` | 保留，完整字段见附录；不是技能安装请求 |
| 推荐追问 `follow_up_suggestions` | suggestions:string[] | `{"type":"follow_up_suggestions","suggestions":["按月份比较"]}` | 保留；可能异步晚于prompt回包，不当执行命令 |
| 计划 `plan_snapshot` | version,action?,plan,summary，preview?/omitted_steps? | `{"type":"plan_snapshot","version":1,"action":"clear","plan":null,"summary":{"steps":0,"verification":0,"risks":0,"assumptions":0}}` | 保留；计划面板不是todo进度 |
| 待办 `todo_snapshot` | action?,items,summary,item?,transition? | `{"type":"todo_snapshot","action":"set","items":[],"summary":{"total":0,"completed":0,"in_progress":0,"pending":0}}` | 保留完整快照替换语义 |
| 目标 `goal_snapshot` | action,goal | `{"type":"goal_snapshot","action":"clear","goal":null}` | 保留 |
| 专家协作 `expert_team_progress` | 团队身份、阶段/成员进度等，由experts构造 | `{"type":"expert_team_progress"}`（字段结构见附录示例，不是最小合法业务结果） | 保留，不能识别成普通sub_agent |
| 用户缺信息 `user_input_request` | version:1,status:waiting,question,missingFields,reason,resumeBehavior | `{"type":"user_input_request","version":1,"status":"waiting","question":"统计哪一年？","missingFields":["year"],"reason":"缺少范围","resumeBehavior":"continue_existing_task"}` | 保留；用户回复继续当前会话，不新增伪工具回包 |
| 用户选择 `user_decision_request` | schemaVersion:1,requestId,status,question,decisionKind,options,defaultOptionId?,autoSubmit,resumeBehavior | `{"type":"user_decision_request","schemaVersion":1,"requestId":"d-1","status":"waiting","question":"交付范围？","decisionKind":"delivery_scope","options":[{"id":"full","label":"完整"},{"id":"brief","label":"摘要"}],"defaultOptionId":"full","autoSubmit":{"allowed":false},"resumeBehavior":"continue_existing_task"}` | 当前回复走prompt._meta.user_decision；未来执行控制合同待单独定义 |
| 子代理实时进度 `sub_agent_progress` | parent_tool_call_id,sub_agent_id,task_preview,title,event及事件字段 | `{"type":"sub_agent_progress","parent_tool_call_id":"c1","sub_agent_id":"sub1","task_preview":"检查","title":"检查","event":"tool_start","tool_name":"read_file"}` | 保留唯一实时子代理判别字段 |
| 子代理最终诊断 `sub_agent_delegation` | 策略、请求/解析的工具技能、约束、文件、预算、用量和调用次数 | `{"type":"sub_agent_delegation"}`（完整结构见附录） | 保留，属于最终工具结果 |
| 子代理失败 `sub_agent_delegation_error` | code,message,retryable及诊断字段 | `{"type":"sub_agent_delegation_error","code":"<实际错误码>","message":"执行失败","retryable":false}` | 保留，不把诊断当最终助手正文 |
| 图片路径处理 `structured_image_attachment_result` | 包装图片解析结果，见附录构造 | `{"type":"structured_image_attachment_result"}`（完整结构见附录） | 图片准备已迁共享层；标准image仍保留 |

`follow_up_suggestions` 的模型内联格式仍首选同名三反引号围栏。为兼容模型漏写围栏，ACP 也识别独占一行的 `follow_up_suggestions`，后接合法 JSON 对象且 `suggestions` 为数组；支持 LF/CRLF、空行和流式分片。确认后的标签与 JSON 不进入可见正文，建议沿用上表的结构化事件发送，最多保留 3 条、每条 160 字符。裸标签未匹配合法载荷时保留原文；未完成候选最多缓冲 16,384 字符，超限释放，避免持续阻塞正文。普通句子内提到标签不会被提取。

宿主可将建议展示为空输入框提示，采用建议不等于发送消息，也不要求弹出决策窗口。模型在元数据前写出的引导语（如“后续建议如下”）仍属于正文；已经发出的历史正文不会被补生成建议回写清理。

tokenUsage为promptTokens/completionTokens/totalTokens/calls，缓存报告有效时带cachedTokens。tools项为name/count，mcp项为server/tool/name/count；current可为 `{type:tool,name}`、`{type:mcp,name,server,tool}` 或 `{type:skill,name}`，这些是嵌套判别值，不是新的ACP方法。

sub_agent_progress.event 已知：step_start(step,max_steps)、tool_start(tool_name)、tool_result(tool_name,success,可选file_changes)、artifact(artifact完整对象)、error(message)、agent_progress(step,content)、llm_output(模型快照字段)。其他内部事件可用类名兜底，宿主保留未知详情，不能因未识别而丢失父工具终态。

plan包含id/title/objective/scope/status/steps[{id,title,details}]/verification/risks/assumptions/created_at/updated_at；可能附审批数据。todo项包含id/task/status/priority/created_at等。裁剪preview不是完整方案正文，宿主不能拿preview覆盖原始业务文档。

### 7.2 文件、资源、工具结果及记忆

| 功能 / type或特征 | 当前参数 | 示例 rawOutput | 应使用参数 |
|---|---|---|---|
| 产物 `artifact` | kind,placement,filename,rel_path,abs_path,uri,mime,size,sha256,produced_at,tool_call_id；description/layout_id/edit_mode?；业务身份及artifact lineage可选 | `{"type":"artifact","kind":"file","filename":"report.csv","rel_path":"report.csv","abs_path":"/workspace/report.csv","uri":"file:///workspace/report.csv","mime":"text/csv","size":128}`（完整事件字段见附录） | 保留本地合同，云端产物解析暂缓 |
| 中间资源 `intermediate_asset` | 与相应产物字典共享字段；部分artifact按路径分类转换 | `{"type":"intermediate_asset","path":"/workspace/tmp/a.png"}` | 保留，不能直接当最终交付文件 |
| 文件变更回执（无固定type） | `file_changes_version:1/2,file_changes:array`，可附session/task/turn双命名字段 | `{"file_changes_version":2,"file_changes":[]}` | 保留，与artifact区分；具体回执项见附录生成点 |
| 通用工具结果 `tool_result` | text,policy_decision | `{"type":"tool_result","text":"[ERROR] denied","policy_decision":{}}` | 保留；无policy且无结构rawOutput时，当前可直接为字符串 |
| 策略决策 `policy_decision` | 工具权限/策略引擎结构，通常嵌套在结果 | `{"type":"policy_decision"}`（结构见附录） | 保留；不是宿主批准回包 |
| 权限诊断 `permission_request` | scope,reason,path等，生产工具各有细项 | `{"type":"permission_request","scope":"filesystem","reason":"需要访问"}` | 实际交互授权使用第8节request_permission，不以诊断文本授予权限 |
| 运行组件缺失 `runtime_component_required` | 组件需求及诊断，见附录 | `{"type":"runtime_component_required"}`（结构见附录） | 保留，安装由宿主能力完成 |
| 写文件分块 `write_file_chunk` | 事务、路径、chunk/累计状态等，分支见附录 | `{"type":"write_file_chunk"}`（结构见附录） | 保留；不等于最终文件已提交 |
| 写事务丢弃 `write_file_transaction_discarded` | 事务及丢弃结果 | `{"type":"write_file_transaction_discarded"}`（结构见附录） | 保留 |
| 最终块冲突 `write_file_final_chunk_conflict` | 冲突诊断与事务身份 | `{"type":"write_file_final_chunk_conflict"}`（结构见附录） | 保留 |
| 已提交状态变化 `write_file_committed_state_changed` | 已提交版本变化诊断 | `{"type":"write_file_committed_state_changed"}`（结构见附录） | 保留 |
| 事务不存在 `write_file_transaction_missing` | 缺失事务诊断 | `{"type":"write_file_transaction_missing"}`（结构见附录） | 保留 |
| 暂存文件写入 `staged_file_write` | 分阶段写入的路径/事务/状态，见附录全部分支 | `{"type":"staged_file_write"}`（结构见附录） | 保留 |
| 执行结果 `execution_result` | 执行状态、产物/验证等开放字段 | `{"type":"execution_result"}`（结构见附录） | 保留，不能替代prompt终态 |
| 图片原生检查 `image_inspection_native` | 原生图像读取结果 | `{"type":"image_inspection_native"}`（结构见附录） | 保留工具结果；与用户image输入块不同 |
| 图片检查 `image_inspection` | 图片描述、资源/模型信息等 | `{"type":"image_inspection"}`（结构见附录） | 保留 |
| 图片快照 `image_attachment_snapshot` | 原图引用及视觉快照信息 | `{"type":"image_attachment_snapshot"}`（结构见附录） | 保留，重试副作用由共享层治理 |
| 视频读取 `video_read` | 视频/帧读取结果 | `{"type":"video_read"}`（结构见附录） | 保留工具结果，不代表ACP支持视频输入块 |
| 搜索 `web_search` | 搜索provider payload + type；可含query/results/来源信息 | `{"type":"web_search","query":"主题","results":[]}` | 保留开放字段，子代理搜索也可在父卡片发送 |
| 记忆检索 `memory_search` | 查询/命中/后端证据，多个分支见附录 | `{"type":"memory_search"}`（完整结构见附录） | 保留，匹配通知不是持久化批准 |
| 纠错记忆列表 `memory_list_corrections` | 纠错项与列表信息 | `{"type":"memory_list_corrections"}`（结构见附录） | 保留 |
| 写纠错记忆 `memory_write_correction` | id,status | `{"type":"memory_write_correction","id":"m1","status":"active"}` | 保留 |
| 替代纠错记忆 `memory_supersede_correction` | id,status | `{"type":"memory_supersede_correction","id":"m1","status":"superseded"}` | 保留 |
| 删除纠错记忆 `memory_delete_correction` | id,status | `{"type":"memory_delete_correction","id":"m1","status":"deleted"}` | 保留 |
| 工具执行异常 `tool_error` | code:TOOL_EXECUTION_FAILED,tool | `{"type":"tool_error","code":"TOOL_EXECUTION_FAILED","tool":"read_file"}` | 保留，状态仍为failed |
| 内置网页提取 `web_extract` | url,final_url,summarized,content_truncated,model,original_chars,returned_chars,from_cache；验证阻塞分支status:verification_required,next_action:open_browser | `{"type":"web_extract","status":"verification_required","url":"https://example.com","final_url":"https://example.com","next_action":"open_browser"}` | 保留，需浏览器接管的验证不能伪造为已读取 |
| 技能引用回执（无type） | `skill_reference`：name/source/path/revision及分页/完整性等字段，精确实现见附录8 | `{"skill_reference":{"name":"spreadsheet","revision":"<digest>","offset":0,"complete":true}}`（形状示意） | 保留，读取结果不等于安装或授权 |
| 其他工具及MCP结果 | 任意JSON或字符串；无type也合法；可附task/turn/policy字段 | `{"rows":3}` 或 `"[OK] done"` | 保留通用渲染，不把未知type当协议错误 |

上述缩略结构示例用于快速定位；每种有type的实际构造字段和完整结构示例均列于附录，动态位置明确以 `<runtime:表达式>` 标注，不用猜造的值充当生产回包。

### 7.3 常量及动态生成的补充类型

以下类型通过常量或函数默认值生成，同样是当前实际载荷；不能仅搜索 `"type": "..."` 字面量。

| 方向 | 功能 / type | 当前协议参数 | 示例 | 应使用参数 |
|---|---|---|---|---|
| D | 市场搜索卡片 `skillhub_recommendations` | status:found/empty/unavailable/rejected，requestKind,query,queries,searchedQueries,gapType,missingCapability,items；拒绝分支code | `{"type":"skillhub_recommendations","status":"empty","requestKind":"capability_gap","query":"表格","queries":["表格"],"searchedQueries":["表格"],"gapType":"missing_specialized_workflow","missingCapability":"表格能力","items":[]}` | 保留；这是工具下行卡片，不是反向搜索请求 |
| D | 市场安装卡片 `skillhub_installation` | status:confirmation_required/installed/already_installed/installed_not_visible/failed；candidate,skillName,items或code,skillId | `{"type":"skillhub_installation","status":"failed","code":"INSTALL_UNAVAILABLE","skillId":"skill-a"}` | 保留 |
| D | 写事务仍活跃 `write_file_transaction_active` | path,transaction_state,next_chunk_index,size_bytes,chunks,final | `{"type":"write_file_transaction_active","path":"/workspace/a.txt","transaction_state":"active","next_chunk_index":1,"size_bytes":10,"chunks":1,"final":false}` | 保留；仍未最终提交 |

市场gapType枚举为 missing_connector、missing_tool_or_runtime、missing_artifact_format、missing_specialized_workflow、missing_domain_knowledge；requestKind为 capability_gap 或 explicit_marketplace_request。三种补充类型、动态artifact分支、tool_result完整字段均在附录第5节。

### 7.4 文本内业务标记

| 方向 | 功能 | 当前承载 / 示例 | 应使用参数 |
|---|---|---|---|
| D | 已注入补充的展示 | 实时成功仍为 `agent_message_chunk.content.text` 的 `[Injected:i-1] 补充内容`；取消/丢弃使用 update._meta.inputStatus | 不能宣称成功标记已全部结构化；客户端恢复时从 inputKind/inputId 合成 injected 状态 |
| D | action_hint | assistant text中的 action_hint fenced block，内容例如 `{"action":"open_settings","params":{"tab":"onboarding"},"display_text":"完善记忆"}`；tab还可为browser-tools | 保留已有文本合同；不是新增JSON-RPC method，完整文本语法见固定版本 ACTION_HINT_PROTOCOL |
| D | 内部错误正文 | text块例如 `Error: ...`，最终响应同时可带error元数据 | 优先结构化结果判定，文本用于展示 |

## 8. 全部下行反向请求及上行响应（5种）

### 8.1 标准权限协商

| 方向 | 功能 | 当前协议参数 | 示例 |
|---|---|---|---|
| D请求 | `session/request_permission` | `sessionId,toolCall:ToolCallUpdate,options:[{optionId,name,kind}]`；toolCall含toolCallId/rawInput权限请求 | `{"sessionId":"s","toolCall":{"toolCallId":"perm-filesystem-read","rawInput":{"scope":"filesystem","reason":"读取文件"}},"options":[{"optionId":"approve","name":"仅本次允许","kind":"allow_once"},{"optionId":"reject","name":"拒绝","kind":"reject_once"}]}` |
| U响应 | 允许或拒绝选择 | `outcome:{outcome:"selected",optionId}` | `{"outcome":{"outcome":"selected","optionId":"approve"}}` |
| U响应 | 取消权限选择 | `outcome:{outcome:"cancelled"}` | `{"outcome":{"outcome":"cancelled"}}` |

可选项取决于权限请求：approve=allow_once；approve_session=allow_always（显示名可由persistent_label指定）；reject=reject_once。临时/持久授权是否支持由请求决定。120秒超时按拒绝；未知选项不能当批准。保留当前协议，不把“同意”prompt代替此回包。

生图费用确认使用同轮权限请求：`rawInput.scope=billing`、`requested_scope=image_generation`，并携带 `turn_id` 与唯一 `invocation_id`。托管图片服务（未配置独立 API Key）的生图和改图在发送 HTTP 请求前等待宿主回复；文件系统的 full_access 不绕过费用确认。宿主决定是否需要确认，并校验账号、会话、轮次和远程发送者；桌面确认卡或远程文本回复均解析到原来的 `session/request_permission`，不会创建新的 prompt 或保存待重放的图片操作。

只有 `approve` / `approve_session` 且原轮次仍有效才继续本次调用；会话授权由宿主管理。拒绝、超时或 RPC 错误时不发送图片请求。取消会结束未决的费用确认，并阻止后续图片请求和重试。托管登录的凭据或组织在等待确认期间发生变化时，需重新发起操作。Agent 等待上限为 120 秒，当前桌面宿主在 115 秒结束未决请求。自定义图片服务和独立 API Key 保留自身计费策略。费用授权等待保留在当前工具调用内，不产生 `waiting_for_user` 结束状态。

生图和改图请求的会话、轮次、标题以及适用的 `X-Org-Code` 可发往任意配置的域名，并随服务允许的重定向传递。Bearer 凭据沿用鉴权策略，私有部署的图片 POST 不跟随重定向。

跨轮图片恢复扩展已移除：不再处理 `_meta.billing_resume` 或 `billing_deferred`，不再发送保存操作的 `operation` 参数、参考图摘要或 `billing_confirmation_required` 结果；宿主应直接回复当前权限请求。

### 8.2 宿主私有反向RPC

| 方向 / 方法 | 功能 | 当前协议参数 | 请求 / 响应示例 | 应使用参数 |
|---|---|---|---|---|
| D→U `_session/runtime_environment` | 查询/准备宿主运行环境 | `sessionId,capability:python/node/git/browser/chromium,action:status/ensure`；工具load中的本地探测不等于所有参数透传到宿主 | `{"sessionId":"s","capability":"python","action":"status"}` → `{"status":"ready","env":{"BOX_AGENT_PYTHON":"/managed/python"}}` | 保留；env只在允许名单内应用，不直接投影模型 |
| D→U `_session/skillhub_search` | 搜索技能市场 | `sessionId,query,gapType,limit`（默认3） | `{"sessionId":"s","query":"表格","gapType":"missing_specialized_workflow","limit":3}` → `{"status":"empty","items":[]}`；有候选为found，不可用为unavailable | 保留能力协商；平台凭据归宿主 |
| D→U `_session/skillhub_install` | 安装已选技能 | `sessionId,skillId,slug,displayName,publisherDisplayName,version` | `{"sessionId":"s","skillId":"skill-a","slug":"sheet","displayName":"表格","publisherDisplayName":"publisher","version":"1.0"}` → `{"status":"installed","skill":{"name":"sheet","slug":"sheet"}}`；成功status为installed或already_installed | 保留；需搜索候选及安装授权 |
| D→U `_session/memory_proposal` | 主动推送记忆提案 | `sessionId,proposals:[{id,content,hits,confidence}],plan?:{currentCore,newCore,consumedEntryIds,rationale}` | `{"sessionId":"s","proposals":[{"id":"m1","content":"偏好简洁","hits":3,"confidence":0.9}]}` → `{"decisions":{"m1":"pin"}}` | 保留 |
| U响应同上 | 记忆计划决策 | 有plan时 `decision:apply/reject/skip`；无plan时 decisions:id→pin/skip/reject | `{"decision":"skip"}` | 保留；120秒超时/不可用按skip，不自动应用 |

运行环境status结果可含 `status,error,errorKind,rpcCode,installerRunning,phase,progress,logTail,logPath,enabledPreference,installationReady,mcp,env` 等宿主诊断；ensure等待上限600秒、其他请求30秒。返回不一定携带全部字段，env受白名单约束。

搜索item字段由Agent归一化：id、slug、name、description、publisherDisplayName、currentVersion、platforms、riskLabels、downloadCount。安装返回skill并触发后续安装验证；不得把任意返回对象都当安装成功。精确status与分支见附录宿主回调解析器。

## 9. 错误、兼容与不支持边界

| 方向 | 功能 | 当前参数 / 条件 | 示例 / 应对 |
|---|---|---|---|
| D错误 | JSON-RPC解析/参数/方法错误 | SDK负责标准错误；具体转换由锁定SDK决定 | `error:{code,message,data?}`；不要硬编码所有异常都是-32602 |
| D错误 | 忙或重绑中 | `code:-32010,data.code:SESSION_BUSY,sessionId?` | 等原任务终态；不要新建空会话绕过 |
| D错误 | 历史编辑失败 | `code:-32011,data.code,history_revision_id?` | INVALID_HISTORY_REVISION、HISTORY_REVISION_CONFLICT、HISTORY_REVISION_IDEMPOTENCY_CONFLICT、HISTORY_ANCHOR_UNAVAILABLE、HISTORY_LOG_UNAVAILABLE、HISTORY_TOOL_PAIR_INVALID、HISTORY_CANCEL_UNCONFIRMED、HISTORY_CANCEL_TIMEOUT、HISTORY_CANCELLED、HISTORY_RESTORE_REQUIRED、HISTORY_EDIT_UNSUPPORTED_CONTINUATION 等 |
| D错误/私有result.error | 输入事实错误 | `INVALID_INPUT_CONTEXT:<field>` | 保留草稿，不派发半条输入 |
| D错误/私有result.error | 共享输入错误 | `INVALID_INPUT_BLOCK/INVALID_INPUT_TEXT/INVALID_INPUT_IMAGE/INVALID_INPUT_BLOCK_COUNT/INVALID_INPUT_IMAGE_COUNT/INVALID_INPUT_IMAGE_SIZE/INVALID_INPUT_ID` | 按2.3修正输入；图片解析也可能抛出底层验证错误，不将所有错误当封闭枚举 |
| D错误/私有result.error | 文件错误 | `INVALID_INPUT_RESOURCE:<reason>` | 包括too_many_files、invalid_blocks、unsupported_uri、outside_session_workspace、permission_denied等；部分reason为底层错误文本，当前不是封闭枚举 |
| D错误 | 轮次重试冲突 | `TURN_IDEMPOTENCY_CONFLICT`、`TURN_ALREADY_ACCEPTED` | 同ID不能假定可重跑；查询/恢复有效历史 |
| D私有result.error | 补充冲突 | `INJECTION_IDEMPOTENCY_CONFLICT` | 不能覆盖原排队输入 |
| D私有result.error | 不存在或状态不符 | session_not_found、no_active_turn、empty_text、empty_injection_id、turn_mismatch、invalid_remaining_seconds | 按对应功能处理；不要按统一error结构强拆 |
| D私有result.error | 未知扩展 | `unknown_method: <name>` | 当前为应用结果，不是所有情况都JSON-RPC MethodNotFound |
| D最终result | 模型/执行错误 | 标准stopReason + `_meta.ok/error/errorCode/errorCategory/errorDetails` | 保留部分内容，联合读取业务终态 |

**SDK有接口，但当前Box-Agent未实现/未使用：** authenticate、session/set_mode、session/set_config_option、session/list、session/delete、session/fork、session/resume、session/close、providers/list/set/disable、logout、NES及document事件等不能作为可用业务入口。下行 fs/read_text_file、fs/write_text_file、terminal/create/output/release/wait_for_exit/kill、elicitation、标准MCP桥接接口没有当前ACP业务调用；Box-Agent文件和终端工具在执行端运行。SDK方法全集不是Box-Agent能力清单。

## 10. 本次交付状态与剩余差异

| 方向 / 功能 | 当前固定提交 | 应使用参数 / 剩余工作 |
|---|---|---|
| U / 恢复 | loadSession:true，稳定ID及 session/load 已实现 | 使用2.2；不得失败后自动 new |
| D / 回放 | 已实现 replay/eventId/turnId/turnStatus、补充 inputKind/inputId | 区分回放事务与实时消息处理 |
| U / 未知会话 | prompt 仍有自动 new 兼容 | 尚需改为明确失败；load 已拒绝未知会话，不能混称未实现 |
| U / 补充 | prompt 数组与旧 text+resources 兼容 | 新客户端发 prompt；字段互斥、稳定ID与错误见4.1 |
| D / 补充终态 | status 回包、取消/丢弃 inputStatus、成功文本标记并存 | 不能声称成功回执全面结构化；取消图片残留待修复 |
| U→内部 / 图片与持久事实 | 共享图片准备、有序事实、原文/派生视图分离已实现 | 不重复发送 link 与旧图片字段；日志升级不向旧包兼容 |
| U→内部 / 版本 | SHA-256、候选与采用区分、有效响应前重试复核 | 不要求客户端发送 version；不承诺后续工具访问冻结工作区 |
| U / 切云 | 固定提交无 cloudHandoff 处理 | 独立工作包目标 prompt._meta.cloudHandoff=true，未合入本基线 |
| D / 切云 | 固定提交无切云回执 | 独立目标 PromptResponse._meta.cloudHandoff=true 及正文 JSON `{instruction,dependency_paths:string[]}` |
| U / 审批、决策、自动恢复 | 保留 prompt 文本及已有 meta/旧续接 | 独立执行控制治理尚未确定新 wire 方法 |
| U / 云资源 | 工作区内 file URI | 需独立上传/导入得到目标 Agent 可读引用；未完成云端联调 |

## 11. 完整性核对与维护

- 已枚举 BoxACPAgent 的5个实际标准入站入口（initialize/new/load/prompt/cancel）和18个私有入站分支。
- 已枚举1种下行标准通知及当前5种主要sessionUpdate业务变体（含load的user_message_chunk）、1种标准权限反向请求、4种私有反向请求，并分别记录相反方向响应。
- 已从固定提交源码提取 metadata读取表达式、共享输入Schema、环境/专家嵌套解析、业务type字典及raw_output动态写入点，见附录。异步追问、子代理嵌套事件、无type结果、文本marker及能力未实现项均独立记录。
- 静态type清单覆盖仓库已知生产者；动态MCP/plugin结果通过开放合同覆盖，不能承诺未来任意安装插件的每个业务字段都能静态穷举。
- 本文为代码静态梳理，不代表已对每条协议运行现场调用。新加入口、meta字段、rawOutput类型或结果分支时同时更新总表与附录。
- 旧图谱基线为 `699b67a2`，仅用于导航；协议事实以本文固定提交为准。未切换当前工作分支，未将未提交切云代码算入现有能力。

校验分为三个层次：路由/字段覆盖；JSON语法与链接；示例对应的业务行为。前两项不能证明第三项。此前对 a8a87c5c 的 MessageDisplay、续接解析及工作区分类校验做过针对性内存探针；这三处逻辑在本次固定提交未变。本次新增合同以固定源代码核对和文档校验为证据；没有运行实际客户端或全协议端到端测试。正文占位示例和源码结构示例仍不代表生产资源有效。

此前语义探针范围：共享ID的连续delta及文字补写身份、无delta的快照身份、完成/未完成回执；有效/空续接、空白过滤、同角色合并、消息数量及字符截断；general/code合法值、缺失及非法分类拒绝。未覆盖的协议不能因这些检查通过而被表述为已全部实测。

本次 PR #250 同步校验：117 个 fenced JSON 块通过语法解析，两份文档的本地链接存在；新增 load/inject 字段按固定源码核对。上述三项旧解析器在 a8a87c5c→9f0db474 无差异；没有将此前探针当作新协议的端到端验证。
