# Box-Agent PR 审查规范

本规范适用于 Box-Agent 的所有非平凡 Pull Request，以及由 Review Agent
生成的自动审查。目标是让合并决策基于可复现证据，并在核心、CLI、ACP、Tool、
Skill 与 packaged runtime 之间保持清晰的责任边界。

文档中的“必须”是合并门禁；“应该”允许维护者在 PR 的 Risk 中记录理由后例外。

## 1. 审查角色与职责

| 角色 | 职责 | 不应做的事 |
| --- | --- | --- |
| PR 作者 | 提供单一范围的改动、TPR、测试和风险说明 | 把补测试、定位范围或生成 proof 转交给 reviewer |
| 本地 Preflight CI | 在准确的 PR Head 上执行确定性安装、编译、测试和构建 | 评价设计、修改代码或替代人工审查 |
| Review Agent | 基于 diff、源码、测试和文档发现具体问题 | 在 Preflight 未通过时继续消耗资源做完整 review；直接修改 PR |
| 人工维护者 | 复核高严重度问题、接受残余风险并作最终合并决定 | 仅凭 Agent 的 `APPROVE` 跳过证据检查 |

所有 Review Agent 的结论都是决策输入，最终合并权限属于人工维护者。

## 2. 合并门禁与执行顺序

PR 必须依次通过以下门禁。前一层未通过时，后续自动 Agent 不启动。

1. **G0 — PR 元数据**：标题、范围和 TPR 完整。
2. **G1 — 本地 Preflight CI**：GitHub required status
   `teamwork/local-ci` 为 `success`。
3. **G2 — Review Agent**：没有未解决的 P0/P1/P2 阻塞问题。
4. **G3 — 人工维护者**：确认 ownership、proof、risk 和残余风险可接受。
5. **G4 — GitHub 合并保护**：required checks 和 required reviews 全部满足。

Preflight 在 detached 临时 worktree 中校验准确的 PR Head SHA，默认步骤为：

```bash
uv sync --frozen --all-extras
uv run python -m compileall -q box_agent
uv run pytest tests/ -q --tb=short \
  --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server
uv build
```

首个失败步骤阻止后续步骤和 Review Agent。代码失败或超时写入 GitHub
`failure`；基础设施故障写入 `error` 并按策略重试。同一仓库、PR、Head SHA
和配置版本复用终态，不得因为扫描周期重复执行。

> 当前本地执行器只用于可信内部成员的 PR，并运行在专用 WSL 服务账号下。
> 临时 worktree 和环境过滤不是运行不可信 fork 的系统级沙箱。

## 3. TPR：开始审查前的必填信息

每个非平凡 PR 必须提供：

### Task

- 改变了什么可观察行为，为什么需要改变。
- 影响哪些入口、模块和调用方。
- 明确哪些内容不在本 PR 范围内。

### Proof

- 精确列出实际执行过的命令及结果，而不是“测试应该通过”。
- 给出能证明本次行为的聚焦回归测试。
- 按影响范围补充全量测试、运行探针、截图、日志或 manifest diff。
- 未执行的验证必须说明原因和影响，不能静默省略。

### Risk

- 兼容性、配置、Secret、数据或协议迁移影响。
- source-only 与 packaged runtime 的差异。
- 回滚方式和跨仓库后续事项。
- 已知但接受的限制与残余风险。

Task、Proof 或 Risk 缺失时，先要求补齐，不开始深入风格审查。

## 4. Review Agent 标准流程

Review Agent 必须按以下顺序工作：

1. 确认当前 Head 的 `teamwork/local-ci` 已通过，并记录状态 context 与 SHA。
2. 确认 base/head 和实际 diff 范围，排除无关改动、生成物和本地文件。
3. 读取 TPR，建立“需求 → 改动 → proof → risk”的对应关系。
4. 使用 `.understand-anything/knowledge-graph.json` 定位相关模块，并比较
   `project.gitCommitHash` 与当前 Head；图谱只作为索引，结论必须由真实源码、
   测试或探针验证。
5. 先检查正确性、安全性、兼容性和 ownership，再检查可维护性与风格。
6. 只报告具有明确触发条件、用户影响和修复方向的问题。
7. 按第 8 节模板输出结论，不得只给模糊的“看起来没问题”。

Review Agent 默认只读。除非任务明确要求修复，否则不得提交代码、推送分支、
修改 PR 或改变 GitHub 状态。

## 5. Ownership 与架构边界

- 共享 Agent 循环、事件、调度、取消、tool-call closure、goal 和 completion
  gate 属于稳定核心，例如 `box_agent/core.py`、`box_agent/events.py` 和共享 helper。
- CLI 只负责终端 UX、slash commands、渲染和本地提示，不应复制 ACP 也需要的行为。
- ACP 负责把共享事件翻译成 protocol updates 和 host extension methods；stdout
  必须保持纯协议输出，诊断信息进入 stderr 或结构化日志。
- Provider wire 行为属于 `box_agent/llm/`，不得扩散到 Tool、Skill、CLI 或 ACP。
- Tool 语义属于 `box_agent/tools/`，并应返回结构化 `ToolResult`。
- Skill 加载属于 `box_agent/skill_loader.py`、`box_agent/skills/` 和
  `box_agent/skills/_manifest.json`。
- PPT/文档生成默认由 Skill 驱动；除非 contract 变化，不向核心循环添加隐藏的
  PPT 专用模式。
- officev3 或 standalone runtime 使用的行为，不能只靠源码测试证明。

共享核心、协议、安全强制点或 packaged runtime 的变化必须由相应 owner 复核。

## 6. 不同改动要求的最小 Proof

| 改动类型 | 最小 Proof |
| --- | --- |
| 共享核心循环、事件、取消、goal、Session Log | 聚焦回归测试 + 相关 core/持久化/ACP 测试 |
| CLI-only 行为 | 聚焦 CLI 测试或命令输出 + 无 ACP 重复实现的确认 |
| ACP/runtime 行为 | ACP 测试或真实探针 + stdout/stderr 边界检查 |
| Tool 行为 | 成功路径、关键失败路径和工作区/权限边界的直接测试 |
| Provider 行为 | wire-format 测试、错误映射、超时/重试和 Secret 暴露检查 |
| MCP 加载/配置 | loader 测试或明确的手动配置/连接探针 |
| Memory 行为 | memory 聚焦测试 + 适用时的 config gating 检查 |
| 内置 Skill | 聚焦 Skill 测试 + `scripts/generate_skills_manifest.py` + manifest diff |
| 推荐/on-demand Skill | manifest exclusion + officev3 推荐卡片影响说明 |
| PPT/文档生成 | 对应 Skill 测试、contract/manifest 校验，必要时视觉或 runtime probe |
| Packaged runtime | runtime build/install/probe，或明确记录 source-only 限制 |
| 配置/Schema | 向后兼容、默认值、非法输入和迁移测试 |
| Docs-only | 链接/路径检查 + `git diff --check` |

先运行能证明 claim 的最小命令，再根据影响范围扩大验证。测试通过不能替代对需求、
ownership 和风险的审查。

## 7. Finding 严重级别

| 级别 | 定义 | 合并处理 |
| --- | --- | --- |
| P0 Critical | 可导致凭据泄露、任意代码执行、数据不可逆损坏、权限绕过或大面积不可用 | 必须修复并重新完整审查 |
| P1 High | 确定的功能错误、协议破坏、CI/runtime 无法运行、竞态或主要兼容性回归 | 必须修复并补回归 proof |
| P2 Medium | 特定但现实条件下的错误、资源泄漏、错误处理缺口或显著维护风险 | 默认阻塞；维护者可在 Risk 中显式接受 |
| P3 Low | 不影响正确性的局部清晰度、性能或一致性改进 | 不阻塞，可转 follow-up |

每条 finding 必须包含：

- **位置**：最小必要文件和行号。
- **触发条件**：什么输入、平台或事件顺序会发生。
- **影响**：具体破坏哪个行为、用户或 contract。
- **证据**：源码路径、失败测试、日志或可复现探针。
- **建议**：说明修复方向，不要求 reviewer 代写完整实现。

以下内容不能作为阻塞 finding：纯个人风格偏好、无法复现的猜测、没有行为影响的
命名意见，以及已经由现有 proof 排除的理论风险。

## 8. Review Agent 输出格式

```markdown
# PR 审查结论

Verdict: APPROVE | REQUEST_CHANGES | COMMENT
Reviewed SHA: <40-character head sha>
Preflight: success | failure | error | missing
TPR: complete | incomplete | not provided by runtime

## TPR 检查
- Task: complete | incomplete | unavailable
- Proof: sufficient | insufficient | unavailable
- Risk: complete | incomplete | unavailable

## 阻塞问题
- [P1] 标题 — `path/to/file.py:123`
  - 触发条件：...
  - 影响：...
  - 证据：...
  - 建议：...

## 非阻塞建议
- [P3] ...

## 验证证据
- `<实际执行的命令>` — passed/failed/not run

## 残余风险
- ...
```

没有阻塞问题时写“无”，不要省略章节。不得声称未实际执行的命令通过。

## 9. Verdict 判定

| 条件 | Verdict |
| --- | --- |
| Preflight 缺失、`failure` 或 `error` | `REQUEST_CHANGES`，不启动完整 Review Agent |
| TPR 缺失或 Proof 无法证明行为 | `REQUEST_CHANGES` |
| 运行上下文未提供 TPR，且没有已证实阻塞问题 | `COMMENT`，等待人工补充证据 |
| 存在未解决 P0/P1 | `REQUEST_CHANGES` |
| 存在 P2 且未被维护者显式接受 | `REQUEST_CHANGES` |
| 仅有 P3 或无 finding，所有门禁满足 | `APPROVE` |
| 仅需提问且当前没有已证实阻塞问题 | `COMMENT` |

修复后必须基于新的 Head SHA 重新运行 Preflight；旧 SHA 的 success 或 review
不能复用。Review Agent 复审时应验证原 finding 的触发条件，而不是只检查 diff
中是否出现了看似相关的修改。

## 10. 必须 Request Changes 的情况

- PR 缺少清晰的 Task / Proof / Risk。
- `teamwork/local-ci` 未成功，或状态对应的不是当前 Head SHA。
- 提供的 proof 没覆盖改动行为或关键失败路径。
- 共享行为在 CLI 和 ACP 中重复实现且没有强理由。
- runtime-sensitive 行为声称已验证但没有 runtime 证据。
- 引入凭据暴露、路径逃逸、命令注入或权限边界回归。
- 内置 Skill 变化但没有重新生成并审查 `_manifest.json`。
- 用户可见、协议或 contributor-facing 行为变化但没有更新文档。
- 混入无关重构、格式化噪音、本地配置、日志、`workspace/`、Secret，或不应提交的
  `.understand-anything` 本地产物。
- 图谱、metadata 和 fingerprints 被单独手改，或刷新基线彼此不一致。

## 11. 常用验证命令

```bash
# Diff 和基础语法
git diff --check
uv run python -m compileall -q box_agent

# 完整 Preflight 测试命令
uv run pytest tests/ -q --tb=short \
  --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server

# 常见聚焦测试
uv run pytest tests/test_core.py -q
uv run pytest tests/test_acp.py -q
uv run pytest tests/test_memory.py -q

# Skill manifest 与包构建
uv run python scripts/generate_skills_manifest.py
uv build

# 仅在 packaged runtime 受影响时
uv run box-agent-build-runtime
```

审查结论必须记录实际执行结果、失败数量和未执行原因。Preflight 成功只证明确定性
命令通过；它不等于架构、行为、安全和风险审查自动通过。
