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

**Execution core** (`core.py`): `run_agent_loop()` is the single source of truth for the agent loop. It is an `AsyncGenerator[AgentEvent, None]` that yields structured events (`StepStart`, `ThinkingEvent`, `ContentEvent`, `ToolCallStart`, `ToolCallResult`, `DoneEvent`, `ArtifactEvent`, etc.) defined in `events.py`. No `print()` or `input()` calls — all I/O is delegated to consumers. Two-layer context compression: Layer 1 micro-compact (zero-cost, replaces old tool results with placeholders every step) + Layer 2 token-aware summarization (LLM summary at 80k threshold). Cancellation support and universal artifact detection (regex-based + workspace diff-based).

**Agent** (`agent.py`): Public API wrapper. `Agent.run_events()` returns the raw event stream; `Agent.run()` is a backward-compatible method that consumes events and renders them to the terminal via `_render_event()`.

**ACP bridge** (`acp/`): Consumes `run_agent_loop()` events and translates them to ACP protocol updates (`sessionUpdate`). Streaming deltas (thinking/content) are forwarded in real-time — each delta triggers an immediate `update_agent_thought`/`update_agent_message` send (no accumulation). Supports `session_mode` via ACP `_meta` (e.g. `data_analysis`, `ppt_plan_chat`, `ppt_outline`, `ppt_editor_standard_html`); when omitted, `intent_classifier.py` auto-classifies on the first prompt (see "Session mode" below). Mode-specific system prompts are injected via `_build_session_prompt()` using a `_MODE_PROMPT_MAP` dict. PPT tools are conditionally registered per session_mode. Extended thinking is opt-in via `_meta.deep_think: bool` on `session/new` (session-level; Anthropic native + OpenAI `reasoning_effort`/Qwen `enable_thinking` both sent). Automatically inherits summarization, logging, and safety from the shared core.

**LLM layer** (`llm/`): Multi-provider via `LLMClient` wrapper. `AnthropicClient` handles Anthropic-protocol APIs; `OpenAIClient` handles OpenAI-protocol APIs. Both implement `LLMClientBase`. The `api_base` is used as-is (no automatic URL suffix), so any third-party endpoint works directly. Both clients accept a session-level `thinking_enabled` kwarg: Anthropic sends `thinking={"type": "enabled", "budget_tokens": 8000}`; OpenAI sends both `reasoning_effort: "medium"` (OpenAI/Azure/GPT-5/o1/o3) and `extra_body: {"enable_thinking": True, "thinking_budget": 8000}` (Qwen/DashScope). Providers that don't recognize the fields silently ignore them.

**Tool system** (`tools/`): Abstract `Tool` base class with `to_schema()` (Anthropic format), `to_openai_schema()`, and `parallel_safe` attribute. `EventEmittingTool(Tool)` adds real-time progress event emission via `asyncio.Queue` — used by `SubAgentTool` and PPT tools. Built-in tools: `ReadTool`, `WriteTool`, `EditTool`, `BashTool`, `BashOutputTool`, `BashKillTool`, `TodoWriteTool`, `TodoReadTool`, `SubAgentTool`, `MemoryWriteTool`, `MemoryReadTool`, `MemorySearchTool`, `PPTPlanChatTool`, `PPTOutlineTool`, `PPTEditorHTMLTool`. MCP tools loaded via `mcp_loader.py`. Skills loaded from `SKILL.md` files with YAML frontmatter via `skill_loader.py`.

**Memory system** (`memory.py`): Dual-file architecture — `MEMORY.md` (core: user identity/preferences, always injected into system prompt) + `CONTEXT.md` (searchable: project context/task patterns, retrieved on demand via `memory_search` tool). `MemoryExtractor` runs as background `asyncio.create_task` at three lifecycle points: before context compression, every N steps, and at agent loop end (`DoneEvent`). Writes only to CONTEXT.md with line-level exact-match merges. One-time LLM-filtered import from `~/.openclaw/` (USER.md + MEMORY.md) into core on first startup. `append_context()` enforces code-level dedup against core — lines already in MEMORY.md are automatically filtered. Config: `enable_memory_extraction`, `memory_extraction_cooldown`, `memory_extraction_step_interval`. Per-session `MemoryExtractor` instances in ACP to avoid cross-session state leaks.

**Sandbox** (`tools/jupyter_tool.py`): Dual-mode execution environment. In normal mode: subprocess kernel in isolated venv (`SandboxEnvironment` + `JupyterKernelSession`). In frozen/runtime mode: in-process kernel (`InProcessKernelSession` via `ipykernel.inprocess`) with bundled packages. `IS_FROZEN` flag (from `sys.frozen`) selects the mode. Runtime package installs go to `~/.box-agent/runtime-packages/` via pip-as-library, gated by `ALLOWED_RUNTIME_PACKAGES` whitelist. Structured error codes: `SANDBOX_INIT_FAILED`, `KERNEL_START_FAILED`, `KERNEL_DIED`, `PACKAGE_NOT_ALLOWED`, `PACKAGE_NOT_AVAILABLE`. All kernel `execute()` calls run in `run_in_executor` + `asyncio.wait_for` to avoid blocking the event loop; pip install operations have a 120s timeout.

**Safety layer** (`tools/safety.py`): Dangerous command detection (rm, sudo, kill, etc.) with user confirmation prompt (supports Chinese). Workspace path validation blocks access outside workspace when `allow_full_access: false`. Auto-backup to `~/.box-agent/trash/{timestamp}/` before file modifications. Non-interactive mode (`--task`) rejects dangerous commands outright.

**Config** (`config.py`): Pydantic models. Load priority: `box_agent/config/` (dev) → `~/.box-agent/config/` (installed) → package directory (fallback). Main files: `config.yaml`, `system_prompt.md`, `analysis_prompt.md`, `ppt_plan_chat_prompt.md`, `ppt_outline_prompt.md`, `ppt_editor_standard_html_prompt.md`, `mcp.json`.

**CLI** (`cli.py`): Interactive mode with prompt_toolkit. In-session commands: `/help`, `/clear`, `/history`, `/stats`, `/log`, `/exit`. Subcommands: `setup`, `config`, `doctor`, `log`. Auto-launches setup wizard on first run or when API connection fails.

## Key Patterns

- All LLM and tool calls are async
- Retry with exponential backoff (`retry.py`, `@async_retry` decorator)
- Tools return `ToolResult` (Pydantic model with success/content/error)
- Skills use progressive disclosure: YAML metadata loaded first, full content on-demand
- Agent workspace defaults to CWD; logs go to `~/.box-agent/log/`
- `asyncio_mode = "auto"` in pytest config — async tests work without markers
- Safety: dangerous commands require confirmation; workspace scope enforced by default; files auto-backed up before modification
- Permission negotiation: in-band blocking flow for out-of-workspace access. `GrantStore` tracks grants at prompt/session scope; `PermissionEngine` consults grant store before policy check. `run_agent_loop()` accepts `permission_negotiator` — on tool denial with `permission_request`, negotiator is called and tool retried on grant. CLI uses interactive terminal prompt (`cli_permissions.py`, `termios`+`run_in_executor`); ACP uses `session/request_permission` reverse RPC (`_PermissionNegotiator` in `acp/__init__.py`, 120s timeout). `officev3_permissions_override` in `session/new._meta` is deprecated (parsed but ignored)
- Artifact detection: two-layer approach — regex scans tool output for `[filename.ext]` references, workspace diff detects files created by any tool (including bash). Both emit `ArtifactEvent` with mime_type, size_bytes, and absolute path
- LibreOffice (`soffice`) is a system dependency, NOT auto-installed. Excel export defaults to pandas + openpyxl. `recalc.py` gracefully handles missing soffice
- Frozen/runtime mode: `IS_FROZEN` flag selects in-process kernel, skips venv creation, routes package installs through whitelist + `~/.box-agent/runtime-packages/`
- Sub-agent: `SubAgentTool` runs tasks in isolated message contexts via `run_agent_loop()`. Child tools exclude `sub_agent` itself (no recursion). Multiple sub-agents execute in parallel via `asyncio.gather` (tools with `parallel_safe = True`)
- EventEmittingTool: base class for tools that emit real-time progress events. `_emit(payload)` pushes to an `asyncio.Queue` that core.py drains in the foreground generator. Both sequential and parallel execution blocks support this pattern. SubAgentTool emits `SubAgentEvent`, PPT tools emit `PPTProgressEvent`
- PPT support: three session_modes (`ppt_plan_chat`, `ppt_outline`, `ppt_editor_standard_html`) with corresponding tools (`ppt_emit_plan`, `ppt_emit_outline`, `ppt_emit_html`). Tools validate payload structure at execution time (required fields, forbidden fields, auto-stringify). `ppt_emit_html` is `parallel_safe=True` for per-page concurrent generation. All events flow via ACP `update_tool_call(tid, raw_output=payload)` where `payload.type` is the officev3 dispatch key. Plan is a deep-thinking task list (not page operations) with dependency DAG for parallel execution. Outline uses old page-keyed format (`{"page_1": {...}}`) with `outline` as JSON string. ACP streaming is real-time (no accumulation)
- Context compression: Layer 1 `_micro_compact()` runs every step, replaces tool results older than last 3 with `[Previous result from {tool}: {first_line}...]`. Layer 2 `_maybe_summarize()` triggers LLM summary when tokens exceed 80k. Logger captures originals — no data loss
- Memory: dual-file `MEMORY.md` (core, always injected) + `CONTEXT.md` (searchable on demand). Two write paths — skill-guided LLM calls `memory_write` for explicit user intent, `MemoryExtractor` auto-extracts to CONTEXT.md in background (`asyncio.create_task`). Core/Context dedup enforced at code level (`append_context()` filters lines present in core). OpenClaw import is one-time LLM-filtered bootstrap into core
- ACP stdout guard: `sys.stdout = sys.stderr` in ACP mode (must use direct assignment, NOT `TextIOWrapper(sys.stderr.buffer)` which destroys stderr on GC). `_real_stdout = sys.__stdout__` (not `sys.stdout`, which may be pre-guarded by `runtime_entry.py`). Real stdout restored only for `stdio_streams()` transport, then re-guarded. All diagnostics and third-party output go to stderr
- Session mode auto-classification: when ACP caller omits `_meta.session_mode` on `session/new`, `SessionState.auto_classify_pending=True`. On the first `prompt`, `intent_classifier.classify_session_mode()` runs a single tool-free LLM call (8s timeout, whitelist-validated, failure → `None`/general). `_apply_session_mode(state, mode)` then retroactively rewrites `messages[0]` preserving the workspace-info footer, re-applies cached `memory_block`, and registers the mode's PPT tool. Explicit `session_mode` skips classification entirely
- Deep-think / extended thinking: session-level toggle via `_meta.deep_think: bool` on `session/new`. Threaded as `thinking_enabled` through `Agent.__init__` → `run_agent_loop` → `LLMClient.generate_stream`. Anthropic path: `thinking={"type": "enabled", "budget_tokens": 8000}` (only `enabled`/`disabled` are valid; no `adaptive`). OpenAI path sends both `reasoning_effort: "medium"` (top-level, OpenAI-official) and `extra_body: {"enable_thinking": True, "thinking_budget": 8000}` (Qwen-compatible). Default off — no thinking-related params sent when disabled. Budget is hardcoded 8000 tokens. Classifier and summary LLM calls always run with `thinking_enabled=False`

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
