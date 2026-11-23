---
name: sn-ppt-dsl
description: 为 SenseNova PPT 任务生成或修改采用 XML 坐标布局的本地可编辑 PPTX；复用重叠、遮挡、溢出校验。用于 Entry 和 Story 已完成后的 PPTX 出口，以及恢复已有 XML 页面任务。
metadata:
  user_visible: false
  allow_override: false
  project: SenseNova-Skills-local
  category: scene
---

# 本地 XML PPT

使用 SML XML 描述自由坐标布局，程序检查后直接导出可编辑 PPTX。所有生成、检查与导出均在本地完成，不使用飞书、Kimi 网页服务或额外文字模型接口。

将本文件所在目录记为绝对 `SKILL_ROOT`，Entry 传入的现有任务目录记为绝对 `DECK_DIR`。所有脚本使用绝对路径。新请求缺少 `task_pack.json`、`info_pack.json` 或 `outline.md` 时先调用同级 `sn-ppt-design-entry` 与 `sn-ppt-design-story`；已有完整 XML 项目可以直接修改、检查和重建。不得另建第二份大纲或任务目录。

## 开始前

1. 读取当前任务包、材料索引、唯一 `outline.md`、已有页面与素材；保留大纲确定的页数、顺序、标题和核心结论。首次写 XML 前，确认新大纲已经写明逐页作用与整套叙事检查；缺失时返回 Story 补齐，不能由出口即兴重排。
2. 首次写 XML 时读取 [本地格式与能力](references/xml-local.md)，需要详细语法时查看 `vendor/doubao/references/xml/xml-schema-quick-ref.md`。本地支持范围以 `xml-local.md` 和编译器报错为准；上游规范中的飞书命令和云端资源步骤不执行。
3. 读取 [设计与素材](references/design.md)，按其中的图片用途规则规划每页素材，再按场景选择一份设计参考。设计风格可以参考 Kimi，但源文件始终使用 XML，不生成 PPTD。
4. 当前 Agent 逐页完成任务。除非用户明确要求，不使用 subagent。使用宿主模型的文字/视觉能力；搜索和生图按同级 `sn-ppt-design-tools/references/capability-policy.md` 路由，不新配模型接口。

## 逐页生产

页面源文件固定为 `DECK_DIR/slides/slide_01.xml`、`slide_02.xml` 等。每个文件只有一个 `<slide>`；图片放入 `DECK_DIR/assets/`，XML 用相对本页文件的路径，例如 `../assets/photo.png`。对每张实际引用的图片，在 `assets/sources.json` 登记文件、用途角色与真实来源。颜色、字体和网格全册统一；每页保留演讲者备注。整理原生生图和附件图片时，在登记素材的同一条 Bash 命令中运行 `python "<SKILL_ROOT>/scripts/review_image.py" "<DECK_DIR>"`。把返回的 `inspect` 路径用于看素材，XML 始终引用原图。备用下载和生图脚本会直接返回这两个路径。

先按 references/design.md 确定并记录全册字阶、每页信息分组与阅读顺序，再填写坐标；成品按 references/qa.md 的层级与阅读顺序逐页判断。允许有意叠放，但正文相互遮挡、内容裁切、主体超出画布必须修复。不要靠无限缩小字号、删除证据或整页截图掩盖问题。

先选一个能代表当前页面结构的真实页写完并检查。通过后，可在一次写稿动作中完成几个已规划且结构相近的页面，一次检查这些页。每页仍使用大纲为它安排的构图和内容；遇到新的表格、图表或复杂布局，先验证其第一张。页数由内容与单次输出容量决定。

```bash
python "<SKILL_ROOT>/scripts/dsl.py" check "<DECK_DIR>" --pages 1
python "<SKILL_ROOT>/scripts/dsl.py" check "<DECK_DIR>" --pages 3 4 6
```

每页独立返回结果。只修失败页，根据其错误和报告中的 `hint`、`related_objects`、`measurement` 定位，修后检查相应页码。需修改公共生成代码时，一并复查它实际改写的页。清零后再复用该结构。警告逐条判断，不把告警总数当美学评分。对确有意图的叠放应通过重新组织元素消除歧义，或报告当前检查器的限制；不得删除校验代码、忽略退出码或绕过导出入口。

## 构建与成品检查

```bash
python "<SKILL_ROOT>/scripts/dsl.py" build "<DECK_DIR>"
python "<SKILL_ROOT>/scripts/dsl.py" preview "<DECK_DIR>/presentation.pptx"
```

`build` 会重新检查整套 XML，验证本地导出支持的属性和素材，生成后检查实际 PPTX 页数、原生文字与对象，并核对原生图表的源数据、导出缓存与内嵌工作簿。成功后原子替换 `presentation.pptx` 并使旧视觉记录失效；失败保留之前的成品。

`preview` 使用 LibreOffice 渲染实际 PPTX，逐页核对中文、英文、拼音、数字和单位的保真；缺失详情写入 `qa/rendered-text.json`。它输出 PDF、逐页原始 PNG、可直接看图并登记的 JPG、整册总览、清晰的分批审阅图和当前预览目录的 `manifest.json`。首次预览写入 `preview/`，PPTX 重建后默认使用新版本目录，不覆盖已看过的图片。按 [成品验收](references/qa.md) 看总览与分批图，只对风险页和可疑区域看原尺寸；实际看过后用 `dsl.py review ... --pages 1 2 --status pass --finding "标题正文清楚，无遮挡或裁切" --evidence-image review_batch_01.jpg` 记录结论；风险页必须提供预览返回的对应单页 JPG。记录绑定当前 PPTX 与图片哈希，页面和所用审阅图都未变时可复用结论。若渲染依赖不可用，保留可用 PPTX，状态记为 `partial`，说明未完成视觉验收，不伪造预览。

全页看图时，按 [成品验收](references/qa.md) 的“成品逻辑复核”核对实际结论与依据、图示关系及全册承接；已有大纲自审和程序通过不能替代这一步。逻辑疑点按同一返修流程记录、修复并复验，未解决时保留 `partial`。

不自动启动浏览器、在线编辑器或工作台。直接用宿主文件/图像查看能力检查生成结果。

预览后看清所有分批图及风险页原图。在当前成品上用 `dsl.py review` 记录实际看过的页与具体观察。发现视觉问题，直接修改该页的 XML 或素材，对任务目录执行 `build`，成功后执行 `preview`，查看返回目录中的新图并登记。构建自动记录受影响页，包括共用素材的所有引用页。可先用 `review --status fix` 留下观察，既有 `repair-start` 命令仍可使用。每页最多两轮返修。构建失败时，上一次可用成品仍然保留；先处理构建错误，再审新版。预算耗尽且缺陷仍在，交付现有成品并注明具体页和问题。在已有 PPTX 上继续补审时，未导出的页面修改会单列；本次结论只属于该 PPTX。需要取回生成其中某页的 XML，可用 `dsl.py page-source presentation.pptx --pages N` 查看只读副本。要交付新修改就直接重建并看新版。

## 修改、恢复与交付

- XML 是唯一页面基线。局部编辑只改相关 XML，复用已有素材；无需再次检索、生图或重写大纲。
- 改动后重新检查对应页面，再重建整份 PPTX。未改的 XML 不重写。最终预览必须对应最新 PPTX；旧图片不能作为新版本的验收依据。
- 交付 `presentation.pptx`，同时提供 XML 源目录、PDF/预览（存在时）。附带一句验证范围；不要声称 XML 校验等于 PowerPoint/WPS 全平台验收。
- 更新任务状态：只有生成、逐页保真、全页视觉记录和返修记录都通过，且 `dsl.py verify` 返回 `visual_review=passed` 后，调用 `publish_artifact`；Harness 核对实际看图记录并决定能否记 `complete`。未解决问题或缺少看图记录时，记 `partial` 和具体页码；保留可用 PPTX。
- SN PPT 统一使用 XML → PPTX。历史任务只有旧页面或整页图片时，复用材料、大纲和局部素材，在原任务目录建立 XML 后继续；旧产物仅作参考，不调用其他生成流程。

## 运行环境

桌面端使用宿主 Python、Node.js 和随包安装的锁定依赖。字体位于 `assets/fonts/`，默认使用 Noto Sans CJK SC。`dsl.py preview` 调用宿主提供的 LibreOffice 和 Poppler，从回渲 PDF 核对实际文字，再生成供看图的页面。PPTX 不嵌入字体；在其他电脑上编辑时可安装同一字体，或改用目标电脑已有字体后重新看图。
