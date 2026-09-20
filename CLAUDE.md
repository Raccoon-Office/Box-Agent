# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response Language

默认使用中文回复。

## Implementation Principles

实现功能时必须同时考虑 macOS、Windows、Linux 三端，以及 CLI 与 ACP runtime 两种入口。核心功能应在共享核心逻辑中实现，CLI 和 ACP runtime 只作为包装层接入，不应各自维护分叉实现。

## Code Discovery With Understand Anything

When investigating code paths, ownership, architecture, dependencies, or impact scope, first check whether `.understand-anything/` exists. This repository expects agents to use Understand Anything as the first navigation layer when it is available, before falling back to broad manual search.

Treat Understand Anything output as an index and orientation aid, not the final source of truth. Always verify conclusions against the real source code with `rg`, direct file reads, focused tests, or runtime evidence before explaining behavior or making edits.

In multi-developer environments, do not assume every checkout has a usable local graph. If `.understand-anything/` or the required Understand Anything tooling is missing, say so briefly and recommend installing/initializing Understand Anything for this repository, then continue with normal source search if the task can still proceed. If the graph appears stale, incomplete, or inconsistent with source code, call that out and verify with source files.

Do not make code changes based only on the graph, and do not launch dashboards or long-running indexing commands unless the task explicitly needs them.

## Collaboration and Review Rules

For every non-trivial change, keep the TPR frame explicit:

- Task: what behavior is changing, which entry points are affected, and what is intentionally out of scope.
- Proof: the exact tests, probes, logs, screenshots, generated manifests, or runtime checks that prove the change.
- Risk: compatibility, packaging/runtime, migration, config/secrets, rollback, and cross-repository impacts.

Prefer small PRs with one behavioral purpose. Shared behavior belongs in shared core logic; CLI and ACP should stay thin adapters unless the request is entry-point-specific. If a source change affects officev3 or any packaged runtime path, explicitly state whether the runtime was rebuilt/installed/probed or whether verification is source-only.

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

**ACP bridge** (`acp/`): Consumes `run_agent_loop()` events and translates them to ACP protocol updates (`sessionUpdate`). Streaming deltas (thinking/content) are forwarded in real-time — each delta triggers an immediate `update_agent_thought`/`update_agent_message` send (no accumulation). Supports `session_mode` via ACP `_meta` (currently `data_analysis` and `code_agent`); when omitted, no mode prompt is applied (general mode) — `session_mode` must be supplied explicitly by the caller. Mode-specific system prompts are injected via `_build_session_prompt()` using the `_MODE_PROMPT_MAP` dict (`data_analysis → analysis_prompt_path`, `code_agent → code_prompt_path`). Automatically inherits summarization, logging, and safety from the shared core.

**LLM layer** (`llm/`): Multi-provider via `LLMClient` wrapper. `AnthropicClient` handles Anthropic-protocol APIs; `OpenAIClient` handles OpenAI-protocol APIs. Both implement `LLMClientBase`. The `api_base` is used as-is (no automatic URL suffix), so any third-party endpoint works directly. Both clients accept a session-level `thinking_enabled` kwarg: Anthropic sends `thinking={"type": "enabled", "budget_tokens": 8000}`; Providers that don't recognize the fields silently ignore them.

**Tool system** (`tools/`): Abstract `Tool` base class with `to_schema()` (Anthropic format), `to_openai_schema()`, and `parallel_safe` attribute. `EventEmittingTool(Tool)` adds real-time progress event emission via `asyncio.Queue` — used by `SubAgentTool`. Built-in tools: `ReadTool`, `WriteTool`, `EditTool`, `BashTool`, `BashOutputTool`, `BashKillTool`, `TodoWriteTool`, `TodoReadTool`, `SubAgentTool`, `MemoryWriteTool`, `MemoryReadTool`, `MemorySearchTool`. MCP tools loaded via `mcp_loader.py`. Skills loaded from `SKILL.md` files with YAML frontmatter via `skill_loader.py`.

**Memory system** (`memory.py`): Dual-file architecture — `MEMORY.md` (core: user identity/preferences, always injected into system prompt) + `CONTEXT.md` (searchable: project context/task patterns, retrieved on demand via `memory_search` tool). `MemoryExtractor` runs as background `asyncio.create_task` at three lifecycle points: before context compression, every N steps, and at agent loop end (`DoneEvent`). Writes only to CONTEXT.md with line-level exact-match merges. One-time LLM-filtered import from `~/.openclaw/` (USER.md + MEMORY.md) into core on first startup. `append_context()` enforces code-level dedup against core — lines already in MEMORY.md are automatically filtered. Config: `enable_memory_extraction`, `memory_extraction_cooldown`, `memory_extraction_step_interval`. Per-session `MemoryExtractor` instances in ACP to avoid cross-session state leaks.

**Sandbox** (`tools/jupyter_tool.py`): Dual-mode execution environment. In normal mode: subprocess kernel in isolated venv (`SandboxEnvironment` + `JupyterKernelSession`). In frozen/runtime mode: in-process kernel (`InProcessKernelSession` via `ipykernel.inprocess`) with bundled packages. `IS_FROZEN` flag (from `sys.frozen`) selects the mode. Runtime package installs go to `~/.box-agent/runtime-packages/` via pip-as-library, gated by `ALLOWED_RUNTIME_PACKAGES` whitelist. Structured error codes: `SANDBOX_INIT_FAILED`, `KERNEL_START_FAILED`, `KERNEL_DIED`, `PACKAGE_NOT_ALLOWED`, `PACKAGE_NOT_AVAILABLE`. All kernel `execute()` calls run in `run_in_executor` + `asyncio.wait_for` to avoid blocking the event loop; pip install operations have a 120s timeout.

**Safety layer** (`tools/safety.py`): Dangerous command detection (rm, sudo, kill, etc.) with user confirmation prompt (supports Chinese). Workspace path validation blocks access outside workspace when `allow_full_access: false`. Auto-backup to `~/.box-agent/trash/{timestamp}/` before file modifications. Non-interactive mode (`--task`) rejects dangerous commands outright.

**Loop guards** (`loop_guards.py`): Per-tool call budgets, no-progress / near-limit wrap-up nudges injected into the conversation, and `CompletionGate` / `build_auto_completion_gate()` — the engine behind the `--no-completion-gate` flag and goal-autopilot "no progress" stop logic. Imported by `core.py`, `cli.py`, `agent.py`, and `acp/`.

**Experts** (`experts.py`): Expert / expert-team session metadata for host integrations (`ExpertProfile`, `ExpertTeamProfile`, `ExpertSessionContext`, workflow stages). Each renders prompt fragments via `render_prompt()` that `acp/` injects into the system prompt. ACP-only (host-supplied via `_meta`).

**Auth** (`auth.py`): Bearer-token / hosted-gateway auth headers (`resolve_auth_token`, `bearer_auth_headers`, `should_attach_auth_header`, `request_auth_headers`). Consumed by `llm/base.py`, `config.py`, `mcp_loader.py`, and `image_generation_tool.py`.

**Shared types** (`schema/`): Core Pydantic/Enum definitions (`LLMProvider`, `Message`, `ToolCall`, `LLMResponse`, `TokenUsage`, `StreamEvent`) re-exported via `schema/__init__.py`. `utils/terminal_utils.py` holds CJK-aware terminal width helpers (`calculate_display_width`, `truncate_with_ellipsis`, `pad_to_width`).

**Config** (`config.py`): Pydantic models. Load priority: `box_agent/config/` (dev) → `~/.box-agent/config/` (installed) → package directory (fallback). Main files: `config.yaml`, `system_prompt.md`, `analysis_prompt.md`, `code_prompt.md`, `mcp.json`.

**CLI** (`cli.py`): Interactive mode with prompt_toolkit. In-session commands: `/help`, `/clear`, `/history`, `/stats`, `/log`, `/exit`. Subcommands: `setup`, `config`, `doctor`, `log`. Auto-launches setup wizard on first run or when API connection fails.

## Key Patterns

- All LLM and tool calls are async
- Retry with exponential backoff (`retry.py`, `@async_retry` decorator)
- Tools return `ToolResult` (Pydantic model with success/content/error)
- Skills use progressive disclosure: YAML metadata loaded first, full content on-demand
- Skills discovery is off the ACP boot critical path: `initialize_base_tools(defer_skills=True)` returns a background `skill_task` (see `box_agent/tools/setup.py::await_skill_discovery`) so stdio setup — and the host's `initialize` RPC — never wait on `rglob("SKILL.md")` + `yaml.safe_load` of a poisoned skills directory. `BoxACPAgent._ensure_skills_loaded()` awaits the task before `newSession` (and again on the first `prompt` as a safety net). During in-flight discovery, `_skills_meta()` returns `None` — the host reads the catalog off `session/new._meta.skills` instead. Parse failures across all SKILL.md files are collected into `SkillLoader.parse_errors` and emitted as ONE aggregate stderr line per `discover_skills` call, not one per file (Windows syscall storm was the timeout root cause). CLI keeps the inline `discover_skills()` call so users still see the "Loading Claude Skills..." status
- Broken skill fallback (Hermes parity): a `SKILL.md` that exists but fails to parse (bad YAML, missing name/description, non-mapping frontmatter, unreadable file) stays in the catalog as a **broken placeholder** — `Skill(name=<parent-dir-name>, description="(SKILL.md malformed — <reason>)", content="", broken=True, broken_reason=<reason>)`. The alternative — silently dropping the entry — leaves skill authors chasing a skill that "vanished" from `## Available Skills`. Broken skills score only by `name_overlap` in `filter_by_query` (their diagnostic description contains generic tokens like "error"/"parse" that would incorrectly match unrelated queries otherwise); `get_skills_metadata_prompt` prefixes them with `⚠️ ` so the model won't confuse them for real skills; `GetSkillTool.execute` renders an `UNAVAILABLE` diagnostic prompt (never empty content) and attaches `raw_output={broken, broken_reason, skill_path}` for host UIs. A missing SKILL.md file is still skipped (Hermes parity: only "file present but malformed" gets the fallback)
- Agent workspace defaults to CWD; logs go to `~/.box-agent/log/`
- `asyncio_mode = "auto"` in pytest config — async tests work without markers
- Safety: dangerous commands require confirmation; workspace scope enforced by default; files auto-backed up before modification
- Permission negotiation: in-band blocking flow for out-of-workspace access. `GrantStore` tracks grants at prompt/session scope; `PermissionEngine` consults grant store before policy check. `run_agent_loop()` accepts `permission_negotiator` — on tool denial with `permission_request`, negotiator is called and tool retried on grant. CLI uses interactive terminal prompt (`cli_permissions.py`, `termios`+`run_in_executor`); ACP uses `session/request_permission` reverse RPC (`_PermissionNegotiator` in `acp/__init__.py`, 120s timeout). `officev3_permissions_override` in `session/new._meta` is deprecated (parsed but ignored)
- Artifact pipeline: every artifact lands under `{workspace}/output/` — the sandbox kernel chdirs there, `write_file` resolves `output/<name>` relative paths there, and the host's file panel only trusts this directory. Detection is two-layer: regex scans tool output for `[filename.ext]` references that resolve under `output/`, workspace diff catches files created by any tool. Both emit a single `ArtifactEvent` (kind/filename/rel_path/abs_path/uri/mime/size/sha256/produced_at). ACP serializes via `_artifact_envelope()` with stable `type: "artifact"` discriminator on `tool_call_update.rawOutput`. Helpers in `core.py`: `ensure_output_dir()`, `safe_output_name()`, `avoid_collision()`, `_make_artifact()`
- LibreOffice (`soffice`) is a system dependency, NOT auto-installed. Excel export defaults to pandas + openpyxl. `recalc.py` gracefully handles missing soffice
- Image watermark: `generate_image` stamps a text watermark (`AI 生成` by default) onto the bitmap **before writing to disk**, so a single call site in `image_generation_tool.py::execute()` covers text-to-image + image-to-image and CLI + ACP alike. Logic lives in `box_agent/tools/watermark.py::apply_text_watermark(image_bytes, mime_type, text)` — bottom-right, semi-transparent white fill + contrast stroke (readable on light/dark), font size = `0.022 × short_edge`. Per-call params: `watermark: false` disables (the PPTX skill always passes this — PPT carries its own watermark), `watermark_text` overrides the wording. The param description is intentionally strict so the model does NOT disable it for "test"/"solid color"/"no text" images. PIL is lazy-imported and **degrades gracefully** — missing Pillow, SVG/GIF (`_RASTERIZABLE_MIME`), or a decode error returns the original bytes with `applied: False`, never failing generation; status rides on `raw_output["watermark"]` and the result text. JPEG (no alpha) is flattened onto white before re-encode. Bundled CJK font `box_agent/skills/canvas-design/canvas-fonts/NotoSansSC-Regular.otf` (ships via `skills/**/*`) is the primary font, then system fonts, then `ImageFont.load_default(size=)`. **Pillow is a MAIN dependency** (not the `runtime` extra) because watermarking is on by default; the frozen runtime's `external_python_sandbox` mode must keep `PIL` bundled (it is NOT in `build_runtime.py`'s exclude sets) since the watermark runs in the ACP main process, not the external sandbox
- Browser automation (Playwright MCP): `@playwright/mcp` is registered in `mcp-example.json` as `playwright` with `--isolated`, `disabled: true` by default. Chromium cache defaults to `~/.box-agent/browsers/` (shared by CLI install and ACP runtime). `box-agent install-browser` runs `playwright install chromium` with `PLAYWRIGHT_BROWSERS_PATH` pinned to this path, then flips `disabled` to `false` in `~/.box-agent/config/mcp.json`. `run_acp_server()` calls `os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "~/.box-agent/browsers")` at startup — hosts that want a different cache export the env var before spawning `box-agent-acp`. If Chromium is absent, Playwright MCP tool calls fail with its own error; we don't wrap. `doctor` reports runtime status
- Managed browser isolation: with `--isolated` on the `playwright` stdio entry, one Chromium process gives every ACP session and each `sub_agent` run its own BrowserContext. Without `--isolated`, or with `tools.mcp.playwright_per_session_context: false`, it falls back to one shared stdio session and `BROWSER_RUNTIME_BUSY`. Details: `docs/DEVELOPMENT_GUIDE.md` / `playwright_session_pool.py`
- Frozen/runtime mode: `IS_FROZEN` flag selects in-process kernel, skips venv creation, routes package installs through whitelist + `~/.box-agent/runtime-packages/`
- Sub-agent: `SubAgentTool` runs tasks in isolated message contexts via `run_agent_loop()`. Child tools exclude `sub_agent` itself (no recursion). Multiple sub-agents execute in parallel via `asyncio.gather` (tools with `parallel_safe = True`)
- EventEmittingTool: base class for tools that emit real-time progress events via an `asyncio.Queue` that core.py drains in the foreground generator. Both sequential and parallel execution blocks support this pattern. SubAgentTool emits `SubAgentEvent` directly.
- PPT support: Provided entirely through the `ppt` skill (loaded from `SKILL.md` via `skill_loader.py`). The agent invokes general-purpose tools; there are no dedicated PPT modes/tools/prompts in box-agent core.
- Context compression: Layer 1 `_micro_compact()` runs every step, replaces tool results older than last 3 with `[Previous result from {tool}: {first_line}...]` (token-aware: keep window shrinks if recent results themselves blow the budget). Layer 2 `_maybe_summarize()` triggers an LLM summary when estimated tokens exceed `context_token_limit` (derived as `0.9 × (context_window − max_output_tokens)`; defaults are 180000/63999 ≈ 104k for user-configured endpoints and 180000/80000 ≈ 90k for hosted xiaohuanxiong endpoints). Logger captures originals — no data loss. Full design: `docs/CONTEXT_COMPRESSION.md` (中文版 `docs/CONTEXT_COMPRESSION_CN.md`)
- Memory: dual-file `MEMORY.md` (core, always injected) + `CONTEXT.md` (searchable on demand). Two write paths — skill-guided LLM calls `memory_write` for explicit user intent, `MemoryExtractor` auto-extracts to CONTEXT.md in background (`asyncio.create_task`). Core/Context dedup enforced at code level (`append_context()` filters lines present in core). OpenClaw import is one-time LLM-filtered bootstrap into core
- Memory maintenance: `MemoryMaintainer` (memory_maintainer.py) runs at CLI/ACP startup, guarded by `.maintainer_last_run` (24h interval). Three phases on `CONTEXT.md` — decay (hits=0 + last_used > `memory_decay_days` → `CONTEXT.archive.md`), cleanup (archive > decay+`memory_archive_days` → `~/.box-agent/memory/trash/<date>/`), dedup (greedy Jaccard at `memory_dedup_jaccard`, winner = higher hits / older created; metadata merged). `ContextEntry` carries HTML-comment header metadata (id/created/last_used/hits/source/confidence + optional core_status/last_proposed); legacy line-based CONTEXT.md auto-migrates on first write
- Memory promotion gate: `run_agent_loop` emits `MemoryProposalEvent` before terminal `DoneEvent` when `memory_promotion_proposal_enabled` and there are entries with `hits >= memory_promotion_hit_threshold`, not rejected, not in cooldown. `consume_core_proposal({id: pin/skip/reject})` — pin moves entry to MEMORY.md (line-dedup), reject sets `core_status="rejected"` (permanent, never re-proposed), skip is no-op (cooldown bumped at emit). CLI: termios prompts via `CLIMemoryProposalNegotiator` + `/memory review` slash command (bypasses cooldown). ACP: `_MemoryProposalNegotiator` calls `extMethod("session/memory_proposal", payload)` with 120s timeout; host opt-in via `_session/memory_proposal` JSON-RPC, returns `{decisions: {id: "pin"|"skip"|"reject"}}`; method_not_found degrades to skip-all
- ACP stdout guard: `sys.stdout = sys.stderr` in ACP mode (must use direct assignment, NOT `TextIOWrapper(sys.stderr.buffer)` which destroys stderr on GC). `_real_stdout = sys.__stdout__` (not `sys.stdout`, which may be pre-guarded by `runtime_entry.py`). Real stdout restored only for the stdio transport setup, then re-guarded. All diagnostics and third-party output go to stderr
- ACP stdin buffer ceiling: do NOT call upstream `acp.stdio_streams()` — it builds `asyncio.StreamReader()` without a `limit`, defaulting to 64 KiB. A single inbound JSON-RPC frame larger than that (e.g. `session/prompt` with base64-inlined images, a large pasted document, or host-injected `_meta`) raises `asyncio.LimitOverrunError` inside `Connection._receive_loop`; the loop's `except` only catches `CancelledError`, so it dies silently and every later RPC future is rejected with `ConnectionError("Connection closed")` — the symptom on the host side. Use `box_agent.acp.stdio_compat.stdio_streams_largebuf()` instead, which replicates the upstream POSIX/Windows stdio helpers verbatim and passes `limit=4 * 1024 * 1024` (4 MiB) to the reader. Everything else in `acp` (Connection, AgentSideConnection, dispatcher, schemas) is still used unchanged. If the 4 MiB ceiling ever bites, raise `_READ_LIMIT` in `stdio_compat.py` — do not switch back to the upstream helper
- Session mode: ACP caller passes `_meta.session_mode` on `session/new` (`data_analysis` or `code_agent`). `_build_session_prompt()` looks up `_MODE_PROMPT_MAP` and injects the matching mode prompt (`analysis_prompt_path` / `code_prompt_path`) into `messages[0]`, preserving the workspace-info footer and the cached `memory_block`. When omitted, no mode prompt is applied (general mode). There is NO auto-classification — the earlier `intent_classifier.py` / `auto_classify_pending` / `_apply_session_mode` path was removed (hosted gateways supply the mode explicitly)
- Deep-think / extended thinking: session-level toggle via `_meta.deep_think: bool` on `session/new`. Threaded as `thinking_enabled` through `Agent.__init__` → `run_agent_loop` → `LLMClient.generate_stream`. Anthropic path: `thinking={"type": "enabled", "budget_tokens": 8000}` (only `enabled`/`disabled` are valid; no `adaptive`). Budget is hardcoded 8000 tokens. Classifier and summary LLM calls always run with `thinking_enabled=False`. Caveat: some models (e.g. MiniMax M2 series) emit thinking blocks unconditionally as part of their default output; the toggle is honored by the wire protocol but cannot suppress provider-side reasoning behavior
- Goal autopilot / completion gate: a persistent goal (`--goal` flag, `box-agent goal ...` subcommand, `/goal` slash command, or ACP `_meta`) drives the agent toward a stated objective across multiple turns. `loop_guards.CompletionGate` checks whether the goal's gaps are closed; `build_auto_completion_gate()` wires it up. Autopilot re-prompts itself until the gate is satisfied or a stop condition hits, governed by `config.py` keys `goal_autopilot_enabled` (default `true`), `goal_autopilot_max_turns` (`3`), `goal_autopilot_max_seconds` (`14400.0`), `goal_autopilot_no_progress_turns` (`2`). Disable per-run with `--no-goal-autopilot`; disable the gate with `--no-completion-gate`
- Cancellation: cooperative — `is_cancelled` callback is checked at five points: top of each step, between LLM stream chunks, after stream completes, before tools, and after each tool. CLI installs a key listener that flips a flag on Esc; ACP flips `state.cancelled` from `cancel()` notification. Mid-stream cancellation breaks the chunk loop and yields `DoneEvent(stop_reason=CANCELLED)` immediately rather than waiting for the LLM to finish
- CLI input: simple Esc-to-cancel listener while the agent runs (`termios` cbreak in a daemon thread, no scroll-region UI). The `inject_queue` plumbing in `run_agent_loop` is preserved for ACP only — ACP exposes `_inject` extension method (turn-active guarded, stale queue drained per turn). CLI does not currently wire up inject
- Lightweight LLM endpoint: ACP extension method `_llm/prompt` for title/summary/classification-style one-shot calls. Bypasses `newSession` entirely — no session, no tools, no skills, no MCP wait, no memory recall/extraction, no conversation history. Backed by `box_agent/llm/lightweight.py::run_lightweight_prompt(llm, prompt, system_prompt=None, timeout=30.0)` which wraps `LLMClient.generate(tools=None, thinking_enabled=False)` in `asyncio.wait_for`. Request: `{prompt, systemPrompt?, timeoutMs?, workspaceLabel?, _meta?: {purpose?}}`. Success: `{text, finishReason, usage:{inputTokens,outputTokens}, durationMs}`. Failure: `{error:{code,message}}` with codes `invalid_args` / `timeout` / `lightweight_failed`. Logs purpose/duration/token counts but never the prompt body
- Per-turn token total: `box_agent/llm/token_meter.py` is a context-local accumulator. `LLMClient.generate`/`generate_stream` (the single choke point all LLM calls funnel through — main multi-step loop, Layer-2 summarization, background `MemoryExtractor`) call `record_usage(usage)`. ACP `_prompt` wraps `_run_turn` with `start_token_meter()`/`reset_token_meter()` and returns the turn total on `PromptResponse._meta.usage.totalTokens` (host-side telemetry; officev3 埋点 reads it). Per-turn scoped (resets each turn, not cumulative). `asyncio.create_task` children copy the context and mutate the same accumulator, so in-turn memory extraction is counted — but fire-and-forget extractions (notably `loop_end`) that finish after the turn returns are NOT reflected (best-effort for memory, exact for main loop + summarization). The `_llm/prompt` lightweight endpoint runs outside any turn, so no meter is active there (no double counting). Tests: `tests/test_token_meter.py`, `tests/test_acp.py::test_acp_prompt_response_reports_turn_token_total`

## Configuration

Run `box-agent setup` for interactive configuration, or manually copy `box_agent/config/config-example.yaml` to `box_agent/config/config.yaml`. Provider field (`anthropic` or `openai`) determines which client is used. The `api_base` is passed through directly — supports any compatible endpoint.

## Publishing

```bash
# Bump version in pyproject.toml and box_agent/__init__.py
# Regenerate the builtin skills whitelist so orphan SKILL.md files left
# behind by downstream installers (e.g. officev3) are filtered out at runtime.
python scripts/generate_skills_manifest.py  # writes box_agent/skills/_manifest.json
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

Runtime structure: `box-agent-runtime/{manifest.json, VERSION, bin/box-agent-acp, runtimes/node}` on macOS. The binary communicates via ACP JSON-RPC over stdio. Hard constraint: stdout = pure ACP protocol, all diagnostics go to stderr.

Key files:

- `scripts/build_runtime.py` — PyInstaller build script, auto-detects platform
- `box_agent/acp/runtime_entry.py` — Clean entry point for standalone binary
- `box_agent/acp/debug_logger.py` — Structured logger (stderr + optional file, env-var controlled)

PyPI: https://pypi.org/project/box-agent/
GitHub: https://github.com/Raccoon-Office/Box-Agent
