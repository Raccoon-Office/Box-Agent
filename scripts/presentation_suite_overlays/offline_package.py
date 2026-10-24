"""Adapt every shipped presentation route to the dedicated offline package."""

import re


BOUNDARY = """
## 离线包执行边界

本分支专供离线包，保留快速、设计静态和设计动态模式。只有模型推理和本地文件/工具
可用；不调用网页搜索、图片搜索、生图、远程下载或依赖安装，不做连通性探测或重试。
恢复任务也直接将联网能力记为 unavailable，不继续历史待执行的联网步骤。
只用用户提供的本地材料、图片与随包字体/库。外部事实缺口标为“待补充（离线未核验）”，
不伪造引用、数据或真实对象图片。没有图片时重排为文字、图表、SVG/Canvas 与本地形状，
保留并披露明确未满足的图片需求，不留下破图或虚假完成状态。不得引入 CDN、远程字体、
脚本、样式或图片；引用链接只是文字证据，不抓取。此边界优先于下文历史联网操作说明，
并须原样交接给每个子任务；独立推理角色仍可使用同一宿主模型和本地工具。
视觉检查只能使用现有模型的图像输入能力；不可用时记录 visual_unverified，不调用代理
视觉服务、不配置其他模型、不重试。实际文件、页数、内容、渲染与资源检查照常执行。

"""

LOCAL_MEDIA = """## 4. 本地素材与来源账本

离线包跳过生图、Serper 搜图和 fetch。只复用 Entry 交接的本地附件或从本地文档提取的图。
父级复制素材到 `DECK_DIR/assets/` 后，用原有 `deck.py asset-register` 登记
`--origin attachment --source-path '<实际附件路径>'`，随后按原计划 asset-assign。
派生素材保留真实 parent asset；不把附件标为 downloaded/generated。无素材时在制作前
调整相应版式，不要求 Image 子任务获取不存在的图片，也不伪造 ready。
图像理解不可用则如实记录未核验，保留身份/裁切不确定性，不能写已经看图。

复制完成后，设置实际的 DECK_DIR、ASSET_PATH（assets/ 下相对路径）、SOURCE_PATH（原始附件
路径）、ASSET_ID 和 GROUP_ID，再按下面的失败即停顺序执行，不给来源字段填虚构 URL/模型：

```bash
python "<SKILL_ROOT>/scripts/deck.py" asset-register "$DECK_DIR" --path "$ASSET_PATH" --origin attachment --source-path "$SOURCE_PATH" &&
python "<SKILL_ROOT>/scripts/deck.py" asset-assign "$DECK_DIR" --path "$ASSET_PATH" --asset-id "$ASSET_ID" --group-id "$GROUP_ID" &&
python "<SKILL_ROOT>/scripts/deck.py" asset-contact "$DECK_DIR" --group-id "$GROUP_ID"
```

"""

VISUAL_LIMIT = """
### 离线包缺少图像理解时的交付

仍运行本地 render、review-prep、内容/页数/资源/占位符检查。存在已知渲染或内容硬伤时
继续修复或交付 partial，不得利用视觉不可用掩盖错误。如果唯一未完成项是模型看图，
在原 `_trace/review-issues.md` 的唯一 Final review contract 中准确记录：
`status: partial`、`final_pixels_inspected: no`、`remaining: visual_unverified`。
该离线合同允许 Standard exporter 导出当前页面的 PPTX，并输出未完成视觉检查的警告。
不使用 --force、不伪写 ready/PASS；task_pack 和最终交付仍为 partial，列出未验页面。
已有真实看图能力时保持正常 ready/yes/none 合同及原质量门。

"""


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Offline overlay needs review: {old[:100]!r}")
    return text.replace(old, new, 1)


def section(text, start, end, replacement):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"Offline section needs review: {start!r}")
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    return before + replacement + end + after


def apply(relative, data):
    if not relative.startswith("skills/") or not relative.endswith((".md", ".py", ".mjs")):
        return data
    text = data.decode("utf-8")
    if relative.endswith("/SKILL.md") or "/subagents/" in relative:
        heading = re.search(r"(?m)^# [^\n]+\n", text)
        if not heading or "## 离线包执行边界" in text:
            raise ValueError(f"Offline instruction overlay needs review: {relative}")
        text = text[:heading.end()] + BOUNDARY + text[heading.end():]
    if relative == "skills/sn-ppt-entry/SKILL.md":
        text = replace(text, "统一接收 PPT 生成请求，建立唯一任务目录，准备材料。Standard 先完成定向外部证据补充，\nDeep 先完成完整 Research，", "统一接收 PPT 生成请求，建立唯一任务目录，准备本地材料。Standard 和 Deep 做材料内证据整理，")
        text = section(text, "### 执行深度", "### 输出格式", """### 执行深度

保留 Draft / Standard / Deep 选择。离线时三者都只使用本地材料：Draft 快速成稿，
Standard 做材料内证据整理，Deep 深入交叉分析已提供的本地文件。Deep 不代表联网研究。
说明事实覆盖边界；不加载外部 Research Skill，不为缺少联网能力要求重选模式。
Standard / Deep 仍保持原有大纲确认，其他用户选择和输出格式不变。

""")
        text = section(text, "## 可选搜索与图像能力", "## 大纲确认", """## 离线能力记录

读取同级 `sn-ppt-tools/references/capability-policy.md`，直接使用无联网能力路径。
在 `task_pack.state.capabilities` 中将 web_search/image_search/image_generate 记为
`source=none`、`status=unavailable`、`last_error=offline_package`，不发起测试请求。
不得要求配置搜索或生图密钥；缺少本地渲染依赖时报告离线安装包不完整。

""")
        text = section(text, "7. **决定外部证据路径**：", "9. **调用 Story**：", """7. **固定离线证据路径**：所有执行深度均写 `required=false`、`executor=null`、
   `mode=null`、`skipped_reason=offline_package`，不使用联网 Research。
8. **完成材料内证据整理**：保留本地文件来源、页码、日期与冲突。缺少外部/时效事实时
   省略可选断言，必需内容标为“待补充（离线未核验）”，记录到 open_questions 后继续。
""")
        text = text.replace('"image_source": "auto"', '"image_source": "local_only"')
    if relative == "skills/sn-ppt-tools/references/capability-policy.md":
        text = "# PPT 离线能力规则\n" + BOUNDARY + """
web_search、image_search、image_generate 直接记为 source=none、status=unavailable、
last_error=offline_package。不要按 native -> bundled 顺序试用，不读取在线工具配置。
本地图片复制/文档提取、字体子集、图表、渲染和导出保持可用。缺少依赖由打包方补齐，
不要求终端用户联网安装。无图片时重排页面；不保留空框、破图或“待生成”文字。
"""
    if relative == "skills/sn-ppt-tools/SKILL.md":
        frontmatter = text.split("---", 2)
        text = "---" + frontmatter[1] + "---\n\n# sn-ppt-tools\n" + BOUNDARY + """
本离线包禁用这套联网备用工具。不要调用 web_search.py、image_search.py、fetch_image.py、
image_generate.py，不读取或配置 API key。直接读取 `references/capability-policy.md`
并继续本地材料流程；不把能力缺失误认为需要重试的服务故障。
"""
    if relative == "skills/sn-ppt-standard/references/box-agent-tool-contract.md":
        text = section(text, "## 4. 生图、搜图与来源账本", "## 5. 父级渲染闭环", LOCAL_MEDIA)
        text = BOUNDARY + VISUAL_LIMIT + text
    if relative == "skills/sn-ppt-story/SKILL.md":
        text = replace(text, "- 不直接搜索网页；外部证据由 Entry 在 Story 之前完成：Standard 使用定向普通搜索，\n  Deep 使用 `sn-deep-research`。",
                       "- 不搜索网页或要求 Entry 补联网证据；所有执行深度只分析已有本地材料。")
    if relative == "skills/sn-ppt-standard/SKILL.md":
        text = text.replace("`sub_agent`、`inspect_images`、`generate_image`、`bash`", "`sub_agent`、`inspect_images`、`bash`")
        text = text.replace("获取或生成位图素材，返回实际路径", "整理已有本地素材，返回实际路径")
        text = text.replace("氛围、隐喻、故事场景、视觉主画面：生成图或高质量位图", "氛围、隐喻、故事场景、视觉主画面：已有本地位图或 Canvas/HTML")
        text = section(text, "`image_opportunity_map` 必须先做一次", "背景不等于一块纯色", """`image_opportunity_map` 先检查可复用的本地人物、产品、场地、作品和证据图片。
有符合要求的图片时仍登记、核对来源并保持统一裁切；用户明确要求展示的附件不可无故省略。
缺少符合要求的本地素材时直接选择有明确焦点的排印、图表或 Canvas/HTML，不要求搜索或生图，
不伪造人物、品牌、产品、事件照片，不以缺少位图为由阻塞整册。明确的图片要求仍需披露未满足。
计划中保持原 `attachment_visual_map`、`image_opportunity_map` 和 `plan/image-strategy.json`：
有图时保持 `images_required` 与实际页面列表；整册无图时使用 `bitmap_exception`，
写 `visible_subject_scan_complete: true`、全部 `reviewed_pages`、不少于 20 字的真实
`exception_reason`。按实际内容选择原有 `pure_typography | pure_chart | wireframe | accuracy_critical`
理由；缺少真实素材不能冒充用户要求无图，也不设图片数量下限。所有事实、页数与来源仍保留。

""")
        text = text.replace("或由 Image 统一生成的背景", "或已有本地背景图")
        text = text.replace("失败素材先换可行的真实图或生成图路线，确实不可得时才改为 Canvas 或排版降级", "失败素材先换已有合适本地图；没有时改为 Canvas 或排版降级")
        text = text.replace("首次部署依赖解析 venv、PyMuPDF、可分发字体、FontTools/Brotli 和 Playwright Chromium；用 `scripts/install.sh` 安装。",
                            "打包方预置 Python、PyMuPDF、可分发字体、FontTools/Brotli 和 Playwright Chromium；任务中不运行 `scripts/install.sh`。")
    if relative == "skills/sn-ppt-standard/SKILL.md" or relative == "skills/sn-ppt-standard/subagents/review.md":
        text += VISUAL_LIMIT
    if relative == "skills/sn-ppt-standard/subagents/image.md":
        text = section(text, "## 3. 媒介分流", "## 4. 工作流", """## 3. 本地媒介分流

真实人物、地点、品牌、产品和事件只用已提供且来源明确的本地图片，不生成替代照片。
已有插画与背景可复用，保留用途、裁切与来源。没有合适素材则报告缺口，由父级调整版式。
数据图表、流程、架构与关系图交给 Slide 使用本地图表库或 Canvas + HTML 标签。

""")
        text = text.replace("未读完前不开始取图或生图", "未读完前不开始处理本地素材")
        text = text.replace("- 网图必须下载到本地，不在页面中 hotlink。", "- 只引用本地文件，不下载网图，也不在页面中 hotlink。")
        text = re.sub(r"(?m)^- 不用 `mv` / `cp` 给图片工具返回的结果私自改名.*$",
                      "- 复制附件或生成裁片后用 `deck.py asset-register` 登记原始路径及 parent asset；不丢失来源。", text)
        text = text.replace("origin: downloaded | generated | material | derived", "origin: attachment | derived")
        text = text.replace("source: <下载 URL | 用户附件路径 | parent asset | generator model>", "source: <用户附件路径 | parent asset>")
        text = text.replace("    prompt: <生成素材的原始 prompt；非生成素材省略>\n", "")
        text = section(text, "## 4. 工作流", "## 5. 质量与红线", """## 4. 离线素材工作流

1. 读取计划和 Entry 交接的实际本地素材路径，保留素材用途、裁切与来源约束。
2. 只复制已有图片或提取本地文档的真实 Figure；不搜索、不下载、不生成替代照片。
3. 需要父级登记或本地裁切时，一次交回实际候选与待办，使用 pending_parent_verification。
   父级按工具契约用 asset-register/asset-assign 维护来源，不伪造 ready。
4. 无符合要求的本地素材时，报告缺失对象和可行的文字/图表版式，由父级回填原计划；
   明确未满足的图片要求保留在交付说明。没有视觉推理时不得声称已核实身份/裁切。

""")
    if relative == "skills/sn-ppt-dazzle/SKILL.md":
        text = text.replace("允许 CDN：**Three.js r128**、GSAP、ECharts、D3、Google Fonts。", "禁止 CDN；动效使用原生 CSS、SVG、Canvas 或已随包提供的本地库。")
        text = text.replace("head（Google Fonts / CDN）", "head（本地字体 / 本地脚本）")
        text = text.replace("中文必须 Google Fonts 引入", "中文必须使用本地")
        text = text.replace("含中文必须 Google Fonts 引入中文字体", "含中文必须使用随包或系统已有的中文字体")
        text = text.replace("禁止只依赖 PingFang SC / Microsoft YaHei", "本地字体缺失时可使用 PingFang SC / Microsoft YaHei 等已安装字体并记录替代")
        text = section(text, "## 6. 配图与生图（可选能力可用时）", "## 7. 美学底线", """## 6. 本地配图

只用用户提供的图片或本地文档提取图，复制到 assets/ 并保留来源；缺少图片时用可编辑
排版、SVG/Canvas 和图表表达，不伪造人物、产品或事件的照片。并列对象统一采用实际
可交付的视觉方案。使用现有模型看图能力时只核对本地图片，不转用其他线上服务。

""")
        text = section(text, "### 阶段 2 —— 生成素材", "### 阶段 3 ——", """### 阶段 2 —— 准备本地素材

按 §6 复制已有素材并登记路径。不生成、不下载；缺少素材时在当前计划中明确无图表达。

""")
        text = re.sub(r"(?m)^2\. \*\*Three\.js 锁 r128\*\*：.*$", "2. **离线动效**：优先原生 CSS/SVG/Canvas。只有已随包提供的库才可使用；没有 Three.js/GSAP/D3 时不要规划依赖它们的效果，不从 CDN 下载。", text)
        text = text.replace('需要的写出生图 prompt 草稿与用途', '需要的列出已有本地文件路径与用途')
    if relative.startswith("skills/sn-ppt-tools/scripts/") and relative.endswith(("web_search.py", "image_search.py", "image_generate.py", "fetch_image.py")):
        # Keep parsing/format helpers for compatibility, but the shipped entry cannot make requests.
        start = text.index("def main(")
        end = text.index('\n\nif __name__ == "__main__":', start)
        signature = text[start:text.index('\n', start)]
        text = text[:start] + signature + '\n    _emit("unavailable", reason="offline_package")\n    return 2\n' + text[end:]
    if relative == "skills/sn-ppt-standard/scripts/serper_images.py":
        start = text.index("def main()")
        end = text.index('\n\nif __name__ == "__main__":', start)
        text = text[:start] + 'def main() -> int:\n    print(json.dumps({"status": "unavailable", "reason": "offline_package"}))\n    return 2\n' + text[end:]
    if relative == "skills/sn-ppt-dazzle/scripts/render_deck.py":
        text = replace(text, "            bypass_csp=True,", "            bypass_csp=True,\n            offline=True,")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/dom_extractor.mjs":
        text = replace(text, "const page = await browser.newPage();", "const page = await browser.newPage({ offline: true });")
        text = replace(text, "  // Page-local ECharts remains usable when the bundled dependency is absent.",
                       "  bundledEchartsSource = readFileSync(new URL('../../../assets/vendor/echarts.min.js', import.meta.url), 'utf8');")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/pptx_builder.mjs":
        text = replace(text, "import PptxGenJS from 'pptxgenjs';",
                       "import { createRequire } from 'node:module';\nconst PptxGenJS = createRequire(import.meta.url)('pptxgenjs');")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/postprocess_pptx.mjs":
        text = replace(text, "import JSZip from 'jszip';",
                       "import { createRequire } from 'node:module';\nconst JSZip = createRequire(import.meta.url)('jszip');")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs":
        text = replace(text, "import { execSync } from 'node:child_process';",
                       "import { createRequire } from 'node:module';")
        text = section(text, "/**\n * 首次运行时自动安装依赖", "// These helpers", '''// Offline package: validate provisioned dependencies; never run installers.
async function ensureDependencies() {
  const require = createRequire(import.meta.url);
  for (const name of ['pptxgenjs', 'jszip', process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || 'playwright']) {
    try { require.resolve(name); }
    catch { throw new Error(`Offline package incomplete: missing ${name}; provision dependencies before shipping`); }
  }
  if (!existsSync(resolve(__dirname, '../../assets/vendor/echarts.min.js'))) {
    throw new Error('Offline package incomplete: missing bundled ECharts');
  }
  const { pickBrowserExe } = await import('./lib/browser_picker.mjs');
  const exe = pickBrowserExe();
  if (!exe || !existsSync(exe)) {
    throw new Error('Offline package incomplete: missing Chromium; bundle the browser before shipping');
  }
}

''')
        text = replace(text, "  if (!args.batch) {\n    await downloadRemoteImages(args.deckDir, htmlFiles);\n  }",
                       "  await downloadRemoteImages(args.deckDir, htmlFiles);")
        text = text.replace("// 确保依赖安装（npm + playwright chromium）。", "// 检查随包依赖；不执行安装或下载。")
        text = text.replace("// 下载与导出使用同一组页面，资源路径相对于各自 HTML 所在目录。", "// 检查待导出页面的资源均来自本地，不抓取远程素材。")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/image_downloader.mjs":
        text = '''import { readFileSync } from 'node:fs';

// Preserve the caller contract, but never fetch or rewrite remote media offline.
export async function downloadRemoteImages(deckDir, htmlFiles) {
  for (const htmlPath of htmlFiles) {
    const html = readFileSync(htmlPath, 'utf-8');
    const remote = /<(?:img|script|iframe|source|video|audio)\\b[^>]*\\bsrc\\s*=\\s*["'](?:https?:)?\\/\\//i.test(html)
      || /<link\\b[^>]*\\bhref\\s*=\\s*["'](?:https?:)?\\/\\//i.test(html)
      || /url\\(\\s*["']?(?:https?:)?\\/\\//i.test(html)
      || /@import\\s+["'](?:https?:)?\\/\\//i.test(html);
    if (remote) throw new Error(`Offline package: replace remote resources with local files in ${htmlPath}`);
  }
}
'''
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/cli_guards.mjs":
        marker = "    for (const [key, required] of Object.entries({"
        code = '''    const offlinePartial = { status: 'partial', final_pixels_inspected: 'no', remaining: 'visual_unverified' };
    if (Object.entries(offlinePartial).every(([key, expected]) => {
      const values = [...section.matchAll(new RegExp(`^[ \\t]*(?:-[ \\t]+)?${key}:[ \\t]*([^\\r\\n]*)\\r?$`, 'gm'))];
      return values.length === 1 && values[0][1].trim() === expected;
    })) {
      process.stderr.write('[WARN] offline visual_unverified: exporting a partial draft without claiming pixel review\\n');
      return;
    }
'''
        text = replace(text, marker, code + marker)
    return (text.rstrip() + "\n").encode("utf-8") if text != data.decode("utf-8") else data
