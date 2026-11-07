"""The same routing contract reaches parent/child requests through every host."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent import session_assembly
from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.env_context import EnvContext
from box_agent.plugins.builtins import SessionResources
from box_agent.prompt_capabilities import render_web_access_policy
from box_agent.mcp_servers.web_extract import WebExtractTool
from box_agent.schema import StreamEvent
from box_agent.session_context import HostBindings, SessionContext, SessionOptions
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import build_auto_loaded_skills_prompt
from box_agent.tools import mcp_tool_catalog
from box_agent.tools.mcp_loader import MCPTool


SKILLS_ROOT = Path(__file__).parents[1] / "box_agent" / "skills"
WEB_TOOLS = frozenset({
    "web_search", "web_extract", "managed_browser_navigate", "managed_browser_snapshot",
    "user_browser_open_tab_and_read", "user_browser_snapshot", "tool_search",
})


async def assemble(tmp_path, profile, tools, *, paused=False, utility=False):
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    options = SessionOptions(
        profile=profile, workspace_dir=tmp_path, sandbox_mode=False, utility=utility,
        state={"env_context": EnvContext.model_validate({
            "browser_connector": {
                "enabled": True, "connected": True, "paused": paused,
                "available": not paused,
            },
        })},
    )
    resources = SessionResources(context=SessionContext(
        config=config, options=options,
        host=HostBindings(system_prompt="Host task instructions"),
    ))
    resources.tools = [SimpleNamespace(name=name) for name in sorted(tools)]
    await session_assembly.prepare_prompt(resources)
    return resources.system_prompt, resources.state["child_system_prompt"]


@pytest.mark.parametrize("profile", ["acp", "cli", "python"])
@pytest.mark.parametrize("paused", [False, True])
async def test_all_hosts_inherit_one_policy_even_when_connector_is_paused(tmp_path, profile, paused):
    parent, child = await assemble(tmp_path, profile, WEB_TOOLS, paused=paused)
    policy = render_web_access_policy(WEB_TOOLS)
    for prompt in (parent, child):
        assert prompt.count("## Web Access Routing") == 1
        assert policy in prompt
        assert "先用 `tool_search`" not in policy
        assert "已注册的通道工具" not in policy


@pytest.mark.parametrize("tools", [
    {"tool_search"}, {"web_extract"}, {"managed_browser_navigate"}, {"user_browser_snapshot"},
])
async def test_partial_sessions_share_rules_without_inheriting_capability_instructions(tmp_path, tools):
    parent, child = await assemble(tmp_path, "acp", tools)
    for prompt in (parent, child):
        routing = prompt.split("## Web Access Routing\n", 1)[1].split("\n## ", 1)[0]
        assert "当前任务实际提供的工具清单" in routing
        assert "向父 Agent 报告" in routing
        for name in ("`user_browser_*`", "`managed_browser_*`", "`web_extract`", "`tool_search`"):
            assert name not in routing


@pytest.mark.parametrize("tools", [set(), {"read_file"}, {"bash"}])
async def test_utility_sessions_without_web_or_discovery_receive_no_web_policy(tmp_path, tools):
    parent, child = await assemble(tmp_path, "python", tools, utility=True)
    assert "## Web Access Routing" not in parent
    assert "## Web Access Routing" not in child


@pytest.mark.parametrize("skills", [
    ("browser-use",), ("research-synthesis",), ("browser-use", "research-synthesis"),
])
async def test_loading_either_or_both_skills_keeps_one_runtime_routing_contract(tmp_path, skills):
    parent, _ = await assemble(tmp_path, "acp", WEB_TOOLS)
    loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    loader.discover_skills()
    rendered = build_auto_loaded_skills_prompt(loader, parent, skills)
    assert set(skills).issubset(rendered.loaded_names)
    assert rendered.system_prompt.count("## Web Access Routing") == 1
    assert render_web_access_policy(WEB_TOOLS) in rendered.system_prompt
    assert "shared **Web Access Routing**" in rendered.system_prompt
    assert "Public-web search, retrieval, crawling, bulk collection" not in rendered.system_prompt
    assert "prefer the standalone Playwright" not in rendered.system_prompt


def test_routing_contract_prioritizes_explicit_targets_and_preserves_recovery_boundaries():
    policy = render_web_access_policy(WEB_TOOLS)
    ordered_rules = [
        "1. 显式目标优先", "2. 延续有效上下文", "3. 依赖用户日常浏览器",
        "4. 独立任务需要打开给用户看", "5. 已知公开 URL", "6. 信息检索尚无目标 URL",
    ]
    positions = [policy.index(rule) for rule in ordered_rules]
    assert positions == sorted(positions)
    for boundary in (
        "同域名已有标签页不能单独作为切入用户状态的理由",
        "动作结果不确定先核验，不重放点击或提交",
        "Fetch 不能替代交互或展示",
        "权限拒绝和访问限制不能靠切换绕过",
        "“让我看/检查”本身不要求系统浏览器",
        "不执行无条件 Fetch→内置→系统降级",
        "WEB_VERIFICATION_REQUIRED",
        "可用浏览器",
        "原 URL",
        "等待用户确认",
        "同一浏览器",
        "不要反复 HTTP 重试",
        "不替用户完成或绕过验证",
    ):
        assert boundary in policy


@pytest.mark.parametrize("profile", ["acp", "cli", "python"])
async def test_routing_contract_reaches_model_requests_on_initial_and_follow_up_turns(tmp_path, profile):
    captured = []

    class CaptureLLM:
        async def generate_stream(self, **kwargs):
            captured.append(kwargs["messages"][0].content)
            yield StreamEvent(type="text", delta="No external request was made.")
            yield StreamEvent(type="finish", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(
            enable_memory=False, enable_memory_extraction=False,
            goal_autopilot_enabled=False, workspace_dir=str(tmp_path),
        ),
        tools=ToolsConfig(
            enable_mcp=False, enable_skills=False, enable_file_tools=False,
            enable_bash=False, enable_plan=False, enable_todo=False, enable_sub_agent=False,
        ),
    )
    session = await AgentSession.open(
        config=config,
        options=SessionOptions(profile=profile, workspace_dir=tmp_path, sandbox_mode=False),
        host=HostBindings(llm_client=CaptureLLM(), tools=[WebExtractTool(None)]),
    )
    try:
        for request in ("总结 https://example.com/article 的正文", "继续核对刚才的文章"):
            session.agent.add_user_message(request)
            await session.agent.run()
        assert len(captured) == 2
        for prompt in captured:
            assert prompt.count("## Web Access Routing") == 1
            assert "5. 已知公开 URL" in prompt
            assert "当前任务实际提供的工具清单" in prompt
            assert "WEB_VERIFICATION_REQUIRED" in prompt
            assert "等待用户确认" in prompt
    finally:
        await session.aclose()


class RequestCapture:
    def __init__(self):
        self.requests = []

    async def generate_stream(self, **kwargs):
        self.requests.append((
            kwargs["messages"][0].content,
            {tool.name for tool in kwargs.get("tools", [])},
        ))
        yield StreamEvent(type="text", delta="No external request was made.")
        yield StreamEvent(type="finish", finish_reason="stop")


def browser_tool():
    return MCPTool(
        name="managed_browser_navigate", server_name="playwright",
        description="Navigate an independent browser",
        parameters={"type": "object", "properties": {}},
        session=SimpleNamespace(), always_load=True,
    )


def session_config(workspace, *, sub_agents=False):
    return Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(
            enable_memory=False, enable_memory_extraction=False,
            goal_autopilot_enabled=False, workspace_dir=str(workspace),
        ),
        tools=ToolsConfig(
            enable_mcp=True, enable_skills=False, enable_file_tools=False,
            enable_bash=False, enable_plan=False, enable_todo=False,
            enable_sub_agent=sub_agents,
        ),
    )


@pytest.mark.parametrize("profile", ["acp", "cli", "python"])
@pytest.mark.parametrize("discovery", ["before_session", "between_turns"])
@pytest.mark.parametrize("version", [1, 2])
async def test_default_session_keeps_policy_when_catalog_tools_arrive(tmp_path, monkeypatch, profile, discovery, version):
    # ACP's shared MCP catalog may already be ready, or finish after creation.
    # Neither path adds catalog-only tools to resources.tools.
    catalog = mcp_tool_catalog.MCPToolCatalog()
    monkeypatch.setattr(mcp_tool_catalog, "_CATALOG", catalog)
    if discovery == "before_session":
        catalog.replace_server("playwright", [browser_tool()])
    llm = RequestCapture()
    config = session_config(tmp_path)
    config.agent.tool_interface_version = version
    session = await AgentSession.open(
        config=config,
        options=SessionOptions(profile=profile, workspace_dir=tmp_path, sandbox_mode=False),
        host=HostBindings(llm_client=llm, base_tools=[], output=lambda _: None),
    )
    try:
        assert "tool_search" in session.agent.tools
        for index, request in enumerate(("打开百度", "继续打开刚才的网站")):
            if discovery == "between_turns" and index == 1:
                catalog.replace_server("playwright", [browser_tool()])
            session.agent.add_user_message(request)
            await session.agent.run()
        assert len(llm.requests) == 2
        for prompt, names in llm.requests:
            assert "tool_search" in names
            assert prompt.count("## Web Access Routing") == 1
        assert ("managed_browser_navigate" in llm.requests[-1][1]) is (version == 1)
        assert ("call_tool" in llm.requests[-1][1]) is (version == 2)
    finally:
        await session.aclose()


@pytest.mark.parametrize("inherit_full_parent", [False, True])
async def test_real_subagent_request_respects_its_restricted_tool_bundle(tmp_path, inherit_full_parent):
    llm = RequestCapture()
    session = await AgentSession.open(
        config=session_config(tmp_path, sub_agents=True),
        options=SessionOptions(profile="acp", workspace_dir=tmp_path, sandbox_mode=False),
        host=HostBindings(
            llm_client=llm, base_tools=[WebExtractTool(None), browser_tool()],
            output=lambda _: None,
        ),
    )
    try:
        child = session.agent.tools["sub_agent"]
        if inherit_full_parent:
            # Also cover callers passing the complete parent prompt instead of
            # the shared assembler's child projection; do not strip caller text.
            child.set_parent_system_prompt(session.agent.system_prompt)
        result = await child.execute(
            task="Read the assigned public article. Report any missing capabilities.",
            required_tools=["web_extract"],
        )
        assert result.success, result.error
        assert llm.requests
        prompt, tools = llm.requests[0]
        assert tools == {"web_extract"}
        assert prompt.count("## Web Access Routing") == 1
        assert "先用 `tool_search`" not in prompt
        assert "已注册的通道工具" not in prompt
        assert "`managed_browser_*`" not in prompt
        assert "向父 Agent 报告" in prompt
    finally:
        await session.aclose()
