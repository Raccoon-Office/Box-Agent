# Box Agent

[English](./README.md) | 中文

**Box Agent** 是一个极简但专业的 Agent 框架，支持多种 LLM 提供商（Anthropic、OpenAI 兼容等）。项目支持交错思维（interleaved thinking）、工具调用和完整的 Agent 循环，适用于处理复杂任务。

该项目具备一系列为稳健、智能的 Agent 开发而设计的特性：

*   ✅ **完整的 Agent 执行循环**：一个完整可靠的执行框架，配备了文件系统和 Shell 操作的基础工具集。
*   ✅ **持久化记忆**：通过内置的 **Session Note Tool**，Agent 能够在多个会话中保留关键信息。
*   ✅ **智能上下文管理**：自动对会话历史进行摘要，可处理长达可配置 Token 上限的上下文，从而支持无限长的任务。
*   ✅ **集成 Claude Skills**：内置 15 种专业技能，涵盖文档处理、设计、测试和开发等领域。
*   ✅ **集成 MCP 工具**：原生支持 MCP 协议，可轻松接入知识图谱、网页搜索等工具。
*   ✅ **全面的日志记录**：为每个请求、响应和工具执行提供详细日志，便于调试。
*   ✅ **简洁明了的设计**：美观的命令行界面和易于理解的代码库，使其成为构建高级 Agent 的理想起点。

## 目录

- [Box Agent](#box-agent)
  - [目录](#目录)
  - [快速开始](#快速开始)
    - [1. 获取 API Key](#1-获取-api-key)
    - [2. 选择使用模式](#2-选择使用模式)
      - [🚀 快速上手模式（推荐新手）](#-快速上手模式推荐新手)
      - [🔧 开发模式](#-开发模式)
  - [ACP \& Zed Editor 集成（可选）](#acp--zed-editor-集成可选)
  - [使用示例](#使用示例)
    - [任务执行](#任务执行)
    - [使用 Claude Skill（例如：PDF 生成）](#使用-claude-skill例如pdf-生成)
    - [网页搜索与摘要（MCP 工具）](#网页搜索与摘要mcp-工具)
  - [测试](#测试)
    - [快速运行](#快速运行)
    - [测试覆盖范围](#测试覆盖范围)
  - [常见问题](#常见问题)
    - [SSL 证书错误](#ssl-证书错误)
    - [模块未找到错误](#模块未找到错误)
  - [相关文档](#相关文档)
  - [社区](#社区)
  - [贡献](#贡献)
  - [许可证](#许可证)
  - [参考资源](#参考资源)

## 快速开始

### 1. 获取 API Key

从您选择的 LLM 提供商获取 API Key：

| 提供商         | 平台地址                                                                 | API Base                          |
| -------------- | ------------------------------------------------------------------------ | --------------------------------- |
| **Anthropic**  | [https://console.anthropic.com](https://console.anthropic.com)           | `https://api.anthropic.com`       |
| **OpenAI**     | [https://platform.openai.com](https://platform.openai.com)              | `https://api.openai.com/v1`       |
| **DeepSeek**   | [https://platform.deepseek.com](https://platform.deepseek.com)          | `https://api.deepseek.com`        |
| **SiliconFlow** | [https://siliconflow.cn](https://siliconflow.cn)                       | `https://api.siliconflow.cn/v1`   |

**获取步骤：**
1. 访问相应平台注册并登录
2. 进入 API Key 管理页面
3. 创建新密钥并妥善保存

### 2. 选择使用模式

**前置要求：安装 uv**

两种使用模式都需要 uv。如果您尚未安装：

```bash
# macOS/Linux/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
python -m pip install --user pipx
python -m pipx ensurepath
# 安装后需要重启 PowerShell

# 安装完成后，重启终端或运行：
source ~/.bashrc  # 或 ~/.zshrc (macOS/Linux)
```

我们提供两种使用模式，请根据您的需求选择：

#### 🚀 快速上手模式（推荐新手）

此模式适合希望快速体验 Box Agent，而无需克隆代码仓库或修改代码的用户。

**安装步骤：**

```bash
# 1. 直接从 GitHub 安装
uv tool install git+https://github.com/Raccoon-Office/Box-Agent.git

# 2. 运行交互式配置向导（自动创建配置文件并引导设置 Provider/API Key）
box-agent setup
```

> 💡 **提示**：如果您希望在本地进行开发或修改代码，请使用下方的"开发模式"。

**配置步骤：**

配置向导会在 `~/.box-agent/config/` 目录下创建配置文件，您可以随时查看或编辑：

```bash
box-agent config          # 查看当前配置
box-agent config --edit   # 用编辑器打开配置文件
```

填入您的 API Key 和对应的 API Base：

```yaml
api_key: "YOUR_API_KEY_HERE"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic"
```

**开始使用：**

```bash
box-agent                                    # 使用当前目录作为工作空间
box-agent --workspace /path/to/your/project  # 指定工作空间目录
box-agent doctor                             # 检查环境与连通性
box-agent --version                          # 查看版本信息

# 管理命令
uv tool upgrade box-agent                    # 升级到最新版本
uv tool uninstall box-agent                  # 卸载工具（如需要）
uv tool list                                  # 查看所有已安装的工具
```

#### 🔧 开发模式

此模式适合需要修改代码、添加功能或进行调试的开发者。

**安装与配置步骤：**

```bash
# 1. 克隆仓库
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent

# 2. 安装 uv（如果尚未安装）
# macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
irm https://astral.sh/uv/install.ps1 | iex
# 安装后需要重启终端

# 3. 同步依赖
uv sync

# 替代方案: 手动安装依赖（如果不使用 uv）
# pip install -r requirements.txt
# 或者安装必需的包:
# pip install tiktoken pyyaml httpx pydantic requests prompt-toolkit mcp

# 4. 初始化 Claude Skills（可选）
git submodule update --init --recursive

# 5. 复制配置模板
```

**macOS/Linux:**
```bash
cp box_agent/config/config-example.yaml box_agent/config/config.yaml
```

**Windows:**
```powershell
Copy-Item box_agent\config\config-example.yaml box_agent\config\config.yaml

# 6. 编辑配置文件
vim box_agent/config/config.yaml  # 或使用您偏好的编辑器
```

填入您的 API Key 和对应的 API Base：

```yaml
api_key: "YOUR_API_KEY_HERE"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic"
max_steps: 100
workspace_dir: "./workspace"
```

> 📖 完整的配置指南，请参阅 [config-example.yaml](box_agent/config/config-example.yaml)

**运行方式：**

选择您偏好的方式运行：

```bash
# 方式 1：作为模块直接运行（适合调试）
uv run python -m box_agent.cli

# 方式 2：以可编辑模式安装（推荐）
uv tool install -e .
# 安装后，您可以在任何路径下运行，且代码更改会立即生效
box-agent
box-agent --workspace /path/to/your/project
```

> 📖 更多开发指引，请参阅 [开发指南](docs/DEVELOPMENT_GUIDE_CN.md)

> 📖 更多生产部署指引，请参阅 [生产指南](docs/PRODUCTION_GUIDE_CN.md)

## ACP & Zed Editor 集成（可选）

Box Agent 支持 [Agent Communication Protocol (ACP)](https://github.com/modelcontextprotocol/protocol)，可与 Zed 等代码编辑器集成。

**在 Zed Editor 中设置：**

1. 以开发模式或工具模式安装 Box Agent
2. 在您的 Zed `settings.json` 中添加：

```json
{
  "agent_servers": {
    "box-agent": {
      "command": "/path/to/box-agent-acp"
    }
  }
}
```

命令路径应为：
- 通过 `uv tool install` 安装：使用 `which box-agent-acp` 的输出结果
- 开发模式：`./box_agent/acp/server.py`

**使用方法：**
- 使用 `Ctrl+Shift+P` → "Agent: Toggle Panel" 打开 Zed 的 Agent 面板
- 从 Agent 下拉列表中选择 "box-agent"
- 直接在编辑器中开始与 Box Agent 对话

## 使用示例

这里有几个 Box Agent 能力的演示。

### 任务执行

*在这个演示中，我们要求 Agent 创建一个简洁美观的网页并在浏览器中显示它，以此展示基础的工具使用循环。*

![演示动图 1: 基础任务执行](docs/assets/demo1-task-execution.gif "基础任务执行演示")

### 使用 Claude Skill（例如：PDF 生成）

*这里，Agent 利用 Claude Skill 根据用户请求创建专业文档（如 PDF 或 DOCX），展示了其强大的高级能力。*

![演示动图 2: Claude Skill 使用](docs/assets/demo2-claude-skill.gif "Claude Skill 使用演示")

### 网页搜索与摘要（MCP 工具）

*此演示展示了 Agent 如何使用其网页搜索工具在线查找最新信息，并为用户进行总结。*

![演示动图 3: 网页搜索](docs/assets/demo3-web-search.gif "网页搜索演示")


## 测试

项目包含了覆盖单元测试、功能测试和集成测试的全面测试用例。

### 快速运行

```bash
# 运行所有测试
pytest tests/ -v

# 仅运行核心功能测试
pytest tests/test_agent.py tests/test_note_tool.py -v
```

### 测试覆盖范围

- ✅ **单元测试** - 工具类、LLM 客户端
- ✅ **功能测试** - Session Note Tool、MCP 加载
- ✅ **集成测试** - Agent 端到端执行
- ✅ **外部服务** - Git MCP 服务器加载


## 常见问题

### SSL 证书错误

如果遇到 `[SSL: CERTIFICATE_VERIFY_FAILED]` 错误:

**测试环境快速修复** (修改 `box_agent/llm.py`):
```python
# 第 50 行: 给 AsyncClient 添加 verify=False
async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
```

**生产环境解决方案**:
```bash
# 更新证书
pip install --upgrade certifi

# 或配置系统代理/证书
```

### 模块未找到错误

确保从项目目录运行:
```bash
cd Box-Agent
python -m box_agent.cli
```

## 相关文档

- [开发指南](docs/DEVELOPMENT_GUIDE_CN.md) - 详细的开发和配置指引
- [生产环境指南](docs/PRODUCTION_GUIDE_CN.md) - 生产部署最佳实践

## 贡献

我们欢迎并鼓励您提交 Issue 和 Pull Request！

- [贡献指南](CONTRIBUTING.md) - 如何为项目做贡献
- [行为准则](CODE_OF_CONDUCT.md) - 社区行为准则

## 许可证

本项目采用 [MIT 许可证](LICENSE) 授权。

## 参考资源

- Anthropic API: https://docs.anthropic.com/claude/reference
- OpenAI API: https://platform.openai.com/docs/api-reference
- Claude Skills: https://github.com/anthropics/skills
- MCP Servers: https://github.com/modelcontextprotocol/servers

---

**⭐ 如果这个项目对您有帮助，请给它一个 Star！**
