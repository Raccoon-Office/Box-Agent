# Repository Guidelines

## Project Structure & Module Organization

`box_agent/` contains the application code: `agent.py` drives the execution loop, `cli.py` exposes the CLI, `llm/` wraps model providers, `tools/` holds built-in tools, `acp/` hosts the ACP server, and `config/` stores example config files. `tests/` contains the automated test suite, with files such as `test_agent.py` and `test_mcp.py`. `examples/` provides runnable demos, while `docs/` and `docs/assets/` hold contributor-facing documentation and images. Treat `workspace/` as runtime scratch space, not committed source.

## Code Discovery & Understand Anything

For architecture, ownership, dependency, onboarding, and change-impact questions,
use this default order:

1. Check the committed `.understand-anything/knowledge-graph.json` and read its
   `project.gitCommitHash` as the analyzed source baseline. Compare that
   baseline with later source changes before treating the graph as current.
   Cross-check `.understand-anything/meta.json` for the refresh timestamp,
   baseline commit, graph version, and analyzed file count. If present, inspect
   `.understand-anything/last-run-summary.json` for refresh status.
2. Use `.understand-anything/knowledge-graph.json`, or the most relevant
   `.understand-anything/domain-graphs/*knowledge-graph.json` when such a graph
   exists, as the initial codebase index.
3. Extract only the relevant nodes, edges, layers, or tour steps with `jq`,
   `rg`, or a focused keyword search. Do not load or summarize the entire graph
   when a narrow query is sufficient.
4. Open the smallest useful set of real source files and verify the graph-based
   conclusion with direct reads, `rg`, focused tests, logs, or runtime probes as
   appropriate.

Treat every graph as a navigation index, not the source of truth. If the graph,
metadata, or tooling is missing or stale, state the limitation and continue
with normal source search when the task can still proceed; recommend a refresh
when it would materially improve the result.

Keep the shared `.understand-anything/knowledge-graph.json`, `meta.json`,
`fingerprints.json`, `.understandignore`, and `config.json` in Git. The graph,
metadata, and fingerprints form one refresh baseline and must be regenerated
and reviewed together; do not hand-edit them. Keep `last-run-summary.json`,
intermediate files, dashboard tokens, trash, and caches local. Add any future
shared domain graph to Git intentionally together with its scope documentation.

## AI Development Workflow

Automated coding assistants must treat this section as the default execution
protocol for repository work.

### Before Editing

1. Read the user request literally and keep inspect, analyze, review, and status
   requests read-only. Do not edit, commit, merge, package, install, or publish
   unless the user asked for that action.
2. Run `git status --short --branch` before changing files. Preserve unrelated
   tracked, staged, and untracked work. Do not reset, clean, stash, switch the
   user's branch, or rewrite existing changes to make the task easier.
3. Classify the behavior before choosing files:
   - host/protocol translation belongs in `box_agent/acp/` or `box_agent/cli.py`
   - tools, skills, providers, storage, and reusable workflow policy belong in
     their capability modules
   - shared contracts and loop invariants belong in the stable API/kernel layer
4. Inspect the nearest existing implementation and its tests before adding a
   new helper, abstraction, directory, dependency, or configuration surface.
5. Define the observable success condition and the smallest check that proves
   it. Distinguish requirements from assumptions and state any assumption that
   materially affects behavior.

### While Editing

- Make the smallest reviewable change that solves the requested behavior.
  Avoid unrelated refactors, formatting churn, renames, and speculative
  compatibility layers.
- Implement shared behavior once behind a shared contract. Keep CLI and ACP as
  adapters; do not maintain parallel copies of the same runtime policy.
- Do not modify `core.py` or another stable-kernel file for product-specific
  behavior that can be expressed through a Tool, Skill, Hook, event consumer,
  run option, completion gate, or `WorkflowPolicy`.
- Preserve public API compatibility unless the task explicitly changes the
  contract. Prefer additive event and option changes; document migrations for
  removals or semantic changes.
- Add or update a direct regression test for every behavior change. Test names
  should describe observable outcomes rather than internal implementation.
- Do not hand-edit generated manifests, lock state, timestamps, or packaged
  artifacts when a repository generator or installer owns them.
- Never put secrets, tokens, local config, user data, logs, `workspace/`
  contents, caches, or temporary analysis artifacts in a patch.

### Verification Matrix

Run focused checks first, then broaden only when the ownership or blast radius
requires it.

| Change type | Minimum proof |
| --- | --- |
| Tool behavior | Direct success-path and important failure-path tool tests |
| Provider or LLM wire behavior | Focused provider tests, including malformed/error responses when relevant |
| Memory or persistence | Focused persistence tests and configuration-gating checks |
| CLI-only behavior | Focused CLI test or captured command output; confirm ACP behavior was not duplicated |
| ACP, events, or host metadata | Focused ACP/protocol tests and stdout/stderr boundary checks |
| MCP loading or configuration | Loader/config tests plus a bounded status or connection probe when applicable |
| Built-in Skill | Focused matcher/loader tests, regenerate `_manifest.json`, and inspect its diff |
| Shared core, scheduling, cancellation, or security invariant | Focused regression tests plus the related core/ACP suite and full suite when feasible |
| Runtime packaging | Build/install/probe evidence, or an explicit statement that validation stopped at source |
| Documentation only | Link/path/command checks and `git diff --check` |

If a broad test fails because of an unrelated baseline problem, report the
focused result and the exact baseline failure separately. Do not describe the
repository as clean when only focused checks passed.

### Runtime and Deployment Truth

For behavior consumed by officev3 or another packaged host, report each state
separately:

```text
source changed -> source tests passed -> runtime built -> runtime installed
-> probe passed -> host restarted -> fresh live task verified
```

Never claim that source tests prove packaged behavior. Installing a runtime into
an officev3 checkout does not update an installed application, and a running
ACP process does not hot-reload a newly installed runtime. If the task stops at
any boundary, name the last completed boundary and the remaining validation.

### Commit and Pull Request Protocol

- Do not commit, push, merge, tag, or publish without explicit user authority.
- Before a commit, inspect `git status`, the intended diff, and the staged diff.
  Stage explicit paths; never use `git add -A` in a dirty checkout.
- Before opening or updating a pull request, rebase the contributor branch onto
  the base repository's latest `main` (`upstream/main` for forks or `origin/main`
  for direct clones). Do not merge `main` into the contributor branch. After
  rebasing a published branch, push only with explicit user authority and use
  `--force-with-lease`; never use `--force`.
- Keep one commit focused on one behavior or subsystem. Use a conventional
  subject such as `feat(tools): ...`, `fix(acp): ...`, `test(memory): ...`, or
  `docs: ...`.
- Regenerate owned artifacts before committing and include them only when the
  source change requires them. Version changes must remain consistent across
  all repository-owned version surfaces and lock state.
- Every non-trivial PR must use TPR: Task states behavior and scope, Proof lists
  exact checks and runtime evidence, and Risk covers compatibility, packaging,
  configuration, migration, rollback, and cross-repository follow-up.

### Required Handoff

End implementation work with a concise report in this form:

```text
Changed: files and observable behavior
Proof: exact tests, checks, probes, or artifacts inspected
Runtime status: source/build/install/probe/restart/live-task boundary reached
Not verified: skipped checks and why
Risk: remaining compatibility, packaging, migration, or follow-up concerns
```

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

## Collaboration & Review Rules

Use TPR in every non-trivial PR description: Task (what changed and what is out of scope), Proof (tests, probes, logs, screenshots, or generated-manifest checks), and Risk (compatibility, packaging, migration, config, or rollback notes). Keep PRs scoped to one behavior or subsystem. For shared behavior, prefer changes in the shared core (`core.py`, shared tools, config, or schema) and keep CLI / ACP code as thin adapters. If a change affects packaged runtime behavior used by officev3, call out whether source-only tests are enough or whether a runtime rebuild/install/probe is required.

## Automated Review Contract

Repository-owned automated review configuration lives under `general_review/`.
The generic `teamwork_review_agents` service supplies scanning, preflight,
orchestration, locking, and audit infrastructure; it must not own Box-Agent CI
commands or repository-specific review policy.

Before reviewing a pull request:

1. Read `docs/REVIEW_GUIDE_CN.md` and `general_review/review.config.yaml`.
2. Treat `general_review/ci/preflight.sh` as the source of truth for the local
   `teamwork/local-ci` gate.
3. Review the complete merge-base diff between the supplied target and change
   refs, not only the latest commit or current checkout.
4. Use the single `general-reviewer` role referenced by
   `general_review/review.config.yaml`. Its reusable Prompt lives only in the
   configured `teamwork_review_agents` checkout and covers design,
   history/target-branch consistency, and security in one review.
5. Use `.understand-anything/` only as a navigation index and verify findings
   against current source, Git history, tests, logs, or probes.
6. Keep all automated Review Agents read-only. Human maintainers retain final
   merge authority.
7. Never commit `general_review/runtime/`, credentials, logs, or generated local
   state.

## Commit & Pull Request Guidelines

Recent history uses conventional-style subjects such as `feat(cli): ...`, `fix(skill): ...`, and `docs: ...`. Keep commits small and scoped. For pull requests, include a clear summary, link related issues when applicable, note config or skill-submodule impacts, and list the test command(s) you ran. Update `README.md`, `CONTRIBUTING.md`, or `docs/` when user-facing behavior changes.
