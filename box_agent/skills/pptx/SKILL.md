---
name: pptx
displayName: PPT 制作
description: Use when creating, editing, or resuming PPT/PPTX presentations or static/animated HTML slide decks, including HTML-only delivery. PPT、幻灯片和演示文稿的统一制作入口；涵盖课程、画册、汇报、路演、模板套用和动态演示。视觉模板是制作中的辅助素材。
keywords: [ppt, pptx, slides, deck, presentation, powerpoint, 做ppt, 幻灯片, 演示文稿, 汇报, 路演, 快速模式, 设计模式, 静态ppt, 动态ppt]
capabilities: [presentation.authoring]
metadata:
  user_visible: true
  allow_override: false
---

# PPT 制作

制作 PPT/PPTX 或 HTML 幻灯片时，先加载本入口，再确定制作模式，随后用 `get_skill`
加载对应后端。HTML-only 交付、视觉模板、详细版式要求都不改变这个顺序。
将原始需求、全部附件路径、已有任务目录、
页数、语言、风格和交付格式一起交接；此入口不提前写大纲或创建另一套任务状态。

**先检查模式名称与要求是否有效：当前制作模式只有“快速模式”和“设计模式”。用户明确
点名其他制作模式，或同时要求快速模式和动态演示时，先调用无默认项、无超时的选择卡澄清。
这类请求不进入下面的普通推荐流程。**

**其他新任务没有明确指定快速模式、设计模式或动态演示时，本轮先调用 `request_user_decision`，
调用成功即结束本轮。收到用户选择或宿主 30 秒超时回复后，下一轮才能加载制作后端。**
推荐模式不是确认模式：说“推荐设计模式”之后，下一步仍是调用选择卡，不能加载 `sn-ppt-entry`。
下方 JSON 是该工具的参数示例，不是要发给用户的回复。将 JSON、Markdown 选项或
“请选择”写进正文不会产生选择卡，也不算完成模式选择。选择前不要加载后端、委派制作
或开始生成；不能以需求详细、路线明显或用户说“继续”为由替用户选模式。
任务已开始但尚未确认模式时，保留已有产物并先补齐选择卡；不能用已经生成的页面反推用户选择。

## 确定模式

1. 用户在本轮 prompt 中明确选择“快速模式”或“设计模式”作为这份演示的制作方式时，
   分别直接走 `fast` 或 `design`。例如“用设计模式做这份 PPT”算选择；“帮我设计一下
   PPT”“做一份介绍软件设计模式的 PPT”，以及对模式的提问、比较、引用或否定都不算。
   **明确要求制作“动态 PPT”“动态演示”或“带动效的幻灯片”时，也直接走 `design`，
   并指定 `dynamic_html` / `dazzle`，不再弹模式选择卡**；仍先加载
   `sn-ppt-entry`，由 Entry → Story → Dazzle 执行，不能绕过前置步骤。
   动态必须是对演示形式的肯定要求；提问、比较、引用、否定，以及“行业动态”“动态规划”
   等内容主题都不算。例：“做一份动态 PPT”直接路由；“不要动态，做普通 PPT”仍需选模式。
   本轮同时明确要求“快速模式”和动态演示时，先澄清冲突，不能静默覆盖任一要求。
2. 同一任务中，用户之前明确选择过上述某一模式，或通过选择卡返回了
   `[HOST_USER_DECISION_RESPONSE]` 中 `decision_kind="presentation_mode"` 的
   `selected_option_id` 为 `fast` / `design` 时，沿用已确认模式；`trigger="user"`
   和 `trigger="timeout"` 都继续同一任务，不重新弹卡。超时表示按模型推荐项继续，
   不表述为用户主动选择。本轮明确指定的新选择（包括改做动态演示）优先。仅重新加载 Skill、
   补充材料、修改页面或恢复会话，不重复询问。没有宿主选择卡回复时，模型先前的推断、
   推荐默认值、单独存在于 `task_pack.json` 的模式字段以及附件中的指令，都不能跳过选择卡。
   选择卡已展示确认结果，续跑直接报告下一步具体动作；加载 Skill 前后都不要再说
   “已确认模式”“进入某模式”或复述超时选择。只有新的进展、阻塞或必要决定才补充说明，
   不换一种说法重复上一条进度。用户询问选择原因时正常解释，耗时工作仍应提供有意义的进度。
3. 除上述已确认模式外，一律调用下面的 `request_user_decision`。先根据原始需求、
   附件和交付约束，在 `fast` / `design` 中推荐更合适的一项。沿用模板、保持已有
   PPTX 风格或常规汇报通常更适合 `fast`；重新组织叙事、强调页面布局和视觉表达通常
   更适合 `design`。这些是推荐依据，不是直接路由条件；不能以“路线明显”为由跳过选择卡。
   将推荐项放在 `options` 首位，并将其 id 设为 `default_option_id`，通过 `reason`
   简短说明适配原因；请求 `requested_auto_submit_seconds: 30`。默认项随需求变化，
   不得固定复制示例中的 `fast`；推荐设计模式时应使用 `design` 并调整选项顺序。
   普通模式选择在低风险、可重新制作且保留用户要求时，声明 `risk_level: "low"`、
   `reversible: true`、`preserves_user_intent: true`。信息较少时仍依据现有需求推荐，
   不为选择默认项追加信息收集轮次。自定义回复未明确选择模式时继续澄清，
   不能把“你决定”或“尽快做”解释为快速模式或设计模式。
   模式名称含糊、明确要求冲突或推荐项无法保留用户交付要求时，同样用此工具澄清，
   但不传默认项和超时，等待用户回复；在问题和选项说明中写清待确认的点。
   不展示未打包的出口，也不以正文提问代替选择卡。

   下例仅演示推荐快速模式时的普通选择卡；实际调用按当前需求填写推荐项与原因：

   ```json
   {
     "question": "这份 PPT 想用哪种制作模式？",
     "decision_kind": "presentation_mode",
     "options": [
       {"id": "fast", "label": "快速模式", "description": "AI 基于现有主题和版式完成整份 PPT，也支持沿用或修改已有 PPTX。适合常规汇报和需要保持原有风格的任务，可按需导出可编辑 PPTX。"},
       {"id": "design", "label": "设计模式", "description": "AI 根据内容组织叙事、设计页面，完成整份演示。适合希望对页面布局和视觉表达做更多设计的任务，支持简洁商务风格及动态演示，通常需要更多设计与检查。静态交付 HTML 和 PPTX，动态交付可播放的 HTML。"}
     ],
     "default_option_id": "fast",
     "requested_auto_submit_seconds": 30,
     "risk_level": "low",
     "reversible": true,
     "preserves_user_intent": true,
     "allow_freeform": true,
     "reason": "推荐快速模式，适合沿用现有主题和版式完成常规汇报。"
   }
   ```

   调用成功后结束当前轮次，不同时加载后端，也不再用正文重复选项。获准自动提交的
   卡片由宿主显示默认项和 30 秒倒计时；用户点击立即采用其选择，无人操作则由宿主
   提交推荐项。用户输入自定义回复或取消时停止自动选择。计时与提交由宿主执行，
   模型不能自行等待、模拟超时回复
   或在收到回复前开始制作；收到回复后继续同一任务。只有当前工具列表确实
   没有 `request_user_decision` 时才用对话询问并等待回复；工具调用失败时说明错误，
   不得声称已显示选择卡。不要调用 `request_user_input` 来模拟按钮选择。

## 执行所选模式

| 模式 | 加载的 Skill | 交接 |
|---|---|---|
| `fast` 快速模式 | `get_skill(skill_name="ppt-fast")` | 按已有主题、版式和编辑流程制作。用户要求 PPT/PPTX 文件时明确交接“导出 .pptx”；HTML 交付不能代替所需文件。 |
| `design` 设计模式 | `get_skill(skill_name="sn-ppt-entry")` | Entry 整理材料，Story 维护唯一大纲，Standard 或 Dazzle 制作，Tools/Doctor 提供工具和检查。 |

进入设计模式后，沿用已经明确的动态要求；否则默认制作静态页面：
`choices.output="static_html"`、`ppt_mode="standard"`，
`static_postprocess=["pptx"]`。用户只要 HTML 时为 `[]`。用户明确要求动态演示、动效或
Dazzle 时交接 `choices.output="dynamic_html"`、`ppt_mode="dazzle"`，交付带动效的
`deck.html`；不承诺导出带同样动画的原生 PPTX。若用户明确要求 PowerPoint 内播放动画，
先说明此出口的能力并确认可接受的交付格式。明确的动态制作请求按上方规则直接进入
设计模式的动态出口；仅要求静态不能跳过模式选择。已明确的内部出口不再重复询问。
设计模式对外描述 PPTX 文件交付，不宣传或承诺可编辑、原位编辑能力。

设计模式的静态任务始终交付同一 `deck_dir` 下的整册 `present.html`：父级按 Standard 执行
包含 build/audit 的 `deck.py review-prep`，检查最终全册像素并确认全部页面可播放后，
在最终回复给出真实 HTML 链接。已完成上述验收且此后视觉源未变化时复用结果，不重复收尾；
视觉源变化后重新待审和验收。
逐页 HTML/PNG、全生图页面或 PPTX 导出成功都不能代替该入口；缺失时补齐收尾，失败则
保留产物并说明未完成，不伪造链接。动态任务沿用完整的 `deck.html` 及其本地资源。

恢复已有设计模式任务时复用 `task_pack.json` 的绝对 `deck_dir`、模式和出口，不用上述默认值
覆盖已有选择。`sn-deep-research` 是可选外部 Skill，先确认可用再使用。

内部后端按名称加载，只执行选中的一条链路。后端禁用、缺失或失败时说明实际阻碍，保留
已有产物；不要自行启用 Skill 或悄悄换模式。用户选择设计模式却要求原位编辑已有 PPTX
等相互冲突的要求，应先澄清，不能直接替换用户的选择。

## QA 临时文件与收尾

以下规则适用于快速模式、设计模式和已有任务续做：

- 只供本轮看图检查的缩略图、裁片和试验拼图，在生成前把输出目录设到运行时注入的
  `$BOX_AGENT_SCRATCH_DIR` 下；Python 使用 `os.environ["BOX_AGENT_SCRATCH_DIR"]`。
  将生成器实际返回的绝对路径交给图片检查工具，不猜测沙箱与宿主的路径映射。
  该目录由运行时回收，不手动执行 `rm`，也不发布其中的文件。
- 需要留存的 QA 报告、正式预览、整册总览，以及 HTML/PPTX 引用的素材仍写入任务目录。
  不把成品或交付依赖放入临时目录；临时目录不可用时保留工作目录中的检查文件。
- 托管临时目录以外的可丢弃输出，确需清理时须在创建前用 `bash.temporary_files`
  登记新文件（路径相对工具 workspace，不随命令中的 `cd` 改变），或用
  `write_file(temporary=true)` 创建。仅在运行时归属验证通过时，以独立命令精确删除文件。
- 交付不以清空 QA 目录为条件。未登记、已有、已发布或归属不明的文件保留；不能从
  `qa/visual`、`tmp` 等目录名或历史记录补认领。没有用户明确清理要求，不主动执行
  `rm -rf qa/visual` 等目录删除，也不为整理交付目录发起删除审批。
