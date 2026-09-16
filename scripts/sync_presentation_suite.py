"""Vendor a pinned SN revision without copying its working tree or local state.

Run with --source-checkout /path/to/sensenova-presentation-int. To update the
bundle, pass the reviewed full --revision, then regenerate the skills manifest.
Maintain SN methods upstream; this copy selects six modules and applies explicit
host integration overlays. Every overlay checks its expected source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import tempfile

import yaml


PINNED_REVISION = "e187295633eb4cb201e6a2a5a206a419eb79990a"
SOURCE_URL = "https://gitlab.sh.sensetime.com/stc-fvg/sensenova-presentation-int.git"
BUNDLE_NAME = "sensenova-presentation-suite"
MODULES = ("dazzle", "doctor", "entry", "standard", "story", "tools")
OVERLAYS = ["metadata.user_visible=false", "metadata.allow_override=false",
            "entry-two-outputs", "story-two-outputs", "doctor-shipped-backends",
            "remove-image-only-output-policy", "legacy-static-task-resume",
            "dazzle-box-native-tools", "bundle-third-party-notices",
            "static-player-delivery-gate", "design-mode-delivery-wording",
            "owned-renderer-lifecycle", "original-uploaded-font-family",
            "intermediate-render-artifacts",
            "explicit-exporter-no-install", "managed-exporter-browser-cleanup",
            "dynamic-render-deck-hash",
            "dynamic-render-absolute-paths", "semantic-dynamic-presentation-scope",
            "dazzle-output-choice-precedence", "skill-owned-formal-finalizer",
            "generic-skill-adoption", "explicit-task-pack-postprocess-path", "retain-public-delivery-method",
            "formal-command-parent-death-browser-guard", "required-reference-finalizer-contract",
            "sequential-ppt-image-inspection"]
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-suite"
LICENSE_INPUT_PATH = "scripts/presentation_suite_licenses/echarts-5.5.0"
LICENSE_INPUT_DIR = Path(__file__).resolve().parents[1] / LICENSE_INPUT_PATH
LICENSE_OUTPUT_PATH = "skills/sn-ppt-standard/assets/licenses/echarts-5.5.0"
LICENSE_SOURCE_URL = "https://raw.githubusercontent.com/apache/echarts/5.5.0"
# Verbatim files from the static runtime's tag, independent of exporter npm dependencies.
LICENSE_INPUT_HASHES = {
    "LICENSE": "634293835b43a6dd2094fa39182a3d9a6b9ca43b7fdb9ac354e8037af2a3093a",
    "NOTICE": "fa99ac3af859d0e13166906dc53a73ad34a08898da7e8ae83407275496e9e30c",
    "licenses/LICENSE-d3": "e1211892da0b0e0585b7aebe8f98c1274fba15bafe47fa1f4ee8a7a502c06304",
}
RUNTIME_INPUT_DIR = Path(__file__).resolve().parent / "presentation_suite_overlays"
_render_lifecycle_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "render_lifecycle.py"))["apply"]
_font_source_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "font_source.py"))["apply"]
_artifact_publication_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "artifact_publication.py"))["apply"]


def _apply_host_metadata(data: bytes) -> bytes:
    text = data.decode("utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError("SN Skill is missing YAML frontmatter")
    frontmatter = match.group(1)
    parsed = yaml.safe_load(frontmatter)
    if not isinstance(parsed, dict):
        raise ValueError("SN Skill frontmatter must be a mapping")
    metadata = parsed.get("metadata")
    if metadata is None:
        frontmatter += "\nmetadata:"
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("SN Skill metadata must be a mapping")
    for key in ("user_visible", "allow_override"):
        if key in metadata:
            frontmatter = re.sub(rf"(?m)^  {key}:.*$", f"  {key}: false", frontmatter)
        else:
            frontmatter = re.sub(r"(?m)^metadata:\s*$", f"metadata:\n  {key}: false", frontmatter)
        if yaml.safe_load(frontmatter)["metadata"].get(key) is not False:
            raise ValueError("Unsupported SN Skill metadata layout; update the host overlay")
    return ("---\n" + frontmatter + "\n---\n" + text[match.end():]).encode("utf-8")


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"SN integration overlay needs review: expected one occurrence of {old[:90]!r}")
    return text.replace(old, new, 1)


def _replace_section(text: str, start: str, end: str, replacement: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"SN integration section needs review: {start!r}")
    before, remaining = text.split(start, 1)
    _, after = remaining.split(end, 1)
    return before + replacement + end + after



SKILL_ADOPTION_PROTOCOL = """
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
"""


def _image_inspection_batch_overlay(relative: str, data: bytes) -> bytes:
    """Serialize PPT image batches without changing the shared tool limit."""
    if relative != "skills/sn-ppt-standard/references/box-agent-tool-contract.md":
        return data
    return _replace_once(
        data.decode("utf-8"),
        '视觉模型优先使用 `strategy="native"`，让当前角色直接看新鲜像素。'
        '只有工具明确返回 `IMAGE_NATIVE_UNSUPPORTED` 时才改用 `proxy`，'
        '并在交接中标明这是代理视觉结论。每次最多检查 6 张图；HTML/CSS 修改后必须先重渲再看。',
        '视觉模型优先使用 `strategy="native"`，让当前角色直接看新鲜像素。'
        '只有工具明确返回 `IMAGE_NATIVE_UNSUPPORTED` 时才改用 `proxy`，'
        '并在交接中标明这是代理视觉结论。HTML/CSS 修改后必须先重渲再看。\n\n'
        '### 视觉检查分批（PPT Skill）\n\n'
        '- 每次模型响应最多调用一次 `inspect_images`，每批最多 4 张图；'
        '工具的通用上限不改变本 Skill 的分批上限。'
        '不得在同一响应中并行发出多个 `inspect_images` 调用（包括多个 `native` 调用）来规避限制。\n'
        '- 等待本批工具实际返回，并由模型看图形成检查结论后，先记录已覆盖页码或素材 ID、'
        '发现的问题和待检查清单，再在后续模型响应中检查下一批；工具调用成功或图片已附加都不等于已看图。\n'
        '- 保留原有总览/联系表与逐页细查流程；总览不能替代逐页细查。'
        '按原验收要求覆盖全部页面和待检素材，遗漏、失败或修改后尚未复看的页面不能标为检查完成。\n'
        '- `REQUEST_BODY_TOO_LARGE` 表示失败请求中的图片未被模型看到，不增加已覆盖清单，'
        '不得宣称本批或整册检查完成。保持原图质量，将失败批次缩小为 2 张、必要时 1 张后顺序重试；'
        '不得降低图片质量、跳过页面，也不得仅为绕过请求字节限制改用 `proxy`。'
        '单张仍超限时按待验收项读取相关高清局部，记录已检查区域及尚未检查区域；'
        '局部检查不能冒称整页已覆盖，只有原验收要求的区域与内容全部核验才标记页面完成。'
        '无法完成覆盖时保留未完成状态、错误与待检清单，如实报告阻塞。'
    ).encode("utf-8")


def _apply_integration_overlay(relative: str, data: bytes) -> bytes:
    """Keep upstream production methods, adapting only the shipped route closure."""
    data = _render_lifecycle_overlay(relative, data)
    data = _font_source_overlay(relative, data)
    data = _artifact_publication_overlay(relative, data)
    data = _image_inspection_batch_overlay(relative, data)
    if relative == "skills/sn-ppt-dazzle/scripts/render_deck.py":
        text = _replace_once(data.decode("utf-8"), "import argparse\n", "import argparse\nimport hashlib\n")
        text = _replace_once(text, "        self.html_path = html_path\n        self.out_dir = out_dir\n",
            "        self.html_path = html_path.resolve()\n        self.out_dir = out_dir.resolve()\n")
        text = _replace_once(text, '            "deck": str(html_path),\n',
            '            "deck": str(self.html_path),\n')
        text = _replace_once(text, '        self.meta["console_errors"] = self.console_errors\n',
            '        self.meta["console_errors"] = self.console_errors\n'
            '        self.meta["deck_sha256"] = hashlib.sha256(self.html_path.read_bytes()).hexdigest()\n')
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/lib/dom_extractor.mjs":
        text = _replace_once(data.decode("utf-8"),
            "export async function extractPages(htmlPaths) {\n"
            "  let browser;\n"
            "  try {\n"
            "    browser = await chromium.launch({ headless: true, executablePath: pickBrowserExe() });",
            "export async function extractPages(htmlPaths) {\n"
            "  let browser, launching, closing;\n"
            "  const managed = process.env.BOX_AGENT_PPTX_MANAGED_DELIVERY === '1';\n"
            "  const closeOwnedBrowser = () => closing ||= (async () => {\n"
            "    const owned = browser || await launching;\n"
            "    if (owned) await owned.close();\n"
            "  })();\n"
            "  const stop = signal => {\n"
            "    void closeOwnedBrowser().catch(error => {\n"
            "      process.stderr.write(`[dom_extractor] owned browser cleanup failed: ${error.message}\\n`);\n"
            "    }).finally(() => process.exit(signal === 'SIGINT' ? 130 : 143));\n"
            "  };\n"
            "  const onTerm = () => stop('SIGTERM');\n"
            "  const onInt = () => stop('SIGINT');\n"
            "  const removeSignals = () => {\n"
            "    if (managed) { process.off('SIGTERM', onTerm); process.off('SIGINT', onInt); }\n"
            "  };\n"
            "  if (managed) { process.on('SIGTERM', onTerm); process.on('SIGINT', onInt); }\n"
            "  try {\n"
            "    let executablePath = pickBrowserExe();\n"
            "    if (managed && executablePath && process.env.PPT_DELIVERY_BROWSER_GUARD) {\n"
            "      process.env.PPT_DELIVERY_BROWSER_EXE = executablePath;\n"
            "      executablePath = process.env.PPT_DELIVERY_BROWSER_GUARD;\n"
            "    }\n"
            "    launching = chromium.launch({ headless: true, executablePath });\n"
            "    browser = await launching;")
        text = _replace_once(text,
            "    // Browser unavailable — return null IR for every page.",
            "    removeSignals();\n"
            "    // Browser unavailable — return null IR for every page.")
        text = _replace_once(text,
            "  } finally {\n    await browser.close();\n  }\n\n  return results;",
            "  } finally {\n"
            "    try { await closeOwnedBrowser(); } finally { removeSignals(); }\n"
            "  }\n\n  return results;")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs":
        text = _replace_once(data.decode("utf-8"),
            "    console.error('[setup] 首次运行，正在安装 npm 依赖...');",
            "    if (process.env.BOX_AGENT_PPTX_NO_INSTALL === '1') {\n"
            "      throw new Error('Exporter dependencies unavailable: missing local pptxgenjs/playwright/echarts; automatic installation disabled (BOX_AGENT_PPTX_NO_INSTALL=1).');\n"
            "    }\n"
            "    console.error('[setup] 首次运行，正在安装 npm 依赖...');")
        text = _replace_once(text,
            "  console.error('[setup] 本地无可用 Chromium，正在安装 Playwright Chromium...');",
            "  if (process.env.BOX_AGENT_PPTX_NO_INSTALL === '1') {\n"
            "    throw new Error('Chromium unavailable: no usable local browser; automatic installation disabled (BOX_AGENT_PPTX_NO_INSTALL=1).');\n"
            "  }\n"
            "  console.error('[setup] 本地无可用 Chromium，正在安装 Playwright Chromium...');")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/assets/vendor/echarts.min.js":
        if not re.search(rb'\.version=["\']5\.5\.0["\']', data):
            raise ValueError("ECharts runtime version needs review against pinned license inputs")
        return data
    if relative == "THIRD_PARTY_NOTICES.md":
        text = _replace_once(data.decode("utf-8"),
            "This package installs Python dependencies declared in `studio/pyproject.toml` and\n"
            "`inference/pyproject.toml`. Their license texts and metadata are available from the\n"
            "corresponding upstream projects and from the installed environment.",
            "This notice covers the third-party assets included in this bundle; it does not grant a license to SN-owned code.\n\n"
            "Python runtime dependencies are declared in `skills/sn-ppt-standard/requirements.txt`.\n"
            "Node.js exporter dependencies are declared in\n"
            "`skills/sn-ppt-standard/scripts/export_pptx/package.json` and its\n"
            "`skills/sn-ppt-standard/scripts/export_pptx/package-lock.json`. These dependencies\n"
            "are installed separately; their license texts and metadata are available from the\n"
            "corresponding upstream projects and installed environments.")
        text = _replace_once(text, "The bundled sn-ppt-web includes:",
                             "The bundled sn-ppt-standard includes:")
        text = _replace_once(text,
            "- Apache ECharts runtime assets, distributed under the Apache License 2.0.",
            "- Apache ECharts 5.5.0 static runtime at\n"
            "  `skills/sn-ppt-standard/assets/vendor/echarts.min.js`, distributed under the\n"
            "  Apache License 2.0. Verbatim upstream license and attribution files are retained at\n"
            f"  `{LICENSE_OUTPUT_PATH}/LICENSE`,\n"
            f"  `{LICENSE_OUTPUT_PATH}/NOTICE`, and\n"
            f"  `{LICENSE_OUTPUT_PATH}/licenses/LICENSE-d3` (BSD 3-Clause subcomponent notice).\n"
            "  Their upstream URLs and SHA256 hashes are recorded in `source.json`. These files\n"
            "  cover the bundled static runtime, independently of the exporter npm version.")
        text = _replace_once(text, "`bundled/fonts/`", "`fonts/`")
        text = _replace_once(text, "`bundled/fonts/OFL-1.1.txt`", "`fonts/OFL-1.1.txt`")
        text = _replace_once(text,
            "`bundled/static-ppt-skill-suite/skills/sn-ppt-web/assets/licenses/OFL-1.1.txt`",
            "`skills/sn-ppt-standard/assets/licenses/OFL-1.1.txt`")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/references/box-agent-tool-contract.md":
        text = _replace_once(data.decode("utf-8"),
            "4. 父级重渲并复看。最终 Review 同样由父级先提供新鲜 PNG、Review 子代理集中修复、父级统一重渲/build；父级统一执行重渲、build、inspect 和 Standard exporter，只有最终 PNG 与 `present.html` 验证通过后才导出，失败登记 `state.status=partial`。",
            "4. 父级重渲并复看。最终 Review 同样由父级先提供新鲜 PNG、Review 子代理集中修复；修复完成后，在最终像素检查前，父级按公共 `pptx` 的绝对路径命令运行 `finalize.py`，由它统一 build/audit/export，再检查最终 PNG 与 `present.html`。不要重复手工导出；像素检查导致源文件修改时重新 finalize 并复看。技术回执不代替视觉检查，检查失败须如实报告未完成与修复项，不能宣称交付完成。")
        text = _replace_once(text,
            "导出成功后，按 stdout JSON 的精确 `output` 路径检查文件；",
            "正式收尾后，读取 stdout JSON 及 `_trace/finalize-receipt.json` 的 `status`、`warnings` 和 `artifacts[].path`，按其中精确路径检查文件；仅 `status=complete` 表示技术核验完成，视觉与内容仍按 Review 实际结果判断。取消或超时必须读取本次失败回执；强制终止留下的 `in_progress` 也不是成功。")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-standard/SKILL.md":
        text = _replace_once(data.decode("utf-8"), "## Box-Agent 兼容入口\n", """## 整册 HTML 完成条件

`<DECK_DIR>/present.html` 是静态整册的必交付入口，包括全生图、只要 PPTX、只要 HTML
和续改任务。逐页 HTML/PNG、子代理完成或 PPTX 导出成功，都不等于整册完成。
父级必须通过下述托管工具或独立 CLI 执行 `deck.py build` 与 `deck.py audit`，确认播放器覆盖全部页且可打开；
再完成最终像素检查及所需 PPTX 导出。只缺播放器时复用已有页面补齐收尾，不重新制作整册。
最终回复必须给出真实 `present.html` 的可点击链接，并保留其依赖的 slides、样式与资源；
未生成或核验失败则保存现有产物、登记 `partial` 和错误，不得声称完成或伪造链接。

Box 公共入口将本 Skill 的正式 build、audit 和所需 PPTX exporter 封装在
`pptx/scripts/finalize.py`；完成页面修复与内容核验后、最终像素检查前，按公共 `pptx` 的命令传入实际
`--workspace`、`--deck-dir`、`--requirements` 和 `--task-pack`，检查具体回执，
不要重复执行收尾。它不依赖安装 box_agent，不解释用户语义，也不创建任务状态机。
静态默认 HTML + PPTX，只要 HTML 的例外必须来自用户；task_pack 字段须与用户原文、
真实选择及后续更正一致，不能从静态字段覆盖动态需求。
独立 SN 未附公共 pptx 脚本时继续按下方原 CLI 流程执行，不改用临时转换器。

## Box-Agent 兼容入口
""")
        text = _replace_once(text,
            'python "$SKILL_ROOT/scripts/deck.py" build "$DECK_DIR" --expected <总页数>\n```',
            'python "$SKILL_ROOT/scripts/deck.py" build "$DECK_DIR" --expected <总页数>\n'
            'python "$SKILL_ROOT/scripts/deck.py" audit "$DECK_DIR" --expected <总页数>\n```')
        text = _replace_once(text,
            '`deck.py build` 生成并校验 `present.html`（缺失即报错，deck.py:1294-1295）。`present.html` 是必交付产物：不得省略 build、不得拿其他文件代替它交付。',
            '`deck.py build` 生成 `present.html`；随后单独执行 `deck.py audit`，检查文件存在、全部页引用、本地资源与播放器运行情况。两条命令都必须成功，不用后续命令掩盖退出码。`present.html` 不得省略，也不能用 PPTX 或其他文件替代；通过后登记绝对路径到 `state.artifacts.present_html` 并在最终回复提供链接。')
        text = _replace_once(text,
            "    - 成功且 `DECK_DIR/<DECK_ID>.pptx` 确实存在后，登记 `state.artifacts.pptx`；",
            "    - Box 只读取正式回执中的 PPTX 路径、状态和错误，不补写已绑定 task_pack。"
            "独立 SN 成功且 `DECK_DIR/<DECK_ID>.pptx` 确实存在后，登记 `state.artifacts.pptx`；")
        text = _replace_once(text,
            '```bash\npython "$SKILL_ROOT/scripts/deck.py" build "$DECK_DIR" --expected <总页数>',
            '以下手工命令与产物登记仅适用于独立 SN；Box 已由 finalize 完成，只读取回执，'
            '不重复命令或补写已绑定 task_pack。\n\n'
            '```bash\npython "$SKILL_ROOT/scripts/deck.py" build "$DECK_DIR" --expected <总页数>')
        text = _replace_once(text,
            "每次失败由运行时恢复该组最后一次已看过的版本。",
            "父级与原页组在返修前保留最后验证版；失败时由原页组显式恢复该版本，不依赖宿主自动恢复。")
        text = _replace_once(text,
            "运行时不得禁止写入最终验收合同明确要求的这两份文件，也不得在收口阶段允许继续修改页面。",
            "Review 和父级应按正常工具权限补齐这两份验收记录，收口阶段不得继续修改页面。")
        text = _replace_once(text,
            "6. 修复确认后先同步讲稿，再执行一次 `deck.py build`。随后重新生成",
            "6. 修复确认后先同步讲稿。Box 公共入口此时执行一次 `pptx/scripts/finalize.py`，"
            "统一完成 build、audit 和所需正式 exporter；不再手工重复。独立 SN 此时执行 "
            "`deck.py build` 并按后续命令 audit/export。随后重新生成")
        text = _replace_once(text,
            "8. **PPTX 导出（必做步骤，不是可选项）**：当 `static_postprocess` 含 `pptx` 时，**必须**执行以下唯一导出命令，不得跳过、不得改用任何其他工具：",
            "8. **PPTX 导出（必做步骤，不是可选项）**：当 `static_postprocess` 含 `pptx` 时，"
            "Box 第 6 步的 finalize 已调用以下唯一 exporter，核对回执与文件即可，不重复执行。"
            "独立 SN **必须**执行以下命令，不得跳过或改用其他工具：")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-entry/SKILL.md":
        text = data.decode("utf-8")
        text = _replace_once(text,
            "`sn-ppt-standard`、`sn-ppt-dazzle` 或 `sn-ppt-creative`。",
            "`sn-ppt-standard` 或 `sn-ppt-dazzle`。")
        text = _replace_section(text, "### 输出格式\n", "### 设计丰富度\n", """### 输出格式

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
`standard`，把原 `web_postprocess`（包括用户明确的 `[]`）迁到 `static_postprocess`，
再移除旧字段；两种后处理字段已有冲突时先澄清，不覆盖已有选择。该迁移不改变用户已选的
设计模式；字段迁移本身不重做 Research 或 Story，本轮标题等内容修改按下方恢复规则
局部更新 Story。旧 `creative` 图片整页出口未提供，保留产物并说明。

""")
        text = _replace_section(text, '当 `choices.output` 是 `static_html` 时，',
            '阶段更新只修改相关字段', """当 `choices.output` 是 `static_html` 时，`ppt_mode` 必须是 `standard`，
`static_postprocess` 默认是 `["pptx"]`；只有用户明确只要 HTML 时写 `[]`。
当 `choices.output` 是 `dynamic_html` 时，`ppt_mode` 必须是 `dazzle`，不触发 PPTX 后处理。
动态任务的 `static_postprocess` 为 `[]`。

| `choices.output` | `ppt_mode` |
|---|---|
| `static_html` | `standard` |
| `dynamic_html` | `dazzle` |

""")
        text = _replace_once(text,
            "媒体能力都不是 Entry 的强制前置。缺失时按 policy 继续；只有 Creative 已被明确选择且\n原生、内置生图都不可用时，暂停 Creative 出口并保留全部前置产物，不自动切换出口。",
            "媒体能力都不是 Entry 的强制前置。缺失时按 policy 继续，用可交付的无图版式表达内容。")
        text = _replace_once(text,
            '2. **路由已有 PPTX**：若任务是编辑、优化、续写或模板填充，交给 `sn-ppt-edit`。若是从零生成，继续本流程。',
            '2. **路由已有 PPTX**：若原位编辑或模板填充与已选设计模式冲突，先澄清是否切换快速模式，用户明确同意后才交给 `ppt-fast`；已有 SN HTML 任务按恢复规则继续。从零生成继续本流程。')
        text = _replace_section(text, '7. **启动生成进度工作台**：', '8. **决定外部证据路径**：', '')
        text = _replace_section(text, '12. **出口分发**：', '## 恢复规则\n', """12. **出口分发**：Story 已完成且当前磁盘 `outline.md` 已按本档位确认后，
    `static_html` 调用 `sn-ppt-standard`，`dynamic_html` 调用 `sn-ppt-dazzle`；始终传入相同绝对
    `deck_dir`。不得绕过 Story；出口不再研究、重排页面或重写大纲。
13. **后处理和收尾**：完成页面修复与内容核验、更新任务包后，父级按公共 `pptx`
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

""")
        # Remove the omitted workbench step while keeping the remaining workflow ordered.
        for number in range(8, 14):
            text = _replace_once(text, f"\n{number}. **", f"\n{number - 1}. **")
        text = _replace_once(text,
            '- 生成进度工作台已启动并提供 `/progress`，或说明非阻塞的跳过原因；\n', '')
        text = _replace_once(text,
            '9. Workbench 启动是生成流程的最佳努力辅助能力；失败不得改变输出选择或中止生成。\n', '')
        text = _replace_once(text,
            '- `deep`：先完成 `sn-deep-research`，再生成正式 `outline.md`；生成前让用户确认。',
            '- `deep`：先确认外部 `sn-deep-research` 可用，再完成研究和正式 `outline.md`；生成前让用户确认。该外部 Skill 未随本套件提供，不可用时说明并让用户选择 Draft 或 Standard，保留已有产物。')
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-dazzle/SKILL.md":
        text = data.decode("utf-8")
        text = _replace_once(text,
            '- `task_pack.choices.output == "dynamic_html"`，或旧任务的 `ppt_mode == "dazzle"`。',
            '- `task_pack.choices.output == "dynamic_html"`，且与用户原文、真实选择回复及后续更正一致。\n'
            '  仅旧任务完全缺少 `choices.output` 字段、且原有动态选择有依据时，才兼容\n'
            '  `ppt_mode == "dazzle"`；空值、unknown 或非法值不算字段缺失。\n\n'
            '`choices.output` 已存在时，旧 `ppt_mode` 不能覆盖它。两者冲突（例如\n'
            '`static_html` 与 `dazzle`）或缺少选择依据时，停止并返回 Entry / 公共入口核对，\n'
            '不得强行进入 Dazzle。任务包本身不能代替用户授权，当前明确更正优先。')
        if text.count("`vision_analyze`") != 5:
            raise ValueError("Dazzle visual-tool overlay needs review")
        text = text.replace("`vision_analyze`", "`inspect_images`")
        text = _replace_once(text, "## 6. 配图与生图（image_generate 可用时）",
            "## 6. 配图与生图（可选能力可用时）")
        text = _replace_once(text,
            "（若工具列表里没有 image_generate，跳过本节，一切视觉均代码绘制。）",
            "Box-Agent 优先使用原生 `generate_image`，视觉核对使用 `inspect_images`。开始需要媒体时读取同级 `sn-ppt-tools/references/capability-policy.md`，按原生、内置、无工具顺序处理；两层都不可用时跳过本节，用代码绘制可交付版式，不伪造图片或持续重试。")
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-story/SKILL.md":
        return _replace_once(data.decode("utf-8"),
            '出口（standard / dazzle / creative）', '出口（standard / dazzle）').encode("utf-8")
    if relative == "skills/sn-ppt-tools/references/capability-policy.md":
        return _replace_once(data.decode("utf-8"),
            '- Creative 的原生与内置图片生成都不可用：只停止 Creative 出口，保留\n  `task_pack.json`、`info_pack.json`、`outline.md`、visual plan 和现有页面；状态写\n  `partial`，不得自动切换出口。\n', '').encode("utf-8")
    if relative == "skills/sn-ppt-doctor/ppt_doctor/check_environment.py":
        text = data.decode("utf-8")
        text = _replace_once(text, '        "python_pptx": module_available("pptx"),\n', '')
        text = _replace_section(text, '        "workbench_runtime": (', '        "native_media": {',
            '        "dynamic_renderer": (skills_dir / "sn-ppt-dazzle/scripts/render_deck.py").is_file(),\n')
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-doctor/SKILL.md":
        text = _replace_once(data.decode("utf-8"), 'HTML-to-PPTX export, Workbench startup, or',
            'HTML-to-PPTX export, dynamic HTML rendering, or')
        for old, new in [
            ('缺少某个可选依赖只影响对应能力，不应阻止其他出口。例如没有 Node 时仍可使用宿主原生\nPPTX 能力；HTML -> PPTX 失败时仍保留 HTML。',
             '缺少某个可选依赖只影响对应能力，不应阻止其他出口。Standard 缺少 Node 或 HTML -> PPTX 失败时保留 HTML；需要 PPTX 则登记 partial 和错误，不改用宿主原生 PPTX 工具。静态 present.html 仍须完成并核验，动态出口保留 deck.html。'),
            ('Node.js 是否可用于 Workbench 和 Static 默认 HTML -> PPTX 兼容版导出；',
             'Node.js 是否可用于 Standard HTML -> PPTX 导出；'),
            ('Standard 渲染脚本、PPTX exporter 和 Workbench runtime 是否存在；',
             'Standard 与动态 HTML 渲染脚本、Standard PPTX exporter 是否存在；'),
            ('- `python-pptx` 是否可用于 Creative 整页图片打包。\n', ''),
        ]:
            text = _replace_once(text, old, new)
        return text.encode("utf-8")
    return data


def sync_suite(source_checkout: Path, revision: str, output_dir: Path = OUTPUT_DIR) -> dict:
    """Replace only a marked generated bundle after staging a complete revision."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full 40-character commit hash")
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink():
        raise ValueError("Refusing to replace a symlink as a managed bundle")
    if output_dir.exists():
        marker = output_dir / "source.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("name") != BUNDLE_NAME:
            raise ValueError("Refusing to replace a directory that is not a managed SN bundle")
    git = ["git", "-C", str(source_checkout)]
    commit = subprocess.check_output([*git, "rev-parse", "--verify", f"{revision}^{{commit}}"], text=True).strip()
    roots = [f"skills/sn-ppt-{module}" for module in MODULES]
    roots += ["webui/bundled/fonts", "webui/THIRD_PARTY_NOTICES.md"]
    records = subprocess.check_output([*git, "ls-tree", "-rz", commit, "--", *roots]).split(b"\0")
    provenance = {"schema_version": 1, "name": BUNDLE_NAME, "repository": SOURCE_URL,
                  "revision": commit,
                  "modules": sorted(f"sn-ppt-{module}" for module in MODULES),
                  "overlays": OVERLAYS, "files": {}}
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sn-suite-sync-", dir=output_dir.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        for record in records:
            if not record:
                continue
            header, raw_path = record.split(b"\t", 1)
            mode, kind, blob = header.decode().split()
            source_path = PurePosixPath(raw_path.decode())
            if kind != "blob" or mode not in {"100644", "100755"} or ".." in source_path.parts:
                raise ValueError(f"Unsupported bundled source entry: {source_path}")
            if source_path.parts[:3] == ("webui", "bundled", "fonts"):
                relative = PurePosixPath("fonts", *source_path.parts[3:])
            elif str(source_path) == "webui/THIRD_PARTY_NOTICES.md":
                relative = PurePosixPath("THIRD_PARTY_NOTICES.md")
            else:
                relative = source_path
            data = subprocess.check_output([*git, "cat-file", "blob", blob])
            source_sha256 = hashlib.sha256(data).hexdigest()
            data = _apply_integration_overlay(str(relative), data)
            if relative.name == "SKILL.md":
                # Qualify inline paths against Entry's existing nested JSON
                # contract; do not invent a second top-level CLI schema.
                text = data.decode("utf-8").replace("`static_postprocess`", "`choices.static_postprocess`")
                data = text.encode("utf-8")
                data += (SKILL_ADOPTION_PROTOCOL + "\n按所选出口的收尾时序，父级从公共 `pptx` Skill 的实际目录运行\n"
                         "`scripts/finalize.py --workspace <工作空间> --deck-dir <同一目录> "
                         "--requirements <需求文件> --task-pack <任务包>`。\n"
                         "先完成任务包阶段/产物字段更新，再执行正式收尾；调用后修改输入必须重跑。\n"
                         "检查 stdout 和 `_trace/finalize-receipt.json` 的产物与警告；技术回执不能替代视觉或内容检查。\n").encode("utf-8")
                data = _apply_host_metadata(data)
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(int(mode[-3:], 8))
            provenance["files"][str(relative)] = {
                "source_path": str(source_path), "source_sha256": source_sha256,
                "sha256": hashlib.sha256(data).hexdigest()
            }
        for relative, expected_sha256 in LICENSE_INPUT_HASHES.items():
            data = (LICENSE_INPUT_DIR / relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise ValueError(f"ECharts license input needs review: {relative}")
            bundled_path = f"{LICENSE_OUTPUT_PATH}/{relative}"
            target = staged / bundled_path
            if target.exists():
                raise ValueError(f"ECharts license input needs review: upstream already supplies {bundled_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
            provenance["files"][bundled_path] = {
                "source_url": f"{LICENSE_SOURCE_URL}/{relative}",
                "input_path": f"{LICENSE_INPUT_PATH}/{relative}",
                "source_path": relative, "source_sha256": expected_sha256,
                "sha256": expected_sha256,
            }
        runtime_relative = "skills/sn-ppt-standard/scripts/render_runtime.py"
        runtime_data = (RUNTIME_INPUT_DIR / "render_runtime.py").read_bytes()
        runtime_target = staged / runtime_relative
        if runtime_target.exists():
            raise ValueError("render lifecycle input needs review: upstream supplies runtime helper")
        runtime_target.parent.mkdir(parents=True, exist_ok=True)
        runtime_target.write_bytes(runtime_data)
        runtime_target.chmod(0o644)
        runtime_hash = hashlib.sha256(runtime_data).hexdigest()
        provenance["files"][runtime_relative] = {
            "input_path": "scripts/presentation_suite_overlays/render_runtime.py",
            "source_path": runtime_relative, "source_sha256": runtime_hash,
            "sha256": runtime_hash,
        }
        required = [f"skills/sn-ppt-{module}/SKILL.md" for module in MODULES]
        required += ["fonts/OFL-1.1.txt", "THIRD_PARTY_NOTICES.md",
                     "skills/sn-ppt-standard/requirements.txt"]
        if any(not (staged / path).is_file() for path in required):
            raise ValueError("Pinned revision is missing required SN modules or font notices")
        (staged / "source.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        previous = Path(temporary) / "previous"
        if output_dir.exists():
            output_dir.rename(previous)
        try:
            staged.rename(output_dir)
        except OSError:
            if previous.exists():
                previous.rename(output_dir)
            raise
    return provenance


def refresh_host_overlays(output_dir: Path) -> dict:
    """Apply append-only overlays to a verified bundle without upstream access."""
    provenance = json.loads((output_dir / "source.json").read_text())
    applied = provenance.get("overlays", [])
    incremental = {
        "intermediate-render-artifacts": _artifact_publication_overlay,
        "sequential-ppt-image-inspection": _image_inspection_batch_overlay,
    }
    pending = OVERLAYS[len(applied):]
    if (provenance.get("name") != BUNDLE_NAME
            or provenance.get("revision") != PINNED_REVISION
            or applied != OVERLAYS[:len(applied)]
            or any(name not in incremental for name in pending)):
        raise ValueError("Bundle requires a full sync from the pinned upstream checkout")
    actual = {p.relative_to(output_dir).as_posix() for p in output_dir.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    if actual != set(provenance["files"]) | {"source.json"}:
        raise ValueError("Bundle file set differs from its provenance")
    replacements = {}
    for relative, record in provenance["files"].items():
        data = (output_dir / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError(f"Bundle file differs from its provenance: {relative}")
        for name in pending:
            data = incremental[name](relative, data)
        replacements[relative] = data
        record["sha256"] = hashlib.sha256(data).hexdigest()
    if not pending:
        return provenance
    provenance["overlays"] = list(OVERLAYS)
    with tempfile.TemporaryDirectory(prefix=".sn-suite-overlay-", dir=output_dir.parent) as temporary:
        staged = Path(temporary) / "bundle"
        shutil.copytree(output_dir, staged, ignore=shutil.ignore_patterns("__pycache__"))
        for relative, data in replacements.items():
            (staged / relative).write_bytes(data)
        (staged / "source.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        previous = Path(temporary) / "previous"
        output_dir.rename(previous)
        try:
            staged.rename(output_dir)
        except OSError:
            previous.rename(output_dir)
            raise
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-checkout", type=Path)
    source.add_argument("--refresh-host-overlays", action="store_true",
                        help="Apply pending append-only overlays to a hash-verified local bundle")
    parser.add_argument("--revision", default=PINNED_REVISION)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    result = (refresh_host_overlays(args.output_dir) if args.refresh_host_overlays
              else sync_suite(args.source_checkout, args.revision, args.output_dir))
    print(f"Synced {len(result['files'])} files from {result['revision']} and pinned license inputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
