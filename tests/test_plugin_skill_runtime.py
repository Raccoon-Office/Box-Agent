"""Real package snapshots entering both existing Skill delivery paths."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from box_agent.plugins.skill_sources import (
    ACTIVATION_ENV, build_skill_plan, confirm_plugin_startup,
    load_plugin_skill_plan, plugin_settings_service, _skill_name,
)
from box_agent.schema import Message
from box_agent.session_log import SessionLog
from box_agent.skill_context import SkillReferenceContext, read_reference
from box_agent.skill_dependencies import SkillDependencyError
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def configuration(**overrides):
    defaults = dict(enabled=True, state_dir=None, target_id="local", profile_id="default")
    return SimpleNamespace(plugin_platform=SimpleNamespace(**{**defaults, **overrides}))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(ACTIVATION_ENV, raising=False)
    monkeypatch.delenv("BOX_AGENT_DISABLE_PLUGINS", raising=False)
    root = tmp_path / "package"
    (root / "skills/guide").mkdir(parents=True)
    (root / "ui").mkdir()
    (root / "skills/guide/SKILL.md").write_text(
        "---\nname: guide\ndescription: Test workflow\n---\n"
        "Use the configured language. Read {{skill:guide}} for this instance.\n")
    (root / "ui/config.html").write_text("<!doctype html><p>Settings</p>")
    (root / "config.schema.json").write_text(json.dumps({
        "type": "object", "additionalProperties": False,
        "properties": {"language": {"type": "string", "enum": ["zh-CN", "en-US"], "default": "zh-CN"}},
    }))
    manifest = {"schema_version": 1, "id": "org.example.guide", "version": "1.0.0",
        "requires": {"box_agent": ">=1.0.0 <2.0.0", "plugin_api": 1},
        "runtime": {"kind": "resources"}, "config_schema": "config.schema.json",
        "ui": {"config": {"entry": "ui/config.html", "bridge_version": 1}},
        "contributes": {"skills": [{"id": "guide", "path": "skills/guide", "config_keys": ["language"]}]}}
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    service = plugin_settings_service(configuration())
    service.install("office", str(root), enabled=True, aliases={"test-guide": "guide"}, request_id="install")
    return service


def start_candidate(service, monkeypatch, request="start", *, sources=()):
    candidate = service.prepare_apply(service.activation_status()["profile_revision"], request)
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    config = configuration()
    plan = load_plugin_skill_plan(config)
    loader = SkillLoader(sources=list(sources), **plan.loader_arguments())
    loader.discover_skills()
    confirm_plugin_startup(config, loader)
    monkeypatch.delenv(ACTIVATION_ENV)
    return config, loader


@pytest.mark.asyncio
async def test_get_skill_and_explicit_selection_read_the_same_config_snapshot(installed, monkeypatch):
    _, loader = start_candidate(installed, monkeypatch)
    runtime = SkillRuntime(loader)
    runtime.select(["test-guide"])
    explicit = read_reference(runtime.materialize_selected_messages([])[0][1])
    tool = await GetSkillTool(loader)._aread("test-guide", reader=runtime.read)
    assert tool.success
    assert explicit[1] in tool.content
    assert '"language": "zh-CN"' in explicit[1]
    assert explicit[0]["plugin"]["instance_id"] == "office"
    assert "{{skill:" not in explicit[1]
    assert '"skill_names": {"guide": "test-guide"}' in explicit[1]


def test_save_does_not_change_current_or_new_normal_hosts_until_candidate_start(installed, monkeypatch):
    original, loader = start_candidate(installed, monkeypatch)
    old_prompt = loader.get_skill("test-guide").to_prompt()
    view = installed.get_config("office")
    installed.save_config("office", {"language": "en-US"}, view["saved_revision"], "save",
                          view["package_digest"], view["schema_digest"])
    assert load_plugin_skill_plan(original).skills[0].to_prompt() == old_prompt
    assert load_plugin_skill_plan(configuration()).skills[0].to_prompt() == old_prompt
    _, new_loader = start_candidate(installed, monkeypatch, "restart")
    assert '"language": "en-US"' in new_loader.get_skill("test-guide").to_prompt()
    assert loader.get_skill("test-guide").to_prompt() == old_prompt


def test_disabled_alias_cannot_fall_back_to_an_old_user_or_builtin_skill(installed, monkeypatch, tmp_path):
    start_candidate(installed, monkeypatch)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "SKILL.md").write_text("---\nname: test-guide\ndescription: old\n---\nOLD METHOD")
    view = installed.get_config("office")
    installed.set_enabled("office", False, view["saved_revision"], "disable")
    _, loader = start_candidate(installed, monkeypatch, "disabled", sources=[(legacy, "user")])
    assert loader.get_skill("test-guide", include_disabled=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "configuration"])
async def test_restored_plugin_task_reports_current_capability_without_rewriting_history(
    installed, monkeypatch, tmp_path, change,
):
    _, loader = start_candidate(installed, monkeypatch)
    log = SessionLog.create(tmp_path / "sessions", session_id="plugin-resume", cwd=tmp_path)
    artifact = tmp_path / "existing-presentation.html"
    artifact.write_text("<p>Previously delivered presentation</p>")
    try:
        previous = SkillRuntime(loader, session_log=log, allow_partial_restore=True)
        result = previous.read("test-guide")
        assert result.success
        records = previous.log_records()
        history = [
            Message(role="tool", name="get_skill", tool_call_id="previous-skill-read", content=result.content),
            Message(role="user", content="Continue this existing presentation."),
        ]
        saved_messages = [message.model_dump() for message in history]
        saved_log = log.path.read_bytes()
        saved_artifact = artifact.read_bytes()
        view = installed.get_config("office")
        if change == "disabled":
            installed.set_enabled("office", False, view["saved_revision"], "disable")
        else:
            installed.save_config("office", {"language": "en-US"}, view["saved_revision"], "save",
                                  view["package_digest"], view["schema_digest"])
        _, current_loader = start_candidate(installed, monkeypatch, "restart")
        restored = SkillRuntime(current_loader, session_log=log, allow_partial_restore=True)
        await restored.arestore_records(records)
        restored.begin_turn()
        projection = SkillReferenceContext(restored).prepare_request(history, budget_chars=32000)
        notice = "\n".join(projection.diagnostics)
        assert "reconfirm" in notice and "Do not automatically replay".lower() in notice.lower()
        assert "reconfirm" in str(projection.messages[-1].content)
        if change == "disabled":
            assert restored.log_records() == []
            assert "currently unavailable" in notice
            assert "SKILL_NOT_FOUND" in notice
            # A tombstone hides the old alias, so do not invent a more specific cause.
            assert "is disabled" not in notice
        else:
            assert restored.log_records()[0]["sha256"] != records[0]["sha256"]
            assert "historical sha256=" in notice
            assert "currently unavailable" not in notice
            assert '"language": "en-US"' in str(projection.messages[-1].content)
        assert [message.model_dump() for message in history] == saved_messages
        assert projection.messages[0].content == history[0].content
        assert log.path.read_bytes() == saved_log
        assert artifact.read_bytes() == saved_artifact
        assert restored.deactivate_reference("test-guide")
        assert not restored.restore_diagnostics
    finally:
        log.close()


def test_wrong_actual_catalog_fails_candidate_and_preserves_previous_active(installed, monkeypatch):
    start_candidate(installed, monkeypatch)
    previous = installed.get_snapshot()
    candidate = installed.prepare_apply(installed.activation_status()["profile_revision"], "bad-start")
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    config = configuration()
    load_plugin_skill_plan(config)
    with pytest.raises(ValueError, match="startup smoke"):
        confirm_plugin_startup(config, SkillLoader(sources=[]))
    assert installed.get_snapshot() == previous
    assert installed.activation_status()["activation"]["status"] == "failed"


def test_changed_package_is_not_hot_reloaded_and_active_failure_keeps_core_available(installed, monkeypatch):
    _, loader = start_candidate(installed, monkeypatch)
    path = loader.get_skill("test-guide").skill_path
    path.chmod(0o644)
    path.write_text("---\nname: guide\ndescription: changed\n---\nCHANGED")
    with pytest.raises(SkillDependencyError, match="changed"):
        SkillRuntime(loader).resolve_reference("test-guide")
    config = configuration()
    broken = load_plugin_skill_plan(config)
    assert not broken.skills and broken.errors
    confirm_plugin_startup(config, SkillLoader(sources=[]))
    assert installed.activation_status()["runtime"]["healthy"] is False


def test_safe_mode_never_reads_a_corrupt_plugin_state(installed, monkeypatch):
    installed.store.path.write_text("broken")
    monkeypatch.setenv("BOX_AGENT_DISABLE_PLUGINS", "1")
    plan = load_plugin_skill_plan(configuration())
    assert plan.generation == "safe-mode" and not plan.skills


def test_two_instances_with_ambiguous_underscores_have_distinct_bounded_names(installed):
    assert _skill_name("a__b", "c") != _skill_name("a", "b__c")
    assert len(_skill_name("x" * 64, "y" * 64)) <= 128
    candidate = installed.prepare_apply(installed.activation_status()["profile_revision"], "names")
    item = installed.get_snapshot(candidate["activation_id"])["instances"]["office"]
    instances = {}
    for instance in ["a__b", "a", "x" * 64]:
        entry = copy.deepcopy(item)
        entry["aliases"] = {}
        instances[instance] = entry
    skills = build_skill_plan({"generation": "test", "instances": instances}).skills
    assert len({s.name for s in skills}) == 3
    assert all(len(s.name) <= 128 for s in skills)


@pytest.mark.asyncio
async def test_actual_session_tools_gate_both_skill_delivery_paths(installed, monkeypatch):
    _, loader = start_candidate(installed, monkeypatch)
    loader.get_skill("test-guide").metadata["requires_host_tools"] = ["missing_tool"]
    runtime = SkillRuntime(loader)
    runtime.available_tools_provider = lambda: []
    runtime.select(["test-guide"])
    assert "PLUGIN_TOOL_UNAVAILABLE" in runtime.materialize_selected_messages([])[0][1]
    result = await GetSkillTool(loader)._aread("test-guide", reader=runtime.read)
    assert not result.success
    assert result.raw_output["code"] == "PLUGIN_TOOL_UNAVAILABLE"


def test_custom_package_store_is_protected_by_existing_file_and_shell_guards(tmp_path):
    from box_agent.tools.safety import builtin_skill_write_error, builtin_skill_command_write_error

    root = tmp_path / "custom"
    plugin_settings_service(configuration(state_dir=str(root)))
    resource = root / "packages" / "digest" / "SKILL.md"
    assert "PLUGIN_RESOURCE_READ_ONLY" in builtin_skill_write_error(resource)
    assert "PLUGIN_RESOURCE_READ_ONLY" in builtin_skill_command_write_error(f"rm '{resource}'", tmp_path)
    assert builtin_skill_write_error(tmp_path / "output.pptx") is None


def test_management_process_needs_no_model_and_cannot_claim_activation_success(tmp_path):
    env = {**os.environ, "BOX_AGENT_HOME": str(tmp_path / "empty")}
    def call(method):
        return subprocess.run([sys.executable, "-m", "box_agent.plugins.management_cli"],
            input=json.dumps({"method": method, "params": {}}), text=True,
            capture_output=True, env=env, timeout=20)
    response = call("plugins.list")
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["ok"]
    rejected = call("profile.confirm_apply")
    assert rejected.returncode == 1 and not json.loads(rejected.stdout)["ok"]


def test_management_accepts_deployment_inputs_without_implicitly_importing_them(tmp_path, monkeypatch):
    from box_agent.plugins.management_cli import handle_request
    home = tmp_path / "profile"
    (home / "config").mkdir(parents=True)
    (home / "config/config.yaml").write_text(yaml.safe_dump({"plugin_platform": {
        "lock_file": "plugins.lock.json", "instances": {"must_not_activate": {}}, "bindings": {},
    }}))
    monkeypatch.setenv("BOX_AGENT_HOME", str(home))
    assert handle_request({"method": "plugins.list", "params": {}})["plugins"] == []


def test_retry_of_old_successful_apply_does_not_preflight_or_restart_new_candidate(installed, monkeypatch):
    from box_agent.plugins.management_cli import handle_request
    request = {"method": "profile.prepare_apply", "params": {
        "expected_profile_revision": installed.activation_status()["profile_revision"], "request_id": "first"}}
    first = handle_request(request)
    monkeypatch.setenv(ACTIVATION_ENV, first["activation_id"])
    config = configuration()
    plan = load_plugin_skill_plan(config)
    loader = SkillLoader(sources=[], **plan.loader_arguments())
    loader.discover_skills()
    confirm_plugin_startup(config, loader)
    monkeypatch.delenv(ACTIVATION_ENV)
    second = installed.prepare_apply(installed.activation_status()["profile_revision"], "second")
    assert handle_request(request) == first
    assert installed.activation_status()["activation"]["activation_id"] == second["activation_id"]


def test_runtime_environment_probe_uses_host_python_and_rejects_missing_modules():
    from box_agent.config import PluginRuntimeProfile
    from box_agent.plugins.runtime_profiles import verify_runtime_profiles
    from box_agent.plugins.manifest import PluginError
    config = configuration(runtime_profiles={"minimal": PluginRuntimeProfile(python_modules=["json"])})
    verify_runtime_profiles(config, ("minimal",), runtime_env={"BOX_AGENT_PYTHON": sys.executable})
    with pytest.raises(PluginError, match="not configured"):
        verify_runtime_profiles(config, ("unknown",))
    config.plugin_platform.runtime_profiles["minimal"] = PluginRuntimeProfile(python_modules=["box_plugin_missing_module"])
    with pytest.raises(PluginError, match="failed its host probe"):
        verify_runtime_profiles(config, ("minimal",), runtime_env={"BOX_AGENT_PYTHON": sys.executable})


@pytest.mark.asyncio
@pytest.mark.parametrize("required", ["certainly_missing_tool", "execute_code"])
async def test_acp_initialize_checks_real_native_tools_before_confirming(installed, monkeypatch, required):
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, LLMConfig, ToolsConfig, AgentConfig
    # Use a new package version rather than mutating an installed snapshot.
    source = Path(os.environ["BOX_AGENT_HOME"]).parent / "package"
    manifest = yaml.safe_load((source / "plugin.yaml").read_text())
    manifest["contributes"]["skills"][0]["requires_host_tools"] = [required]
    (source / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    current = installed.get_config("office")
    installed.install("office", str(source), enabled=True, aliases={"test-guide": "guide"},
                      expected_revision=current["saved_revision"], request_id="replace")
    candidate = installed.prepare_apply(installed.activation_status()["profile_revision"], "missing")
    monkeypatch.setenv(ACTIVATION_ENV, candidate["activation_id"])
    config = Config(llm=LLMConfig(api_key="test"), tools=ToolsConfig(), agent=AgentConfig())
    plan = load_plugin_skill_plan(config)
    loader = SkillLoader(sources=[], **plan.loader_arguments())
    loader.discover_skills()
    adapter = BoxACPAgent(object(), config, object(), [], "system", skill_loader=loader)
    if required == "certainly_missing_tool":
        with pytest.raises(SkillDependencyError, match=required):
            await adapter.initialize(SimpleNamespace(field_meta={}))
        assert installed.activation_status()["activation"]["status"] == "failed"
        assert installed.get_snapshot()["generation"] == "builtin"
    else:
        response = await adapter.initialize(SimpleNamespace(field_meta={}))
        assert response.field_meta["plugin_runtime"]["healthy"]
        assert installed.activation_status()["activation"]["status"] == "active"
        await adapter.aclose()


def test_plugin_declared_tools_enter_normal_exposure_without_registering_new_code(installed, monkeypatch, tmp_path):
    from box_agent.agent import Agent
    from tests.test_local_tool_search import LocalTool
    _, loader = start_candidate(installed, monkeypatch)
    loader.get_skill("test-guide").metadata["requires_host_tools"] = ["append_file"]
    tool = LocalTool("append_file")
    agent = Agent(llm_client=object(), system_prompt="test", tools=[tool],
                  skill_runtime=SkillRuntime(loader), workspace_dir=str(tmp_path))
    offered = lambda: agent.mcp_tool_exposure.prepare_tools(list(agent.tools.values())).offered_names
    assert "append_file" not in offered()
    agent.skill_runtime.select(["test-guide"])
    assert "append_file" in offered()
    assert agent.tools["append_file"] is tool
