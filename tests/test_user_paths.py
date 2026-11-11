"""Opt-in profile routing; tests never change the process HOME variable."""

from pathlib import Path

import pytest

from box_agent.config import AgentConfig, Config
from box_agent.user_paths import browser_cache_path, box_agent_home, configured_box_agent_home, state_path


def write_config(path: Path, **extra) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "api_key": "fixture-key", "api_base": "https://example.invalid/v1",
        "provider": "openai", "model": "fixture", "enable_memory": False,
        "tools": {"enable_skills": False, "enable_mcp": False}, **extra,
    }), encoding="utf-8")


def test_unset_root_keeps_legacy_paths_and_config_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert configured_box_agent_home() is None
    assert box_agent_home() == tmp_path / ".box-agent"
    assert box_agent_home(home_dir=tmp_path / "another") == tmp_path / "another/.box-agent"
    assert state_path("sessions") == tmp_path / ".box-agent/sessions"
    assert state_path("log", "relative-log") == Path("relative-log")
    assert AgentConfig().memory_dir == "~/.box-agent/memory"
    assert AgentConfig().workspace_dir == "./workspace"


def test_explicit_root_is_authoritative_without_creating_directories(tmp_path, monkeypatch):
    root = tmp_path / "独立 profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    assert box_agent_home(home_dir=tmp_path / "ignored") == root
    assert state_path("config/auth.json") == root / "config/auth.json"
    assert state_path("log", "custom/log") == root / "custom/log"
    assert not root.exists()
    assert AgentConfig().memory_dir == str(root / "memory")
    assert AgentConfig().workspace_dir == str(root / "workspace")


@pytest.mark.parametrize("override", [None, "browser-cache", "../external-cache", "0"])
def test_profile_browser_cache_is_stable_across_working_directories(tmp_path, monkeypatch, override):
    root = tmp_path / "profile"
    env = {"BOX_AGENT_HOME": str(root)}
    if override is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = override
    expected = "0" if override == "0" else str(
        (root / (override if override is not None else "browsers")).resolve()
    )
    assert browser_cache_path(env=env) == expected
    monkeypatch.chdir(tmp_path)
    assert browser_cache_path(env=env) == expected
    assert not root.exists()


def test_browser_cache_without_profile_preserves_host_relative_override(tmp_path):
    assert browser_cache_path(env={"PLAYWRIGHT_BROWSERS_PATH": "browser-cache"}) == "browser-cache"
    assert browser_cache_path(env={}, home_dir=tmp_path) == str(tmp_path / ".box-agent/browsers")


@pytest.mark.parametrize("value", ["", " ", "relative/profile", "~/profile", "C:relative", "/"])
def test_invalid_explicit_root_never_falls_back(value, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", value)
    with pytest.raises(ValueError, match="BOX_AGENT_HOME"):
        box_agent_home()


def test_explicit_root_rejects_home_and_owned_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="BOX_AGENT_HOME"):
        box_agent_home()
    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    for override in [tmp_path / "old/auth.json", "../old/auth.json"]:
        with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
            state_path("config/auth.json", override)


def test_symlink_escape_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "config").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable on this platform")
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        state_path("config/auth.json")


def test_config_is_only_loaded_from_explicit_profile(tmp_path, monkeypatch):
    root, cwd, package = tmp_path / "profile", tmp_path / "cwd", tmp_path / "package"
    cwd.mkdir()
    write_config(cwd / "box_agent/config/config.yaml", model="wrong-cwd")
    write_config(package / "config/config.yaml", model="wrong-package")
    write_config(root / "config/config.yaml")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    monkeypatch.setattr(Config, "get_package_dir", classmethod(lambda cls: package))
    config = Config.load()
    assert config.llm.model == "fixture"
    assert config.llm.auth_file == str(root / "config/auth.json")
    assert config.agent.memory_dir == str(root / "memory")
    assert not config.agent.enable_memory


def test_missing_isolated_config_does_not_bootstrap_or_fall_back(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    with pytest.raises(FileNotFoundError, match="config.yaml"):
        Config.load()
    assert not root.exists()


def test_only_bundled_prompt_resources_may_fall_back(tmp_path, monkeypatch):
    root, package = tmp_path / "profile", tmp_path / "package"
    (package / "config").mkdir(parents=True)
    for name in ["system_prompt.md", "mcp.json", "auth.json", "config.yaml"]:
        (package / "config" / name).write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    monkeypatch.setattr(Config, "get_package_dir", classmethod(lambda cls: package))
    assert Config.find_config_file("system_prompt.md") == package / "config/system_prompt.md"
    for name in ["mcp.json", "auth.json", "config.yaml"]:
        assert Config.find_config_file(name) is None


def test_explicit_config_and_memory_auth_paths_cannot_read_old_profile(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    outside = tmp_path / "outside.yaml"
    write_config(outside)
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        Config.from_yaml(outside)
    for key in ["auth_file", "memory_dir"]:
        write_config(root / "config/config.yaml", **{key: str(tmp_path / "old")})
        with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
            Config.load()


@pytest.mark.asyncio
async def test_profile_does_not_inherit_login_token_env_or_openclaw_memory(tmp_path, monkeypatch):
    from box_agent.auth import resolve_auth_token
    from box_agent.memory import MemoryManager

    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    monkeypatch.setenv("BOX_AGENT_AUTH_TOKEN", "not-a-real-token")
    assert resolve_auth_token() == ""
    assert resolve_auth_token("explicit-fixture") == "explicit-fixture"
    auth_file = root / "config/auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"token":"profile-fixture"}', encoding="utf-8")
    assert resolve_auth_token(auth_file=auth_file) == "profile-fixture"
    memory = MemoryManager()
    original_is_dir = Path.is_dir

    def guard(path):
        assert path != Path.home() / ".openclaw", "Global OpenClaw must not be inspected"
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", guard)
    assert await memory.import_openclaw(object()) == ""
    assert not memory._openclaw_imported_marker.exists()


def test_mcp_writer_preserves_in_profile_loader_path_but_rejects_external_path(tmp_path, monkeypatch):
    from box_agent.tools import mcp_loader
    from box_agent.tools.mcp_config_tool import _resolve_write_target

    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    custom = root / "config/custom/mcp.json"
    monkeypatch.setattr(mcp_loader, "get_mcp_config_path", lambda: str(custom))
    assert _resolve_write_target() == custom
    outside = tmp_path / "outside/mcp.json"
    monkeypatch.setattr(mcp_loader, "get_mcp_config_path", lambda: str(outside))
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        _resolve_write_target()
    assert not outside.parent.exists()


def test_profile_private_data_allowance_does_not_include_legacy_box_agent(tmp_path, monkeypatch):
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine

    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    policy = CapabilityPolicy(filesystem_scope="session_workspace", session_workspace_root=str(root / "workspace"))
    engine = PermissionEngine(policy, root / "workspace")
    assert engine.check("filesystem.read", {"path": str(root / "config/auth.json")}).allowed
    assert not engine.check("filesystem.read", {"path": str(Path.home() / ".box-agent/config/auth.json")}).allowed


def test_absolute_config_lookup_and_owned_overrides_are_contained(tmp_path, monkeypatch):
    from box_agent.llm.model_profiles import default_model_profile_registry_path
    from box_agent.session_trace import default_session_trace_dir

    root = tmp_path / "profile"
    write_config(root / "config/config.yaml")
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    assert Config.find_config_file(str(root / "config/config.yaml")) == root / "config/config.yaml"
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        Config.find_config_file(str(tmp_path / "old/config.yaml"))
    for name, getter in [("BOX_AGENT_MODEL_PROFILES_FILE", default_model_profile_registry_path), ("BOX_AGENT_SESSION_TRACE_DIR", default_session_trace_dir)]:
        monkeypatch.setenv(name, str(tmp_path / "old"))
        with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
            getter()


def test_profile_default_node_cache_does_not_choose_a_bundled_write_location(tmp_path, monkeypatch):
    from box_agent.tools import runtime

    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    monkeypatch.setattr(runtime, "_bundled_node_runtime_root", lambda: tmp_path / "readonly-bundle")
    assert runtime.NodeRuntimeManager().root == root / "runtimes/node"


def test_profile_validation_error_does_not_echo_config_credentials(tmp_path, monkeypatch):
    from box_agent.config import LLMConfig, ToolsConfig

    root = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(root))
    with pytest.raises(ValueError) as error:
        Config(llm=LLMConfig(api_key="credential-must-not-appear"),
               agent=AgentConfig(memory_dir=str(tmp_path / "outside")), tools=ToolsConfig())
    assert "credential-must-not-appear" not in str(error.value)
    assert "input_value" not in str(error.value)
