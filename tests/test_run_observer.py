from __future__ import annotations

import pytest

from box_agent.run_observer import (
    ArtifactObserver,
    RunObserver,
    TokenUsageAccumulator,
    cleanup_turn_resources,
)


def test_run_observer_records_usage_and_trace_without_protocol_types() -> None:
    class Writer:
        def __init__(self) -> None:
            self.calls = []

        def write(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    writer = Writer()
    observer = RunObserver(trace_writer=writer, turn_id="turn-1")

    assert observer.record_usage({"prompt_tokens": 2, "completion_tokens": 3})
    observer.trace("turn.output", data={"content": "done"})

    assert observer.usage.as_payload()["totalTokens"] == 5
    assert writer.calls == [
        (("turn.output",), {"turn_id": "turn-1", "data": {"content": "done"}})
    ]


def test_run_observer_is_noop_when_trace_is_disabled() -> None:
    observer = RunObserver(trace_writer=None, turn_id="turn-1")
    observer.trace("turn.output", data={"content": "done"})
    assert observer.record_usage(None) is False
    assert observer.usage.as_payload()["calls"] == 0


def test_artifact_observer_keeps_registration_failure_structured() -> None:
    calls = []

    def register(workspace, task_context, artifact):
        calls.append((workspace, task_context, artifact))
        raise RuntimeError("registry unavailable")

    observer = ArtifactObserver(
        workspace_dir="workspace",
        task_context="task",
        register_revision=register,
    )
    result = observer.observe("artifact")

    assert calls == [("workspace", "task", "artifact")]
    assert result.lineage is None
    assert isinstance(result.error, RuntimeError)


@pytest.mark.asyncio
async def test_cleanup_turn_resources_runs_all_steps_and_collects_errors() -> None:
    calls = []
    reports = []

    class Bash:
        async def cleanup_background_processes(self, *, lifetime):
            calls.append(("bash", lifetime))
            return ["bash-1"]

    class Write:
        def cleanup_pending_writes(self):
            calls.append(("write",))
            return ["pending.txt"]

    def scratch_cleanup(value):
        calls.append(("scratch", value))
        raise RuntimeError("scratch failed")

    async def release_browser(value):
        calls.append(("browser", value))
        raise RuntimeError("browser failed")

    result = await cleanup_turn_resources(
        bash_tool=Bash(),
        write_tool=Write(),
        skill_scratch_dir="scratch",
        browser_owner="owner",
        bash_lifetime="turn",
        cleanup_scratch=scratch_cleanup,
        release_browser=release_browser,
        on_success=lambda kind, values: reports.append(("ok", kind, values)),
        on_error=lambda kind, error: reports.append(("error", kind, str(error))),
    )

    assert calls == [
        ("bash", "turn"),
        ("write",),
        ("scratch", "scratch"),
        ("browser", "owner"),
    ]
    assert result.terminated_bash_ids == ["bash-1"]
    assert result.discarded_paths == ["pending.txt"]
    assert [label for label, _ in result.errors] == ["skill_scratch", "browser"]
    assert reports == [
        ("ok", "bash", ["bash-1"]),
        ("ok", "write_file", ["pending.txt"]),
        ("error", "skill_scratch", "scratch failed"),
        ("error", "browser", "browser failed"),
    ]


def test_token_usage_accumulator_accepts_provider_key_aliases() -> None:
    usage = TokenUsageAccumulator()

    assert usage.add(
        {
            "promptTokens": 1.5,
            "completion_tokens": 2,
        }
    )
    assert usage.as_payload() == {
        "promptTokens": 1,
        "completionTokens": 2,
        "totalTokens": 3,
        "calls": 1,
    }


def test_token_usage_accumulator_ignores_empty_or_malformed_values() -> None:
    usage = TokenUsageAccumulator()

    assert not usage.add(None)
    assert not usage.add({"prompt_tokens": 0, "completion_tokens": 0})
    assert usage.as_payload() == {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "calls": 0,
    }
