"""Add only host Chromium preparation guidance to the three presentation skills."""

_PREPARATION = '''## Chromium 准备

执行依赖 Chromium 的渲染前，若宿主支持，优先调用 `local_environment(capability="chromium", action="ensure")`；就绪后用新的子进程继续原任务。宿主不支持或未提供该工具时沿用原渲染流程。准备失败或超时时，由 Agent 根据错误和任务需要自主判断继续、修复或采用其他可用方式，不因准备接口失败直接停止整个任务。

安装可能仍在后台进行，先确认状态、避免并发安装或修改正在安装的环境；尊重用户取消。如实报告渲染结果并保留原有验收要求，不混淆独立 Chromium 与 Electron/MCP 的就绪状态。用户明确要求只检查、不安装时，不调用 `ensure`。

'''

_ANCHORS = {
    'skills/sn-ppt-standard/SKILL.md': '## 整册 HTML 完成条件\n',
    'skills/sn-ppt-dazzle/SKILL.md': '## 0. Entry / Story 输入契约（最高优先级）\n',
    'skills/sn-ppt-doctor/SKILL.md': '## 持久配置\n',
}
REPLACEMENTS = {relative: [(anchor, _PREPARATION + anchor)]
                for relative, anchor in _ANCHORS.items()}
# The script remains diagnostic-only; preparation above belongs to the Agent.
REPLACEMENTS['skills/sn-ppt-doctor/SKILL.md'].append(
    ('Doctor 只报告，不安装依赖', 'Doctor 脚本只报告，不安装依赖'))


def apply(relative, data):
    if relative not in REPLACEMENTS:
        return data
    text = data.decode("utf-8")
    for old, new in REPLACEMENTS[relative]:
        if text.count(old) != 1:
            raise ValueError(f"Host Chromium preparation overlay needs review: {relative}")
        text = text.replace(old, new, 1)
    return text.encode("utf-8")
