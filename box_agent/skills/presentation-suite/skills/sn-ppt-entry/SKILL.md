---
name: sn-ppt-entry
description: Use when a user asks to create a presentation, slide deck, PPT, or PPTX from a query and optional files, or to resume an existing SenseNova presentation task.
metadata:
  allow_override: false
  project: SenseNova-Skills
  tier: 1
  category: scene
  user_visible: false
triggers:
  - "生成 PPT"
  - "做一套 PPT"
  - "做一份演示"
  - "继续生成 PPT"
  - "sn-ppt-entry"
---

# sn-ppt-entry

统一接收 PPT 生成请求，建立唯一任务目录，准备材料。Standard 先完成定向外部证据补充，
Deep 先完成完整 Research，**下一步必须调用 `sn-ppt-story` 生成公共 `outline.md`**，再按 `choices.output` 分发到 `sn-ppt-standard` 或 `sn-ppt-dazzle`。

这是一套 Skill 的入口，不是独立运行时。文本理解、视觉理解和推理使用宿主 Agent
已有能力。普通搜索、图片搜索和图片生成优先使用宿主原生工具；原生能力不存在或实际
调用不可用时，才使用 PPT 整包自带的 `sn-ppt-tools`。不得要求用户另外配置模型客户端，
不得调用 `model_client.py`、`stage.py` 或为本流程再造 CLI 调度层。

## Box-Agent 兼容入口

在 Box-Agent 中，从本次加载的 Skill 提示取得 Entry 的绝对 Skill Root，记为
`<ENTRY_SKILL_ROOT>`。Entry 的所有确定性脚本都必须使用
`python "<ENTRY_SKILL_ROOT>/scripts/<name>.py"` 调用；禁止使用 `$SKILL_DIR`、当前工作目录
或相对的 `skills/` 路径。所有任务产物仍必须写入同一个绝对 `$DECK_DIR`，并在调用 Story
和出口 Skill 时逐字复用该路径。

## 三个独立选择

### 执行深度

- `draft`：尽快形成可看的方向稿。默认不做外部事实搜索，大纲完成后直接生产。
- `standard`：质量和等待时间平衡。Story 前由 Entry 做定向外部搜索和证据补充，不运行
  完整 `sn-deep-research`；生成前让用户看 `outline.md`。
- `deep`：先确认外部 `sn-deep-research` 可用，再完成研究和正式 `outline.md`；生成前让用户确认。该外部 Skill 未随本套件提供，不可用时说明并让用户选择 Draft 或 Standard，保留已有产物。

Entry 根据任务复杂度、事实时效性、材料完整度和用户措辞推荐一档，同时展示
Draft / Standard / Deep。用户可以覆盖；用户未覆盖时采用推荐，不为这个选择增加一轮
阻塞问答。

### 输出格式

本套件是公共 `pptx` 入口下的设计模式，保留两个表达出口：

- `static_html` -> `sn-ppt-standard`：静态 PPT 页面，始终交付整册 `present.html`，默认另交付 `.pptx`。
- `dynamic_html` -> `sn-ppt-dazzle`：带动效和翻页交互的 `deck.html`；不承诺保留动画的 PPTX。

对外描述 PPTX 文件交付，不宣传或承诺可编辑、原位编辑能力。
以公共入口核对的用户原文和真实选择回复为准，保留完整原始需求及后续更正；当前用户
明确更正优先。`task_pack.json` 只记录任务信息，不能授权模式或覆盖这些依据。
在演示或课件任务中，肯定要求播放时对象运动、内容随时间变化，或操作引发讲解内容变化，
就是动态要求，使用 `dynamic_html`；例如课件里行星转动或滑块控制过程变化。
按完整需求的含义判断，不依赖“动态 PPT”等固定词；普通翻页、编辑标题不是内容动效。
老师、太阳系等身份或题材、“生动一点”等风格，以及“行业动态”“动态规划”等主题，
都不能据此推断动态；提问、比较、引用和否定也不是动态制作授权。
`unknown`、解析失败或缺失字段不等于静态。先恢复已有选择与原文依据；仍无法确定或
相互冲突时返回公共入口，用真实选择卡澄清，不重建任务或静默默认 `static_html`。
只有确认完整需求没有动态要求、没有未决解析失败或冲突时才默认 `static_html`；
静态仍默认 HTML + PPTX，只要 HTML 时关闭对应 PPTX 后处理。
下方 `task_pack.json` 是已确认静态任务的示例，不能作为覆盖动态原需求的默认值。
独立网页模拟器、应用或数据看板不因 HTML、动画或交互而进入本流程；已有演示的局部
文字、颜色修改保留目录、输出和已完成阶段，不因制作时的编辑操作切换为动态。
静态与动态共用 Entry 和 Story，再按确认的 `choices.output` 进入唯一生产出口。
已有 PPTX 的原位编辑、模板填充与已选设计模式冲突时，保留原始需求、附件和交付格式，
先向用户澄清是否改用快速模式；只有用户明确同意后才加载 `ppt-fast`，不得自动切换。
已有 SN HTML 任务继续使用其任务目录与输出选择。
恢复旧静态任务的 `web_html` / `web` 字段时，先读取原任务包，保留相同绝对 `deck_dir`、
材料、大纲、页面和已交付产物，仅将 `choices.output` 改为 `static_html`、`ppt_mode` 改为
`standard`，把原 `web_postprocess`（包括用户明确的 `[]`）迁到 `choices.static_postprocess`，
再移除旧字段；两种后处理字段已有冲突时先澄清，不覆盖已有选择。该迁移不改变用户已选的
设计模式；字段迁移本身不重做 Research 或 Story，本轮标题等内容修改按下方恢复规则
局部更新 Story。旧 `creative` 图片整页出口未提供，保留产物并说明。

### 设计丰富度

- `restrained`（克制）：政企、学术、法务、财务或明确要求简洁。
- `rich`（丰富）：默认，适合多数业务汇报、产品介绍和正式演示。
- `high_creative`（高创意）：发布会、品牌传播、创意提案或明确要求强视觉。

用户明确指定时直接采用；否则结合场景、受众、输出格式和措辞推荐。显示当前选择与
三档名称，但不单独阻塞。用户在生成前、确认大纲时或后续修改中覆盖时，更新同一份
`task_pack.json`。丰富度只决定视觉投入，不改变事实标准和 Story 遵循要求。

## 唯一目录规则

新任务的输出位置沿用已经验证稳定的原 Entry 规则，不得重新推断：

1. 先执行 `pwd -P`，把该命令**实际返回的完整绝对路径**逐字记为 `workspace_root`。宿主显示的
   session workspace、repo 根或其他路径标签都不能替代这次 `pwd -P` 的结果；尤其不得自行
   删除返回路径末尾的 `output`、`workspace` 等目录名。
2. 唯一父目录是 `<workspace_root>/ppt_decks/`。
3. 目录名是 `<topic_concise>_<YYYYMMDD_HHMMSS>`。
4. 创建后在同一条命令中用 `pwd -P`/绝对化结果回显 `deck_dir`，立即写入
   `task_pack.deck_dir`，此后全链路只逐字复用该值，禁止根据框架 workspace 信息重新拼接。
5. 不使用用户 home 根、Skill 目录、repo 根、`/tmp`、另一个 workspace 或
   `PPT_DECK_ROOT`。
6. 不能创建 `<workspace_root>/ppt_decks/` 时停止并报告权限问题，不换地方继续。

Box-Agent 的文件工具相对根通常是 `<session_workspace>/output`，而会话元数据中的 workspace
可能是它的父目录。两者不是同一路径。创建 `task_pack.json`、`info_pack.json`、`outline.md`
和后续产物时必须全部使用上述同一个 `deck_dir`；每次文件工具返回的绝对落盘路径都要位于
该目录。若返回路径不一致，立即停止并修正路径，不得在两个 `ppt_decks/` 之间继续。JSON 和
Markdown 直接用文件工具写入，不要用 `execute_code` 内嵌一个重新推导的绝对路径。

已有任务必须复用已有 `task_pack.json` 中的绝对 `deck_dir`；继续任务不得新建目录。
所有 Skill 安装目录只读。

## `task_pack.json`

创建目录后立即写入，并在每个阶段边界原地更新。它在任务完成后仍然保留，用于进度
展示、任务中断恢复和后续编辑。保持字段少而稳定：

```json
{
  "schema_version": "ppt_task_v2",
  "deck_id": "topic_20260729_143015",
  "deck_dir": "/absolute/workspace/ppt_decks/topic_20260729_143015",
  "workspace_root": "/absolute/workspace",
  "ppt_mode": "standard",
  "params": {
    "role": "...",
    "audience": "...",
    "scene": "...",
    "page_count": 8,
    "language": "zh-Hans",
    "image_source": "auto",
    "infographic_source": "echarts"
  },
  "request": {
    "query": "...",
    "source_files": []
  },
  "choices": {
    "execution_depth": "standard",
    "output": "static_html",
    "design_richness": "rich",
    "static_postprocess": ["pptx"]
  },
  "state": {
    "status": "preparing",
    "current_stage": "entry",
    "completed_stages": [],
    "research": {
      "required": false,
      "executor": null,
      "mode": null
    },
    "capabilities": {},
    "artifacts": {},
    "last_error": null,
    "updated_at": "ISO-8601"
  },
  "created_at": "ISO-8601"
}
```

当 `choices.output` 是 `static_html` 时，`ppt_mode` 必须是 `standard`，
`choices.static_postprocess` 默认是 `["pptx"]`；只有用户明确只要 HTML 时写 `[]`。
当 `choices.output` 是 `dynamic_html` 时，`ppt_mode` 必须是 `dazzle`，不触发 PPTX 后处理。
动态任务的 `choices.static_postprocess` 为 `[]`。

| `choices.output` | `ppt_mode` |
|---|---|
| `static_html` | `standard` |
| `dynamic_html` | `dazzle` |

阶段更新只修改相关字段，不重写用户选择和历史完成项。开始阶段时写
`current_stage/status/updated_at`；完成时把阶段加入 `completed_stages` 并把真实产物的
绝对路径写入 `artifacts`；失败时写 `last_error`。文件系统中的真实产物优先于过时的状态
字段。

## `info_pack.json`

只承载材料和事实信息，不复制运行状态：

```json
{
  "user_query": "...",
  "user_assets": {
    "reference_images": [],
    "reference_image_captions": {},
    "reference_docs": [],
    "reference_docs_failed": []
  },
  "document_digest": {
    "topic_summary": "...",
    "key_sections": [],
    "key_points": [],
    "data_highlights": [],
    "conflicts": [],
    "open_questions": [],
    "inherited_tables": [],
    "inherited_images": []
  },
  "raw_documents": "/absolute/deck/raw_documents.json",
  "research_report": null
}
```

`document_digest` 由宿主 Agent 阅读完整材料后直接写入，不通过额外模型 API。数字、专有
名词、时间、单位和材料间冲突必须保真。上传图片和文档内图片继续遵守“一张图片只理解
一次”的原有能力：宿主 Agent 用原生视觉能力读取后，把上传图片说明缓存到
`reference_image_captions[absolute_path]`，把文档内图片说明缓存到
`raw_documents.documents[].inherited_images[].visual_summary`。已有缓存且图片未变时跳过，
下游统一读取缓存；不创建独立 caption 服务。

## 可选搜索与图像能力

把当前 Skill 的同级目录解析为 skills 根，并固定：

```text
PPT_TOOLS_DIR = <skills root>/sn-ppt-tools
```

开始需要某项能力时读取
`<PPT_TOOLS_DIR>/references/capability-policy.md`，按
`native -> bundled -> none` 选择。`sn-ppt-tools` 是 PPT 整包的一部分，不做“是否安装”
判断，也不扫描其他仓库。只把每类能力的来源、状态、非敏感错误摘要和更新时间写入
`task_pack.state.capabilities`；不得记录 key 或 Authorization header。

媒体能力都不是 Entry 的强制前置。缺失时按 policy 继续，用可交付的无图版式表达内容。
需要用户处理配置时只提示运行 `sn-ppt-doctor`；不要在 Entry 重复变量清单。Doctor 会显示
Hermes/OpenClaw 实际读取的用户级 `.env`、缺失项和配置模板。

## 新任务流程

1. **进入和识别**：回显已经进入 Entry。提取角色、受众、场景、页数、语言、附件、
   明确的设计要求，以及用户是否明确要求 Static 只交付 HTML。已经提供的信息不再询问。
2. **路由已有 PPTX**：若原位编辑或模板填充与已选设计模式冲突，先澄清是否切换快速模式，用户明确同意后才交给 `ppt-fast`；已有 SN HTML 任务按恢复规则继续。从零生成继续本流程。
3. **推荐三个选择**：确定执行深度、输出格式和设计丰富度。只有输出意图确实无法判断且会产生完全不同交付物时才询问；其他情况先给推荐并继续。
4. **建立固定目录**：严格按“唯一目录规则”创建目录和初始 `task_pack.json`。
5. **解析附件**：对 PDF、DOCX、MD、TXT 执行：

   ```bash
   python "<ENTRY_SKILL_ROOT>/scripts/parse_user_docs.py" \
     --files <absolute paths...> \
     --output "<deck_dir>/raw_documents.json" \
     --asset-dir "<deck_dir>/source_assets"
   ```

   Markdown 表格与本地图片引用、DOCX 表格与内嵌图片、PDF 文本、`inherited_images` 以及按需生成的 `page_visuals` 都要保留；`page_visuals[].path` 必须是 `$DECK_DIR/source_assets/` 下的绝对路径，并随`raw_documents.json` 一起交接。未生成页图时，Figure 页裁切任务必须返回 `blocked`。一份文件失败时记录到 `reference_docs_failed`，继续处理其他文件。
6. **理解全部材料**：读取 `raw_documents.json` 中全部正文、表格和图片索引；必要时分段
   阅读，但不得只读开头。用宿主 Agent 原生视觉能力逐张理解相关上传图片和文档内图片，
   按上述兼容字段缓存，失败项记录后继续。再生成 `document_digest`，写
   `info_pack.json`。Story 和出口不得重复理解已有缓存的图片，除非文件变化或当前任务确实
   需要重新核对视觉细节。
7. **决定外部证据路径**：
   - Draft：默认跳过，写 `required=false`、`executor=null`、`mode=null`。用户明确要求核查
     某个事实时，可直接用普通搜索完成该核查，但不为 Draft 启动完整 Deep Research。
   - Standard：默认写 `required=true`、`executor=entry`、`mode=null`。不得因为“用户材料
     看起来足够”而跳过；只要用户未禁止联网且普通搜索可用，就必须在 Story 前执行至少一次
     真实普通搜索。
   - Deep：写 `required=true`、`executor=sn-deep-research`；默认 `mode=normal`，复杂、
     多维、争议、高时效或高风险任务使用 `heavy`。`quick / normal / heavy` 只属于
     `sn-deep-research`，不用于描述 Standard。
   - 用户明确禁止外部搜索或要求只使用给定材料时，不联网，写 `required=false`、
     `executor=null`、`mode=null` 和 `skipped_reason=user_forbidden`，只做材料内证据审计。
8. **完成外部证据补充**：先按可选能力规则确定普通搜索来源：宿主原生网页搜索可用时
   直接使用；否则调用 `<PPT_TOOLS_DIR>/scripts/web_search.py`。
   - Standard 由 Entry 当前 Agent 做定向搜索，不调用 `sn-deep-research`，不做 scout、
     多维拆解、补研循环或完整研究报告编排。搜索目标只来自用户 query、材料中的时效性风险、
     关键事实和会影响叙事的明显缺口；优先官方和一手来源，记录标题、URL、日期、支持的结论
     及未解决限制。停止条件是 Story 所需关键证据已经覆盖或缺口已经明确，不为扩写而漫游。
     把简洁结果写到 `<deck_dir>/research/report.md`。
   - Deep 把普通搜索来源、绝对 `report_dir=<deck_dir>/research` 和当前任务材料交给
     `sn-deep-research`，按 `mode=normal|heavy` 完整运行。`sn-ppt-tools` 只作为搜索能力
     fallback，不是另一套 Research 流程。
   - 两条路径完成后都把同一个 `<deck_dir>/research/report.md` 写入
     `info_pack.research_report` 和 `task_pack.state.artifacts.research_report`，再进入 Story。
   - 原生与内置搜索都不可用时，不伪造 Research，也不阻塞 PPT：把
     `state.research.required=false`、`state.research.executor=null`、
     `state.research.skipped_reason=search_unavailable` 和
     `info_pack.document_digest.open_questions` 中的事实覆盖限制写清楚，然后直接进入
     Story。
   - 搜索能力存在但 Research 本身尚未完成时，仍不得先写正式大纲。
9. **调用 Story**：把同一 `deck_dir` 交给 `sn-ppt-story`。Story 读取 query、
   `info_pack.json`、`raw_documents.json` 和已有 Research，生成唯一
   `<deck_dir>/outline.md`。
10. **大纲交互**：Draft 默认继续生产，同时告知大纲路径；Standard / Deep 展示一行整体
    叙事和路径，等待用户修改或确认。继续前重新读取磁盘上的 `outline.md`，不能使用聊天
    中的旧副本。
11. **出口分发**：Story 已完成且当前磁盘 `outline.md` 已按本档位确认后，
    `static_html` 调用 `sn-ppt-standard`，`dynamic_html` 调用 `sn-ppt-dazzle`；始终传入相同绝对
    `deck_dir`。不得绕过 Story；出口不再研究、重排页面或重写大纲。
12. **后处理和收尾**：完成页面修复与内容核验、更新任务包后，父级按公共 `pptx`
    的 `scripts/finalize.py` 命令统一执行 Standard 的 build、audit 和所需正式 exporter；
    不再手工重复 build/audit/export。核对回执中的 `<deck_dir>/present.html` 存在、覆盖
    全部页面、播放器可打开，随后按 Standard 覆盖 build 后的最终像素，必要的图片与
    Review 记录只写 renders / _trace，不再修改制作输入。逐页 HTML/PNG 或已有 PPTX
    不能替代这一步。静态默认 HTML + PPTX，只有用户明确只要 HTML 时省略 PPTX。
    只缺播放器时复用已有页面补齐收尾，不重做 Research、Story 或整册页面。
    任务包只登记调用前已验证存在的产物；本次新增产物以正式回执为准，不为补记字段
    修改已绑定输入。最终回复给出回执中的真实 HTML/PPTX 链接，保留本地依赖资源。
    必需产物缺失或转换失败时保留产物并披露 partial 与原始错误；不得用宿主工具、
    python-pptx 或自写脚本替换 exporter，不伪造路径。动态完成原 Dazzle 全册检查后由
    同一 finalize 命令核验 `deck.html` 与本地资源；它不会调用静态 exporter。
    独立 SN 没有公共 pptx 脚本时按 Standard 原 build → audit → exporter 流程收尾，
    再核对最终像素与真实文件，不额外调用不存在的公共脚本。

## 恢复规则

当用户提供 deck 目录，或当前目录下存在明确的任务包时：

1. 读取 `task_pack.json`、`info_pack.json` 和磁盘实际文件。
2. 当前 `outline.md` 存在时，以磁盘版本为 Story 真相。
3. Research 报告、outline、页面、渲染图和最终产物存在时，不因状态字段滞后而重做。
4. 从最早一个“必要产物确实缺失”的阶段继续。
5. 用户本轮要求修改标题、结论或事实时，先调用 Story 仅更新同一 `outline.md` 的受影响项，不因局部修改重做材料或 Research；用户修改 outline 后，只失效 Story 之后受影响的页面及其派生 PPTX；Research 和材料解析不自动重跑；若 HTML、PNG、讲稿、播放器均完整而仅 PPTX 缺失，只重跑对应出口的 exporter。
6. 不创建 `task_pack_v2.json`、`outline_v2.md` 或第二个 deck 目录。

## 进度反馈

超过约 30 秒的工作必须让用户看到进度。至少在以下边界各回显一句：

- 已识别任务与三个选择；
- 已建立 `deck_dir`；
- 附件解析开始/完成；
- Research 开始/完成或明确跳过；
- Story 开始/`outline.md` 已生成；
- 等待确认或已进入出口；
- 页面生产进度；页面完成后回显 `present.html`、PNG、讲稿和播放器的真实路径。
- 后处理与最终产物；PPTX 失败时保留 HTML、PNG、讲稿和播放器，状态为 `partial`，不得伪造 PPTX 路径，并把失败原因写入 `task_pack.state.last_error`。

进度以用户能理解的阶段描述为主，不暴露内部 prompt、模型调用或细碎状态字段。

## 硬规则

1. 原有能力没有被明确删除时必须保留。
2. 不在 Entry 生成页面、图片或视觉方案。
3. 不允许出口自行搜索或重新决定页序、标题和核心结论。
4. 不静默切换输出格式或设计档位。
5. 可选搜索或图像能力失败时最多尝试原生一次、内置一次；随后执行 policy 的无工具路径。
6. 不用 mock 数字冒充事实；缺口应回到 Research/Story 或明确标示。
7. 不因 Static 默认的 PPTX 后处理失败删除可用的 HTML、图片或 PPTX。
8. 不新增外部模型客户端、额外 API 配置、通用状态机或大型调度脚本。

## 方法采用与同任务恢复

公共 `pptx` 的需求与交付义务贯穿整个任务，保持它与当前后端同时采用。
下方阶段替换只退休已完成内部阶段，不退休公共 `pptx`；最终产物、回执及真实交付
完成后才可 `get_skill(skill_name="pptx", usage="release")`。

实际执行本方法用 `get_skill(..., usage="use")`（默认值）；仅查看其他出口、Tools/Doctor
文档用 `usage="reference"`，不改变当前采用方法或用户路线。完成阶段后读取下一阶段时
可传 `replace=["旧方法名"]`，仅新读取成功后退休旧方法；读取失败保留原方法并处理错误。
例如 Entry → Story 用 `get_skill(skill_name="sn-ppt-story", usage="use", replace=["sn-ppt-entry"])`；
Story → 已确认的 Standard/Dazzle 同样替换 Story。只退休方法用
`get_skill(skill_name="旧方法名", usage="release")`。不要以参考读取自动切换制作出口。
仅用户明确开始独立新任务才传 `new_task=True`，并在该新任务第一次方法读取时、澄清前声明；
不得在后续阶段交接时补传。选择卡回复、继续、补充材料、页面修改、
压缩后恢复和阶段交接均延续原任务，不能把短回复当成完整需求。
从可用对话历史、通用任务上下文及实际工作文件恢复原始目标、全部附件、用户明确选择、
后续更正、页数、格式、同一绝对目录与已完成阶段。task_pack 是工作数据，不能证明用户选择。
原始动态要求与 task_pack 静态字段冲突时先修正工作数据，保留 Research/Story 成果；
真实选择依据丢失或冲突未解时用 `request_user_decision` 澄清，不从默认字段推断静态。

按所选出口的收尾时序，父级从公共 `pptx` Skill 的实际目录运行
`scripts/finalize.py --workspace <工作空间> --deck-dir <同一目录> --requirements <需求文件> --task-pack <任务包>`。
先完成任务包阶段/产物字段更新，再执行正式收尾；调用后修改输入必须重跑。
检查 stdout 和 `_trace/finalize-receipt.json` 的产物与警告；技术回执不能替代视觉或内容检查。
