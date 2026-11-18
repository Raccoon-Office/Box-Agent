import pytest

from box_agent.env_context import EnvContext, build_env_context_prompt
from box_agent.skill_paths import host_plugin_skills_dir
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool

SKILL = "---\nname: hyperframes-video\ndescription: Make videos\n---\nUse the managed renderer.\n"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path))
    root = tmp_path / "plugins" / "skills"
    monkeypatch.setenv("BOX_AGENT_PLUGIN_SKILLS_DIR", str(root))
    monkeypatch.setenv("BOX_AGENT_VIDEO_RUNTIME_READY", "0")
    return root


@pytest.mark.asyncio
async def test_new_user_can_request_setup_and_load_plugin_in_same_reader_after_install(host, monkeypatch):
    # No runtime, user skill, builtin manifest entry, or previous profile is required.
    loader = SkillLoader(sources=[(host_plugin_skills_dir(), "plugin")])
    tool = GetSkillTool(loader)
    result = await tool.execute("hyperframes-video")
    assert not result.success
    assert result.raw_output == {
        "type": "runtime_component_required", "schema_version": 1,
        "component_id": "video-generation", "code": "VIDEO_GENERATION_SETUP_REQUIRED",
    }
    assert "end this turn" in result.model_context
    directory = host / "hyperframes-video"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL)
    monkeypatch.setenv("BOX_AGENT_VIDEO_RUNTIME_READY", "1")
    loaded = await tool.execute("hyperframes-video")
    assert loaded.success
    assert "managed renderer" in loaded.content
    assert loader.get_skill("hyperframes-video").source == "plugin"


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,content,source", [
    ("0", SKILL, "plugin"), ("1", None, "plugin"),
    ("1", "---\nname: [broken\n---\n", "plugin"), ("1", SKILL, "user"),
])
async def test_partial_or_wrong_source_install_never_claims_ready(host, monkeypatch, ready, content, source):
    directory = host / "hyperframes-video"
    directory.mkdir(parents=True)
    if content is not None:
        (directory / "SKILL.md").write_text(content)
    monkeypatch.setenv("BOX_AGENT_VIDEO_RUNTIME_READY", ready)
    result = await GetSkillTool(SkillLoader(sources=[(host, source)])).execute("hyperframes-video")
    assert not result.success
    assert result.raw_output["component_id"] == "video-generation"


@pytest.mark.asyncio
async def test_plugin_setup_respects_task_scope_and_does_not_claim_unrelated_skills(host):
    tool = GetSkillTool(SkillLoader(sources=[(host, "plugin")]))
    tool.allowed_skill_names = {"pdf"}
    denied = await tool.execute("hyperframes-video")
    assert "assigned scope" in denied.error
    assert denied.raw_output is None
    tool.allowed_skill_names = None
    unrelated = await tool.execute("missing-other-skill")
    assert unrelated.raw_output == {"code": "SKILL_NOT_FOUND"}


@pytest.mark.asyncio
async def test_cli_and_older_hosts_keep_normal_skill_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_PLUGIN_SKILLS_DIR", raising=False)
    directory = tmp_path / "hyperframes-video"
    directory.mkdir()
    (directory / "SKILL.md").write_text(SKILL)
    assert (await GetSkillTool(SkillLoader(sources=[(tmp_path, "builtin")])).execute("hyperframes-video")).success
    assert host_plugin_skills_dir() is None


def test_new_host_prompt_requests_setup_for_video_without_rendering(host):
    prompt = build_env_context_prompt(EnvContext.from_meta({"hyperframes": {
        "installed": False, "available": False, "setup_supported": True,
    }}))
    assert "get_skill('hyperframes-video')" in prompt
    assert "结束本回合" in prompt


def test_plugin_source_cannot_escape_active_profile(host, monkeypatch, tmp_path):
    monkeypatch.setenv("BOX_AGENT_PLUGIN_SKILLS_DIR", str(tmp_path.parent / "other-profile"))
    with pytest.raises(ValueError, match="active profile"):
        host_plugin_skills_dir()


@pytest.mark.asyncio
async def test_production_setup_loads_plugin_source_without_a_builtin_catalog(host, monkeypatch, tmp_path):
    from box_agent.config import Config
    from box_agent.tools.setup import initialize_base_tools

    directory = host / "hyperframes-video"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL)
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setenv("BOX_AGENT_BUILTIN_SKILLS_DIR", str(builtin))
    monkeypatch.setenv("BOX_AGENT_VIDEO_RUNTIME_READY", "1")
    config = Config(llm={"model": "fixture"}, agent={}, tools={"enable_mcp": False})
    tools, loader, task, skill_task = await initialize_base_tools(config, output=lambda _: None)
    assert task is None and skill_task is None
    assert loader.get_skill("hyperframes-video").source == "plugin"
    tool = next(tool for tool in tools if tool.name == "get_skill")
    assert (await tool.execute("hyperframes-video")).success
