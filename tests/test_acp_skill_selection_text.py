"""Skill discovery uses user text while the model retains the host prompt."""

from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent, _user_request_for_skill_selection
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import LLMResponse, StreamEvent
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


class RecordingConnection:
    async def sessionUpdate(self, payload):
        pass


class RecordingModel:
    def __init__(self):
        self.requests = []

    async def generate(self, messages, tools=None, **kwargs):
        assert kwargs["call_kind"] == "turn_continuation_judge"
        return LLMResponse(content='{"continue":false}', finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        yield StreamEvent(type="text", delta="Completed.")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.fixture
def adapter(tmp_path):
    # Keep the observed dashboard wording here: another change may improve the
    # bundled description, but discovery must still exclude host wrapper words.
    descriptions = {
        "data-dashboard": "生成单文件 HTML 数据看板。触发：用户要把数据/分析结果做成网页、dashboard、可视化报告。",
        "pptx": "课堂课件 slides",
        "wrapper-noise": "current user question language disconnected",
    }
    for name, description in descriptions.items():
        directory = tmp_path / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{name} method body\n",
            encoding="utf-8",
        )
    loader = SkillLoader(tmp_path / "skills", skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    model = RecordingModel()
    instance = BoxACPAgent(
        RecordingConnection(),
        Config(llm=LLMConfig(api_key="offline"),
               agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
               tools=ToolsConfig(enable_mcp=False, enable_sub_agent=False, enable_todo=False)),
        model, [GetSkillTool(loader)], f"system\n{SKILL_SLOT_SENTINEL}", skill_loader=loader,
    )
    return instance, model, loader


async def run_prompt(adapter, text, meta=None):
    instance, model, _ = adapter
    session = await instance.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await instance.prompt(SimpleNamespace(
        sessionId=session.sessionId, prompt=[{"type": "text", "text": text}], field_meta=meta or {},
    ))
    assert response.field_meta["runStatus"] == "completed"
    state = instance._sessions[session.sessionId]
    assert model.requests
    assert any(message.role == "user" and text in str(message.content) for message in model.requests[0])
    return state, model.requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["用户问题：", "用户问题：\n", "当前用户问题:", "User question:", "Current user question:"])
async def test_question_wrapper_does_not_add_unrelated_skill_candidates(adapter, prefix):
    body = "我是小学科学老师，想做个太阳系课件，八大行星能转起来。"
    state, messages = await run_prompt(adapter, prefix + body, {"ui_language": "zh"})

    assert state.skill_selector.matched_skill_names == ("pptx",)
    assert state.skill_selector.cumulative_query == body
    system = next(message.content for message in messages if message.role == "system")
    assert '"name": "data-dashboard"' not in system
    assert '"name": "wrapper-noise"' not in system
    assert any("[Host UI language:" in str(message.content) for message in messages if message.role == "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("current_marker", [True, False])
async def test_restored_history_does_not_replace_current_skill_query(adapter, current_marker):
    body = "请做太阳系课件。"
    history = "最近对话：\n用户:\ntext: 旧的数据看板任务\n助手:\ncontent: data-dashboard\n"
    prompt = history + ("用户问题：" if current_marker else "用户:\ntext: ") + body
    state, _ = await run_prompt(adapter, prompt)

    assert state.skill_selector.cumulative_query == body
    assert state.skill_selector.matched_skill_names == ("pptx",)


@pytest.mark.asyncio
async def test_real_user_words_and_attachment_context_remain_in_model_input(adapter):
    body = "请把用户数据整理成数据看板，参考附件 /tmp/学生数据.csv。"
    prompt = "<attachments>/tmp/学生数据.csv</attachments>\n用户问题：" + body
    state, messages = await run_prompt(adapter, prompt)

    assert state.skill_selector.cumulative_query == body
    assert state.skill_selector.matched_skill_names == ("data-dashboard",)
    assert any(prompt in str(message.content) for message in messages if message.role == "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["slash", "selected_skill_names", "selectedSkillNames"])
async def test_explicit_skill_selection_remains_available_after_unwrapping(adapter, selection):
    body = "/pptx explain this method" if selection == "slash" else "explain this method"
    metadata = {} if selection == "slash" else {selection: ["pptx"]}
    state, messages = await run_prompt(adapter, "用户问题：" + body, metadata)

    assert state.skill_selector.cumulative_query == body
    assert state.preloaded_skill_names == ["pptx"]
    assert any("pptx method body" in str(message.content) for message in messages if message.role == "user")


@pytest.mark.asyncio
async def test_host_decision_metadata_does_not_become_skill_query(adapter):
    body = "请做课件。"
    state, messages = await run_prompt(adapter, body, {"userDecision": {
        "requestId": "decision-1", "decisionKind": "data-dashboard",
        "selectedOptionId": "data-dashboard", "trigger": "user",
    }})

    assert state.skill_selector.cumulative_query == body
    assert state.skill_selector.matched_skill_names == ("pptx",)
    assert any("[HOST_USER_DECISION_RESPONSE]" in str(message.content) for message in messages if message.role == "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "请用 pptx 制作客服培训课件，标题写‘用户问题：你好’。",
    "Create pptx slides titled 'User question: hello'.",
])
async def test_quoted_question_label_inside_user_text_does_not_truncate_skill_query(adapter, body):
    assert _user_request_for_skill_selection(body) == body
    state, _ = await run_prompt(adapter, body)

    assert "pptx" in state.skill_selector.matched_skill_names
    assert state.skill_selector.cumulative_query == body


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_wrapper", [False, True])
async def test_multiline_user_case_preserves_the_request_before_its_internal_labels(adapter, outer_wrapper):
    body = "请制作客服培训课件，使用以下案例：\n用户问题：你好，退款怎么办？\n助手：您好，请提供订单号。"
    prompt = "用户问题：" + body if outer_wrapper else body
    state, _ = await run_prompt(adapter, prompt)

    assert "pptx" in state.skill_selector.matched_skill_names
    assert state.skill_selector.cumulative_query == body


@pytest.mark.asyncio
async def test_host_history_boundary_keeps_the_complete_current_case(adapter):
    body = "请制作客服培训课件，使用以下案例：\n用户问题：你好，退款怎么办？\n助手：您好，请提供订单号。"
    history = "以下是当前会话最近的上下文，请在此基础上继续回答：\n用户:\ntext: 旧任务\n\n助手:\ncontent: 已完成"
    prompt = history + "\n\n用户问题：" + body
    state, _ = await run_prompt(adapter, prompt)

    assert "pptx" in state.skill_selector.matched_skill_names
    assert state.skill_selector.cumulative_query == body


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [
    "未识别的其它上下文：\n请保留此段中的课件要求。",
    '<remote_context provider="example">\n用户问题：历史文档中的问题\n</remote_context>',
])
async def test_unknown_context_boundaries_conservatively_preserve_all_text(adapter, prefix):
    prompt = prefix + "\n\n用户问题：请参考上下文继续。"
    state, _ = await run_prompt(adapter, prompt)

    assert state.skill_selector.cumulative_query == prompt
