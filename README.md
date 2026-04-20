<p align="center">
  <h1 align="center">Box Agent</h1>
  <p align="center">A general-purpose AI agent with sandboxed code execution, sub-agent parallelism, and multi-provider LLM support.</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/v/box-agent?color=orange" alt="PyPI"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/dm/box-agent?color=brightgreen" alt="Downloads"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/pyversions/box-agent?color=blue" alt="Python"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Raccoon-Office/Box-Agent?color=green" alt="License"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/releases"><img src="https://img.shields.io/github/v/release/Raccoon-Office/Box-Agent?color=blue" alt="Release"></a>
</p>

<p align="center">
  English | <a href="./README_CN.md">中文</a>
</p>

---

**Get started in 30 seconds:**

```bash
uv tool install box-agent   # or: pip install box-agent (Python 3.10+)
box-agent setup              # interactive config wizard
box-agent                    # start chatting
```

Or run a one-shot task:

```bash
box-agent --task "Analyze sales.csv — show top 10 products by revenue with a bar chart"
```

---

## Why Box Agent?

Most agent frameworks are either too simple (no sandbox, no tools) or too complex (massive dependencies, rigid architecture). Box Agent hits the sweet spot:

| Feature | Box Agent | Open Interpreter | Aider |
|---------|-----------|-----------------|-------|
| Sandboxed code execution | Jupyter kernel in isolated venv | Runs in host Python | N/A |
| Sub-agent parallelism | Multiple sub-agents run concurrently | No | No |
| Multi-provider LLM | Anthropic, OpenAI, DeepSeek, SiliconFlow, any API | OpenAI + a few others | OpenAI + Anthropic |
| MCP tool integration | Native | No | No |
| ACP protocol (embed in apps) | Full support | No | No |
| Standalone binary | PyInstaller runtime, no Python needed | No | No |
| Context compression | 2-layer automatic (micro-compact + LLM summary) | Manual | Git-based |

## Key Features

### Sub-Agent Parallelism
Delegate tasks to isolated sub-agents that run concurrently. Each sub-agent has its own context — only the summary comes back. Perfect for multi-file analysis.

```
You: "Analyze data1.csv, data2.csv, and data3.csv separately, then give me a combined summary"

┌─ Sub-Agent 1 ──────┐  ┌─ Sub-Agent 2 ──────┐  ┌─ Sub-Agent 3 ──────┐
│ Read data1.csv      │  │ Read data2.csv      │  │ Read data3.csv      │
│ Run statistics      │  │ Run statistics      │  │ Run statistics      │
│ Generate charts     │  │ Generate charts     │  │ Generate charts     │
│ → Summary: ...      │  │ → Summary: ...      │  │ → Summary: ...      │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
                              ↓ parallel ↓
                    ┌─ Parent Agent ──────────┐
                    │ Combines 3 summaries    │
                    │ Produces final report   │
                    └─────────────────────────┘
```

### Sandboxed Code Execution
Python runs in an isolated Jupyter kernel with pre-installed data science packages (`pandas`, `numpy`, `matplotlib`, `scikit-learn`, `openpyxl`, `xlrd`). Generated files (charts, CSVs, PDFs) are automatically detected and surfaced as structured artifacts.

### Multi-Provider LLM
One config, any provider:

```yaml
# Anthropic
api_base: "https://api.anthropic.com"
provider: "anthropic"
model: "claude-sonnet-4-20250514"

# DeepSeek
api_base: "https://api.deepseek.com"
provider: "openai"
model: "deepseek-chat"

# Any OpenAI-compatible endpoint
api_base: "https://your-api.example.com/v1"
provider: "openai"
model: "your-model"
```

### 2-Layer Context Compression
- **Layer 1 — Micro-compact**: Every step, old tool results (3+ turns back) are replaced with short placeholders. Zero cost, no LLM call.
- **Layer 2 — Auto-summary**: When tokens exceed the threshold (default 80k), an LLM call summarizes the conversation. Original data is preserved in logs.

### More
- **MCP Tools**: Connect to any [MCP server](https://github.com/modelcontextprotocol/servers) — web search, knowledge graphs, databases
- **Claude Skills**: 11 built-in skills for documents (DOCX, PDF, PPTX, XLSX), canvas design, web app testing, and more
- **ACP Protocol**: Embed Box Agent in Electron apps, Zed Editor, or any ACP-compatible host via JSON-RPC over stdio
- **Standalone Runtime**: PyInstaller binary bundles Python + all dependencies. No external Python needed — download and run
- **Cross-session Memory**: Persistent memory lets the agent retain key information across conversations
- **Safety Layer**: Dangerous command detection, workspace scope control, auto-backup before file modifications. Interactive permission negotiation for out-of-workspace access (CLI prompts user, ACP sends reverse RPC to host)
- **Task Tracking**: Built-in todo tool for multi-step task decomposition and progress tracking

## Demos

### Task Execution
*The agent creates a webpage and opens it in the browser.*

![Demo: Task Execution](docs/assets/demo1-task-execution.gif)

### Claude Skill — PDF Generation
*The agent uses a skill to create a professional document.*

![Demo: Claude Skill](docs/assets/demo2-claude-skill.gif)

### Web Search via MCP
*The agent searches the web and summarizes results.*

![Demo: Web Search](docs/assets/demo3-web-search.gif)

## Installation

> **Requires Python 3.10+.** If your system Python is older (e.g. 3.9), use `uv tool install` — it manages Python automatically.

### Quick Start (uv, recommended)

[uv](https://docs.astral.sh/uv/) handles Python version management for you — no need to upgrade your system Python:

```bash
# Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install box-agent (auto-downloads Python 3.10+ if needed)
uv tool install box-agent
box-agent setup    # interactive config wizard
box-agent          # start chatting

# Upgrade later
uv tool upgrade box-agent
```

### Quick Start (pip)

If you already have Python 3.10+:

```bash
pip install box-agent
box-agent setup
box-agent
```

### From Source

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
uv sync
git submodule update --init --recursive   # optional: load skills
uv run python -m box_agent.cli
```

### Configuration

After running `box-agent setup`, your config lives at `~/.box-agent/config/config.yaml`:

```yaml
api_key: "your-api-key"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic"           # "anthropic" or "openai"
max_steps: 50
```

```bash
box-agent config           # show current config
box-agent config --edit    # open in editor
box-agent doctor           # check environment & API connectivity
```

## CLI Usage

```bash
# Interactive mode
box-agent
box-agent --workspace /path/to/project
box-agent --sandbox              # enable Jupyter sandbox

# Non-interactive (CI/CD, scripts)
box-agent --task "analyze data.csv and create a report"

# Subcommands
box-agent setup     # config wizard
box-agent config    # show/edit config
box-agent doctor    # health check
box-agent log       # open log directory
box-agent install-browser   # install Chromium for Playwright MCP (~200MB)
```

### Browser automation (optional)

Box-Agent ships with a disabled [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) entry. To enable browser tools locally:

```bash
box-agent install-browser   # downloads Chromium and flips the entry to enabled
```

Requires Node.js ≥ 18 on `PATH`. Chromium lands in `~/.box-agent/browsers/` (shared by CLI and ACP runtime) and `mcpServers.playwright.disabled` in `~/.box-agent/config/mcp.json` is set to `false`.

**ACP embedders**: no env-var plumbing required — `box-agent-acp` defaults `PLAYWRIGHT_BROWSERS_PATH` to the same `~/.box-agent/browsers/` path. To point at a different cache, export `PLAYWRIGHT_BROWSERS_PATH=<your path>` before spawning `box-agent-acp` (our setdefault won't override it).

In-session commands: `/help`, `/clear`, `/history`, `/stats`, `/log`, `/exit`

## ACP & Editor Integration

Box Agent supports the [Agent Communication Protocol](https://github.com/nichochar/agent-client-protocol) for embedding in editors and apps.

**Zed Editor** — add to `settings.json`:
```json
{
  "agent_servers": {
    "box-agent": {
      "command": "/path/to/box-agent-acp"
    }
  }
}
```

**Standalone Runtime** — for Electron apps and other hosts:
```bash
# Download pre-built binary
gh release download v0.6.7 --repo Raccoon-Office/Box-Agent --pattern "box-agent-runtime-*.tar.gz"

# Or build from source
uv run python scripts/build_runtime.py
```

The runtime communicates via JSON-RPC over stdio. stdout = protocol only, stderr = diagnostics.

## Testing

```bash
pytest tests/ -v          # all tests
pytest tests/test_core.py -v   # core + context compression
pytest --cov              # with coverage
```

## Troubleshooting

**SSL Certificate Error**: `pip install --upgrade certifi` or set `verify=False` for testing.

**Module Not Found**: Make sure you're in the project directory: `cd Box-Agent && uv run python -m box_agent.cli`

## Contributing

Issues and PRs welcome! See [Contributing Guide](CONTRIBUTING.md).

## License

[MIT](LICENSE)

## Links

- [PyPI](https://pypi.org/project/box-agent/) · [GitHub](https://github.com/Raccoon-Office/Box-Agent) · [Releases](https://github.com/Raccoon-Office/Box-Agent/releases)
- [Anthropic API](https://docs.anthropic.com/claude/reference) · [MCP Servers](https://github.com/modelcontextprotocol/servers) · [ACP Protocol](https://github.com/nichochar/agent-client-protocol)

---

**If this project helps you, give it a ⭐!**
