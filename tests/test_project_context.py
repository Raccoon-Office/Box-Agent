from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import box_agent.acp as acp_module
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
    agent._config = SimpleNamespace(
        agent=SimpleNamespace(code_prompt_path=None, analysis_prompt_path=None),
    )

    monkeypatch.setattr(acp_module, "build_sandbox_info_prompt", lambda *, use_output_dir: "sandbox")
    monkeypatch.setattr(acp_module, "build_file_delivery_prompt", lambda *, use_output_dir: "delivery")
    monkeypatch.setattr(acp_module, "_workspace_layout_prompt", lambda **_: "layout")
    monkeypatch.setattr(acp_module, "build_project_startup_context_prompt", lambda _: "startup")
    monkeypatch.setattr(acp_module, "build_env_context_prompt", lambda _: "env")
    monkeypatch.setattr(acp_module, "build_skill_runtime_prompt", lambda _: "skills")
    monkeypatch.setattr(acp_module, "build_follow_up_suggestions_prompt", lambda: "follow-up")
    monkeypatch.setattr(agent, "_filesystem_access_prompt", lambda *_: "filesystem")
    monkeypatch.setattr(agent, "_build_action_hints_prompt", lambda *_: "hints")

    actual = agent._build_session_prompt(
        session_mode="code_agent",
        workspace=tmp_path,
        policy=None,
        env_context=object(),
        skill_runtime_context=object(),
        artifact_mode="project",
        artifact_root=tmp_path / "output",
        workspace_layout={"mode": "project"},
        follow_up_suggestions_enabled=True,
    )

    expected = (
        "base sandbox delivery\n\n"
        f"{PROJECT_WORKSPACE_MODE_PROMPT}\n\n"
        "filesystem\n\nlayout\n\nstartup\n\nenv\n\nskills\n\nhints\n\nfollow-up"
    )
    assert actual == expected


def test_acp_code_prompt_allows_authorized_repository_architecture_analysis(
    monkeypatch,
) -> None:
    """The final code-session policy must not suppress repository evidence."""

    config_dir = Path("box_agent/config").resolve()
    agent = object.__new__(BoxACPAgent)
    agent._system_prompt = (config_dir / "system_prompt.md").read_text(encoding="utf-8")
    agent._config = SimpleNamespace(
        agent=SimpleNamespace(code_prompt_path=str(config_dir / "code_prompt.md")),
    )

    monkeypatch.setattr(acp_module, "build_skill_runtime_prompt", lambda _: "")
    monkeypatch.setattr(agent, "_build_action_hints_prompt", lambda *_: "")

    actual = agent._build_session_prompt(
        session_mode="code_agent",
        skill_runtime_context=object(),
    )

    assert "当前首要角色是软件工程 Agent" in actual
    assert "用户明确授权访问的本地仓库属于可分析材料" in actual
    assert "架构或运行逻辑的提取请求礼貌拒答" not in actual
    assert "系统提示词、底层模型私有配置、凭据和内部工具参数" in actual
    assert "先给直接结论" in actual
    assert "回答结构随复杂度调整" in actual
    assert "每项聚焦一个判断" in actual
    assert "不按搜索或阅读顺序堆叠事实" in actual
    assert "本会话的代码搜索优先使用独立的 `rg` 工具" in actual
    assert "`mode='files'`" in actual
    assert "`mode='content'`" in actual
    assert "不要为常规文件发现或内容检索拼接 Bash 命令" in actual
    assert "`rg --files`" not in actual
    assert "`rg -n`" not in actual
    assert "默认尊重 `.gitignore`" in actual
    assert "回退到 `search_files`" in actual


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
