"""Edited branches must reach the model without any discarded future history."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import StreamEvent
from box_agent.session_continuation import HISTORY_EDIT_SCHEMA_VERSION


class Connection:
    async def sessionUpdate(self, payload):
        pass


class CaptureLLM:
    def __init__(self):
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(deepcopy(messages))
        yield StreamEvent(type="text", delta="generated-answer")
        yield StreamEvent(type="finish", finish_reason="stop")


def adapter(tmp_path, llm):
    return BoxACPAgent(Connection(), Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=2),
        tools=ToolsConfig(enable_sub_agent=False),
    ), llm, [], "system")


def request(session, text, seed=None):
    return SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": text}],
                           field_meta={"session_continuation": seed} if seed else {})


@pytest.mark.asyncio
async def test_edited_branch_reaches_model_with_only_kept_prefix_and_survives_restart(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        old = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "old-branch"}))
        await host.prompt(request(old, "discarded-question"))
        branch = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "edited-branch"}))
        assert 2 in branch.field_meta["capabilities"]["session_continuation_versions"]
        seed = {"schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "edited-branch",
                "reason": "history_edit", "truncated": False,
                "messages": [{"role": "user", "content": "kept-question"},
                             {"role": "assistant", "content": "kept-answer"}]}
        await host.prompt(request(branch, "edited-question", seed))
        context = [(m.role, m.content) for m in llm.requests[-1][1:]]
        assert context[:2] == [("user", "kept-question"), ("assistant", "kept-answer")]
        assert "edited-question" in context[-1][1]
        assert "discarded-question" not in str(context)
        old_log = host._sessions[old.sessionId].agent.session_log
        assert "discarded-question" in str([(m.role, m.content) for m in old_log.replay().messages])
        resumed = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "edited-branch"}))
        await host.prompt(request(resumed, "follow-up"))
        context = [(m.role, m.content) for m in llm.requests[-1][1:]]
        assert context[:2] == [("user", "kept-question"), ("assistant", "kept-answer")]
        assert "edited-question" in str(context) and "follow-up" in context[-1][1]
        assert "discarded-question" not in str(context)
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_editing_first_turn_starts_without_the_old_context(tmp_path):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        branch = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "first-edit"}))
        seed = {"schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "first-edit",
                "reason": "history_edit", "messages": [], "truncated": False}
        await host.prompt(request(branch, "new-first", seed))
        assert [m.role for m in llm.requests[-1]] == ["system", "user"]
        assert "new-first" in llm.requests[-1][-1].content
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign-session", "partial-history"])
async def test_invalid_edit_is_rejected_before_new_input_reaches_model(tmp_path, invalid):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        branch = await host.newSession(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"session_id": "owned-edit"},
        ))
        seed = {
            "schema_version": HISTORY_EDIT_SCHEMA_VERSION,
            "product_session_id": "foreign" if invalid == "foreign-session" else "owned-edit",
            "reason": "history_edit",
            "messages": [],
            "truncated": invalid == "partial-history",
        }
        with pytest.raises(ValueError, match="HISTORY_EDIT"):
            await host.prompt(request(branch, "must-not-be-appended", seed))
        assert not llm.requests
        state = host._sessions[branch.sessionId]
        assert len(state.agent.messages) == 1
        assert state.turn_counter == 0
        assert state.continuation_applied is False
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_source", [False, True])
async def test_edited_branch_copies_session_images_and_preserves_inference_age(tmp_path, close_source):
    llm = CaptureLLM()
    llm.capabilities = {"image_input": True}
    host = adapter(tmp_path, llm)
    encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    try:
        original = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "image-source"}))
        await host.prompt(SimpleNamespace(sessionId=original.sessionId, field_meta={}, prompt=[
            {"type": "text", "text": "Remember this image."},
            {"type": "image", "data": encoded, "mimeType": "image/png"},
        ]))
        source_state = host._sessions[original.sessionId]
        source_log = source_state.agent.session_log
        projection = source_log.replay()
        image_ref = projection.images[0]
        prior_user = next(message for message in projection.messages if message.role == "user")
        if close_source:
            await source_state.aclose()
            source_log.close()
            host._sessions.pop(original.sessionId)
        branch = await host.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "image-branch"}))
        seed = {
            "schema_version": HISTORY_EDIT_SCHEMA_VERSION, "product_session_id": "image-branch",
            "reason": "history_edit", "truncated": False, "source_session_id": "image-source",
            "messages": [{"role": "user", "content": prior_user.content,
                          "image_refs": [{"imageId": image_ref["imageId"]}]}],
        }
        await host.prompt(request(branch, "Continue.", seed))
        branch_log = host._sessions[branch.sessionId].agent.session_log
        assert branch_log.replay().model_round == projection.model_round + 1
        copied = branch_log.replay().images[0]
        assert copied["createdRound"] == image_ref["createdRound"]
        assert (branch_log.path.parent / copied["contentRef"]).read_bytes()
        assert encoded not in branch_log.path.read_text()
        assert any(isinstance(message.content, list) for message in llm.requests[-1])
        await host.prompt(request(branch, "Third inference."))
        assert any(isinstance(message.content, list) and any(block.get("type") == "input_image" for block in message.content)
                   for message in llm.requests[-1])
        await host.prompt(request(branch, "Fourth inference."))
        assert not any(isinstance(message.content, list) and any(block.get("type") == "input_image" for block in message.content)
                       for message in llm.requests[-1])
        assert branch_log.replay().images[0]["evictionReason"] == "round_limit"
    finally:
        await host.aclose()
