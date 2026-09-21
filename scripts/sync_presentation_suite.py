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


PINNED_REVISION = "b5ef18018940e63eafe28081460a402ec68632b5"
SOURCE_URL = "https://gitlab.sh.sensetime.com/stc-fvg/sensenova-presentation-int.git"
BUNDLE_NAME = "sensenova-presentation-suite"
MODULES = ("dazzle", "doctor", "entry", "standard", "story", "tools")
OVERLAYS = ["metadata.user_visible=false", "metadata.allow_override=false",
            "entry-two-outputs", "story-two-outputs", "doctor-shipped-backends",
            "remove-image-only-output-policy", "current-task-resume",
            "dazzle-box-native-tools", "bundle-third-party-notices",
            "static-player-delivery-gate", "design-mode-delivery-wording",
            "owned-renderer-lifecycle", "original-uploaded-font-family",
            "intermediate-render-artifacts", "sequential-ppt-image-inspection",
            "source-relative-pptx-page-directories", "explicit-delivery-scopes",
            "presentation-progress-without-reconfirmation", "host-playwright-runtime",
            "bounded-image-inspection-recovery"]
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
_export_page_directories_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "export_page_directories.py"))["apply"]
_delivery_scope_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "delivery_scope.py"))["apply"]
_host_playwright_overlay = runpy.run_path(str(RUNTIME_INPUT_DIR / "host_playwright.py"))["apply"]

_image_inspection_recovery_overlay = runpy.run_path(
    str(RUNTIME_INPUT_DIR / "image_inspection_recovery.py")
)["apply"]


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


def _presentation_progress_overlay(relative: str, data: bytes) -> bytes:
    if relative != "skills/sn-ppt-entry/SKILL.md":
        return data
    text = _replace_once(data.decode("utf-8"),
        "超过约 30 秒的工作必须让用户看到进度。至少在以下边界各回显一句：",
        "超过约 30 秒的工作必须让用户看到有意义的进度。选择卡已展示模式确认结果，"
        "续跑直接报告下一步具体动作；加载本 Skill 前后都不要再次宣布进入模式或复述超时选择。"
        "只在出现新的进展、结果、阻塞或必要决定时更新，不换一种说法重复上一条进度。"
        "用户询问选择原因时正常解释。以下边界按实际变化回显，可合并相邻的短步骤：")
    text = _replace_once(text, "- 已识别任务与三个选择；",
                         "- 新识别的重要任务约束（不复述已经确认的模式）；")
    return text.encode("utf-8")


def _apply_integration_overlay(relative: str, data: bytes) -> bytes:
    """Keep upstream production methods, adapting only the shipped route closure."""
    data = _render_lifecycle_overlay(relative, data)
    data = _font_source_overlay(relative, data)
    data = _artifact_publication_overlay(relative, data)
    data = _image_inspection_batch_overlay(relative, data)
    data = _export_page_directories_overlay(relative, data)
    data = _delivery_scope_overlay(relative, data)
    data = _presentation_progress_overlay(relative, data)
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
    if relative == "skills/sn-ppt-standard/SKILL.md":
        text = _replace_once(data.decode("utf-8"), "## Box-Agent 兼容入口\n", """## 整册 HTML 完成条件

`<DECK_DIR>/present.html` 是静态整册的必交付入口，包括全生图、只要 PPTX、只要 HTML
和续改任务。逐页 HTML/PNG、子代理完成或 PPTX 导出成功，都不等于整册完成。
父级必须执行下方包含 build/audit 的 `deck.py review-prep`，确认播放器覆盖全部页且可打开；
再完成最终像素检查及所需 PPTX 导出。只缺播放器时复用已有页面补齐收尾，不重新制作整册。
最终回复必须给出真实 `present.html` 的可点击链接，并保留其依赖的 slides、样式与资源；
未生成或核验失败则保存现有产物、登记 `partial` 和错误，不得声称完成或伪造链接。

## Box-Agent 兼容入口
""")
        text = _replace_once(text,
            '`present.html` 是必交付产物：不得省略构建、不得拿其他文件代替它交付。',
            '`present.html` 是必交付产物：不得省略构建、不得拿其他文件代替它交付。'
            '父级验收通过后登记绝对路径到 `state.artifacts.present_html`，最终回复提供真实链接；'
            '命令失败保持非零状态，不用后续命令掩盖退出码。')
        return text.encode("utf-8")
    if relative == "skills/sn-ppt-entry/SKILL.md":
        text = data.decode("utf-8")
        # Preserve the public entry's managed scratch rule when regenerating
        # from upstream; this host-only rule was previously edited in-bundle.
        text = _replace_once(text, "所有 Skill 安装目录只读。\n",
            "所有 Skill 安装目录只读。\n\n"
            "上述目录规则适用于持久文件。仅供本轮看图检查的缩略图、裁片等遵循公共 `pptx` 入口的\n"
            "“QA 临时文件与收尾”：生成到 `$BOX_AGENT_SCRATCH_DIR`，交由运行时回收。需保留的\n"
            "QA 报告、正式预览和交付依赖仍放 `deck_dir`；未登记的已有 QA 文件保留，交付不要求\n"
            "清空目录，不主动发起递归删除或删除审批。\n")
        text = _replace_once(text,
            "`sn-ppt-standard`、`sn-ppt-dazzle` 或 `sn-ppt-creative`。",
            "`sn-ppt-standard` 或 `sn-ppt-dazzle`。")
        text = _replace_once(text, "# sn-ppt-entry\n", """# sn-ppt-entry

执行前，必须已有用户明确要求设计模式或无冲突的动态演示，或公共 `pptx` 入口已收到
`presentation_mode` 选择卡的 `design` 回复（用户点击或宿主超时均可）。
若只是模型推荐了设计模式，先返回 `get_skill(skill_name="pptx")` 完成模式选择，
不要建立任务目录、写任务包或开始研究。加载本 Skill 本身不表示模式已经确认。
已确认模式的同一任务续作不重复选择。
""")
        text = _replace_section(text, "### 输出格式\n", "### 设计丰富度\n", """### 输出格式

本套件是公共 `pptx` 入口下的设计模式，保留两个表达出口：

- `static_html` -> `sn-ppt-standard`：静态 PPT 页面，始终交付整册 `present.html`，默认另交付 `.pptx`。
- `dynamic_html` -> `sn-ppt-dazzle`：带动效和翻页交互的 `deck.html`；不承诺保留动画的 PPTX。

对外描述 PPTX 文件交付，不宣传或承诺可编辑、原位编辑能力。
用户未明确要求动态时采用 `static_html`；只要 HTML 时关闭对应 PPTX 后处理。
已有 PPTX 的原位编辑、模板填充与已选设计模式冲突时，保留原始需求、附件和交付格式，
先向用户澄清是否改用快速模式；只有用户明确同意后才加载 `ppt-fast`，不得自动切换。
已有 SN HTML 任务继续使用其任务目录与输出选择。

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
13. **后处理和收尾**：静态页面完成后，父级核对 Standard 的 `deck.py review-prep`
    结果及最终 Review 合同，确认 `<deck_dir>/present.html` 存在、覆盖全部页面且播放器
    可打开，最终全册像素已检查。仅有逐页 HTML/PNG 或 PPTX 不能代替这些验收。
    本轮尚未准备待审产物时，执行 Standard 的 `review-prep` 后完成最终像素检查；
    已验收且此后视觉源未变化时直接消费结果，不重复 build/audit 或改动页面。
    随后按 `static_postprocess` 使用 Standard 自有 exporter
    `scripts/export_pptx/html_to_pptx.mjs` 导出 PPTX，默认同时交付；只有用户明确只要 HTML
    时才可省略 PPTX，任何静态任务都不能因此省略 `present.html`。
    只缺播放器时复用已有页面补齐收尾，不重做 Research、Story 或整册页面。
    把已验证的绝对路径登记到 `task_pack.state.artifacts.present_html`，最终回复必须给出
    `present.html` 的可点击链接，保留它引用的 slides、样式与资源，不能只给文件夹或 PPTX。
    必需产物缺失或转换失败时保留现有产物、状态写 `partial` 并记录错误；不得用宿主工具、
    python-pptx 或自写脚本替换 exporter，不伪造文件路径。动态出口交付 `deck.html` 及其
    实际使用的本地资源，并提供真实 HTML 链接。只登记真实存在的产物到 `task_pack.state.artifacts`。

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
            data = _host_playwright_overlay(str(relative), data)
            data = _image_inspection_recovery_overlay(str(relative), data)
            if relative.name == "SKILL.md":
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
    incremental = {"intermediate-render-artifacts": _artifact_publication_overlay,
                   "sequential-ppt-image-inspection": _image_inspection_batch_overlay,
                   "source-relative-pptx-page-directories": _export_page_directories_overlay,
                   "explicit-delivery-scopes": _delivery_scope_overlay,
                   "presentation-progress-without-reconfirmation": _presentation_progress_overlay,
                   "host-playwright-runtime": _host_playwright_overlay,
                   "bounded-image-inspection-recovery": _image_inspection_recovery_overlay}
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
