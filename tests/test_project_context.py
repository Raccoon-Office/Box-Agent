from __future__ import annotations

from types import SimpleNamespace

import box_agent.session_assembly as session_assembly
from box_agent.acp import BoxACPAgent
from box_agent.acp.project_context import build_project_startup_context_prompt as acp_project_prompt
from box_agent.project_context import (
    PROJECT_WORKSPACE_MODE_PROMPT,
    append_prompt_segment,
    build_project_startup_context_prompt,
    compose_prompt_segments,
    replace_prompt_placeholders,
)


def test_prompt_composer_matches_cli_and_acp_common_snapshot() -> None:
    """Lock the shared replacement/segment order before further extraction."""

    base = "system {SANDBOX_INFO} {FILE_DELIVERY_INFO}"
    expected = (
        "system sandbox output delivery\n\n"
        "## Project Workspace Mode\n"
        "- Treat files as the deliverable.\n\n"
        "## Project Startup Context\n"
        "workspace=/tmp/project\n\n"
        "## Environment\n"
        "shell=demo\n\n"
        "## Skills\n"
        "skill=demo\n\n"
        "## Memory\n"
        "fact=demo"
    )

    actual = compose_prompt_segments(
        base,
        replacements={
            "{SANDBOX_INFO}": "sandbox output",
            "{FILE_DELIVERY_INFO}": "delivery",
        },
        segments=(
            "## Project Workspace Mode\n- Treat files as the deliverable.",
            "## Project Startup Context\nworkspace=/tmp/project",
            None,
            "## Environment\nshell=demo",
            "## Skills\nskill=demo",
            "## Memory\nfact=demo",
        ),
    )

    assert actual == expected


def test_prompt_composer_preserves_empty_file_backed_overlay_snapshot() -> None:
    assert append_prompt_segment("base", "", skip_empty=False) == "base\n\n"


def test_prompt_composer_keeps_host_specific_optional_overlays_out_of_common_path() -> None:
    """ACP expert/filesystem hints are caller segments, not hidden mode branches."""

    assert compose_prompt_segments(
        "base",
        segments=("filesystem", "layout", "expert", None),
    ) == "base\n\nfilesystem\n\nlayout\n\nexpert"


def test_acp_session_prompt_builder_preserves_overlay_order(monkeypatch, tmp_path) -> None:
    """Exercise the real ACP builder so adapter omissions/reordering are visible."""

    agent = object.__new__(BoxACPAgent)
    agent._system_prompt = "base {SANDBOX_INFO} {FILE_DELIVERY_INFO}"
    agent._memory = None
    agent._config = SimpleNamespace(
        agent=SimpleNamespace(code_prompt_path=None, analysis_prompt_path=None),
    )

    monkeypatch.setattr(session_assembly, "build_sandbox_info_prompt", lambda: "sandbox")
    monkeypatch.setattr(session_assembly, "build_file_delivery_prompt", lambda *_: "delivery")
    monkeypatch.setattr(session_assembly, "_workspace_layout_prompt", lambda **_: "layout")
    monkeypatch.setattr(session_assembly, "build_project_startup_context_prompt", lambda _: "startup")
    monkeypatch.setattr(session_assembly, "build_env_context_prompt", lambda _: "env")
    monkeypatch.setattr(session_assembly, "build_skill_runtime_prompt", lambda _: "skills")
    monkeypatch.setattr(session_assembly, "build_follow_up_suggestions_prompt", lambda: "follow-up")
    monkeypatch.setattr(session_assembly, "_filesystem_access_prompt", lambda *_: "filesystem")
    monkeypatch.setattr(session_assembly, "_build_action_hints_prompt", lambda *_: "hints")

    actual = agent._build_session_prompt(
        session_mode="code_agent",
        workspace=tmp_path,
        policy=None,
        env_context=object(),
        skill_runtime_context=object(),
        workspace_layout={"mode": "project"},
        follow_up_suggestions_enabled=True,
    )

    expected = (
        "base sandbox delivery\n\n"
        f"{PROJECT_WORKSPACE_MODE_PROMPT}\n\n"
        "filesystem\n\nlayout\n\nstartup\n\nenv\n\nskills\n\nhints\n\nfollow-up"
    )
    assert actual == expected


def test_neutral_project_context_preserves_acp_compatibility(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# local instructions\n", encoding="utf-8")

    assert build_project_startup_context_prompt(tmp_path) == acp_project_prompt(tmp_path)


def test_append_prompt_segment_preserves_existing_spacing() -> None:
    assert append_prompt_segment("base\n", "segment") == "base\n\nsegment"
    assert append_prompt_segment("base", "") == "base"
    assert append_prompt_segment("base", None) == "base"
    assert append_prompt_segment("base", "", skip_empty=False) == "base\n\n"


def test_replace_prompt_placeholders_applies_all_replacements_in_order() -> None:
    assert replace_prompt_placeholders(
        "{A}\n{B}",
        {"{A}": "one", "{B}": "two"},
    ) == "one\ntwo"


def test_compose_prompt_segments_preserves_exact_order_and_spacing() -> None:
    assert compose_prompt_segments(
        "base {FIRST} {SECOND}\n",
        replacements={"{FIRST}": "one", "{SECOND}": "two"},
        segments=("segment-a", None, "", "segment-b"),
    ) == "base one two\n\nsegment-a\n\nsegment-b"


def test_project_workspace_mode_prompt_preserves_adapter_text() -> None:
    assert PROJECT_WORKSPACE_MODE_PROMPT == (
        "## Project Workspace Mode\n"
        "- This session is editing an existing code/project workspace.\n"
        "- Do not create or use an `output/` folder unless the user explicitly asks for one.\n"
        "- Treat file edits, generated source files, tests, and build results in the project tree as the deliverable."
    )


def _fake_git(monkeypatch, responses):
    """Route project_context's git calls through ``responses[args[0]]``.

    A response is either a ``(returncode, stdout)`` tuple or an exception
    instance to raise (e.g. ``subprocess.TimeoutExpired``).
    """
    import subprocess

    import box_agent.project_context as project_context

    def fake_run(cmd, **_kwargs):
        response = responses[cmd[1]]
        if isinstance(response, BaseException):
            raise response
        returncode, stdout = response
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(project_context.subprocess, "run", fake_run)


def test_git_status_timeout_is_reported_unknown_not_clean(monkeypatch, tmp_path) -> None:
    import subprocess

    _fake_git(monkeypatch, {
        "rev-parse": (0, str(tmp_path)),
        "branch": (0, "main"),
        "status": subprocess.TimeoutExpired(["git", "status"], 1.5),
    })

    prompt = build_project_startup_context_prompt(tmp_path)

    assert "- Git repository: yes" in prompt
    assert "- Status: unknown" in prompt
    assert "Status: clean" not in prompt


def test_git_status_failure_is_reported_unknown_not_clean(monkeypatch, tmp_path) -> None:
    _fake_git(monkeypatch, {
        "rev-parse": (0, str(tmp_path)),
        "branch": (0, "main"),
        "status": (128, ""),
    })

    prompt = build_project_startup_context_prompt(tmp_path)

    assert "- Status: unknown" in prompt
    assert "Status: clean" not in prompt


def test_git_rev_parse_timeout_is_not_reported_as_non_repository(monkeypatch, tmp_path) -> None:
    import subprocess

    _fake_git(monkeypatch, {
        "rev-parse": subprocess.TimeoutExpired(["git", "rev-parse"], 1.5),
    })

    prompt = build_project_startup_context_prompt(tmp_path)

    assert "- Git repository: unknown" in prompt
    assert "no or unavailable" not in prompt


def test_git_answers_keep_existing_wording(monkeypatch, tmp_path) -> None:
    _fake_git(monkeypatch, {"rev-parse": (128, "")})
    assert "- Git repository: no or unavailable from this workspace." in (
        build_project_startup_context_prompt(tmp_path)
    )

    _fake_git(monkeypatch, {
        "rev-parse": (0, str(tmp_path)),
        "branch": (0, "main"),
        "status": (0, ""),
    })
    assert "- Status: clean" in build_project_startup_context_prompt(tmp_path)


def test_acp_run_git_keeps_none_on_timeout_contract(monkeypatch, tmp_path) -> None:
    import subprocess

    from box_agent.acp.project_context import _run_git

    _fake_git(monkeypatch, {"status": subprocess.TimeoutExpired(["git", "status"], 1.5)})
    assert _run_git(tmp_path, ["status", "--short"]) is None


def test_acp_session_prompt_renders_current_date_per_session(monkeypatch) -> None:
    """The ACP server keeps the raw template; each session renders today's date."""
    import datetime as dt

    import box_agent.tools.setup as setup

    today = {"value": dt.date(2026, 10, 9)}

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return today["value"]

    monkeypatch.setattr(setup, "date", FakeDate)
    monkeypatch.setattr(session_assembly, "build_sandbox_info_prompt", lambda: "sandbox")
    monkeypatch.setattr(session_assembly, "build_file_delivery_prompt", lambda *_: "delivery")
    monkeypatch.setattr(session_assembly, "build_env_context_prompt", lambda _: "")
    monkeypatch.setattr(session_assembly, "build_skill_runtime_prompt", lambda _: "")
    monkeypatch.setattr(session_assembly, "_build_action_hints_prompt", lambda *_: "")
    config = SimpleNamespace(agent=SimpleNamespace(code_prompt_path=None, analysis_prompt_path=None))
    template = "今天日期：`{{.CurrentDate}}`"

    def build() -> str:
        return session_assembly.build_acp_session_prompt(config, template, None, None)

    assert "今天日期：`2026-10-09`" in build()
    today["value"] = dt.date(2026, 10, 10)
    assert "今天日期：`2026-10-10`" in build()


def test_bash_description_does_not_model_unrequested_git_writes() -> None:
    from box_agent.tools.bash_tool import BashTool

    for is_windows in (True, False):
        tool = BashTool.__new__(BashTool)
        tool.default_timeout_seconds = 120
        tool.max_timeout_seconds = 600
        tool.is_windows = is_windows
        tool._bundled_win_bash = None
        description = tool.description

        assert ("PowerShell" in description) is is_windows
        assert "git commit" not in description
        assert "Do not commit, push" in description


def test_code_prompt_states_git_safety_rules() -> None:
    from pathlib import Path

    code_prompt = Path("box_agent/config/code_prompt.md").read_text(encoding="utf-8")

    assert "不 commit、push" in code_prompt
    assert "`reset --hard`" in code_prompt
    assert "未提交的改动" in code_prompt
