# Code Search Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded `grep` and `glob` tools for code-agent sessions, backed by an OfficeV3-injected ripgrep executable while retaining `search_files` as the compatibility fallback.

**Architecture:** A private ripgrep runner owns executable resolution, safe subprocess execution, output bounds, cancellation, and path permission checks. Two narrow public tools expose content search and file discovery. Workspace assembly registers them only for `code_agent` sessions when ripgrep is available; all sessions continue to receive `search_files`.

**Tech Stack:** Python 3, asyncio subprocesses, pytest/pytest-asyncio, existing Box-Agent Tool and permission APIs.

**Spec:** User-approved design in the current task: OfficeV3 packages `rg.exe`, makes it discoverable to the Box-Agent process, and Box-Agent exposes dedicated code-search tools without downloading binaries.

## Global Constraints

- Keep `search_files` registered for compatibility and fallback behavior.
- Prefer the explicit `BOX_AGENT_RG` executable path, then fall back to `PATH` for existing OfficeV3 and development environments.
- Never invoke ripgrep through a shell and never download it from Box-Agent.
- Default searches respect ignore files and exclude Git metadata.
- Do not modify the stable agent loop or core scheduling behavior.
- Do not commit or push without explicit user authorization.

---

### Task 1: Ripgrep-backed search tools

**Files:**
- Create: `box_agent/tools/ripgrep_tool.py`
- Create: `tests/test_ripgrep_tool.py`
- Modify: `box_agent/tools/__init__.py`

**Interfaces:**
- Produces: `resolve_ripgrep_executable(runtime_env=None) -> str | None`
- Produces: `GrepTool.execute(pattern, path=".", include=None, limit=100)`
- Produces: `GlobTool.execute(pattern, path=".", limit=100)`

- [x] Write failing tests for executable resolution, brace globs that retain ignore semantics, structured grep matches, result truncation, permission denial, invalid regex, and cancellation-safe subprocess cleanup.
- [x] Run `uv run pytest tests/test_ripgrep_tool.py -q` and confirm failure because the new module does not exist.
- [x] Implement the minimum shared runner and public tools needed by those tests.
- [x] Re-run `uv run pytest tests/test_ripgrep_tool.py -q` until green.

### Task 2: Code-mode registration and fallback

**Files:**
- Modify: `box_agent/tools/setup.py`
- Modify: `box_agent/session_assembly.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_session_adapter_assembly.py` if shared assembly coverage requires it.

**Interfaces:**
- Consumes: `resolve_ripgrep_executable`, `GrepTool`, and `GlobTool` from Task 1.
- Produces: code-agent tool lists containing `search_files`, `grep`, and `glob` when ripgrep is available; general sessions and unavailable-ripgrep sessions retain `search_files` without the new tools.

- [x] Add failing observable registration tests for code mode, general mode, and unavailable ripgrep.
- [x] Run the focused tests and confirm the code-mode registration assertion fails.
- [x] Pass `session_mode` through shared session assembly and conditionally register the two tools.
- [x] Re-run the focused setup and session-assembly tests until green.

### Task 3: Prompt contract and integration regression

**Files:**
- Modify: `box_agent/config/code_prompt.md`
- Modify: `box_agent/config/system_prompt.md`
- Modify: `box_agent/tools/bash_tool.py`
- Modify: `tests/test_acp_auto_mode.py`
- Modify: `tests/test_bash_tool.py`
- Modify: schema fixtures only if the intentional public tool description changes require regeneration.

**Interfaces:**
- Consumes: the `grep`, `glob`, `read_file`, and `search_files` tool names.
- Produces: a consistent model contract that prefers dedicated tools in code mode and treats Bash ripgrep as an advanced fallback only.

- [x] Add or update behavioral prompt/tool-description tests that fail under the contradictory current guidance.
- [x] Make the smallest prompt and Bash description edits that establish one consistent search policy.
- [x] Run focused ACP, Bash, CLI, and tool schema tests.
- [x] Run `git diff --check`, inspect the complete diff, and run the related broader test subset before reporting completion.
