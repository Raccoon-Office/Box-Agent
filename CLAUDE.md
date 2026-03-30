# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Box-Agent is a minimal yet professional AI agent framework supporting multiple LLM providers (Anthropic, OpenAI-compatible, DeepSeek, SiliconFlow, and any third-party API). It features interleaved thinking, tool calling, MCP support, and a Claude Skills system.

## Build & Run Commands

```bash
# Setup
uv sync
git submodule update --init --recursive  # Load skills

# Run (development)
uv run python -m box_agent.cli
# Run (installed)
box-agent

# Non-interactive mode
box-agent --task "do something"

# CLI subcommands
box-agent setup             # Interactive setup wizard
box-agent config            # Show current configuration
box-agent config --edit     # Open config in editor
box-agent doctor            # Check environment & API connectivity
box-agent log               # Open log directory

# Tests
pytest tests/ -v                         # All tests
pytest tests/test_agent.py -v            # Single test file
pytest tests/test_agent.py::TestAgent::test_method -v  # Single test
pytest --cov                             # With coverage

# ACP server
box-agent-acp
```

## Architecture

**Execution core** (`core.py`): `run_agent_loop()` is the single source of truth for the agent loop. It is an `AsyncGenerator[AgentEvent, None]` that yields structured events (`StepStart`, `ThinkingEvent`, `ContentEvent`, `ToolCallStart`, `ToolCallResult`, `DoneEvent`, `ArtifactEvent`, etc.) defined in `events.py`. No `print()` or `input()` calls — all I/O is delegated to consumers. Includes token-aware summarization (80k default, cl100k_base), cancellation support, and universal artifact detection (regex-based + workspace diff-based).

**Agent** (`agent.py`): Public API wrapper. `Agent.run_events()` returns the raw event stream; `Agent.run()` is a backward-compatible method that consumes events and renders them to the terminal via `_render_event()`.

**ACP bridge** (`acp/`): Consumes `run_agent_loop()` events and translates them to ACP protocol updates (`sessionUpdate`). Supports `session_mode` via ACP `_meta` (e.g. `data_analysis` mode injects `analysis_prompt.md`). Automatically inherits summarization, logging, and safety from the shared core.

**LLM layer** (`llm/`): Multi-provider via `LLMClient` wrapper. `AnthropicClient` handles Anthropic-protocol APIs; `OpenAIClient` handles OpenAI-protocol APIs. Both implement `LLMClientBase`. The `api_base` is used as-is (no automatic URL suffix), so any third-party endpoint works directly.

**Tool system** (`tools/`): Abstract `Tool` base class with `to_schema()` (Anthropic format) and `to_openai_schema()`. Built-in tools: `ReadTool`, `WriteTool`, `EditTool`, `BashTool`, `BashOutputTool`, `BashKillTool`, `SessionNoteTool`. MCP tools loaded via `mcp_loader.py`. Skills loaded from `SKILL.md` files with YAML frontmatter via `skill_loader.py`.

**Sandbox** (`tools/jupyter_tool.py`): Dual-mode execution environment. In normal mode: subprocess kernel in isolated venv (`SandboxEnvironment` + `JupyterKernelSession`). In frozen/runtime mode: in-process kernel (`InProcessKernelSession` via `ipykernel.inprocess`) with bundled packages. `IS_FROZEN` flag (from `sys.frozen`) selects the mode. Runtime package installs go to `~/.box-agent/runtime-packages/` via pip-as-library, gated by `ALLOWED_RUNTIME_PACKAGES` whitelist. Structured error codes: `SANDBOX_INIT_FAILED`, `KERNEL_START_FAILED`, `KERNEL_DIED`, `PACKAGE_NOT_ALLOWED`, `PACKAGE_NOT_AVAILABLE`.

**Safety layer** (`tools/safety.py`): Dangerous command detection (rm, sudo, kill, etc.) with user confirmation prompt (supports Chinese). Workspace path validation blocks access outside workspace when `allow_full_access: false`. Auto-backup to `~/.box-agent/trash/{timestamp}/` before file modifications. Non-interactive mode (`--task`) rejects dangerous commands outright.

**Config** (`config.py`): Pydantic models. Load priority: `box_agent/config/` (dev) → `~/.box-agent/config/` (installed) → package directory (fallback). Main files: `config.yaml`, `system_prompt.md`, `analysis_prompt.md`, `mcp.json`.

**CLI** (`cli.py`): Interactive mode with prompt_toolkit. In-session commands: `/help`, `/clear`, `/history`, `/stats`, `/log`, `/exit`. Subcommands: `setup`, `config`, `doctor`, `log`. Auto-launches setup wizard on first run or when API connection fails.

## Key Patterns

- All LLM and tool calls are async
- Retry with exponential backoff (`retry.py`, `@async_retry` decorator)
- Tools return `ToolResult` (Pydantic model with success/content/error)
- Skills use progressive disclosure: YAML metadata loaded first, full content on-demand
- Agent workspace defaults to CWD; logs go to `~/.box-agent/log/`
- `asyncio_mode = "auto"` in pytest config — async tests work without markers
- Safety: dangerous commands require confirmation; workspace scope enforced by default; files auto-backed up before modification
- Artifact detection: two-layer approach — regex scans tool output for `[filename.ext]` references, workspace diff detects files created by any tool (including bash). Both emit `ArtifactEvent` with mime_type, size_bytes, and absolute path
- LibreOffice (`soffice`) is a system dependency, NOT auto-installed. Excel export defaults to pandas + openpyxl. `recalc.py` gracefully handles missing soffice
- Frozen/runtime mode: `IS_FROZEN` flag selects in-process kernel, skips venv creation, routes package installs through whitelist + `~/.box-agent/runtime-packages/`

## Configuration

Run `box-agent setup` for interactive configuration, or manually copy `box_agent/config/config-example.yaml` to `box_agent/config/config.yaml`. Provider field (`anthropic` or `openai`) determines which client is used. The `api_base` is passed through directly — supports any compatible endpoint.

## Publishing

```bash
# Bump version in pyproject.toml and box_agent/__init__.py
uv build
uvx twine upload dist/box_agent-<version>*
gh release create v<version> dist/box_agent-<version>* --repo Raccoon-Office/Box-Agent --title "v<version>"
```

### Standalone Runtime Build

```bash
# Build PyInstaller binary for current platform
uv run python scripts/build_runtime.py
# Output: dist/runtime/box-agent-runtime-v{version}-{platform}-{arch}.tar.gz

# Upload runtime artifact to the same GitHub Release
gh release upload v<version> dist/runtime/box-agent-runtime-*.tar.gz --repo Raccoon-Office/Box-Agent
```

Runtime structure: `box-agent-runtime/{manifest.json, VERSION, bin/box-agent-acp}`. The binary communicates via ACP JSON-RPC over stdio. Hard constraint: stdout = pure ACP protocol, all diagnostics go to stderr.

Key files:
- `scripts/build_runtime.py` — PyInstaller build script, auto-detects platform
- `box_agent/acp/runtime_entry.py` — Clean entry point for standalone binary
- `box_agent/acp/debug_logger.py` — Structured logger (stderr + optional file, env-var controlled)

PyPI: https://pypi.org/project/box-agent/
GitHub: https://github.com/Raccoon-Office/Box-Agent
