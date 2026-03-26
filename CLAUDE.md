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

**Execution core** (`core.py`): `run_agent_loop()` is the single source of truth for the agent loop. It is an `AsyncGenerator[AgentEvent, None]` that yields structured events (`StepStart`, `ThinkingEvent`, `ContentEvent`, `ToolCallStart`, `ToolCallResult`, `DoneEvent`, etc.) defined in `events.py`. No `print()` or `input()` calls — all I/O is delegated to consumers. Includes token-aware summarization (80k default, cl100k_base) and cancellation support.

**Agent** (`agent.py`): Public API wrapper. `Agent.run_events()` returns the raw event stream; `Agent.run()` is a backward-compatible method that consumes events and renders them to the terminal via `_render_event()`.

**ACP bridge** (`acp/`): Consumes `run_agent_loop()` events and translates them to ACP protocol updates (`sessionUpdate`). Automatically inherits summarization, logging, and safety from the shared core.

**LLM layer** (`llm/`): Multi-provider via `LLMClient` wrapper. `AnthropicClient` handles Anthropic-protocol APIs; `OpenAIClient` handles OpenAI-protocol APIs. Both implement `LLMClientBase`. The `api_base` is used as-is (no automatic URL suffix), so any third-party endpoint works directly.

**Tool system** (`tools/`): Abstract `Tool` base class with `to_schema()` (Anthropic format) and `to_openai_schema()`. Built-in tools: `ReadTool`, `WriteTool`, `EditTool`, `BashTool`, `BashOutputTool`, `BashKillTool`, `SessionNoteTool`. MCP tools loaded via `mcp_loader.py`. Skills loaded from `SKILL.md` files with YAML frontmatter via `skill_loader.py`.

**Safety layer** (`tools/safety.py`): Dangerous command detection (rm, sudo, kill, etc.) with user confirmation prompt (supports Chinese). Workspace path validation blocks access outside workspace when `allow_full_access: false`. Auto-backup to `~/.box-agent/trash/{timestamp}/` before file modifications. Non-interactive mode (`--task`) rejects dangerous commands outright.

**Config** (`config.py`): Pydantic models. Load priority: `box_agent/config/` (dev) → `~/.box-agent/config/` (installed) → package directory (fallback). Main files: `config.yaml`, `system_prompt.md`, `mcp.json`.

**CLI** (`cli.py`): Interactive mode with prompt_toolkit. In-session commands: `/help`, `/clear`, `/history`, `/stats`, `/log`, `/exit`. Subcommands: `setup`, `config`, `doctor`, `log`. Auto-launches setup wizard on first run or when API connection fails.

## Key Patterns

- All LLM and tool calls are async
- Retry with exponential backoff (`retry.py`, `@async_retry` decorator)
- Tools return `ToolResult` (Pydantic model with success/content/error)
- Skills use progressive disclosure: YAML metadata loaded first, full content on-demand
- Agent workspace defaults to CWD; logs go to `~/.box-agent/log/`
- `asyncio_mode = "auto"` in pytest config — async tests work without markers
- Safety: dangerous commands require confirmation; workspace scope enforced by default; files auto-backed up before modification

## Configuration

Run `box-agent setup` for interactive configuration, or manually copy `box_agent/config/config-example.yaml` to `box_agent/config/config.yaml`. Provider field (`anthropic` or `openai`) determines which client is used. The `api_base` is passed through directly — supports any compatible endpoint.

## Publishing

```bash
# Bump version in pyproject.toml and box_agent/__init__.py
uv build
uvx twine upload dist/box_agent-<version>*
gh release create v<version> dist/box_agent-<version>* --title "v<version>"
```

PyPI: https://pypi.org/project/box-agent/
GitHub: https://github.com/Raccoon-Office/Box-Agent
