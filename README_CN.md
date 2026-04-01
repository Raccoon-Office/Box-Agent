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

| 特性 | Box Agent | Open Interpreter | Aider |
|------|-----------|-----------------|-------|
| 沙箱代码执行 | 隔离 venv 中的 Jupyter 内核 | 在宿主 Python 中运行 | 不支持 |
| 子 Agent 并行 | 多个子 Agent 并发运行 | 不支持 | 不支持 |
| 多 LLM 提供商 | Anthropic、OpenAI、DeepSeek、SiliconFlow 及任何 API | OpenAI + 少量其他 | OpenAI + Anthropic |
| MCP 工具集成 | 原生支持 | 不支持 | 不支持 |
| ACP 协议（嵌入应用） | 完整支持 | 不支持 | 不支持 |
| 独立二进制 | PyInstaller 运行时，无需 Python | 不支持 | 不支持 |
| 上下文压缩 | 双层自动（微压缩 + LLM 摘要） | 手动 | 基于 Git |

## 核心特性

### 子 Agent 并行

将任务委派给隔离的子 Agent 并发运行。每个子 Agent 拥有独立上下文 — 只返回摘要结果。非常适合多文件分析。

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

### 双层上下文压缩
- **第一层 — 微压缩**：每一步自动将旧工具结果（3+ 轮之前）替换为简短占位符。零成本，无需 LLM 调用。
- **第二层 — 自动摘要**：当 Token 数超过阈值（默认 80k）时，由 LLM 对对话进行摘要。原始数据保留在日志中。

### 更多特性
- **MCP 工具**：接入任何 [MCP 服务器](https://github.com/modelcontextprotocol/servers) — 网页搜索、知识图谱、数据库
- **Claude Skills**：11 种内置技能，涵盖文档处理（DOCX、PDF、PPTX、XLSX）、画布设计、Web 应用测试等
- **ACP 协议**：通过 JSON-RPC over stdio 将 Box Agent 嵌入 Electron 应用、Zed 编辑器或任何 ACP 兼容宿主
- **独立运行时**：PyInstaller 二进制打包 Python 及所有依赖。无需外部 Python — 下载即用
- **跨会话记忆**：持久化记忆让 Agent 在多次对话间保留关键信息
- **安全防护**：危险命令检测、工作区范围控制、文件修改前自动备份
- **任务追踪**：内置 Todo 工具，支持多步骤任务分解与进度跟踪

## 演示

### 任务执行
*Agent 创建网页并在浏览器中打开。*

![演示: 任务执行](docs/assets/demo1-task-execution.gif)

### Claude Skill — PDF 生成
*Agent 使用技能创建专业文档。*

![演示: Claude Skill](docs/assets/demo2-claude-skill.gif)

### MCP 网页搜索
*Agent 搜索网页并总结结果。*

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
git submodule update --init --recursive   # 可选：加载技能
uv run python -m box_agent.cli
```

### 配置

运行 `box-agent setup` 后，配置文件位于 `~/.box-agent/config/config.yaml`：

```yaml
api_key: "your-api-key"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic"           # "anthropic" 或 "openai"
max_steps: 50
```

```bash
box-agent config           # 查看当前配置
box-agent config --edit    # 用编辑器打开配置
box-agent doctor           # 检查环境与 API 连通性
```

## CLI 用法

```bash
# 交互模式
box-agent
box-agent --workspace /path/to/project
box-agent --sandbox              # 启用 Jupyter 沙箱

# 非交互模式（CI/CD、脚本）
box-agent --task "分析 data.csv 并生成报告"

# 子命令
box-agent setup     # 配置向导
box-agent config    # 查看/编辑配置
box-agent doctor    # 健康检查
box-agent log       # 打开日志目录
```

会话内命令：`/help`、`/clear`、`/history`、`/stats`、`/log`、`/exit`

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
# 下载预构建二进制
gh release download v0.6.7 --repo Raccoon-Office/Box-Agent --pattern "box-agent-runtime-*.tar.gz"

# 或从源码构建
uv run python scripts/build_runtime.py
```

运行时通过 JSON-RPC over stdio 通信。stdout = 纯协议数据，stderr = 诊断信息。

## 测试

```bash
pytest tests/ -v          # 所有测试
pytest tests/test_core.py -v   # 核心 + 上下文压缩
pytest --cov              # 带覆盖率
```

## 常见问题

**SSL 证书错误**：`pip install --upgrade certifi` 或在测试环境设置 `verify=False`。

**模块未找到**：确保在项目目录下运行：`cd Box-Agent && uv run python -m box_agent.cli`

## 贡献

欢迎提交 Issue 和 Pull Request！详见 [贡献指南](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)

## 链接

- [PyPI](https://pypi.org/project/box-agent/) · [GitHub](https://github.com/Raccoon-Office/Box-Agent) · [Releases](https://github.com/Raccoon-Office/Box-Agent/releases)
- [Anthropic API](https://docs.anthropic.com/claude/reference) · [MCP Servers](https://github.com/modelcontextprotocol/servers) · [ACP Protocol](https://github.com/nichochar/agent-client-protocol)

---

**如果这个项目对你有帮助，请给它一个 ⭐！**
