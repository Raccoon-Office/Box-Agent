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
    assert "用户明确选择本地 HyperFrames" in prompt


@pytest.mark.asyncio
async def test_ready_video_plugin_remains_callable_when_only_connector_setup_was_skipped(host, monkeypatch):
    directory = host / "hyperframes-video"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL)
    monkeypatch.setenv("BOX_AGENT_VIDEO_RUNTIME_READY", "1")
    tool = GetSkillTool(SkillLoader(sources=[(host, "plugin")]))
    tool.allow_component_setup = False
    assert (await tool.execute("hyperframes-video")).success
    tool.skip_video_generation = True
    skipped = await tool.execute("hyperframes-video")
    assert not skipped.success and skipped.raw_output is None


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", ["connector", "component"])
async def test_acp_continues_video_with_available_tools_after_setup_is_declined(host, tmp_path, skip):
    from box_agent.acp import BoxACPAgent
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from tests.test_acp import DoneLLM, DummyConn
    from tests.test_acp_connector_discovery import config, create, prompt
    from tests.test_connector_discovery import entry, snapshot

    class VideoLLM(DoneLLM):
        pending = []

        async def generate_stream(self, messages, tools=None, **kwargs):
            if self.pending:
                name, arguments = self.pending.pop(0)
                self.calls += 1
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id=f"video-{self.calls}", type="function",
                    function=FunctionCall(name=name, arguments=arguments),
                )])
            else:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event

    def receipts():
        outputs = [notification.model_dump(by_alias=True, exclude_none=True)["update"].get("rawOutput")
                   for notification in conn.updates]
        return [output["type"] for output in outputs if isinstance(output, dict)
                and output.get("type") in {"connector_action_required", "runtime_component_required"}]

    loader = SkillLoader(sources=[(host, "plugin")])
    llm, conn = VideoLLM(), DummyConn()
    agent = BoxACPAgent(conn, config(tmp_path), llm, [GetSkillTool(loader)], "system", skill_loader=loader)
    catalog = snapshot([entry("seko", kind="mcp", skills=[])])
    search = ("tool_search", {"connector": "seko"})
    skill = ("get_skill", {"skill_name": "hyperframes-video"})
    initial_receipt = "connector_action_required" if skip == "connector" else "runtime_component_required"
    try:
        session = await create(agent, tmp_path, catalog=catalog)
        llm.pending = [search if skip == "connector" else skill]
        await prompt(agent, session.sessionId, "制作视频。", metadata={"connector_discovery": catalog})
        assert receipts() == [initial_receipt]
        choice = ({"connector_continuation": {"request_id": "skip", "skip_connector_id": "seko"}}
                  if skip == "connector" else
                  {"component_continuation": {"request_id": "skip", "skip_component_id": "video-generation"}})
        llm.pending = [skill, search, ("write_file", {"path": "storyboard.md", "content": "Opening shot"})]
        resumed = await prompt(agent, session.sessionId, "跳过设置，继续原任务。", metadata={
            "connector_discovery": catalog, **choice,
        })
        assert resumed.field_meta["runStatus"] == "completed"
        assert receipts() == [initial_receipt]
        assert (tmp_path / "storyboard.md").read_text() == "Opening shot"
        llm.pending = [skill]
        await prompt(agent, session.sessionId, "这次安装本地视频插件。", metadata={"connector_discovery": catalog})
        assert receipts() == [initial_receipt, "runtime_component_required"]
    finally:
        await agent.aclose()


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
