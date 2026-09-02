# Contributing Guide

Thank you for your interest in the Box Agent project! We welcome contributions of all forms.

## How to Contribute

### Reporting Bugs

If you find a bug, please create an Issue and include the following information:

- **Problem Description**: A clear description of the problem.
- **Steps to Reproduce**: Detailed steps to reproduce the issue.
- **Expected Behavior**: What you expected to happen.
- **Actual Behavior**: What actually happened.
- **Environment Information**:
  - Python version
  - Operating system
  - Versions of relevant dependencies

### Suggesting New Features

If you have an idea for a new feature, please create an Issue first to discuss it:

- Describe the purpose and value of the feature.
- Explain the intended use case.
- Provide a design proposal if possible.

### Submitting Code

#### Getting Started

1. Fork this repository.
2. Clone your fork:
   ```bash
   git clone https://github.com/Raccoon-Office/Box-Agent box-agent
   cd box-agent
   ```

3. Create a new branch:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/your-bug-fix
   ```

4. Install development dependencies:
   ```bash
   uv sync
   ```

5. Run a quick local check:
   ```bash
   uv run pytest tests/ -q
   ```

6. Start the development CLI when you need a manual smoke test:
   ```bash
   uv run python -m box_agent.cli
   ```

#### Team Collaboration Baseline

- Prefer small PRs that change one behavior or one subsystem.
- Follow the [layered architecture and ownership rules](docs/ARCHITECTURE.md). Shared behavior does not automatically belong in `core.py`: prefer a capability/policy module behind the public contracts, and keep CLI/ACP code as adapters.
- Before changing code paths, use `.understand-anything/` as the first navigation aid when it is available, then verify the path with source reads, `rg`, tests, logs, or runtime probes. See the [code map guide](docs/UNDERSTAND_ANYTHING.md) for scope and refresh steps.
- Keep the shared Understand Anything refresh baseline and configuration in Git (`knowledge-graph.json`, `meta.json`, `fingerprints.json`, `.understandignore`, and `config.json` under `.understand-anything/`). Regenerate and review the graph, metadata, and fingerprints together when architecture boundaries or the guided tour change. Do not commit `last-run-summary.json`, intermediate, trash, dashboard tokens, or cache files.
- Do not include local credentials or user config. `config.yaml`, `mcp.json`, logs, and `workspace/` are local runtime files.
- If a change affects officev3 or any packaged runtime, say whether you verified only source behavior or also rebuilt/installed/probed the runtime artifact.

#### Maintaining Design and Change Records

The [design index](docs/design/README.md) and
[change-history index](docs/changes/README.md) are shared project records for
contributors, maintainers, and reviewers. They should make the current design
and the decisions that shaped it easy to understand, but they are not a log of
every implementation detail or commit.

Update `docs/design/README.md` when a change adds, changes, renames, or retires
a material subsystem, ownership boundary, public or host protocol,
compatibility rule, or source-of-truth design document. Add or revise the
applicable routing row with recognizable areas or paths, the documents to read
first, the current boundary to preserve, and the primary proof anchors. Keep
English/Chinese document pairs aligned when both exist. An internal refactor or
bug fix that preserves the documented design normally does not require an
index update.

Update `docs/changes/README.md` when a change has lasting relevance to a public
or host protocol, stable kernel or tool contract, security boundary,
compatibility default, migration, release/runtime expectation, rollback, or
cross-repository dependency. A detailed entry should record the available
change or PR reference, durable effect, compatibility or migration impact,
proof anchors, residual gaps, and rollback direction. Do not invent a future
merge SHA when the change is still under review; use the PR and available
implementation commit, then reconcile merge information later.

When adding or revising a detailed history entry, update the quick-routing
table if the current effective decision or its relationship to earlier entries
has changed. Use stable, searchable module, path, protocol, and configuration
names, but keep the summary readable. State whether an entry supersedes,
hardens, rolls back, or must be read together with an earlier decision instead
of silently replacing history.

Update both indexes when one PR changes a design boundary and also establishes
a durable compatibility, migration, security, release, or rollback decision.
Ordinary implementation notes, commits with no lasting contract impact, and
changes already covered accurately by the indexes do not need separate
entries.

#### Ownership Boundaries

- `box_agent/core.py` is a changeable but low-churn kernel owned by the core team. Product and capability modules must use `Agent.run_events()` or the explicit shared APIs; they must not import the Core implementation directly.
- Agent-loop invariants, event semantics, scheduling, cancellation, tool-call closure, and security enforcement points belong to the stable kernel/contracts. Core changes require a core-maintainer review.
- Reusable tools, skills, providers, and storage belong in the capability layer unless they require a new host-neutral kernel contract. Put stateful domain workflows in a self-contained Skill or plugin; do not add a second durable state source beside Session Log.
- CLI code should handle terminal interaction, rendering, slash commands, and local prompts. It should not fork core behavior that ACP also needs.
- ACP code should translate shared events into ACP protocol updates and host extension methods. Keep stdout protocol-clean; diagnostics belong on stderr or structured logs.
- Provider-specific wire behavior belongs in `box_agent/llm/`; do not spread provider assumptions into tools, skills, CLI, or ACP.
- Tool behavior belongs in `box_agent/tools/` and should return structured `ToolResult` data. Add direct regression tests for new tool semantics.
- Built-in skill loading is controlled by `box_agent/skill_loader.py`, `box_agent/skills/`, and `box_agent/skills/_manifest.json`. When built-in skills change, regenerate the manifest before review.
- PPT/document capabilities are Skill-driven unless there is an explicit host-neutral core contract change. Intent routing, progress derivation, validation, and recovery instructions belong in the owning Skill or plugin; do not add a hidden format-specific lifecycle to the core loop, adapters, or generic tools. Cross-cutting fail-closed Tool safety remains a shared Tool contract.
- Packaged runtime behavior is not proven by source edits alone. If officev3 or a standalone runtime depends on the change, document the runtime rebuild/install/probe status.

#### TPR Pull Request Standard

Every non-trivial PR should be reviewable through TPR:

- **Task**: what behavior changed, why, affected entry points, and what is out of scope.
- **Proof**: exact commands, tests, probes, screenshots, logs, regenerated manifests, or runtime checks.
- **Risk**: compatibility, packaging/runtime impact, migration, config/secrets, rollback plan, and cross-repository follow-up.

The PR must also identify affected architecture layers and record whether
target-branch changes after the merge base were considered. The expanded
[Pull Request Review Standard](docs/PR_REVIEW_STANDARD.md) defines the review
gates used for non-trivial changes.

#### Development Process

1. **Write Code**
   - Follow the project's code style (see the [Development Guide](docs/DEVELOPMENT_GUIDE.md)).
   - Add necessary comments and docstrings.
   - Keep your code clean and concise.

2. **Add Tests**
   - Add test cases for new features.
   - Ensure all tests pass:
     ```bash
     pytest tests/ -v
     ```

3. **Update Documentation**
   - If you add a new feature, update the README or relevant documentation.
   - Keep documentation in sync with your code.

4. **Commit Changes**
   - Use clear commit messages:
     ```bash
     git commit -m "feat(tools): Add new file search tool"
     # or
     git commit -m "fix(agent): Fix error handling for tool calls"
     ```
   
   - Commit message format:
     - `feat`: A new feature
     - `fix`: A bug fix
     - `docs`: Documentation updates
     - `style`: Code style adjustments
     - `refactor`: Code refactoring
     - `test`: Test-related changes
     - `chore`: Build or auxiliary tools

5. **Rebase onto the Latest `main`**
   - Before opening or updating a Pull Request, rebase your branch onto the
     latest `main` from the base repository. Do not merge `main` into your
     feature branch.
   - For a fork-based contribution, add the base repository as `upstream` once,
     then rebase:
     ```bash
     git remote add upstream https://github.com/Raccoon-Office/Box-Agent.git  # once
     git fetch upstream main
     git rebase upstream/main
     ```
   - Direct collaborators may use `origin/main` instead. If the branch was
     already pushed, coordinate before rewriting a shared branch and update it
     with `git push --force-with-lease`; never use `--force`.

6. **Push to Your Fork**
   ```bash
   git push origin feature/your-feature-name
   ```

7. **Create a Pull Request**
   - Create a Pull Request on GitHub.
   - Clearly describe your changes.
   - Reference any related Issues if applicable.

#### Pull Request Checklist

Before submitting a PR, please ensure:

- [ ] The PR description includes Task, Proof, and Risk.
- [ ] The code follows the project's style guide and existing architecture.
- [ ] Focused tests were run for the changed behavior.
- [ ] Broader tests were run when shared core, tools, MCP, memory, CLI, ACP, skills, or packaging behavior changed.
- [ ] Necessary regression tests were added or an explicit reason is given.
- [ ] Relevant documentation was updated.
- [ ] The design and change-history indexes were assessed and updated when design boundaries or durable decisions were affected.
- [ ] Generated manifests were regenerated when built-in skills changed.
- [ ] Runtime rebuild/install/probe status is stated when packaged runtime behavior is affected.
- [ ] No unrelated changes, local config, logs, workspace files, or generated Understand Anything graph/cache files are included.
- [ ] The commit message is clear and follows the conventional style used in this repository.
- [ ] The branch was rebased onto the latest base `main` before this PR was opened or updated; `main` was not merged into the feature branch.

### Code Review

All Pull Requests will be reviewed. Maintainers use the detailed
[Maintainer Review Guide](docs/REVIEW_GUIDE.md) for review order, blockers, and
proof expectations:

- For the full PR gate, severity, and verdict contract, also read the
  [Pull Request Review Standard](docs/PR_REVIEW_STANDARD.md).

- Review starts from the TPR evidence. Missing proof is treated as incomplete work, not as a reviewer task.
- Reviewers should check behavior, ownership boundaries, tests, packaging/runtime implications, and documentation before style details.
- For shared behavior, reviewers should confirm the change is not implemented separately in CLI and ACP unless there is a clear entry-point-specific reason.
- For skill changes, reviewers should check `box_agent/skills/_manifest.json` and any officev3 recommendation-card impact.
- For packaged runtime changes, reviewers should check whether source tests are enough or a runtime rebuild/install/probe is required.
- Once approved, the PR will be merged into the main branch.

## Code Style Guide

### Python Code Style

Follow PEP 8 and the Google Python Style Guide:

```python
# Good example ✅
class MyClass:
    """A brief description of the class.
    
    A more detailed description...
    """
    
    def my_method(self, param1: str, param2: int = 10) -> str:
        """A brief description of the method.
        
        Args:
            param1: Description of parameter 1.
            param2: Description of parameter 2.
        
        Returns:
            Description of the return value.
        """
        pass

# Bad example ❌
class myclass:  # Class names should be PascalCase
    def MyMethod(self,param1,param2=10):  # Method names should be snake_case
        pass  # Missing docstring
```

### Type Hinting

Use Python type hints:

```python
from typing import List, Dict, Optional, Any

async def process_messages(
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int] = None
) -> str:
    """Process a list of messages."""
    pass
```

### Testing

- Write tests for new features.
- Keep tests simple and clear.
- Ensure tests cover critical paths.

```python
import pytest
from box_agent.tools.my_tool import MyTool

@pytest.mark.asyncio
async def test_my_tool():
    """Test the custom tool."""
    tool = MyTool()
    result = await tool.execute(param="test")
    assert result.success
    assert "expected" in result.content
```

## Community Guidelines

Please follow our [Code of Conduct](CODE_OF_CONDUCT.md) and be friendly and respectful.

## Questions and Help

If you have any questions:

- Check the [README](README.md) and [documentation](docs/).
- Search existing Issues.
- Create a new Issue to ask a question.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

---

Thank you again for your contribution! 🎉
