# 维护者 Review 指南

审查非平凡 PR 时使用这份指南。目标是让 review 决策基于证据、保持一致，并且容易解释。

## Review 顺序

1. **Task**：确认 PR 只有一个清晰的行为或子系统范围。
2. **Proof**：检查提交者提供的测试、探针、日志、截图、manifest 或 runtime
   验证是否真的证明了改动行为。
3. **Risk**：检查兼容性、打包/runtime 影响、迁移、配置、回滚和跨仓库后续事项。
4. **Ownership**：确认改动放在正确层级。
5. **Diff Hygiene**：检查是否混入无关改动、生成文件、本地配置、日志、workspace
   文件或过期 graph/cache。

如果 Task、Proof 或 Risk 缺失，先要求补齐，再做深入代码风格审查。

## 归属检查

- 共享 Agent 循环行为属于共享核心模块，例如 `box_agent/core.py`、
  `box_agent/events.py` 以及相关共享 helper。
- CLI 应负责终端 UX、slash commands、渲染和本地提示，不应复制 ACP 也需要的行为。
- ACP 应负责把共享事件翻译成 protocol updates 和 host extension methods。stdout
  必须保持纯协议输出。
- Provider wire 行为属于 `box_agent/llm/`。
- Tool 语义属于 `box_agent/tools/`，应返回结构化 `ToolResult`。
- Skill 加载属于 `box_agent/skill_loader.py`、`box_agent/skills/` 和
  `box_agent/skills/_manifest.json`。
- PPT/文档生成默认由 skill 驱动，除非 PR 明确修改核心 contract。
- Packaged runtime 行为不能只靠源码改动证明。

## 不同改动需要的 Proof

| 改动类型 | 最小 proof |
| --- | --- |
| 共享核心循环、事件、取消、goal、Session Log | 聚焦回归测试 + 相关 core/持久化/ACP 测试 |
| CLI-only 行为 | 聚焦 CLI 测试或命令输出，并确认没有复制 ACP 行为 |
| ACP/runtime 行为 | ACP 测试或探针，并考虑 stdout/stderr 边界 |
| Tool 行为 | 覆盖成功路径和关键失败路径的直接 tool 测试 |
| MCP 加载/配置 | loader 测试，或明确的手动配置/探针记录 |
| Memory 行为 | memory 聚焦测试，必要时包含 config gating 检查 |
| 内置 skill manifest | `uv run python scripts/generate_skills_manifest.py` 和 manifest diff |
| 推荐/on-demand skill | manifest exclusion 检查 + officev3 推荐卡片影响说明 |
| Packaged runtime | runtime build/install/probe 状态，或明确说明只验证源码 |
| Docs-only 改动 | 链接/路径检查和 `git diff --check` |

## 阻塞项

遇到以下情况应 request changes：

- PR 缺少清晰的 Task / Proof / Risk。
- 共享行为在 CLI 和 ACP 中重复实现，且没有强理由。
- 提供的 proof 没覆盖本次改动行为。
- 声称验证了 runtime-sensitive 行为，但没有 runtime 证据。
- 混入生成 graph/cache、日志、凭据、`workspace/` 或本地配置。
- 内置 skills 变化但没有重新生成 `box_agent/skills/_manifest.json`。
- 用户可见行为变化但没有更新相关文档。
- diff 混入无关重构或格式化噪音。

## 常用本地命令

```bash
git diff --check
uv run pytest tests/ -q
uv run pytest tests/test_core.py -q
uv run pytest tests/test_acp.py -q
uv run pytest tests/test_memory.py -q
uv run python scripts/generate_skills_manifest.py
uv run box-agent-build-runtime
```

先运行能证明当前 claim 的最小命令；当改动触及共享行为或 runtime packaging 时，再扩大验证范围。
