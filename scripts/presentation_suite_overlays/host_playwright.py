"""Keep bundled presentation renderers on the host Playwright SDK and browser."""

REPLACEMENTS = {
    'skills/sn-ppt-entry/SKILL.md': [
        (r'''在 Box-Agent 中，从本次加载的 Skill 提示取得 Entry 的绝对 Skill Root，记为
''', r'''渲染复用宿主已提供的 Playwright。宿主设置 `BOX_AGENT_PLAYWRIGHT_MODULE_PATH` 时，
Node 脚本必须加载该绝对模块路径，并把 `BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`
传给 `chromium.launch` 的 `executablePath`。优先调用整包正式渲染脚本，不另装 SDK 或
浏览器，不用裸 `require('playwright')` 选择工作区中的旧副本。宿主文件缺失时报告客户端修复，
不要改用其他浏览器版本继续。

在 Box-Agent 中，从本次加载的 Skill 提示取得 Entry 的绝对 Skill Root，记为
'''),
    ],
    'skills/sn-ppt-standard/scripts/export_pptx/lib/browser_picker.mjs': [
        (r'''import { chromium } from 'playwright';
''', r'''import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
export const { chromium } = require(process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH || 'playwright');
'''),
        (r'''  // ① 环境变量覆盖（与 render.py 同一变量，box-agent 运行脚本已导出）
''', r'''  const host = process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
    || process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  if (host) {
    const p = resolve(expandHome(host));
    if (!isExecutable(p)) throw new Error(`Host Playwright browser is unavailable: ${p}`);
    return p;
  }
  // ① 环境变量覆盖（与 render.py 同一变量，box-agent 运行脚本已导出）
'''),
        (r'''  return expected;
}''', r'''  return expected;
}
'''),
    ],
    'skills/sn-ppt-standard/scripts/export_pptx/lib/dom_extractor.mjs': [
        (r'''import { chromium } from 'playwright';
import { pickBrowserExe } from './browser_picker.mjs';
''', r'''import { chromium, pickBrowserExe } from './browser_picker.mjs';
'''),
    ],
    'skills/sn-ppt-standard/scripts/export_pptx/screenshot.mjs': [
        (r'''import { chromium } from 'playwright';
import { pickBrowserExe } from './lib/browser_picker.mjs';
''', r'''import { chromium, pickBrowserExe } from './lib/browser_picker.mjs';
'''),
    ],
    'skills/sn-ppt-standard/scripts/render.py': [
        (r'''    override = os.environ.get("PPT_SKILL_BROWSER_EXE")
    if override:
        override = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(override):
            raise BrowserUnavailable(f"PPT_SKILL_BROWSER_EXE 不存在: {override}")
''', r'''    override = (
        os.environ.get("BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or os.environ.get("PPT_SKILL_BROWSER_EXE")
    )
    if override:
        override = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(override):
            raise BrowserUnavailable(f"Configured Playwright browser is unavailable: {override}")
'''),
    ],
}


def apply(relative, data):
    if relative not in REPLACEMENTS:
        return data
    text = data.decode("utf-8")
    for old, new in REPLACEMENTS[relative]:
        if text.count(old) != 1:
            raise ValueError(f"Host Playwright overlay needs review: {relative}")
        text = text.replace(old, new, 1)
    return text.encode("utf-8")
