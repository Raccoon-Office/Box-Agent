# T13：System 与 Tool 现状清单

源码基线 `2f6501b`，2026-09-28 核对。图谱和 meta 的基线仍为 `699b67a2`，433 个文件、版本 1.0.0、刷新时间 2026-09-10；本地无 last-run-summary。本清单以源码为准，不刷新或手改图谱。

## System 来源、作用域与顺序

此处“顺序”是源码装配顺序；不存在覆盖所有段落的统一优先级解析器。多个来源拼进同一 system 字符串，后置段落并不构成后端强制优先级。权限和预算仍由执行层决定。

| 来源与入口 | 选择／装配顺序 | 作用域和更新 |
| --- | --- | --- |
| `config.py:Config.find_config_file` | 显式 BOX_AGENT_HOME 优先且隔离；缺失时仅不可变 Prompt 可回退包资源。未指定 profile 时：cwd 的 box_agent/config → 用户 config → 包 config | 配置与 Prompt 加载；不能仅按仓库模板推断真实发送内容 |
| `acp/__init__.py` 的启动装配；`tools/setup.py:render_system_prompt_template` | 读取 system_prompt_path，替换日期；按 Skill 可用性设置目录占位。缺失时使用默认文本 | ACP 进程基础模板；日期不是每一步刷新 |
| `session_assembly.py:prepare_prompt` | 完整 prepared host resources 直接借用；ACP 和 CLI 路径分别装配 | Session；宿主传入内容可与包模板不同 |
| `session_assembly.py:build_acp_session_prompt` | 模板中的沙箱／交付占位 → code 模式标识 → 文件访问／工作区布局 → 可选目录组织 → code 项目启动信息 → 环境 → Skill runtime 提示 → Action Hint → 可选 follow-up → 模式 Prompt → expert Prompt | 依赖 Session 模式、能力和选项；不是所有段落始终启用 |
| `project_context.py:build_project_startup_context_prompt` | code 模式读取工作区根 AGENTS.md、Git 状态等；AGENTS 内容有 12000 字符界限 | 启动快照；提醒后续检查更近的 AGENTS，不等于自动加载整个父目录链 |
| `session_assembly.py:prepare_prompt` 的 ACP 后续段落 | 非 utility 的 Memory recall → 图像能力提示 → host.prompt_suffix | Session 装配；Memory 是独立跨会话来源 |
| CLI 路径 | 基础模板 → 占位 → code 项目及 code Prompt → 图像 → Skill runtime → 环境 → Memory | 与 ACP 的条件和顺序不完全相同 |
| `agent.py` 初始化和 `set_system_prompt` | 补 Current Workspace（若缺少）和 Discoverable tools；同步子 Agent；首个 Message 为 system | Agent／Session；mode/expert 更新可替换 Prompt |
| `skill_context.py`、`context_input.py:DefaultContextEngine.prepare_request` | 去除已验证旧 Skill system 后缀；Skill 正文按预算投影到普通上下文；correction、transient follow-up 为请求补充 | 请求；不能把所有 Skill/Memory 内容都归类为 system |
| `kernel/context_engine.py` | 历史压缩、摘要与可信工具状态恢复 | 请求／后续历史；摘要不能当成原始工具证据 |

最终链路：宿主／配置 → Session 装配 → Agent 首条 system 和运行尾段 → Kernel 准备工具及 Context 投影／压缩 → `llm/llm_wrapper.py` 的 `llm.request` → OpenAI/Anthropic Provider wire 转换 → 模型。

本项验收器读取现有 `llm.request`，记录请求级 system、messages、工具定义 SHA-256，以及实际模型、provider、thinking 开关和完整可见 schema。它是 **Provider 转换前** 的请求证据；不是 HTTP 字节抓包。最终 wire 的角色转换、图片处理、提供方内部重试还需 Provider debug 或专项 probe。本次真实模型启动受阻，没有取得新的请求清单，不能拿 fixture 名单冒充桌面实际能力。

## Tool 定义与反馈

| 环节 | 当前入口与可观察契约 |
| --- | --- |
| 能力装配 | `tools/setup.py`：条件注册文件 read/write/edit/append/search/query_jsonl，bash/output/kill，execute_code/status，Skill，Memory，plan/todo，sub_agent，图像、Obsidian、用户输入/决策、产物发布、执行回执、调度、MCP 配置；MCP 来自 loader/catalog。不是固定全量工具表 |
| 名称／描述／参数 | `tools/base.py:Tool` 的 name、description、parameters；to_schema/to_openai_schema 生成模型定义。别名通过 build_tool_name_index 归一 |
| 本地发现 | `tools/local_tool_exposure.py`：DISCOVERABLE_LOCAL_NAMES 延迟集合；文件工具、活跃计划／目标／后台进程、Skill hints、宿主 require_tools 决定直接暴露；不授予权限 |
| MCP 与搜索 | `agent.py` 注册 tool_search；`tools/mcp_tool_search.py` 的 ExposureManager 选择 schema，搜索后激活；新工具需进入当前请求可见集 |
| 请求快照 | `tools/engine/preparation.py:prepare_tools` → PreparedTools；冻结定义、实际对象、别名、MCP generation、可信去重标志；执行时验证绑定 |
| 参数与执行 | Tool.invoke 验 schema 和参数，返回 INVALID_TOOL_SCHEMA / INVALID_TOOL_ARGUMENTS；engine 负责请求准入、Hook、权限、预算、调度和闭合；描述不扩大执行能力 |
| 结果 | `tools/engine/results.py` 分开处理 visible_content/error、model_context、重复错误缩写、资源回执和持久内容；ToolResultStorage 负责裁剪及可恢复引用；role=tool 的内容可能不同于 UI 展示 |
| 证据 | `tool.request` 有参数、准入状态；`tool.response` 有 success、可见内容、model_content、policy_decision 和时长。ACP rawInput 是参数而非可靠工具名；验收器据 trace 的 tool_name 关联 call_id，不从 UI 标题猜工具 |

## 最小样本与既有失败依据

全部输入为提交的合成数据，不包含用户原始失败日志。

| ID／分组 | 独立验收 | 选择依据与边界 |
| --- | --- | --- |
| q0-file／开发 | 修改指定 JSON 字段，保留另一个字段 | 文件读写与保留约束；相关真实缺陷历史见 T9b |
| q0-source／开发 | JSON 精确包含已批准版负责人、预算和来源 ID；输入未改 | `test_system_prompt_contract.py` 的原文/线索区分；本地固定资料，不证明联网检索质量 |
| q0-skill／开发 | 成功读取 xlsx Skill；ZIP/XML 检查单元格值 | 已有 Skill loader/上下文测试；只检结构和值，不代表视觉质量、公式正确性或全部办公 Skill |
| q0-long／保留 | query_jsonl 成功；指定记录精确匹配；输入 SHA 保留 | 已有 JSONL bounded-query Prompt 契约；1000 行约 46 KB，测试长输入定位，不声称触发上下文压缩 |
| q0-wait／保留 | 输入请求工具成功、无文件搜索/读写/执行、无伪造 total.json | 已有缺失附件 Prompt 契约；这是等待输入，权限反向 RPC 等待另用桌面 fixture |
| q0-recover／开发 | 指定源文件读取失败后成功读取备用文件，fallback 内容正确、未补造缺失源、原附件未改 | 文件缺失恢复；不是未知外部副作用重放测试 |
| q0-complete／保留 | 文件实际为 JSON 数字 391，成功读文件工具证据 | T2 正常终止不等于完成；直接回归含“口头完成但无文件”和错误值反例 |

保留集未用于 Prompt 或工具调参。后续优化先用开发集定位，固定改动后再运行保留集。全部 immutable attempts 都报告，未捕获／损坏证据独列未验证，不从分母静默删除。

## 记录和对照限制

`baseline-context.json` 保存 Git 基线、实际 tracked 源文件哈希、评估器/样本版本、profile 配置文件哈希和 Python/系统信息；不复制配置正文或凭据。selection/manifest/run 沿用现有模型绑定和输入指纹。质量报告保存每例校验、终止、完整性、总尝试耗时、工具次数/失败数、请求定义与指纹、LLM timing 和提供方已报 token usage。总耗时包含进程启动及清理。

价格、服务端模型部署版本、首个“有效”结果的语义时刻、提供方未暴露的内部重试和账单无法从现有证据可靠得出；成本保持 null，不能按零费用解释。LLM first_content timing 仅是文本出现时刻。不同绝对 cwd、日期或 profile 会改变 system 指纹，比较时需先解释差异，不能将哈希变化直接归因于提示词优化。

本项不保存第二份 Session 权威状态，不执行模型自评。原始记录和质量 JSON 仅在本地输出目录；提交的实际结果见 [T13 验证](T13-minimal-quality-baseline.md)。
