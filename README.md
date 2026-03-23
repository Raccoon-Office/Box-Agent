# Box Agent

English | [中文](./README_CN.md)

**Box Agent** is a minimal yet professional demo project that showcases the best practices for building agents with the MiniMax M2.5 model. Leveraging an Anthropic-compatible API, it fully supports interleaved thinking to unlock M2's powerful reasoning capabilities for long, complex tasks.

This project comes packed with features designed for a robust and intelligent agent development experience:

*   ✅ **Full Agent Execution Loop**: A complete and reliable foundation with a basic toolset for file system and shell operations.
*   ✅ **Persistent Memory**: An active **Session Note Tool** ensures the agent retains key information across multiple sessions.
*   ✅ **Intelligent Context Management**: Automatically summarizes conversation history to handle contexts up to a configurable token limit, enabling infinitely long tasks.
*   ✅ **Claude Skills Integration**: Comes with 15 professional skills for documents, design, testing, and development.
*   ✅ **MCP Tool Integration**: Natively supports MCP for tools like knowledge graph access and web search.
*   ✅ **Comprehensive Logging**: Detailed logs for every request, response, and tool execution for easy debugging.
*   ✅ **Clean & Simple Design**: A beautiful CLI and a codebase that is easy to understand, making it the perfect starting point for building advanced agents.

## Table of Contents

- [Box Agent](#box-agent)
  - [Table of Contents](#table-of-contents)
  - [Quick Start](#quick-start)
    - [1. Get API Key](#1-get-api-key)
    - [2. Choose Your Usage Mode](#2-choose-your-usage-mode)
      - [🚀 Quick Start Mode (Recommended for Beginners)](#-quick-start-mode-recommended-for-beginners)
      - [🔧 Development Mode](#-development-mode)
  - [ACP \& Zed Editor Integration(optional)](#acp--zed-editor-integrationoptional)
  - [Usage Examples](#usage-examples)
    - [Task Execution](#task-execution)
    - [Using a Claude Skill (e.g., PDF Generation)](#using-a-claude-skill-eg-pdf-generation)
    - [Web Search \& Summarization (MCP Tool)](#web-search--summarization-mcp-tool)
  - [Testing](#testing)
    - [Quick Run](#quick-run)
    - [Test Coverage](#test-coverage)
  - [Troubleshooting](#troubleshooting)
    - [SSL Certificate Error](#ssl-certificate-error)
    - [Module Not Found Error](#module-not-found-error)
  - [Related Documentation](#related-documentation)
  - [Community](#community)
  - [Contributing](#contributing)
  - [License](#license)
  - [References](#references)

## Quick Start

### 1. Get API Key

MiniMax provides both global and China platforms. Choose based on your network environment:

| Version    | Platform                                                       | API Base                   |
| ---------- | -------------------------------------------------------------- | -------------------------- |
| **Global** | [https://platform.minimax.io](https://platform.minimax.io)     | `https://api.minimax.io`   |
| **China**  | [https://platform.minimaxi.com](https://platform.minimaxi.com) | `https://api.minimaxi.com` |

**Steps to get API Key:**
1. Visit the corresponding platform to register and login
2. Go to **Account Management > API Keys**
3. Click **"Create New Key"**
4. Copy and save it securely (key is only shown once)

> 💡 **Tip**: Remember the API Base address corresponding to your chosen platform, you'll need it for configuration

### 2. Choose Your Usage Mode

**Prerequisites: Install uv**

Both usage modes require uv. If you don't have it installed:

```bash
# macOS/Linux/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
python -m pip install --user pipx
python -m pipx ensurepath
# Restart PowerShell after installation

# After installation, restart your terminal or run:
source ~/.bashrc  # or ~/.zshrc (macOS/Linux)
```

We offer two usage modes - choose based on your needs:

#### 🚀 Quick Start Mode (Recommended for Beginners)

Perfect for users who want to quickly try Box Agent without cloning the repository or modifying code.

**Installation:**

```bash
# 1. Install directly from GitHub
uv tool install git+https://github.com/MiniMax-AI/Box-Agent.git

# 2. Run interactive setup wizard (creates config and guides you through provider/key setup)
box-agent setup
```

> 💡 **Tip**: If you want to develop locally or modify code, use "Development Mode" below

**Configuration:**

The setup wizard creates config files in `~/.box-agent/config/`. You can review or edit them anytime:

```bash
box-agent config          # Show current configuration
box-agent config --edit   # Open config file in your editor
```

Fill in your API Key and corresponding API Base:

```yaml
api_key: "YOUR_API_KEY_HERE"          # API Key from step 1
api_base: "https://api.minimax.io"  # Global
# api_base: "https://api.minimaxi.com"  # China
model: "MiniMax-M2.5"
```

**Start Using:**

```bash
box-agent                                    # Use current directory as workspace
box-agent --workspace /path/to/your/project  # Specify workspace directory
box-agent doctor                             # Check environment and connectivity
box-agent --version                          # Check version

# Management commands
uv tool upgrade box-agent                    # Upgrade to latest version
uv tool uninstall box-agent                  # Uninstall if needed
uv tool list                                  # View all installed tools
```

#### 🔧 Development Mode

For developers who need to modify code, add features, or debug.

**Installation & Configuration:**

```bash
# 1. Clone the repository
git clone https://github.com/MiniMax-AI/Box-Agent.git
cd Box-Agent

# 2. Install uv (if you haven't)
# macOS/Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
irm https://astral.sh/uv/install.ps1 | iex
# Restart terminal after installation

# 3. Sync dependencies
uv sync

# Alternative: Install dependencies manually (if not using uv)
# pip install -r requirements.txt
# Or install required packages:
# pip install tiktoken pyyaml httpx pydantic requests prompt-toolkit mcp

# 4. Initialize Claude Skills (Optional)
git submodule update --init --recursive

# 5. Copy config template
```

**macOS/Linux:**
```bash
cp box_agent/config/config-example.yaml box_agent/config/config.yaml
```

**Windows:**
```powershell
Copy-Item box_agent\config\config-example.yaml box_agent\config\config.yaml

# 6. Edit config file
vim box_agent/config/config.yaml  # Or use your preferred editor
```

Fill in your API Key and corresponding API Base:

```yaml
api_key: "YOUR_API_KEY_HERE"          # API Key from step 1
api_base: "https://api.minimax.io"  # Global
# api_base: "https://api.minimaxi.com"  # China
model: "MiniMax-M2.5"
max_steps: 100
workspace_dir: "./workspace"
```

> 📖 Full configuration guide: See [config-example.yaml](box_agent/config/config-example.yaml)

**Run Methods:**

Choose your preferred run method:

```bash
# Method 1: Run as module directly (good for debugging)
uv run python -m box_agent.cli

# Method 2: Install in editable mode (recommended)
uv tool install -e .
# After installation, run from anywhere and code changes take effect immediately
box-agent
box-agent --workspace /path/to/your/project
```

> 📖 For more development guidance, see [Development Guide](docs/DEVELOPMENT_GUIDE.md)

> 📖 For more production deployment guidance, see [Production Guide](docs/PRODUCTION_GUIDE.md)

## ACP & Zed Editor Integration(optional)

Box Agent supports the [Agent Communication Protocol (ACP)](https://github.com/modelcontextprotocol/protocol) for integration with code editors like Zed.

**Setup in Zed Editor:**

1. Install Box Agent in development mode or as a tool
2. Add to your Zed `settings.json`:

```json
{
  "agent_servers": {
    "box-agent": {
      "command": "/path/to/box-agent-acp"
    }
  }
}
```

The command path should be:
- If installed via `uv tool install`: Use the output of `which box-agent-acp`
- If in development mode: `./box_agent/acp/server.py`

**Usage:**
- Open Zed's agent panel with `Ctrl+Shift+P` → "Agent: Toggle Panel"
- Select "box-agent" from the agent dropdown
- Start conversations with Box Agent directly in your editor

## Usage Examples

Here are a few examples of what Box Agent can do.

### Task Execution

*In this demo, the agent is asked to create a simple, beautiful webpage and display it in the browser, showcasing the basic tool-use loop.*

![Demo GIF 1: Basic Task Execution](docs/assets/demo1-task-execution.gif "Basic Task Execution Demo")

### Using a Claude Skill (e.g., PDF Generation)

*Here, the agent leverages a Claude Skill to create a professional document (like a PDF or DOCX) based on the user's request, demonstrating its advanced capabilities.*

![Demo GIF 2: Claude Skill Usage](docs/assets/demo2-claude-skill.gif "Claude Skill Usage Demo")

### Web Search & Summarization (MCP Tool)

*This demo shows the agent using its web search tool to find up-to-date information online and summarize it for the user.*

![Demo GIF 3: Web Search](docs/assets/demo3-web-search.gif "Web Search Demo")

## Testing

The project includes comprehensive test cases covering unit tests, functional tests, and integration tests.

### Quick Run

```bash
# Run all tests
pytest tests/ -v

# Run core functionality tests
pytest tests/test_agent.py tests/test_note_tool.py -v
```

### Test Coverage

- ✅ **Unit Tests** - Tool classes, LLM client
- ✅ **Functional Tests** - Session Note Tool, MCP loading
- ✅ **Integration Tests** - Agent end-to-end execution
- ✅ **External Services** - Git MCP Server loading


## Troubleshooting

### SSL Certificate Error

If you encounter `[SSL: CERTIFICATE_VERIFY_FAILED]` error:

**Quick fix for testing** (modify `box_agent/llm.py`):
```python
# Line 50: Add verify=False to AsyncClient
async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
```

**Production solution**:
```bash
# Update certificates
pip install --upgrade certifi

# Or configure system proxy/certificates
```

### Module Not Found Error

Make sure you're running from the project directory:
```bash
cd Box-Agent
python -m box_agent.cli
```

## Related Documentation

- [Development Guide](docs/DEVELOPMENT_GUIDE.md) - Detailed development and configuration guidance
- [Production Guide](docs/PRODUCTION_GUIDE.md) - Best practices for production deployment

## Community

Join the MiniMax official community to get help, share ideas, and stay updated:

- **WeChat Group**: Scan the QR code on [Contact Us](https://platform.minimaxi.com/docs/faq/contact-us) page to join

## Contributing

Issues and Pull Requests are welcome!

- [Contributing Guide](CONTRIBUTING.md) - How to contribute
- [Code of Conduct](CODE_OF_CONDUCT.md) - Community guidelines

## License

This project is licensed under the [MIT License](LICENSE).

## References

- MiniMax API: https://platform.minimax.io/docs
- MiniMax-M2: https://github.com/MiniMax-AI/MiniMax-M2
- Anthropic API: https://docs.anthropic.com/claude/reference
- Claude Skills: https://github.com/anthropics/skills
- MCP Servers: https://github.com/modelcontextprotocol/servers

---

**⭐ If this project helps you, please give it a Star!**
