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

| Feature                      | Box Agent                                         | Open Interpreter      | Aider              |
| ---------------------------- | ------------------------------------------------- | --------------------- | ------------------ |
| Sandboxed code execution     | Jupyter kernel in isolated venv                   | Runs in host Python   | N/A                |
| Sub-agent parallelism        | Multiple sub-agents run concurrently              | No                    | No                 |
| Multi-provider LLM           | Anthropic, OpenAI, DeepSeek, SiliconFlow, any API | OpenAI + a few others | OpenAI + Anthropic |
| MCP tool integration         | Native                                            | No                    | No                 |
| ACP protocol (embed in apps) | Full support                                      | No                    | No                 |
| Standalone binary            | PyInstaller runtime, no Python needed             | No                    | No                 |
| Context compression          | Staged automatic compaction + LLM summary          | Manual                | Git-based          |

## Key Features

### Sub-Agent Parallelism

Delegate isolated work to sub-agents with optional Skills, explicit tool
selection, and hard step/tool-call budgets. Every child uses the same
general-purpose agent loop and inherits the parent's live policy. Omitting
`required_tools` exposes all current parent tools; an explicit list narrows that
boundary.
The parent remains responsible for conflict handling, the final deliverable,
and verification.

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

New-style delegation is deny-by-default (`read_only: true`, `network: false`,
`external_side_effect: false`). See the
[sub-agent delegation contract](docs/SUB_AGENT_DELEGATION.md) for schemas,
limits, compatibility behavior, and host diagnostics.

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

### Staged Context Compression

- **Oversized tool results**: Individual results are persisted immediately when needed; fresh parallel results also share a 50k-character pre-request budget. The model receives a stable preview while full text remains on disk. Read results are exempt and stay bounded by Read's own line/character controls.
- **Usage-aware auto-summary**: The next request is estimated from the latest real API usage plus subsequent messages. When it reaches the model-derived safety threshold, older history is summarized into a `user` message while bounded recent messages and todo, plan, and skill state are restored.
- **Tool-call arguments**: Write/edit arguments remain verbatim until a whole-history summary replaces their turn; they are not independently compacted.
- **Legacy safety guard**: Internal history placeholders from older or externally supplied sessions are rejected if a model tries to reuse them as executable file/code arguments; Box-Agent requests one clean regeneration instead of writing the placeholder to disk.

### More

- **MCP Tools**: Connect to any [MCP server](https://github.com/modelcontextprotocol/servers) — web search, knowledge graphs, databases
- **Claude Skills**: 32 built-in skills for documents (DOCX, PDF, PPTX, XLSX), canvas design, Obsidian, web app testing, and more
- **ACP Protocol**: Embed Box Agent in Electron apps, Zed Editor, or any ACP-compatible host via JSON-RPC over stdio
- **Standalone Runtime**: PyInstaller binary bundles Python + all dependencies. No external Python needed — download and run
- **Cross-session Memory**: Persistent memory lets the agent retain key information across conversations
- **Safety Layer**: Dangerous command detection, workspace scope control, auto-backup before file modifications. Interactive permission negotiation for out-of-workspace access (CLI prompts user, ACP sends reverse RPC to host)
- **Planning Snapshots**: Structured plan tool for rendering objective, scope, steps, verification, and risks in host UIs
- **Task Tracking**: Built-in todo tool for multi-step task decomposition and progress tracking

## Demos

### Task Execution

_The agent creates a webpage and opens it in the browser._

![Demo: Task Execution](docs/assets/demo1-task-execution.gif)

### Claude Skill — PDF Generation

_The agent uses a skill to create a professional document._

![Demo: Claude Skill](docs/assets/demo2-claude-skill.gif)

### Web Search via MCP

_The agent searches the web and summarizes results._

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
uv run python -m box_agent.cli
```

## Contributor Quickstart

If you are joining the project as a collaborator, start here before changing
code:

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
git submodule update --init --recursive   # needed for bundled skills
uv sync
uv run python -m box_agent.cli --help
uv run pytest tests/test_core.py -q
```

Read these files first:

- `AGENTS.md` — repo-local engineering rules and verification expectations.
- `CONTRIBUTING.md` — contribution flow, PR checklist, and commit style.
- `docs/REVIEW_GUIDE.md` — maintainer review order, blockers, and proof requirements.
- `docs/DEVELOPMENT_GUIDE.md` — deeper architecture and development notes.
- `docs/INTEGRATION.md` — ACP/runtime integration details for host apps.

Project map:

| Area | Where to start |
| ---- | -------------- |
| Agent execution loop | `box_agent/core.py`, `box_agent/agent.py`, `box_agent/events.py` |
| CLI and config | `box_agent/cli.py`, `box_agent/config.py`, `box_agent/config/` |
| LLM providers | `box_agent/llm/` |
| Built-in tools | `box_agent/tools/` |
| ACP server/runtime embedding | `box_agent/acp/`, `box_agent/build_runtime_cli.py` |
| Skills | `box_agent/skills/`, `box_agent/tools/skill_loader.py` |
| Tests | `tests/test_<area>.py` |

Common development loop:

```bash
# Run the smallest relevant test while iterating
uv run pytest tests/test_bash_tool.py -q

# Run the broader suite before handing off
uv run pytest tests/ -q

# Catch whitespace/patch formatting issues
git diff --check
```

Use focused tests for the area you touched: tools in `tests/test_*_tool.py`,
LLM behavior in `tests/test_llm*.py` / `tests/test_error_messages.py`, ACP in
`tests/test_acp*.py`, memory in `tests/test_memory*.py`, and runtime packaging
in `tests/test_build_runtime.py` / `tests/test_cli_runtime.py`. Tests that need
real provider credentials are skipped unless the required API keys are present.

When a change affects the standalone runtime used by a host app, source changes
are not enough: rebuild the runtime, install it into the host, restart the
running ACP process, then probe the installed runtime. For local packaging:

```bash
uv run box-agent-build-runtime
```

Build a versioned runtime and install the resulting archive into the usual
officev3 checkout in one command:

```bash
uv run box-agent-build-runtime --version 0.8.82 --install-officev3
```

Pass an explicit checkout path after `--install-officev3`, or set
`BOX_AGENT_OFFICEV3_DIR`, when officev3 is stored elsewhere.

### Configuration

After running `box-agent setup`, your config lives at `~/.box-agent/config/config.yaml`:

```yaml
api_key: "your-api-key"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic" # "anthropic" or "openai"
max_steps: 300
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
goal_autopilot_enabled: true
goal_autopilot_max_turns: 3
goal_autopilot_max_seconds: 14400
goal_autopilot_no_progress_turns: 2
```

Main and sub-agent loops use the same model-derived context limit. Configure
that shared limit through `context_window` and `max_output_tokens`; there is no
separate child context-window setting.

Tool limits are omitted by default so runtime upgrades can supply updated
defaults from `box_agent/config.py`. Add only deliberate overrides under
`tool_limits:`; inspect the current effective values with
`box-agent config --json`.

```bash
box-agent config                    # show current config summary
box-agent config --get model        # print one config value
box-agent config --set max_steps 300
box-agent config --set goal_autopilot_max_turns 5
box-agent config --set tool_limits.external_skill.max_tool_calls 160
box-agent config --json             # machine-readable config summary
box-agent config --edit             # open in editor
box-agent doctor                    # check environment & API connectivity
box-agent doctor --json             # machine-readable health check
```

## CLI Usage

```bash
# Interactive mode
box-agent
box-agent --workspace /path/to/project
box-agent --no-sandbox           # disable Jupyter sandbox

# Non-interactive (CI/CD, scripts)
box-agent --task "analyze data.csv and create a report"
box-agent --task "analyze data.csv" --json          # append execution summary JSON
box-agent --task "local file task" --no-verify-api  # skip startup API probe
box-agent --task "create a PPT" --force-plan-start  # publish a plan before work
box-agent --task "create a PPT" --no-completion-gate
box-agent --goal "Ship CLI parity" --task "finish tests"
box-agent --goal "Ship CLI parity" --task "finish tests" --no-goal-autopilot
box-agent --deep-think --task "review this repo"    # enable thinking mode when supported

# Subcommands
box-agent setup              # config wizard
box-agent config             # show/edit config
box-agent doctor             # health check
box-agent log                # open log directory
box-agent goal status        # show persistent workspace goal
box-agent goal complete --evidence "tests passed"
box-agent install-browser   # install Chromium for Playwright MCP (~200MB)
box-agent install-node      # install managed Node.js runtime for skills (macOS)
```

### Browser automation (optional)

Box-Agent ships with a disabled [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) entry. To enable browser tools locally:

```bash
box-agent install-browser   # downloads Chromium and flips the entry to enabled
```

Requires Node.js ≥ 18 on `PATH`. Chromium lands in `~/.box-agent/browsers/` (shared by CLI and ACP runtime) and `mcpServers.playwright.disabled` in `~/.box-agent/config/mcp.json` is set to `false`.

**ACP embedders**: no env-var plumbing required — `box-agent-acp` defaults `PLAYWRIGHT_BROWSERS_PATH` to the same `~/.box-agent/browsers/` path. To point at a different cache, export `PLAYWRIGHT_BROWSERS_PATH=<your path>` before spawning `box-agent-acp` (our setdefault won't override it).

In-session commands: `/help`, `/clear`, `/clear_all`, `/history`, `/stats`, `/sandbox_status`, `/log`, `/goal`, `/memory review`, `/exit`

Use `/goal <objective>` or `--goal "<objective>"` to keep a durable workspace objective attached to later turns. The CLI persists it under `~/.box-agent/goals/`; later turns include that goal until you run `/goal pause`, `/goal resume`, `/goal block <reason>`, `/goal complete <evidence>`, or `/goal clear`. Scripted runs can manage it with `box-agent goal ...`.

In non-interactive `--task` mode and ACP sessions, active goals also use bounded autopilot: when a turn ends naturally but the goal is still `active`, Box-Agent automatically continues in the same session until the model marks the goal `complete`, marks it `blocked`, the user cancels, `goal_autopilot_max_turns` / `goal_autopilot_max_seconds` is reached, or `goal_autopilot_no_progress_turns` consecutive automatic continuations make no recorded goal progress. Use `--no-goal-autopilot` for one CLI run, or set `goal_autopilot_enabled: false` in config.

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
# Download pre-built binary (latest release; omit the tag to always get the newest)
gh release download --repo Raccoon-Office/Box-Agent --pattern "box-agent-runtime-*.tar.gz"

# Or build from source (current platform)
uv run box-agent-build-runtime

# Build macOS Intel/x64 runtime from Apple Silicon
# Requires a separate x86_64 venv because PyInstaller cannot bundle arm64 wheels into an x64 binary.
# One-time setup:
#   arch -x86_64 /bin/bash -c 'curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$HOME/.local/bin-x64" sh'
#   UV_PROJECT_ENVIRONMENT=.venv-x64 arch -x86_64 ~/.local/bin-x64/uv sync
# Build:
UV_PROJECT_ENVIRONMENT=.venv-x64 BOX_AGENT_RUNTIME_TARGET=darwin-x64 arch -x86_64 ~/.local/bin-x64/uv run box-agent-build-runtime
```

The runtime communicates via JSON-RPC over stdio. stdout = protocol only, stderr = diagnostics.
macOS runtime archives include Box-Agent's pinned Node.js runtime for skills
under `box-agent-runtime/runtimes/node/`; npm cache/prefix state remains in
`~/.box-agent/runtimes/node/sandbox/`.

## Testing

```bash
uv run pytest tests/ -v          # all tests
uv run pytest tests/test_core.py -v   # core + context compression
uv run pytest --cov              # with coverage
```

## Troubleshooting

**SSL Certificate Error**: `pip install --upgrade certifi` or set `verify=False` for testing.

**Module Not Found**: Make sure you're in the project directory: `cd Box-Agent && uv run python -m box_agent.cli`

## Contributing

Issues and PRs welcome! See [Contributing Guide](CONTRIBUTING.md).

## License

[MIT](LICENSE)

## Links

- [Documentation](docs/README.md)
- [PyPI](https://pypi.org/project/box-agent/) · [GitHub](https://github.com/Raccoon-Office/Box-Agent) · [Releases](https://github.com/Raccoon-Office/Box-Agent/releases)
- [Anthropic API](https://docs.anthropic.com/claude/reference) · [MCP Servers](https://github.com/modelcontextprotocol/servers) · [ACP Protocol](https://github.com/nichochar/agent-client-protocol)

---

**If this project helps you, give it a ⭐!**
