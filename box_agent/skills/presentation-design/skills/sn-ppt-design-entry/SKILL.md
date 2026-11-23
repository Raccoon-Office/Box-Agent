---
name: sn-ppt-design-entry
description: 接收新建或继续 SenseNova PPT 演示文稿的请求，准备材料和唯一大纲，统一交给 sn-ppt-dsl 生成 XML 坐标页面及可编辑 PPTX。
metadata:
  user_visible: false
  allow_override: false
  project: SenseNova-Skills-local
  tier: 1
  category: scene
triggers:
  - "生成 PPT"
  - "做一套 PPT"
  - "做一份演示"
  - "继续生成 PPT"
  - "sn-ppt-design-entry"
---

# SN PPT 统一入口

唯一生成流程：**Entry 准备材料 → Story 编排 outline.md → sn-ppt-dsl 生成 XML → 本地校验与 PPTX 导出 → 实际成品预览**。所有设计风格共用这条流程；页面内容、布局和后续编辑都以 XML 为基线。

这是 Skill 入口，使用宿主 Agent 的文字和视觉能力。普通搜索、搜图和生图优先使用宿主原生工具，缺失或一次调用失败后才使用同级 `sn-ppt-design-tools`。不另配文字模型接口或重造调度器。除非用户明确要求，整个任务由当前 Agent 完成。

将本文件所在目录记为绝对 `ENTRY_SKILL_ROOT`，其父目录是 skills 根。脚本使用绝对路径调用；所有任务产物写入同一个绝对 `DECK_DIR`，跨技能逐字复用该路径。

在 Box-Agent 中，本入口承接公共技能 `pptx` 已确认的设计模式。尚未确认模式的新任务返回 `get_skill(skill_name="pptx")`。通过 `get_skill(skill_name="sn-ppt-design-story")` 读取大纲规则，再通过 `get_skill(skill_name="sn-ppt-dsl")` 读取写页和验收规则。已有大纲确认或用户已明确授权自动继续时，沿用该选择。

## 既有 PPTX 的小范围保真编辑

用户仅要求修改上传 PPTX 的标题、个别文字、页码，或新增目录页且保留原正文与版式时，沿用公共入口 `pptx` 的编辑分流，用户确认后调用 `ppt-fast` 做局部 OOXML 编辑，并保留原文件。此类任务不强制迁移整册到 SML，也不运行外部研究或重新编排正文；迁移可能破坏合并单元格、组合形状、动画和母版。仅编辑明确指定的形状、关系和页序，新增页复用原主题，记录原页码与新页码映射。验收须核对总页数、每个指定标题、目录页码，以及未指定正文/图表/媒体不变；有本机 PowerPoint 时可用实际 PowerPoint 导出 PDF 验收。下述统一生成流程用于新建、重构整套，或已有 SML 项目。

## 任务选择

新建/重构任务的输出固定为 `xml_pptx`，不提供生成格式分支。只按任务需要确定以下两项；用户已经明确的信息不再询问。

- 执行深度：`draft` 尽快成稿，默认不搜索；`standard` 默认，先定向补充外部证据；`deep` 先运行 `sn-deep-research` 再写大纲。根据事实时效性和材料完整度推荐，用户可以覆盖。
- 设计丰富度：`restrained` 克制、`rich` 丰富（默认）、`high_creative` 高创意。丰富度影响配色、构图和素材投入，不改变 XML → PPTX 流程，也不把整页生成图作为页面源文件。

## 唯一任务目录

1. 新任务先执行 `pwd -P`，实际返回的完整绝对路径就是 `workspace_root`；不得根据会话标签自行删除末尾的 `output` 或 `workspace`。
2. 创建 `<workspace_root>/ppt_decks/<topic_concise>_<YYYYMMDD_HHMMSS>`，回显绝对路径并立即写入 `task_pack.deck_dir`。
3. 此后所有文件写入同一 `deck_dir`，包括材料、研究、大纲、XML、素材、校验和成品；不使用第二个工作区、Skill 目录或临时目录交付。
4. 无法创建时报告权限问题，不擅自换目录。已有任务复用任务包中的绝对目录，不新建副本。

Box-Agent 的文件工具根可能是会话 workspace 下的 `output`，须以 `pwd -P` 为准。每次文件工具返回的绝对路径必须位于同一 `deck_dir`；不一致时先修正路径。

## 任务包与材料包

新建目录后写入 `task_pack.json`，阶段边界只更新有关字段：

```json
{
  "schema_version": "ppt_task_v2",
  "deck_id": "topic_20260922_143015",
  "deck_dir": "/absolute/workspace/ppt_decks/topic_20260922_143015",
  "workspace_root": "/absolute/workspace",
  "ppt_mode": "dsl",
  "params": {
    "role": "...", "audience": "...", "scene": "...",
    "page_count": 8, "language": "zh-Hans", "image_source": "auto"
  },
  "request": {"query": "...", "source_files": []},
  "choices": {"execution_depth": "standard", "output": "xml_pptx", "design_richness": "rich"},
  "state": {
    "status": "preparing", "current_stage": "entry", "completed_stages": [],
    "research": {"required": false, "executor": null, "mode": null},
    "capabilities": {}, "artifacts": {}, "last_error": null, "updated_at": "ISO-8601"
  },
  "created_at": "ISO-8601"
}
```

`ppt_mode` 固定为 `dsl`，`choices.output` 固定为 `xml_pptx`。开始阶段写 `current_stage/status/updated_at`；完成后登记真实文件绝对路径并更新 `completed_stages`；失败写 `last_error`。真实文件优先于滞后的状态字段。

`info_pack.json` 只记录材料事实，不复制任务状态：

```json
{
  "user_query": "...",
  "user_assets": {
    "reference_images": [], "reference_image_captions": {},
    "reference_docs": [], "reference_docs_failed": []
  },
  "document_digest": {
    "topic_summary": "...", "key_sections": [], "key_points": [],
    "data_highlights": [], "conflicts": [], "open_questions": [],
    "inherited_tables": [], "inherited_images": []
  },
  "raw_documents": "/absolute/deck/raw_documents.json",
  "research_report": null
}
```

由宿主 Agent 阅读完整材料后生成摘要，保真数字、单位、日期、专有名词和冲突。上传图片的理解缓存到 `reference_image_captions[absolute_path]`，文档内图片说明缓存到 `raw_documents.documents[].inherited_images[].visual_summary`。图片未变且已有缓存时复用，不重新调用 caption 服务。

## 执行流程

1. **识别任务**：确定主题、受众、场景、页数、语言、附件、研究深度和设计丰富度。修改已有 XML 项目时直接按下文恢复；用户只有已有 PPTX 时先读取实际内容及版面，将其作为迁移参考建立 XML，不能承诺未支持对象的无损往返，也不能切回其他生成器。
2. **建立目录和任务包**：按唯一目录规则写入文件。
3. **解析附件**：PDF、DOCX、MD、TXT 使用：

   ```bash
   python "<ENTRY_SKILL_ROOT>/scripts/parse_user_docs.py" \
     --files <absolute paths...> \
     --output "<DECK_DIR>/raw_documents.json" \
     --asset-dir "<DECK_DIR>/source_assets"
   ```

   保留正文、表格、本地图片引用、内嵌图片和按需生成的 `page_visuals`。页图必须位于 `source_assets/`；没有真实页图时不能假装完成 Figure 裁切。单份文件解析失败记录后继续其他材料。
4. **理解材料**：完整阅读原文、表格和图片索引，缓存相关图片说明，写入 `info_pack.json`。长材料可以分段读取，但不能只看开头。
5. **完成证据补充**：
   - Draft 默认跳过外部研究；明确要求核查的事实按需搜索。
   - Standard 由当前 Agent 做定向普通搜索；用户未禁止联网且搜索可用时，至少实际搜索一次。优先一手来源，记录支持结论、URL、日期和未解决限制，写入 `<DECK_DIR>/research/report.md`。不自动升级完整深度研究。
   - Deep 调用 `sn-deep-research`，传同一目录下的 `report_dir=<DECK_DIR>/research`，通常 `mode=normal`，复杂任务可用 `heavy`；不因该技能引用角色而默认启用 subagent。
   - 用户要求仅使用给定材料时不联网，记录 `skipped_reason=user_forbidden`。原生和内置搜索均不可用时记录 `skipped_reason=search_unavailable` 与事实覆盖边界，不伪造研究报告。
   - 将真实报告路径写入 `info_pack.research_report` 与 `state.artifacts.research_report`。需要的研究尚未完成时，不能先写正式大纲。
6. **调用 Story**：把同一个绝对 `DECK_DIR` 传给 `sn-ppt-design-story`，生成唯一 `outline.md`，不得自行绕过 Story 即兴生产页面。
7. **大纲交互**：Draft 默认继续并告知大纲路径；Standard/Deep 展示整体叙事和大纲路径，本轮调用下方的大纲确认卡并结束。收到批准回执再开始写页。用户原话已明确“大纲不用确认”或“自动完成”时可直接继续。选中设计模式仅决定制作方式。继续前重新读取磁盘上的大纲，并确认 Story 已在同一文件中写出全套页作用链、相邻页承接、重复与缺环的检查结果；缺失时返回 Story 补齐。
8. **规划素材并生成 XML**：调用唯一出口 `sn-ppt-dsl`，传入同一绝对 `DECK_DIR`。先按每页图片承担的作用选择用户素材、可信搜图、生图或原生形状；需要准确辨认的实物不能用模型生成图充当证据，氛围插画不能在生图失败后悄悄改用手绘 SVG。页数、顺序、标题、核心结论以 Story 为准；出口不能重新研究或重排叙事。
9. **导出与验收**：由 `sn-ppt-dsl/scripts/dsl.py` 检查 XML、生成 `presentation.pptx`，再渲染实际 PPTX。逐项处理重叠/溢出错误，检查预览中的字体、换行和遮挡；静态检查成功不等于视觉验收完成。
10. **交付**：交付 PPTX、`slides/` XML 源文件及实际存在的 PDF/预览。登记真实路径；导出和预览检查全部完成才写 `complete`，否则写 `partial` 与具体原因。失败保留 XML、素材和上一份可用 PPTX。

## 客户端大纲确认

Standard / Deep 先展示当前 `outline.md` 的可点击链接和叙事概括。用户已明确跳过大纲确认时进入写页；其余 Standard / Deep 任务在此调用 `request_user_decision`，调用成功即结束本轮。写页在客户端返回 `approve` 后开始。若用户要求亲自确认或必要信息有缺口，省略默认选项、超时和三项安全声明，等待答复。完整大纲可用以下卡片。

```json
{
  "question": "是否按此大纲继续？60 秒内未操作将按当前大纲继续制作；需要修改可直接填写意见。",
  "decision_kind": "outline_confirmation",
  "options": [
    {"id": "approve", "label": "按此大纲继续", "description": "沿用已展示的大纲制作页面。"},
    {"id": "revise", "label": "需要修改", "description": "先修改大纲，再开始制作。"}
  ],
  "default_option_id": "approve",
  "requested_auto_submit_seconds": 60,
  "risk_level": "low",
  "reversible": true,
  "preserves_user_intent": true,
  "allow_freeform": true
}
```

按宿主回执处理 `approve` 或 `revise`。有修改意见时继续编辑同一份大纲。批准后固定本轮页序与核心结论；确需改动已批准的大纲时，把原因交给用户决定。交付时提供最终 PPTX 的可点击文件链接。

## 可选搜索与素材能力

按同级 `sn-ppt-design-tools/references/capability-policy.md` 的 `native → bundled → none` 顺序选择。媒体能力不作为 Entry 的强制前置。缺图时改用文字、形状、原生图表等 XML 版式，不留下空图片框。

生图和搜图仅提供嵌入 XML 的局部素材。按 `sn-ppt-dsl/references/design.md` 逐页判断图片用途，将实际使用的图片、来源、许可或模型写入 `assets/sources.json`；图片可跨页复用。能力状态记录到 `state.capabilities`，只含来源、状态、非敏感错误摘要和时间；不得记录密钥和鉴权头。配置问题调用 `sn-ppt-design-doctor`，不在入口重复配置模型。

## 恢复与迁移

- 读取现有任务包、材料、磁盘大纲和真实产物；不重复已经完成的研究或材料解析。
- 已有 XML：只修改受影响页，再校验、重建并渲染；预览必须对应最新 PPTX。
- 历史任务尚无 XML：复用材料、大纲和素材，在同一任务目录逐页建立 XML。原产物仅作参考，不再按旧生成方式续跑；成功生成新 PPTX 后才更新交付状态。
- 统一任务包的 `ppt_mode=dsl` 与 `choices.output=xml_pptx`，移除不再适用的旧后处理选择。文件缺失时从最早必需阶段补齐，不把历史成品算作本次 XML 导出成功。
- 只缺 PPTX 时重跑 XML 构建；只缺预览时渲染现有最新 PPTX。不能因为状态滞后重写未变的 XML。
- 不创建第二份大纲、任务包或 deck 目录。已有用户文件不因流程迁移而删除。

## 论文限时汇报

- 把主讲、Q&A驻留页和备查附录分别计页；用户给总页数上限时三者都计入上限。用代码加总逐页建议秒数，备注标累计时间，附录不纳入主讲。
- 全文与附录均需阅读，固定arXiv版本；先核对论文表格再重绘，不把搜索摘要、当前仓库结果和论文排行榜混用。保留一份数值JSON，导出后回读PPT原生表格逐格对照原论文。
- 解释评估口径时区分二元通过率和软评分、逐次择优和多次平均、失败案例内占比和全体错误率；把汇报者推论与作者报告分开。
- 每页写可讲述的备注和建议时长，另交付时间表与QA页码索引；明确时长需真人彩排，不能仅由字数声称已实测12分钟。

## 进度与边界

在聊天中报告材料准备、研究、大纲、XML 页面生产、导出和验收进度。长时间执行期间持续给出有意义的阶段更新，不自动启动工作台或浏览器。

事实缺口回到材料/研究/Story 处理，不用示例数字冒充事实。编译器不支持的对象应明确说明，或在不改变内容的前提下用支持的 XML 元素重构；不能跳过校验或换生成流程。Skill 安装目录只读，任务产物全部写入 `DECK_DIR`。
