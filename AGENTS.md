# Repository Guidelines

## Project Structure & Module Organization

`box_agent/` contains the application code: `agent.py` drives the execution loop, `cli.py` exposes the CLI, `llm/` wraps model providers, `tools/` holds built-in tools, `acp/` hosts the ACP server, and `config/` stores example config files. `tests/` contains the automated test suite, with files such as `test_agent.py` and `test_mcp.py`. `examples/` provides runnable demos, while `docs/` and `docs/assets/` hold contributor-facing documentation and images. Treat `workspace/` as runtime scratch space, not committed source.

## Build, Test, and Development Commands

Use `uv` for local development.

- `uv sync`: install project and dev dependencies from `pyproject.toml` and `uv.lock`.
- `uv run python -m box_agent.cli`: run the CLI in development mode.
- `uv tool install -e .`: install `box-agent` and `box-agent-acp` as editable local commands.
- `pytest tests/ -v`: run the full test suite.
- `pytest tests/test_agent.py -v`: run a focused subset while iterating.

If you need bundled skills, run `git submodule update --init --recursive` before testing skill-related changes.

## Coding Style & Naming Conventions

Follow PEP 8 with 4-space indentation. Use type hints for public functions and async interfaces. Keep modules and functions in `snake_case`, classes in `PascalCase`, and test files named `test_<area>.py`. Match the existing style in `box_agent/tools/` and `box_agent/llm/`: short docstrings where needed, small focused helpers, and minimal unrelated refactors.

## Testing Guidelines

Pytest is the test runner, with `pytest-asyncio` enabled for async tests. Add or update tests for every behavior change, especially around tool execution, MCP loading, session memory, and CLI flows. Name tests after observable behavior, for example `test_bash_tool_rejects_outside_workspace`. There is no stated coverage gate, but changed code should have direct regression coverage.

## Commit & Pull Request Guidelines

Recent history uses conventional-style subjects such as `feat(cli): ...`, `fix(skill): ...`, and `docs: ...`. Keep commits small and scoped. For pull requests, include a clear summary, link related issues when applicable, note config or skill-submodule impacts, and list the test command(s) you ran. Update `README.md`, `CONTRIBUTING.md`, or `docs/` when user-facing behavior changes.
