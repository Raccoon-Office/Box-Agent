<p align="center">
  <h1 align="center">Box Agent</h1>
  <p align="center">通用 AI Agent 框架，支持沙箱代码执行、子 Agent 并行和多 LLM 提供商。</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/v/box-agent?color=orange" alt="PyPI"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/dm/box-agent?color=brightgreen" alt="Downloads"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/pyversions/box-agent?color=blue" alt="Python"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Raccoon-Office/Box-Agent?color=green" alt="License"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/releases"><img src="https://img.shields.io/github/v/release/Raccoon-Office/Box-Agent?color=blue" alt="Release"></a>
</p>

<p align="center">
  <a href="./README.md">English</a> | 中文
</p>

---

**30 秒快速上手：**

```bash
uv tool install box-agent   # 或: pip install box-agent (需 Python 3.10+)
box-agent setup              # 交互式配置向导
box-agent                    # 开始对话
```

或执行单次任务：

```bash
box-agent --task "分析 sales.csv — 按收入展示前 10 名产品的柱状图"
```

---

## 为什么选择 Box Agent？

大多数 Agent 框架要么太简单（无沙箱、无工具），要么太复杂（依赖臃肿、架构僵化）。Box Agent 恰好取得了平衡：

| 特性                 | Box Agent                                           | Open Interpreter     | Aider              |
| -------------------- | --------------------------------------------------- | -------------------- | ------------------ |
| 沙箱代码执行         | 隔离 venv 中的 Jupyter 内核                         | 在宿主 Python 中运行 | 不支持             |
| 子 Agent 并行        | 多个子 Agent 并发运行                               | 不支持               | 不支持             |
| 多 LLM 提供商        | Anthropic、OpenAI、DeepSeek、SiliconFlow 及任何 API | OpenAI + 少量其他    | OpenAI + Anthropic |
| MCP 工具集成         | 原生支持                                            | 不支持               | 不支持             |
| ACP 协议（嵌入应用） | 完整支持                                            | 不支持               | 不支持             |
| 独立二进制           | PyInstaller 运行时，无需 Python                     | 不支持               | 不支持             |
| 上下文压缩           | 分阶段自动压缩 + LLM 摘要                            | 手动                 | 基于 Git           |

## 核心特性

### 子 Agent 并行

把隔离工作委派给子 Agent，并按需声明顶层 Skills、工具子集以及步骤/工具调用
硬预算。所有子 Agent 都运行相同的通用 agent loop 并继承父 Agent 的策略边界；
省略 `required_tools` 时继承父 Agent 当前全部工具，显式列表则收窄工具边界。
父 Agent 始终负责冲突处理、最终交付物和验证。

```
用户: "分别分析 data1.csv、data2.csv 和 data3.csv，然后给出综合总结"

┌─ 子 Agent 1 ──────┐  ┌─ 子 Agent 2 ──────┐  ┌─ 子 Agent 3 ──────┐
│ 读取 data1.csv      │  │ 读取 data2.csv      │  │ 读取 data3.csv      │
│ 运行统计分析        │  │ 运行统计分析        │  │ 运行统计分析        │
│ 生成图表            │  │ 生成图表            │  │ 生成图表            │
│ → 摘要: ...         │  │ → 摘要: ...         │  │ → 摘要: ...         │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
                              ↓ 并行 ↓
                    ┌─ 父 Agent ────────────┐
                    │ 汇总 3 份摘要          │
                    │ 生成最终报告           │
                    └─────────────────────────┘
```

新式委派默认拒绝扩权（`read_only: true`、`network: false`、
`external_side_effect: false`）。完整 schema、限制、兼容行为与宿主诊断见
[子 Agent 委派契约](docs/SUB_AGENT_DELEGATION_CN.md)。

### 沙箱代码执行

Python 运行在隔离的 Jupyter 内核中，预装数据科学包（`pandas`、`numpy`、`matplotlib`、`scikit-learn`、`openpyxl`、`xlrd`）。生成的文件（图表、CSV、PDF）会被自动检测并以结构化 Artifact 呈现。

### 多 LLM 提供商

一份配置，任意切换：

```yaml
# Anthropic
api_base: "https://api.anthropic.com"
provider: "anthropic"
model: "claude-sonnet-4-20250514"

# DeepSeek
api_base: "https://api.deepseek.com"
provider: "openai"
model: "deepseek-chat"

# 任何 OpenAI 兼容端点
api_base: "https://your-api.example.com/v1"
provider: "openai"
model: "your-model"
```

### 分阶段上下文压缩

- **Layer 0 — 大型工具结果**：生成型产物的读取结果在进入模型历史前压缩；工具调用参数不再被单独压缩，在整段历史摘要替换其所在轮次前会保留原文。
- **Layer 1 — 微压缩**：每一步自动将更旧的工具结果替换为简短占位符。零成本，无需 LLM 调用。
- **Layer 2 — 自动摘要**：当 Token 数超过推导阈值时触发（用户自配 endpoint 默认约 104k token），由 LLM 对对话进行摘要。原始数据保留在日志中。
- **旧历史安全保护**：如果模型错误地把旧版或外部 session 中的内部历史占位符当成文件或代码参数复用，Box-Agent 会拒绝执行并请求一次干净重生成，避免占位符被写入磁盘。

### 更多特性

- **MCP 工具**：接入任何 [MCP 服务器](https://github.com/modelcontextprotocol/servers) — 网页搜索、知识图谱、数据库
- **Claude Skills**：32 种内置技能，涵盖文档处理（DOCX、PDF、PPTX、XLSX）、画布设计、Obsidian、Web 应用测试等
- **ACP 协议**：通过 JSON-RPC over stdio 将 Box Agent 嵌入 Electron 应用、Zed 编辑器或任何 ACP 兼容宿主
- **独立运行时**：PyInstaller 二进制打包 Python 及所有依赖。无需外部 Python — 下载即用
- **跨会话记忆**：持久化记忆让 Agent 在多次对话间保留关键信息
- **安全防护**：危险命令检测、工作区范围控制、文件修改前自动备份。工作区外访问支持交互式权限协商（CLI 终端询问用户，ACP 反向 RPC 询问宿主）
- **结构化计划**：内置 Plan 工具，支持宿主渲染目标、范围、步骤、验证方式和风险
- **任务追踪**：内置 Todo 工具，支持多步骤任务分解与进度跟踪

## 演示

### 任务执行

_Agent 创建网页并在浏览器中打开。_

![演示: 任务执行](docs/assets/demo1-task-execution.gif)

### Claude Skill — PDF 生成

_Agent 使用技能创建专业文档。_

![演示: Claude Skill](docs/assets/demo2-claude-skill.gif)

### MCP 网页搜索

_Agent 搜索网页并总结结果。_

![演示: 网页搜索](docs/assets/demo3-web-search.gif)

## 安装

> **需要 Python 3.10+。** 如果系统 Python 版本较低（如 3.9），请使用 `uv tool install` — 它会自动管理 Python 版本。

### 快速安装（uv，推荐）

[uv](https://docs.astral.sh/uv/) 会自动管理 Python 版本，无需升级系统 Python：

```bash
# 安装 uv（如尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装 box-agent（如需要会自动下载 Python 3.10+）
uv tool install box-agent
box-agent setup    # 交互式配置向导
box-agent          # 开始对话

# 后续升级
uv tool upgrade box-agent
```

### 快速安装（pip）

如果已有 Python 3.10+：

```bash
pip install box-agent
box-agent setup
box-agent
```

### 从源码安装

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
uv sync
uv run python -m box_agent.cli
```

## 新协作者快速开始

如果你是新加入的协作者，改代码前先从这里开始：

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
git submodule update --init --recursive   # 使用内置 skills 时需要
uv sync
uv run python -m box_agent.cli --help
uv run pytest tests/test_core.py -q
```

建议先读这几个文件：

- `AGENTS.md` — 仓库本地开发规则、范围控制和验证要求。
- `CONTRIBUTING_CN.md` — 贡献流程、PR checklist 和提交信息格式。
- `docs/REVIEW_GUIDE_CN.md` — 维护者 review 顺序、阻塞项和 proof 要求。
- `docs/DEVELOPMENT_GUIDE_CN.md` — 更完整的架构与开发说明。
- `docs/INTEGRATION.md` — ACP / 独立运行时与宿主应用集成说明。

项目地图：

| 模块 | 入口文件 |
| ---- | -------- |
| Agent 执行循环 | `box_agent/core.py`、`box_agent/agent.py`、`box_agent/events.py` |
| CLI 与配置 | `box_agent/cli.py`、`box_agent/config.py`、`box_agent/config/` |
| LLM Provider | `box_agent/llm/` |
| 内置工具 | `box_agent/tools/` |
| ACP 服务与运行时嵌入 | `box_agent/acp/`、`box_agent/build_runtime_cli.py` |
| Skills | `box_agent/skills/`、`box_agent/tools/skill_loader.py` |
| 测试 | `tests/test_<area>.py` |

日常开发循环：

```bash
# 迭代时先跑最小相关测试
uv run pytest tests/test_bash_tool.py -q

# 交付前跑更宽的测试
uv run pytest tests/ -q

# 检查空白字符和 patch 格式问题
git diff --check
```

按改动范围选择测试：工具改动看 `tests/test_*_tool.py`，LLM 行为看
`tests/test_llm*.py` / `tests/test_error_messages.py`，ACP 看
`tests/test_acp*.py`，memory 看 `tests/test_memory*.py`，运行时打包看
`tests/test_build_runtime.py` / `tests/test_cli_runtime.py`。需要真实 provider
凭据的 integration 测试在没有对应 API key 时会跳过。

如果改动会影响宿主应用使用的独立运行时，只改源码还不够：需要重新打包运行时、
安装到宿主应用、重启正在运行的 ACP 进程，然后探测已安装的运行时。本地打包命令：

```bash
uv run box-agent-build-runtime
```

### 配置

运行 `box-agent setup` 后，配置文件位于 `~/.box-agent/config/config.yaml`：

```yaml
api_key: "your-api-key"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic" # "anthropic" 或 "openai"
max_steps: 200
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
goal_autopilot_enabled: true
goal_autopilot_max_turns: 3
goal_autopilot_max_seconds: 14400
goal_autopilot_no_progress_turns: 2
```

main agent 与 subagent 使用同一个由模型配置推导出的上下文阈值。请通过
`context_window` 和 `max_output_tokens` 配置该共享阈值，不再提供独立的子代理
上下文窗口配置。

```bash
box-agent config                    # 查看当前配置摘要
box-agent config --get model        # 打印单个配置值
box-agent config --set max_steps 300
box-agent config --set goal_autopilot_max_turns 5
box-agent config --json             # 机器可读配置摘要
box-agent config --edit             # 用编辑器打开配置
box-agent doctor                    # 检查环境与 API 连通性
box-agent doctor --json             # 机器可读健康检查
```

## CLI 用法

```bash
# 交互模式
box-agent
box-agent --workspace /path/to/project
box-agent --no-sandbox           # 关闭 Jupyter 沙箱

# 非交互模式（CI/CD、脚本）
box-agent --task "分析 data.csv 并生成报告"
box-agent --task "分析 data.csv" --json          # 追加执行摘要 JSON
box-agent --task "本地文件任务" --no-verify-api  # 跳过启动时 API 探测
box-agent --task "生成一份 PPT" --force-plan-start  # 工作前先发布计划
box-agent --task "生成一份 PPT" --no-completion-gate
box-agent --goal "补齐 CLI 能力" --task "跑完测试"
box-agent --goal "补齐 CLI 能力" --task "跑完测试" --no-goal-autopilot
box-agent --deep-think --task "审查这个仓库"      # 支持时启用 thinking 模式

# 子命令
box-agent setup             # 配置向导
box-agent config            # 查看/编辑配置
box-agent doctor            # 健康检查
box-agent log               # 打开日志目录
box-agent goal status       # 查看当前工作区持久目标
box-agent goal complete --evidence "测试已通过"
box-agent install-browser   # 安装 Playwright MCP 所需 Chromium（约 200MB）
box-agent install-node      # 安装技能脚本使用的托管 Node.js 运行时（macOS）
```

会话内命令：`/help`、`/clear`、`/clear_all`、`/history`、`/stats`、`/sandbox_status`、`/log`、`/goal`、`/memory review`、`/exit`

使用 `/goal <目标>` 或 `--goal "<目标>"` 可以给当前工作区设置持久目标。CLI 会把目标保存到 `~/.box-agent/goals/`，后续 turn 会自动带上；可用 `/goal pause`、`/goal resume`、`/goal block <原因>`、`/goal complete <证据>`、`/goal clear` 管理，也可以用 `box-agent goal ...` 做脚本化管理。

在非交互 `--task` 模式和 ACP 会话里，active goal 会启用有边界的自动续跑：如果一轮自然结束但 goal 仍是 `active`，Box-Agent 会在同一个 session 内自动继续，直到模型把 goal 标记为 `complete`、标记为 `blocked`、用户取消，达到 `goal_autopilot_max_turns` / `goal_autopilot_max_seconds`，或连续 `goal_autopilot_no_progress_turns` 个自动续跑轮次没有记录到 goal 进展。单次 CLI 运行可用 `--no-goal-autopilot` 关闭，也可以在配置中设置 `goal_autopilot_enabled: false`。

## ACP 与编辑器集成

Box Agent 支持 [Agent Communication Protocol](https://github.com/nichochar/agent-client-protocol)，可嵌入编辑器和应用。

**Zed Editor** — 在 `settings.json` 中添加：

```json
{
  "agent_servers": {
    "box-agent": {
      "command": "/path/to/box-agent-acp"
    }
  }
}
```

**独立运行时** — 用于 Electron 应用和其他宿主：

```bash
# 下载预构建二进制（最新发布；省略 tag 即自动取最新版本）
gh release download --repo Raccoon-Office/Box-Agent --pattern "box-agent-runtime-*.tar.gz"

# 或从源码构建（当前平台）
uv run box-agent-build-runtime

# 在 Apple Silicon 上构建 macOS Intel/x64 运行时
# 需要单独的 x86_64 venv —— PyInstaller 无法把 arm64 wheel 塞进 x64 产物。
# 一次性准备：
#   arch -x86_64 /bin/bash -c 'curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$HOME/.local/bin-x64" sh'
#   UV_PROJECT_ENVIRONMENT=.venv-x64 arch -x86_64 ~/.local/bin-x64/uv sync
# 打包：
UV_PROJECT_ENVIRONMENT=.venv-x64 BOX_AGENT_RUNTIME_TARGET=darwin-x64 arch -x86_64 ~/.local/bin-x64/uv run box-agent-build-runtime
```

运行时通过 JSON-RPC over stdio 通信。stdout = 纯协议数据，stderr = 诊断信息。

## 测试

```bash
uv run pytest tests/ -v          # 所有测试
uv run pytest tests/test_core.py -v   # 核心 + 上下文压缩
uv run pytest --cov              # 带覆盖率
```

## 常见问题

**SSL 证书错误**：`pip install --upgrade certifi` 或在测试环境设置 `verify=False`。

**模块未找到**：确保在项目目录下运行：`cd Box-Agent && uv run python -m box_agent.cli`

## 贡献

欢迎提交 Issue 和 Pull Request！详见 [贡献指南](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)

## 链接

- [文档索引](docs/README.md)
- [PyPI](https://pypi.org/project/box-agent/) · [GitHub](https://github.com/Raccoon-Office/Box-Agent) · [Releases](https://github.com/Raccoon-Office/Box-Agent/releases)
- [Anthropic API](https://docs.anthropic.com/claude/reference) · [MCP Servers](https://github.com/modelcontextprotocol/servers) · [ACP Protocol](https://github.com/nichochar/agent-client-protocol)

---

**如果这个项目对你有帮助，请给它一个 ⭐！**
