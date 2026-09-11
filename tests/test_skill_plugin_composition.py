"""Skill readers and the final plugin tool catalog must share one source."""

from dataclasses import replace

import pytest

from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_catalog_tool import ListSkillsTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def _catalog(tmp_path, label):
    directory = tmp_path / label / "shared"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: shared\ndescription: Shared method\n---\nBODY-FROM-{label}\n",
        encoding="utf-8",
    )
    loader = SkillLoader(directory.parent)
    loader.discover_skills()
    return loader, {"get_skill": GetSkillTool(loader), "list_skills": ListSkillsTool(loader)}


class _ReadingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self):
        self.requests = []

    async def generate_stream(self, *, messages, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if len(self.requests) == 1:
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id="read-shared", type="function",
                         function=FunctionCall(name="get_skill", arguments={"skill_name": "shared"}))
            ])
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.parametrize("replace_reader", [False, True])
async def test_plugin_catalog_replacement_requires_matching_skill_reader(tmp_path, monkeypatch, replace_reader):
    import box_agent.composition as composition
    from box_agent.core import run_agent_loop
    from box_agent.kernel.ports import SkillEnginePort, ToolCatalogPort
    from box_agent.plugins.defaults import DEFAULT_CAPABILITY_SCHEMA
    from box_agent.plugins.host import PluginHost

    _, original_catalog = _catalog(tmp_path, "A")
    replacement_loader, replacement_catalog = _catalog(tmp_path, "B")
    replacement_reader = SkillRuntime(replacement_loader)
    original_host_factory = composition.create_default_plugin_host
    hosts = []

    def create_replaced_host(**capabilities):
        source = original_host_factory(**capabilities)
        descriptors = []
        for descriptor in source.discover():
            if descriptor.capabilities == (ToolCatalogPort,):
                descriptor = replace(descriptor, factory=lambda: replacement_catalog)
            elif replace_reader and descriptor.capabilities == (SkillEnginePort,):
                descriptor = replace(descriptor, factory=lambda: replacement_reader)
            descriptors.append(descriptor)
        host = PluginHost(tuple(descriptors), schema=DEFAULT_CAPABILITY_SCHEMA)
        hosts.append(host)
        return host

    monkeypatch.setattr(composition, "create_default_plugin_host", create_replaced_host)
    llm = _ReadingLLM()

    async def run():
        return [event async for event in run_agent_loop(
            llm=llm, tools=original_catalog, messages=[Message(role="user", content="Apply shared")],
            max_steps=3, workspace_dir=str(tmp_path),
        )]

    if replace_reader:
        await run()
        tool_messages = [message for message in llm.requests[-1] if message.role == "tool"]
        assert "BODY-FROM-B" in tool_messages[0].content
        assert "BODY-FROM-A" not in str(llm.requests)
    else:
        with pytest.raises(ValueError, match="Skill.*source"):
            await run()
        assert llm.requests == []
    assert hosts[0]._live_records == []
    assert hosts[0]._closed


def test_explicit_skill_reader_is_borrowed_only_with_matching_catalog(tmp_path):
    from box_agent.composition import compose_default_kernel_services

    loader_a, catalog_a = _catalog(tmp_path, "A")
    _, catalog_b = _catalog(tmp_path, "B")
    reader = SkillRuntime(loader_a)
    reader.select(["shared"])
    arguments = {"llm": _ReadingLLM(), "tools": catalog_a, "skill_engine": reader}
    assert compose_default_kernel_services(arguments).skill_engine is reader
    assert reader.state.selected == ("shared",)

    with pytest.raises(ValueError, match="Skill.*source"):
        compose_default_kernel_services({**arguments, "tools": catalog_b})
    assert reader.loader is loader_a
    assert reader.state.selected == ("shared",)


def test_mixed_get_and_list_skill_sources_are_rejected(tmp_path):
    from box_agent.composition import compose_default_kernel_services

    _, catalog_a = _catalog(tmp_path, "A")
    _, catalog_b = _catalog(tmp_path, "B")
    catalog = {"get_skill": catalog_a["get_skill"], "list_skills": catalog_b["list_skills"]}
    with pytest.raises(ValueError, match="Skill.*source"):
        compose_default_kernel_services({"llm": _ReadingLLM(), "tools": catalog})


def test_unrelated_loader_attribute_does_not_choose_skill_read_source(tmp_path):
    from box_agent.composition import compose_default_kernel_services
    from box_agent.tools.base import Tool, ToolResult

    loader_a, _ = _catalog(tmp_path, "A")
    loader_b, catalog_b = _catalog(tmp_path, "B")

    class OtherTool(Tool):
        name = "other"
        description = "Not a Skill reader"
        parameters = {"type": "object", "properties": {}}
        skill_loader = loader_a

        async def execute(self):
            return ToolResult(success=True, content="done")

    services = compose_default_kernel_services({"llm": _ReadingLLM(), "tools": {"other": OtherTool(), **catalog_b}})
    assert services.skill_engine.loader is loader_b


def test_default_reader_restores_actual_tool_history_before_kernel_compaction(tmp_path):
    from box_agent.composition import compose_default_kernel_services

    loader, catalog = _catalog(tmp_path, "A")
    prior_read = SkillRuntime(loader).read("shared")
    history = [
        Message(role="user", content="Earlier task"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="earlier-skill", type="function",
                     function=FunctionCall(name="get_skill", arguments={"skill_name": "shared"}))
        ]),
        Message(role="tool", name="get_skill", tool_call_id="earlier-skill", content=prior_read.model_context),
    ]
    services = compose_default_kernel_services({"llm": _ReadingLLM(), "tools": catalog, "messages": history})

    assert services.skill_engine.state.reads["shared"].prompt == loader.get_skill("shared").to_prompt()
    assert services.skill_engine.state.reads["shared"].delivered_complete
    assert history[-1].content == prior_read.model_context
