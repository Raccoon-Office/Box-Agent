from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import box_agent.cli as cli
from box_agent.config import ToolLimitsConfig
from box_agent.workspace_registry import WorkspaceRegistry


def _write_config(path: Path, api_key: str = "sk-test-key") -> None:
    path.write_text(
        "\n".join(
            [
                f'api_key: "{api_key}"',
                'api_base: "https://api.openai.com/v1"',
                'model: "gpt-4o"',
                'provider: "openai"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_cmd_config_get_reads_expanded_config_defaults(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: config_path)

    exit_code = cli.cmd_config(get_key="llm.max_output_tokens")

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "63999"


def test_cmd_config_set_bootstraps_and_updates_raw_yaml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, api_key="YOUR_API_KEY_HERE")
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: None)
    monkeypatch.setattr(cli.Config, "_ensure_user_config", lambda: config_path)

    exit_code = cli.cmd_config(set_pair=("api_key", "sk-new-key"))

    assert exit_code == 0
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["api_key"] == "sk-new-key"


def test_cmd_config_set_updates_nested_tool_limit(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: config_path)

    exit_code = cli.cmd_config(
        set_pair=("tool_limits.external_skill.max_tool_calls", "96")
    )

    assert exit_code == 0
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["tool_limits"]["external_skill"]["max_tool_calls"] == 96
    assert (
        cli.Config.from_yaml(config_path).tool_limits.external_skill.max_tool_calls
        == 96
    )


def test_cmd_config_set_rolls_back_invalid_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: config_path)

    exit_code = cli.cmd_config(set_pair=("context_window", "not-an-int"))

    assert exit_code == 1
    assert config_path.read_text(encoding="utf-8") == original


def test_cmd_config_json_masks_secret_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, api_key="sk-secret-token")
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: config_path)

    exit_code = cli.cmd_config(json_output=True)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["llm"]["api_key"] == "sk-s****oken"


def test_config_parses_goal_autopilot_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write("goal_autopilot_enabled: false\n")
        f.write("goal_autopilot_max_turns: 5\n")
        f.write("goal_autopilot_max_seconds: 120\n")
        f.write("goal_autopilot_no_progress_turns: 4\n")

    config = cli.Config.from_yaml(config_path)

    assert config.agent.goal_autopilot_enabled is False
    assert config.agent.goal_autopilot_max_turns == 5
    assert config.agent.goal_autopilot_max_seconds == 120
    assert config.agent.goal_autopilot_no_progress_turns == 4


def test_config_sub_agent_token_limit_defaults_and_overrides(tmp_path: Path) -> None:
    # Default when absent from yaml.
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    defaults = cli.Config.from_yaml(default_path).agent
    assert defaults.max_steps == 300
    assert defaults.sub_agent_token_limit == 50_000

    # Overridable for advanced/host scenarios; config-example.yaml documents it
    # only as a comment so generated configs continue to inherit runtime updates.
    override_path = tmp_path / "override.yaml"
    _write_config(override_path)
    with override_path.open("a", encoding="utf-8") as f:
        f.write("sub_agent_token_limit: 12345\n")

    assert cli.Config.from_yaml(override_path).agent.sub_agent_token_limit == 12345


def test_config_batch_synthesis_timeout_defaults_and_overrides(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    assert (
        cli.Config.from_yaml(default_path).agent.sub_agent_batch_synthesis_timeout_seconds
        == 600.0
    )

    override_path = tmp_path / "override.yaml"
    _write_config(override_path)
    with override_path.open("a", encoding="utf-8") as f:
        f.write("sub_agent_batch_synthesis_timeout_seconds: 123.5\n")

    assert (
        cli.Config.from_yaml(override_path).agent.sub_agent_batch_synthesis_timeout_seconds
        == 123.5
    )


def test_config_tool_limits_defaults_and_nested_overrides(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    defaults = cli.Config.from_yaml(default_path).tool_limits
    assert defaults.web_search.total_calls == 50
    assert defaults.external_skill.max_tool_calls == 128
    assert defaults.presentation.deep_research_max_tool_calls == 200
    assert defaults.sub_agent.general_max_tool_calls == 32

    override_path = tmp_path / "override.yaml"
    _write_config(override_path)
    with override_path.open("a", encoding="utf-8") as f:
        f.write(
            "tool_limits:\n"
            "  web_search:\n"
            "    batch_size: 4\n"
            "    total_calls: 40\n"
            "  external_skill:\n"
            "    max_tool_calls: 96\n"
            "  presentation:\n"
            "    research_rounds: 5\n"
            "  sub_agent:\n"
            "    general_max_steps: 20\n"
            "    legacy_max_steps: 60\n"
        )

    limits = cli.Config.from_yaml(override_path).tool_limits
    assert limits.web_search.batch_size == 4
    assert limits.web_search.total_calls == 40
    assert limits.web_search.deep_research_total_calls == 100
    assert limits.external_skill.max_tool_calls == 96
    assert limits.external_skill.completion_reserve_calls == 10
    assert limits.presentation.research_rounds == 5
    assert limits.sub_agent.general_max_steps == 20
    assert limits.sub_agent.legacy_max_steps == 60


def test_config_example_does_not_pin_tool_limit_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    example_path = cli.Config.get_package_dir() / "config" / "config-example.yaml"
    data = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert "tool_limits" not in data
    assert "max_steps" not in data
    assert "sub_agent_token_limit" not in data
    assert "sub_agent_batch_synthesis_timeout_seconds" not in data

    monkeypatch.setenv("HOME", str(tmp_path))
    generated_path = cli.Config._ensure_user_config()
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    assert "tool_limits" not in generated
    assert "max_steps" not in generated
    assert "sub_agent_token_limit" not in generated
    assert "sub_agent_batch_synthesis_timeout_seconds" not in generated


def test_config_rejects_invalid_tool_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.yaml"
    _write_config(config_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write("tool_limits:\n  web_search:\n    batch_size: 0\n")

    with pytest.raises(ValueError):
        cli.Config.from_yaml(config_path)


@pytest.mark.parametrize(
    ("path", "maximum"),
    [
        (("general", "final_summary_after_calls"), 512),
        (("general", "wrapup_remaining_steps"), 50),
        (("web_search", "batch_size"), 32),
        (("web_search", "total_calls"), 512),
        (("web_search", "deep_research_total_calls"), 512),
        (("search_files", "consecutive_empty_limit"), 50),
        (("external_skill", "max_tool_calls"), 512),
        (("external_skill", "completion_reserve_calls"), 128),
        (("presentation", "max_tool_calls"), 512),
        (("presentation", "completion_reserve_calls"), 128),
        (("presentation", "deep_research_max_tool_calls"), 512),
        (("presentation", "research_rounds"), 20),
        (("sub_agent", "general_max_steps"), 256),
        (("sub_agent", "general_max_tool_calls"), 256),
        (("sub_agent", "legacy_max_steps"), 256),
        (("sub_agent", "no_progress_steps"), 50),
    ],
)
def test_tool_limit_upper_bounds(path: tuple[str, str], maximum: int) -> None:
    section, field = path
    section_values = {field: maximum}
    if field == "completion_reserve_calls":
        section_values["max_tool_calls"] = 512
        if section == "presentation":
            section_values["deep_research_max_tool_calls"] = 512

    accepted = ToolLimitsConfig(**{section: section_values})
    assert getattr(getattr(accepted, section), field) == maximum

    section_values[field] = maximum + 1
    with pytest.raises(ValueError):
        ToolLimitsConfig(**{section: section_values})


def test_cmd_config_set_rolls_back_tool_limit_above_maximum(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(cli.Config, "find_config_file", lambda _name: config_path)

    exit_code = cli.cmd_config(
        set_pair=("tool_limits.external_skill.max_tool_calls", "513")
    )

    assert exit_code == 1
    assert config_path.read_text(encoding="utf-8") == original


def test_config_mcp_connect_timeout_defaults_and_overrides(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    assert cli.Config.from_yaml(default_path).tools.mcp.connect_timeout == 60.0

    override_path = tmp_path / "override.yaml"
    _write_config(override_path)
    with override_path.open("a", encoding="utf-8") as f:
        f.write("tools:\n")
        f.write("  mcp:\n")
        f.write("    connect_timeout: 15\n")
    assert cli.Config.from_yaml(override_path).tools.mcp.connect_timeout == 15.0


def test_enable_playwright_migrates_managed_timeout_defaults(tmp_path: Path) -> None:
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": [
                            "-y",
                            "@playwright/mcp@latest",
                            "--timeout-navigation",
                            "60000",
                        ],
                        "disabled": True,
                        "execute_timeout": 60,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cli._enable_playwright_in_mcp(mcp_path)

    playwright = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"][
        "playwright"
    ]
    assert playwright["disabled"] is False
    assert playwright["execute_timeout"] == 45.0
    assert playwright["args"][-2:] == ["--timeout-navigation", "30000"]
    assert playwright["args"].count("--timeout-navigation") == 1


def test_enable_playwright_preserves_custom_execute_timeout(tmp_path: Path) -> None:
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp@latest"],
                        "execute_timeout": 90,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cli._enable_playwright_in_mcp(mcp_path)

    playwright = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"][
        "playwright"
    ]
    assert playwright["execute_timeout"] == 90
    assert playwright["args"][-2:] == ["--timeout-navigation", "30000"]


def test_config_parallel_tool_timeout_defaults_and_overrides(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    assert cli.Config.from_yaml(default_path).agent.parallel_tool_timeout_seconds == 900.0

    override_path = tmp_path / "override.yaml"
    _write_config(override_path)
    with override_path.open("a", encoding="utf-8") as f:
        f.write("parallel_tool_timeout_seconds: 12.5\n")

    assert cli.Config.from_yaml(override_path).agent.parallel_tool_timeout_seconds == 12.5

    disabled_path = tmp_path / "disabled.yaml"
    _write_config(disabled_path)
    with disabled_path.open("a", encoding="utf-8") as f:
        f.write("parallel_tool_timeout_seconds: 0\n")

    assert cli.Config.from_yaml(disabled_path).agent.parallel_tool_timeout_seconds == 0


def test_context_resource_dedup_defaults_on_and_can_be_disabled(tmp_path: Path) -> None:
    default_path = tmp_path / "default.yaml"
    _write_config(default_path)
    assert cli.Config.from_yaml(default_path).agent.context_resource_dedup_enabled is True

    disabled_path = tmp_path / "disabled.yaml"
    _write_config(disabled_path)
    with disabled_path.open("a", encoding="utf-8") as stream:
        stream.write("context_resource_dedup_enabled: false\n")

    assert cli.Config.from_yaml(disabled_path).agent.context_resource_dedup_enabled is False


def test_cmd_doctor_json_returns_structured_status(monkeypatch, capsys) -> None:
    async def fake_api_status(_config):
        return cli._doctor_check("ok", "api ok")

    monkeypatch.setattr(cli, "_doctor_config_status", lambda: (cli._doctor_check("ok", "config ok"), object()))
    monkeypatch.setattr(cli, "_doctor_api_status", fake_api_status)
    monkeypatch.setattr(cli, "_doctor_sandbox_status", lambda: cli._doctor_check("ok", "sandbox ok"))
    monkeypatch.setattr(cli, "_doctor_mcp_status", lambda: cli._doctor_check("warning", "mcp missing"))
    monkeypatch.setattr(cli, "_doctor_browser_status", lambda: cli._doctor_check("warning", "browser missing"))
    monkeypatch.setattr(cli, "_doctor_obsidian_status", lambda: cli._doctor_check("warning", "obsidian missing"))

    exit_code = asyncio.run(cli.cmd_doctor(json_output=True))

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"]["api"]["message"] == "api ok"


def test_cli_api_probe_is_classified_as_utility() -> None:
    class CapturingClient:
        def __init__(self) -> None:
            self.kwargs = None

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(content="ok")

    client = CapturingClient()
    response = asyncio.run(cli._probe_llm_api(client))

    assert response.content == "ok"
    assert client.kwargs["call_kind"] == "utility"
    assert client.kwargs["messages"][0].role == "user"
    assert client.kwargs["messages"][0].content == "hi"


def test_cmd_doctor_json_returns_nonzero_on_error(monkeypatch, capsys) -> None:
    async def fake_api_status(_config):
        return cli._doctor_check("skipped", "no config")

    monkeypatch.setattr(cli, "_doctor_config_status", lambda: (cli._doctor_check("error", "missing"), None))
    monkeypatch.setattr(cli, "_doctor_api_status", fake_api_status)
    monkeypatch.setattr(cli, "_doctor_sandbox_status", lambda: cli._doctor_check("ok", "sandbox ok"))
    monkeypatch.setattr(cli, "_doctor_mcp_status", lambda: cli._doctor_check("warning", "mcp missing"))
    monkeypatch.setattr(cli, "_doctor_browser_status", lambda: cli._doctor_check("warning", "browser missing"))
    monkeypatch.setattr(cli, "_doctor_obsidian_status", lambda: cli._doctor_check("warning", "obsidian missing"))

    exit_code = asyncio.run(cli.cmd_doctor(json_output=True))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"]["config"]["status"] == "error"


def test_main_returns_run_agent_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    async def fake_run_agent(*args, **kwargs):
        assert args[0] == tmp_path
        assert kwargs["task"] == "do work"
        assert kwargs["verify_api"] is False
        assert kwargs["json_summary"] is True
        assert kwargs["deep_think"] is True
        assert kwargs["force_plan_start"] is True
        assert kwargs["completion_gate_enabled"] is False
        assert kwargs["goal_autopilot_enabled"] is False
        assert kwargs["initial_goal"] == "ship goal"
        return 7

    monkeypatch.setattr(cli, "parse_args", lambda: argparse.Namespace(
        command=None,
        workspace=str(tmp_path),
        task="do work",
        goal="ship goal",
        json=True,
        no_verify_api=True,
        deep_think=True,
        force_plan_start=True,
        no_completion_gate=True,
        no_goal_autopilot=True,
        no_sandbox=False,
    ))
    monkeypatch.setattr(cli.Config, "_ensure_user_config", lambda: config_path)
    monkeypatch.setattr(
        cli.Config,
        "from_yaml",
        lambda _path: SimpleNamespace(llm=SimpleNamespace(api_key="sk-test-key")),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    assert cli.main() == 7


def test_main_persists_code_workspace_type_without_creating_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    async def fake_run_agent(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        cli,
        "parse_args",
        lambda: argparse.Namespace(
            command=None,
            workspace=str(workspace),
            workspace_type="code",
            task="inspect code",
            goal=None,
            json=False,
            no_verify_api=True,
            deep_think=False,
            force_plan_start=False,
            no_completion_gate=True,
            no_goal_autopilot=True,
            no_sandbox=False,
        ),
    )
    monkeypatch.setattr(cli.Config, "_ensure_user_config", lambda: config_path)
    monkeypatch.setattr(
        cli.Config,
        "from_yaml",
        lambda _path: SimpleNamespace(llm=SimpleNamespace(api_key="sk-test-key")),
    )
    monkeypatch.setattr(cli, "run_agent", fake_run_agent)

    assert cli.main() == 0
    assert WorkspaceRegistry().get(workspace).task_type == "code"
    assert not (workspace / "output").exists()


def test_cmd_goal_persists_workspace_goal(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert cli.cmd_goal(workspace, action="set", text=["Ship", "goal"], json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["goal"]["objective"] == "Ship goal"
    assert first["goal"]["status"] == "active"

    assert cli.cmd_goal(
        workspace,
        action="complete",
        evidence=["uv run pytest tests/ -q passed"],
        json_output=True,
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["goal"]["status"] == "complete"
    assert second["goal"]["evidence"] == ["uv run pytest tests/ -q passed"]
    assert second["goal"]["completedBy"] == "cli"

    stored = cli._load_goal_state(workspace)
    assert stored is not None
    assert stored.status == "complete"
    assert stored.evidence == ["uv run pytest tests/ -q passed"]
