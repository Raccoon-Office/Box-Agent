# Kernel and Plugin Host Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** Reorganize the current `box_agent.core` implementation into a stable kernel and startup-time plugin composition layer without changing the public Agent, CLI, ACP, runtime, event, or configuration contracts.

**Architecture:** Keep `box_agent.core` as a compatibility facade while moving behavior into cohesive `box_agent.kernel` modules. The kernel owns loop invariants and typed ports; `box_agent.plugins` owns static registration, validation, dependency resolution, activation, and disposal. Existing runtime objects are registered as caller-owned defaults so callers continue to pass the same arguments and receive the same events.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, typing Protocols, pytest, pytest-asyncio, uv.

**Spec:** `docs/superpowers/specs/2026-09-03-kernel-plugin-architecture-design.md`

## Implementation outcome

This plan has been executed on `codex/kernel-plugin-refactor`. The task bodies
below preserve the pre-implementation RED/GREEN sequence and review checkpoints;
current source, tests, and the linked design are authoritative where the final
implementation deliberately differs from an early step.

- `core.run_agent_loop` remains an async-generator compatibility facade and
  `runtime.run_agent_loop` continues through that facade so monkeypatched timing
  defaults retain their first-iteration behavior. It is intentionally not the
  same function object as the kernel entrypoint.
- The kernel receives only an immutable `KernelServices`; outer composition
  owns PluginHost activation and legacy-argument translation.
- Default descriptors capture caller-owned instances without taking disposal
  ownership. Explicit third-party descriptors retain runtime Port validation.
- The implemented lifecycle scopes are process, session, and run. Default
  captured capabilities are run-scoped.
- State extraction introduced `ToolBudgetState` and `ToolExecutionState`; other
  orchestration state remains local to `AgentLoopKernel` to avoid behavior
  changes during this reorganization.
- Full-suite runs retain environment/generated-asset baseline failures, which
  are reported separately from the focused green compatibility matrices.

## Global Constraints

- Preserve the exact `Agent.run_events`, `box_agent.runtime.run_agent_loop`, `box_agent.runtime.invoke_tool_with_permissions`, CLI, and ACP call surfaces.
- Preserve `box_agent.core` imports used by existing tests and downstream callers throughout the migration.
- Preserve event order, stop reasons, in-place message mutation, tool-call closure, permission retries, context compaction, artifact detection, and session durability behavior.
- Keep the logger namespace `box_agent.core` until a separately approved compatibility change.
- Do not introduce hot loading, entry-point discovery, a product workflow state machine, or a `WorkflowPolicy` port.
- Follow TDD for every production edit: add or strengthen a focused test, observe the expected failure, implement the smallest change, and rerun focused tests.
- Do not commit, push, merge, tag, or publish without explicit user authorization. Commit commands below are review checkpoints only.
- Treat the current baseline failures in three artifact assertions as pre-existing. Do not change those semantics as part of this refactor.

---

### Task 1: Establish the Kernel Package and Enforce Dependency Direction

**Files:**

- Create: `box_agent/kernel/__init__.py`
- Create: `tests/test_kernel_compatibility.py`
- Modify: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Add a failing kernel surface test**

```python
def test_kernel_package_exposes_loop_entrypoint() -> None:
    from box_agent.core import run_agent_loop as legacy_entrypoint
    from box_agent.kernel import run_agent_loop as kernel_entrypoint

    assert legacy_entrypoint is kernel_entrypoint
```

- [ ] **Step 2: Run the new test and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py -q`

Expected: import failure because `box_agent.kernel` does not exist yet.

- [ ] **Step 3: Create the package with a temporary compatibility export**

```python
"""Stable agent-loop kernel."""

from box_agent.core import run_agent_loop

__all__ = ["run_agent_loop"]
```

- [ ] **Step 4: Extend the architecture test to scan all stable kernel files**

Replace the fixed stable-file list with a helper that includes `core.py`, `loop_guards.py`, `runtime.py`, and every Python file below `kernel/`. Assert that none imports `box_agent.acp`, `box_agent.cli`, or contains the prohibited workflow tokens.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_architecture_boundaries.py -q`

- [ ] **Step 6: Review checkpoint**

Inspect: `git diff --check` and `git diff -- box_agent/kernel tests/test_kernel_compatibility.py tests/test_architecture_boundaries.py`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): establish compatibility boundary"`

---

### Task 2: Extract Context Management Behind `ContextEngine`

**Files:**

- Create: `box_agent/kernel/context_engine.py`
- Modify: `box_agent/core.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_core.py`
- Test: `tests/test_skill_preload.py`

- [ ] **Step 1: Add failing compatibility tests for moved context symbols**

Parameterize the legacy and new module exports for:

```python
CONTEXT_SYMBOLS = (
    "CompactionOutcome",
    "_create_summary",
    "_maybe_summarize",
    "_restore_runtime_state",
    "_select_recent_messages",
    "_estimate_context_from_latest_response",
    "_fallback_context_estimate",
    "_bound_text_middle",
)
```

Assert each symbol imported from `box_agent.core` is identical to the symbol in `box_agent.kernel.context_engine`.

- [ ] **Step 2: Run the compatibility test and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py -q`

Expected: import failure for `box_agent.kernel.context_engine`.

- [ ] **Step 3: Move the context implementation mechanically**

Move the compaction dataclass, summary constants, context estimation helpers, restoration logic, and summarization logic to `context_engine.py`. Keep signatures and constant values unchanged. Use:

```python
_log = logging.getLogger("box_agent.core")
```

Import and re-export the moved symbols from `core.py`; do not add wrappers unless monkeypatch behavior requires a late-bound value.

- [ ] **Step 4: Run focused context tests**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_core.py -q -k "summary or context or compact or restore or recent_message or bound_text or tool_message_content"`

Run: `uv run pytest tests/test_skill_preload.py -q`

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): extract context engine"`

---

### Task 3: Extract Provider Streaming Behind `StreamController`

**Files:**

- Create: `box_agent/kernel/stream_controller.py`
- Modify: `box_agent/core.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_llm_activity.py`
- Test: `tests/test_truncation_continuation.py`
- Test: `tests/test_length_retry_no_double_render.py`

- [ ] **Step 1: Add a failing test for late-bound stream defaults**

The test must prove that monkeypatching `box_agent.core.LLM_PROVIDER_STALE_SECONDS` still affects a call through the legacy `_stream_with_activity` export. This prevents a mechanical move from silently freezing the new module's constant.

- [ ] **Step 2: Run the stream tests and verify RED for the new module surface**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_llm_activity.py -q`

- [ ] **Step 3: Move stream control and retain a compatibility wrapper**

Move `_resolve_provider_stale_seconds` and the stream iterator implementation to `stream_controller.py`. Keep a small legacy wrapper in `core.py` that passes the current core constants into the controller:

```python
async def _stream_with_activity(stream, *, stale_seconds=None):
    effective_stale = (
        LLM_PROVIDER_STALE_SECONDS if stale_seconds is None else stale_seconds
    )
    async for event in stream_with_activity(
        stream,
        stale_seconds=effective_stale,
        activity_interval_seconds=LLM_ACTIVITY_INTERVAL_SECONDS,
    ):
        yield event
```

- [ ] **Step 4: Run focused stream tests**

Run: `uv run pytest tests/test_llm_activity.py tests/test_truncation_continuation.py tests/test_length_retry_no_double_render.py -q`

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): extract stream controller"`

---

### Task 4: Extract Permission Negotiation Behind `PermissionGateway`

**Files:**

- Create: `box_agent/kernel/permission_gateway.py`
- Modify: `box_agent/core.py`
- Modify: `box_agent/runtime.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_permission_negotiation.py`

- [ ] **Step 1: Add failing import and behavior tests**

Assert `_negotiate_tool_permission_chain` is exported by both the compatibility facade and `permission_gateway`, and add a runtime test proving `invoke_tool_with_permissions` still retries through the same permission sequence.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_permission_negotiation.py -q`

- [ ] **Step 3: Move permission helpers without changing payloads**

Move `_permission_event_kwargs`, `_approve_tool_permission`, `_policy_decision_payload`, and `_negotiate_tool_permission_chain`. Preserve retry limits, denial/error payloads, and `ToolResult` behavior.

Change `runtime.py` to import the implementation from `kernel.permission_gateway`; keep the legacy re-export in `core.py` for compatibility.

- [ ] **Step 4: Run focused permission and runtime tests**

Run: `uv run pytest tests/test_permission_negotiation.py tests/test_kernel_compatibility.py -q`

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): extract permission gateway"`

---

### Task 5: Unify Tool Result Post-processing

**Files:**

- Create: `box_agent/kernel/tool_result_pipeline.py`
- Modify: `box_agent/core.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_core.py`
- Test: `tests/test_tool_call_closure.py`
- Test: `tests/test_session_trace.py`

- [ ] **Step 1: Add failing tests for the result pipeline contract**

Introduce a small immutable input record and test a public kernel helper that applies a successful tool result to messages before exposing the corresponding result event. The assertion order is:

```python
assert messages[-1].role == "tool"
assert emitted_event.tool_call_id == tool_call.id
assert messages[-1].tool_call_id == tool_call.id
```

Add compatibility identity tests for existing private helpers moved into this module, including browser snapshot persistence, model-history recovery, web-search normalization/deduplication, and dangling-tool-call cleanup.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_tool_call_closure.py -q`

- [ ] **Step 3: Move pure result helpers first**

Move cohesive, behavior-preserving helper groups into `tool_result_pipeline.py`. Preserve all payload schemas and logger names. Import/re-export them from `core.py` while the main loop still calls the legacy names.

- [ ] **Step 4: Route serial and parallel completion through one pipeline**

Extract the duplicated post-invocation logic from `run_agent_loop` into one async pipeline. Both serial execution and parallel task completion must call the same function; ordering policy remains in the caller.

- [ ] **Step 5: Run focused result tests**

Run: `uv run pytest tests/test_tool_call_closure.py tests/test_session_trace.py tests/test_core.py -q -k "tool_result or tool_message or web_search or model_history or browser_snapshot or dangling"`

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): unify tool result pipeline"`

---

### Task 6: Extract Tool Scheduling and Cancellation

**Files:**

- Create: `box_agent/kernel/tool_engine.py`
- Create: `box_agent/kernel/state.py`
- Modify: `box_agent/core.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_core.py`
- Test: `tests/test_inject.py`
- Test: `tests/test_sub_agent_tool.py`

- [ ] **Step 1: Add failing state and scheduling tests**

Define and test explicit `RunState`, `ToolBudgetState`, and `ToolExecutionState` dataclasses. Add assertions for serial order, bounded parallelism, cancellation grace, timeouts, and delegated/total budget accounting using current observable events.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py tests/test_core.py -q -k "parallel or cancel or timeout or budget"`

- [ ] **Step 3: Move scheduling code into `ToolEngine`**

The engine accepts tools, limits, cancellation callback, permission gateway, and result pipeline explicitly. It returns kernel-owned execution outcomes and never imports ACP or CLI modules.

- [ ] **Step 4: Replace closure state with explicit dataclasses**

Move only the state that belongs to scheduling and per-run counters. Preserve `messages` as the caller-provided mutable list and preserve all event ordering.

- [ ] **Step 5: Run focused and related suites**

Run: `uv run pytest tests/test_core.py tests/test_inject.py tests/test_sub_agent_tool.py -q`

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): extract tool engine"`

---

### Task 7: Move the Orchestrator to `AgentLoopKernel`

**Files:**

- Create: `box_agent/kernel/loop.py`
- Modify: `box_agent/kernel/__init__.py`
- Modify: `box_agent/core.py`
- Modify: `tests/test_kernel_compatibility.py`
- Test: `tests/test_core.py`
- Test: `tests/test_hooks.py`
- Test: `tests/test_thinking.py`

- [ ] **Step 1: Add a failing facade/signature test**

Capture the legacy signature before the move and assert the facade and kernel exports match it:

```python
assert inspect.signature(core.run_agent_loop) == inspect.signature(kernel.run_agent_loop)
assert core.run_agent_loop is kernel.run_agent_loop
```

- [ ] **Step 2: Run the compatibility test and verify RED**

Run: `uv run pytest tests/test_kernel_compatibility.py -q`

- [ ] **Step 3: Introduce `AgentLoopKernel`**

Move the remaining orchestration into `kernel/loop.py` and make the async generator delegate to a kernel object:

```python
async def run_agent_loop(...):
    kernel = AgentLoopKernel(...)
    async for event in kernel.run():
        yield event
```

Keep the parameter list and defaults byte-for-byte compatible. `core.py` becomes imports, compatibility wrappers, and `__all__` declarations only.

- [ ] **Step 4: Point the production bridge at the kernel**

Change `runtime.py` to import `run_agent_loop` from `box_agent.kernel`. Do not change the wrapper signature or model-tool context behavior.

- [ ] **Step 5: Run core integration tests**

Run: `uv run pytest tests/test_core.py tests/test_hooks.py tests/test_thinking.py tests/test_kernel_compatibility.py tests/test_architecture_boundaries.py -q`

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(kernel): move agent loop orchestrator"`

---

### Task 8: Define Kernel-owned Ports and Default Adapters

**Files:**

- Create: `box_agent/kernel/ports.py`
- Create: `box_agent/plugins/__init__.py`
- Create: `box_agent/plugins/defaults.py`
- Create: `tests/test_plugin_host.py`
- Modify: `box_agent/kernel/loop.py`

- [ ] **Step 1: Add failing structural port tests**

Define runtime-checkable or statically inspectable Protocol contracts for LLM, memory, session, permission, hook, and tool capabilities. Tests instantiate adapters around the current concrete objects and verify calls and results are forwarded unchanged.

- [ ] **Step 2: Run the port tests and verify RED**

Run: `uv run pytest tests/test_plugin_host.py -q`

- [ ] **Step 3: Implement minimal Protocols**

Protocols describe only operations already consumed by the kernel. They must not expose ACP/CLI types or add lifecycle behavior to the existing concrete services.

- [ ] **Step 4: Implement default adapters**

Wrap current LLM, memory manager/extractor, SessionLog, permission negotiator, HookManager, and Tool objects. Preserve `None` semantics with explicit null adapters where the loop currently treats a capability as optional.

- [ ] **Step 5: Compose ports from existing run arguments**

The unchanged `run_agent_loop` arguments are converted to a per-run port bundle internally. No new caller argument or configuration key is introduced.

- [ ] **Step 6: Run focused integration tests**

Run: `uv run pytest tests/test_plugin_host.py tests/test_core.py tests/test_permission_negotiation.py tests/test_hooks.py -q`

- [ ] **Step 7: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "refactor(plugins): add kernel ports and default adapters"`

---

### Task 9: Add Typed Registries and a Static Plugin Host

**Files:**

- Create: `box_agent/plugins/descriptors.py`
- Create: `box_agent/plugins/registries.py`
- Create: `box_agent/plugins/host.py`
- Modify: `box_agent/plugins/__init__.py`
- Modify: `box_agent/plugins/defaults.py`
- Modify: `tests/test_plugin_host.py`
- Modify: `box_agent/kernel/loop.py`

- [ ] **Step 1: Add failing lifecycle and validation tests**

Cover deterministic registration order, duplicate IDs, missing required capabilities, dependency cycles, activation rollback, reverse-order disposal, singleton/session/run scopes, and exact type lookup. Add a test proving there is no entry-point scan or runtime hot-reload path.

- [ ] **Step 2: Run the host tests and verify RED**

Run: `uv run pytest tests/test_plugin_host.py -q`

- [ ] **Step 3: Implement descriptors and typed registries**

Use immutable descriptors and capability-specific registries. Reject ambiguous registrations during validation rather than silently choosing one.

- [ ] **Step 4: Implement the lifecycle**

Implement `discover -> validate -> resolve_dependencies -> activate -> dispose`. Discovery consumes an explicit descriptor collection supplied by `defaults.py`; it does not scan packages or import paths.

- [ ] **Step 5: Integrate static default composition**

Build the default host from the existing runtime objects at startup/per-run composition time. Pass a resolved immutable port bundle to `AgentLoopKernel`; the kernel must not query the host as a service locator.

- [ ] **Step 6: Run focused plugin and kernel tests**

Run: `uv run pytest tests/test_plugin_host.py tests/test_kernel_compatibility.py tests/test_architecture_boundaries.py tests/test_core.py -q`

- [ ] **Step 7: Review checkpoint**

Run: `git diff --check`

Commit only if explicitly authorized: `git commit -m "feat(plugins): add static plugin host"`

---

### Task 10: Prove CLI and ACP Compatibility

**Files:**

- Modify: `tests/test_kernel_compatibility.py`
- Modify: relevant ACP tests under `tests/`
- Modify: relevant CLI tests under `tests/`
- Modify: `docs/ARCHITECTURE.md` or the nearest existing architecture document

- [ ] **Step 1: Add adapter contract tests**

Assert `Agent.run_events` still calls the runtime bridge with the same options, ACP continues to use `invoke_tool_with_permissions`, CLI/ACP receive the same ordered event types for a deterministic fake LLM/tool run, and stdout remains protocol-clean.

- [ ] **Step 2: Run focused adapter tests and verify they catch an intentional local mismatch**

Use a temporary monkeypatch in the test or a deliberately incomplete expected trace, observe failure, then restore the correct expectation before production edits.

- [ ] **Step 3: Update architecture documentation**

Document the compatibility facade, kernel dependency direction, port ownership, static plugin lifecycle, scope model, and replacement procedure. Do not claim dynamic discovery support.

- [ ] **Step 4: Run broad verification**

Run: `uv run pytest tests/test_architecture_boundaries.py tests/test_kernel_compatibility.py tests/test_plugin_host.py -q`

Run: `uv run pytest tests/ -q`

Run: `git diff --check`

Expected: all new and affected tests pass. If the three known artifact assertions still fail unchanged, report them separately with the focused green results and compare against the recorded baseline.

- [ ] **Step 5: Verify source/runtime boundary**

Run: `uv run python -m box_agent.cli --help`

If no packaged runtime install was requested, report status as source changed and source tests run; mark build/install/probe/restart/live-task as not performed.

- [ ] **Step 6: Final review checkpoint**

Inspect: `git status --short --branch`, `git diff --stat`, and the complete intended diff.

Commit only if explicitly authorized: `git commit -m "refactor(core): complete kernel plugin composition"`
