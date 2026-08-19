"""Integration tests for the Box ACP adapter."""

import asyncio
import base64
import json
import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp import text_block, update_agent_message

from box_agent.acp import (
    BoxACPAgent,
    _inject_item_id,
    _latest_user_request_for_plan_detection,
    _looks_like_plan_approval_text,
    _tool_result_raw_output,
    _user_decision_response_from_meta,
)
from box_agent.acp.stdio_compat import _READ_LIMIT
from box_agent.completion import should_resume_pending_completion_gate
from box_agent.events import ContextCheckpointEvent, DoneEvent, StopReason
from box_agent.config import (
    AgentConfig,
    Config,
    FilesystemPermissions,
    ImageGenerationConfig,
    LLMConfig,
    MCPConfig,
    Officev3Config,
    Officev3Paths,
    Officev3Permissions,
    ToolLimitsConfig,
    ToolsConfig,
)
from box_agent.memory import MemoryManager
from box_agent.llm import SessionBoundLLM
from box_agent.loop_guards import CompletionGate
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, TokenUsage, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.bash_tool import BackgroundShellManager
from box_agent.tools.jupyter_tool import MAX_EXECUTE_CODE_CHARS
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader
from box_agent.tools.skill_tool import create_skill_tools
from box_agent.tools.setup import (
    SANDBOX_INFO_PROMPT,
    build_file_delivery_prompt,
    build_sandbox_info_prompt,
)
from box_agent.workflows import EXTERNAL_SKILL_WORKFLOW_KIND, recover_completion_gate
from box_agent.workspace_registry import WorkspaceRegistry


class DummyConn:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


def test_acp_context_summary_routes_auto_by_tags_and_locks_manual_model(tmp_path):
    class CloneTrackingLLM:
        model = "main-model"
        max_output_tokens = 64_000

        def __init__(self):
            self.clones = []

        def for_model(self, model, *, max_output_tokens=None):
            clone = SimpleNamespace(
                model=model,
                max_output_tokens=max_output_tokens,
            )
            self.clones.append(clone)
            return clone

    main_llm = CloneTrackingLLM()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        main_llm,
        [],
        "system",
    )
    auto_session_llm = SessionBoundLLM(main_llm)
    auto_session_llm.set_auto_model_candidates(
        [
            {
                "model": "analysis-model",
                "tags": ["analysis"],
                "abilityLevel": 3,
            },
            {
                "model": "summary-model",
                "tags": ["summary", "fast"],
                "abilityLevel": 1,
            },
        ]
    )

    summary_llm = agent._summary_llm_for_session(
        session_llm=auto_session_llm,
        session_id="session-1",
        title="Summary test",
        client_info=None,
    )

    assert len(main_llm.clones) == 1
    assert summary_llm.model == "summary-model"
    assert summary_llm.max_output_tokens == 4_096

    manual_llm = CloneTrackingLLM()
    locked_summary_llm = agent._summary_llm_for_session(
        session_llm=SessionBoundLLM(manual_llm),
        session_id="session-locked",
        title="Locked model",
        client_info=None,
    )
    assert locked_summary_llm.model == "main-model"
    assert manual_llm.clones[0].model == "main-model"


def test_acp_normalizes_structured_user_decision_response_meta():
    assert _user_decision_response_from_meta(
        {
            "userDecision": {
                "request_id": "decision-1",
                "decision_kind": "delivery_scope",
                "selected_option_id": "keep_full",
                "selected_option_label": "保持完整版本",
                "trigger": "timeout",
            }
        }
    ) == {
        "request_id": "decision-1",
        "decision_kind": "delivery_scope",
        "selected_option_id": "keep_full",
        "selected_option_label": "保持完整版本",
        "custom_text": "",
        "trigger": "timeout",
    }
    assert _user_decision_response_from_meta(
        {
            "user_decision": {
                "request_id": "decision-2",
                "decision_kind": "content_direction",
                "custom_text": "突出年度案例",
            }
        }
    ) == {
        "request_id": "decision-2",
        "decision_kind": "content_direction",
        "selected_option_id": "",
        "selected_option_label": "",
        "custom_text": "突出年度案例",
        "trigger": "user",
    }


@pytest.mark.asyncio
async def test_acp_workspace_config_methods_share_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(workspace)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system")

    saved = await agent.extMethod(
        "workspace/set",
        {"path": str(workspace), "taskType": "code"},
    )
    loaded = await agent.extMethod("workspace/get", {"path": str(workspace)})
    listed = await agent.extMethod("workspace/list", {})

    assert saved["workspace"]["task_type"] == "code"
    assert loaded["workspace"] == saved["workspace"]
    assert listed["workspaces"] == [saved["workspace"]]
    assert Path(saved["configPath"]) == (
        tmp_path / "home" / ".box-agent" / "config" / "workspaces.json"
    )


@pytest.mark.asyncio
async def test_acp_uses_saved_code_type_when_host_omits_session_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    WorkspaceRegistry().set(workspace, "code")
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(workspace)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=str(workspace), field_meta={}))
    state = agent._sessions[session.sessionId]

    assert state.session_mode == "code_agent"
    assert state.artifact_mode == "project"
    assert state.output_dir is None
    assert "Software Engineering Mode (code_agent)" in state.agent.system_prompt
    assert "Project Workspace Mode" in state.agent.system_prompt


class HangingConn:
    async def sessionUpdate(self, payload):
        await asyncio.Event().wait()


class DummyLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                thinking="calling echo",
                tool_calls=[
                    ToolCall(
                        id="tool1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ],
                finish_reason="tool",
            )
        return LLMResponse(content="done", thinking=None, tool_calls=None, finish_reason="stop")

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="thinking", delta="calling echo")
            yield StreamEvent(type="text", delta="calling tool")
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="tool1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")


class CorrelationCaptureLLM:
    def __init__(self):
        self.calls: list[dict] = []

    async def generate(self, messages, tools=None, **kwargs):
        self.calls.append({"mode": "generate", **kwargs})
        return LLMResponse(content="done", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls.append({"mode": "stream", **kwargs})
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class BackgroundBashLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="background-bash",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={
                                "command": "sleep 100",
                                "run_in_background": True,
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None, **_):
        return LLMResponse(content="done", finish_reason="stop")


class StagedBeginLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="staged-begin",
                        type="function",
                        function=FunctionCall(
                            name="staged_file_write",
                            arguments={"action": "begin", "path": "unfinished.html"},
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None, **_):
        return LLMResponse(content="done", finish_reason="stop")


class PointsInsufficientLLM:
    async def generate_stream(self, messages, tools=None, **kwargs):
        if False:
            yield StreamEvent(type="finish", finish_reason="stop")
        error = RuntimeError("Bad request")
        error.status_code = 400
        error.body = {
            "error": {"code": 1000007, "message": "insufficient_points"}
        }
        raise error

    async def generate(self, messages, tools=None, **kwargs):
        raise AssertionError("streaming path expected")


def test_acp_stdio_reader_allows_large_resume_frames():
    assert _READ_LIMIT >= 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_acp_session_update_send_times_out(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(HangingConn(), config, DummyLLM(), [], "system")
    agent._SESSION_UPDATE_TIMEOUT_SECONDS = 0.01

    with pytest.raises(TimeoutError, match="ACP session update timed out"):
        await agent._send("session-1", update_agent_message(text_block("hello")))


def test_sandbox_prompt_requires_execute_code_for_explicit_python_results():
    assert "用户要求“用/使用/运行 Python”得到一个具体结果" in SANDBOX_INFO_PROMPT
    assert "必须调用 `execute_code` 返回真实执行结果" in SANDBOX_INFO_PROMPT
    assert "不要只给代码示例" in SANDBOX_INFO_PROMPT


def test_sandbox_prompt_limits_single_execute_code_argument_size():
    assert (
        f"每次 `execute_code(code=...)` 控制在 {MAX_EXECUTE_CODE_CHARS} 字符以内"
        in SANDBOX_INFO_PROMPT
    )
    assert "共享样式/HTML/CSS/JS/JSON manifest/base64/生成文件正文" in SANDBOX_INFO_PROMPT
    assert "不要等到 `EXECUTE_CODE_TOO_LARGE` 后才拆" in SANDBOX_INFO_PROMPT
    assert "不要把大段内容塞进一个工具参数" in SANDBOX_INFO_PROMPT


def test_project_sandbox_prompt_does_not_point_at_output_dir():
    prompt = build_sandbox_info_prompt(use_output_dir=False)

    assert "当前工作区/代码项目根目录" in prompt
    assert "不要默认创建或使用 `output/`" in prompt
    assert "cwd 已是 `{workspace}/output/`" not in prompt


def test_file_delivery_prompt_uses_dynamic_loopback_preview_and_reclaims_it():
    prompt = build_file_delivery_prompt(use_output_dir=True)

    assert "Playwright MCP 不要打开 `file://`" in prompt
    assert "http.server 0 --bind 127.0.0.1" in prompt
    assert "bash_output" in prompt
    assert "bash_kill" in prompt
    assert "任务结束时 runtime 会兜底回收" in prompt


def test_acp_plan_approval_text_accepts_short_confirmations():
    assert _looks_like_plan_approval_text("好的")
    assert _looks_like_plan_approval_text("可以")
    assert not _looks_like_plan_approval_text("hello")


def test_latest_user_request_for_plan_detection_ignores_history_plan_snapshot():
    prompt = """以下是当前会话最近的上下文，请在此基础上继续回答：
用户:
text: 可以开始

助手:
plan: {"title": "执行计划", "steps": []}

用户问题：ok"""

    assert _latest_user_request_for_plan_detection(prompt) == "ok"


def test_latest_user_request_for_plan_detection_falls_back_to_last_user_block():
    prompt = """用户：
text: 先做这个

助手：
plan: {"title": "旧计划", "steps": []}

用户：
text: ok"""

    assert _latest_user_request_for_plan_detection(prompt) == "ok"


class TodoLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="todo1",
                        type="function",
                        function=FunctionCall(
                            name="todo_write",
                            arguments={
                                "action": "set",
                                "todos": [
                                    {
                                        "task": "Plan host integration",
                                        "status": "in_progress",
                                    },
                                    {
                                        "task": "Verify host snapshot",
                                        "status": "pending",
                                    },
                                ],
                            },
                        ),
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="todo2",
                        type="function",
                        function=FunctionCall(
                            name="todo_write",
                            arguments={
                                "action": "transition",
                                "todo_id": "1",
                                "next_todo_id": "2",
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="general", finish_reason="stop")


class PlanLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="plan1",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Plan host integration",
                                "objective": "Render plans separately from todo progress.",
                                "steps": [{"title": "Add plan_snapshot handling"}],
                                "verification": ["Check rawOutput.type"],
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="general", finish_reason="stop")


class PlanAfterRetryLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="text", delta="draft answer")
            yield StreamEvent(type="finish", finish_reason="stop")
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="plan-retry",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Forced host plan",
                                "objective": "Render a full plan after a forced host request.",
                                "steps": [{"title": "Publish plan_write"}],
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="general", finish_reason="stop")


class PlanApprovalThenEchoLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="plan-approval",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Approval-gated plan",
                                "objective": "Wait for host approval before echoing.",
                                "steps": [{"title": "Call echo after approval"}],
                            },
                        ),
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="echo-after-approval",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "approved"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="general", finish_reason="stop")


class DoneLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class EmptyFinalAnswerLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="empty-final-echo",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "evidence"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="finish", finish_reason="stop")


class SkillUsageLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="skill-1",
                        type="function",
                        function=FunctionCall(
                            name="get_skill",
                            arguments={"skill_name": "theme-factory"},
                        ),
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="skill-2",
                        type="function",
                        function=FunctionCall(
                            name="get_skill",
                            arguments={"skill_name": "html-templates"},
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class RepeatedSkillUsageLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        requested_skills = ["paid-skill", "paid-skill", "missing-skill"]
        if self.calls <= len(requested_skills):
            skill_name = requested_skills[self.calls - 1]
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id=f"skill-repeat-{self.calls}",
                        type="function",
                        function=FunctionCall(
                            name="get_skill",
                            arguments={"skill_name": skill_name},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class SkillTool(Tool):
    @property
    def name(self):
        return "get_skill"

    @property
    def description(self):
        return "Load a skill"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
        }

    async def execute(self, skill_name: str):
        if skill_name == "missing-skill":
            return ToolResult(success=False, content="", error="missing skill")
        return ToolResult(success=True, content=f"loaded {skill_name}")


class CapabilityUsageLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    ),
                    ToolCall(
                        id="mcp-1",
                        type="function",
                        function=FunctionCall(name="browser_open", arguments={"url": "https://example.com"}),
                    ),
                ],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(
                type="finish",
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            )

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class FakeMCPTool(Tool):
    @property
    def name(self):
        return "browser_open"

    @property
    def server_name(self):
        return "browser"

    @property
    def tool_name(self):
        return "open"

    @property
    def description(self):
        return "Open a browser URL"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"url": {"type": "string"}}}

    async def execute(self, url: str):
        return ToolResult(success=True, content=f"opened {url}")


class ParentPromptCaptureTool(Tool):
    def __init__(self):
        self.parent_system_prompt = ""

    @property
    def name(self):
        return "parent_prompt_capture"

    @property
    def description(self):
        return "Capture parent prompt updates"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    def set_parent_system_prompt(self, system_prompt: str) -> None:
        self.parent_system_prompt = system_prompt

    async def execute(self):
        return ToolResult(success=True, content="captured")


class CaptureMessagesLLM:
    def __init__(self):
        self.calls: list[list[tuple[str, str]]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.calls.append([(msg.role, msg.content) for msg in messages])
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class PreloadedSkillThenGetSkillLLM(CaptureMessagesLLM):
    async def generate_stream(self, messages, tools=None, **_):
        self.calls.append([(msg.role, msg.content) for msg in messages])
        if len(self.calls) == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="preloaded-skill",
                        type="function",
                        function=FunctionCall(
                            name="get_skill", arguments={"skill_name": "pptx"}
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class GoalCompleteLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="goal1",
                        type="function",
                        function=FunctionCall(
                            name="goal_write",
                            arguments={
                                "action": "complete",
                                "evidence": ["ACP goal completion test passed"],
                                "completed_by": "model",
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class GoalAutopilotCompleteLLM:
    def __init__(self):
        self.calls = 0
        self.messages: list[list[tuple[str, str]]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        self.messages.append([(msg.role, msg.content) for msg in messages])
        if self.calls == 1:
            yield StreamEvent(type="text", delta="not done yet")
            yield StreamEvent(type="finish", finish_reason="stop")
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="goal-auto-complete",
                        type="function",
                        function=FunctionCall(
                            name="goal_write",
                            arguments={
                                "action": "complete",
                                "evidence": ["autopilot continuation verified completion"],
                                "completed_by": "model",
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="goal done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class GoalAutopilotNeverCompleteLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        yield StreamEvent(type="text", delta=f"still active {self.calls}")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class GoalAutopilotBlockLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="text", delta="need more work")
            yield StreamEvent(type="finish", finish_reason="stop")
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="goal-auto-block",
                        type="function",
                        function=FunctionCall(
                            name="goal_write",
                            arguments={
                                "action": "block",
                                "blocked_reason": "Waiting for third-party credentials",
                                "progress": ["Detected provider authentication gap"],
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="blocked")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class PrematurePptLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="text", delta="现在开始制作 PPT：")
            yield StreamEvent(type="finish", finish_reason="stop")
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="ppt-write",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "output/bid-proposal.pptx",
                                "content": "fake pptx payload",
                            },
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="PPT 已生成。")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class CompleteDeckTool(Tool):
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.calls = 0

    @property
    def name(self):
        return "complete_deck"

    @property
    def description(self):
        return "Writes a deterministic controlled-deck fixture"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        self.calls += 1
        qa_dir = self.output_dir / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "index.html").write_text(
            "<html><body><section class='slide'>ready</section></body></html>",
            encoding="utf-8",
        )
        for name in (
            "outline_check.json",
            "deck_contract.json",
            "deck_spec.json",
            "truth_check.json",
            "image_manifest.json",
            "html_self_check.json",
            "runtime_probe.json",
        ):
            (qa_dir / name).write_text('{"ok": true}', encoding="utf-8")
        return ToolResult(success=True, content="controlled deck complete")


class ClarifyThenResumePptLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="ask-market-size",
                        type="function",
                        function=FunctionCall(
                            name="request_user_input",
                            arguments={
                                "question": "请补充市场规模口径。",
                                "missing_fields": ["TAM", "SAM", "SOM"],
                                "reason": "融资路演不能虚构市场数据。",
                            },
                        ),
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(type="text", delta="请补充 TAM、SAM 和 SOM。")
            yield StreamEvent(type="finish", finish_reason="stop")
        elif self.calls == 3:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="complete-deck",
                        type="function",
                        function=FunctionCall(name="complete_deck", arguments={}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="已根据补充数据继续完成 HTML。")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class UsageLLM:
    """Mimics the ``LLMClient`` choke point: records usage on each finish.

    Used to verify the per-turn token meter flows into the prompt
    response ``_meta.usage`` without depending on a live provider.
    """

    def __init__(self, per_call_total: int = 30):
        self._per_call_total = per_call_total

    async def generate_stream(self, messages, tools=None, **_):
        from box_agent.llm.token_meter import record_usage
        from box_agent.schema import TokenUsage

        yield StreamEvent(type="text", delta="done")
        usage = TokenUsage(total_tokens=self._per_call_total)
        record_usage(usage)
        yield StreamEvent(type="finish", finish_reason="stop", usage=usage)

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


class LongAnswerLLM:
    async def generate_stream(self, messages, tools=None, **_):
        for chunk in ["李白是唐代诗人，" * 20, "他的诗歌想象瑰丽，" * 20, "后世称他为诗仙。"]:
            yield StreamEvent(type="text", delta=chunk)
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="", finish_reason="stop")


class MalformedActionHintLLM:
    async def generate_stream(self, messages, tools=None, **_):
        yield StreamEvent(type="text", delta="好的。")
        yield StreamEvent(type="text", delta="```action")
        yield StreamEvent(
            type="text",
            delta='_hint { "action": "open_settings", "params": {"tab": "onboarding"}, ',
        )
        yield StreamEvent(
            type="activity",
            activity={"protocol": "agent_activity_v1", "phase": "provider_stream"},
        )
        yield StreamEvent(
            type="text",
            delta='"display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。\n" } ```',
        )
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="", finish_reason="stop")


class SubAgentLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="sub1",
                        type="function",
                        function=FunctionCall(name="sub_agent", arguments={"task": "Inspect one file", "title": "file probe"}),
                    )
                ],
            )
        elif self.calls == 2:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="child1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "child"}),
                    )
                ],
            )
        elif self.calls == 3:
            yield StreamEvent(type="text", delta="child summary")
            yield StreamEvent(type="finish", finish_reason="stop")
        else:
            yield StreamEvent(type="text", delta="parent done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="general", finish_reason="stop")


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echo helper"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str):
        return ToolResult(success=True, content=f"tool:{text}")


@pytest.mark.asyncio
async def test_mcp_reconnect_injects_hidden_deferred_state_into_active_turns_only(
    tmp_path,
    monkeypatch,
):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_sub_agent=False,
            mcp=MCPConfig(deferred_loading_enabled=True),
        ),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system")
    active_session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={})
    )
    inactive_session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={})
    )
    active_state = agent._sessions[active_session.sessionId]
    inactive_state = agent._sessions[inactive_session.sessionId]
    active_state.turn_active = True
    runtime_tool = EchoTool()
    runtime_tool.mcp_always_load = True

    async def reconnect(_name):
        return {"success": True, "toolCount": 1, "tools": ["echo"]}

    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.reconnect_mcp_server",
        reconnect,
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.get_mcp_tools_for_server",
        lambda _name: [runtime_tool],
    )

    result = await agent.extMethod("mcp/reconnect", {"name": "demo"})

    assert result["success"] is True
    assert "echo" not in active_state.agent.tools
    assert "echo" not in inactive_state.agent.tools
    update = active_state.inject_queue.get_nowait()
    assert update["user_visible"] is False
    assert update["source"] == "runtime"
    assert "connected" in update["content"]
    assert "deferred catalog" in update["content"]
    assert "alwaysLoad tool(s) are already visible" in update["content"]
    assert "tool_search" in update["content"]
    assert inactive_state.inject_queue.empty()


@pytest.mark.asyncio
async def test_acp_auth_refresh_reconnects_and_injects_deferred_catalog_update(
    tmp_path,
    monkeypatch,
):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_sub_agent=False,
            mcp=MCPConfig(deferred_loading_enabled=True),
        ),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system")
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]
    state.turn_active = True
    runtime_tool = EchoTool()
    runtime_tool.mcp_always_load = False

    async def reconnect_changed():
        return [{"name": "hosted", "success": True}]

    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.reconnect_auth_failed_mcp_servers_if_token_changed",
        reconnect_changed,
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.get_mcp_tools_for_server",
        lambda _name: [runtime_tool],
    )

    result = await agent._refresh_mcp_after_auth_change()

    assert result == [{"name": "hosted", "success": True}]
    update = state.inject_queue.get_nowait()
    assert update["source"] == "runtime"
    assert "hosted" in update["content"]
    assert "tool_search" in update["content"]


@pytest.mark.asyncio
async def test_acp_prompt_checks_for_mcp_auth_refresh(acp_agent, monkeypatch):
    agent, _ = acp_agent
    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={}))
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(agent, "_refresh_mcp_after_auth_change", refresh)

    await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    )

    assert calls == 1


@pytest.mark.asyncio
async def test_initial_mcp_catalog_ready_is_injected_into_active_turn(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_sub_agent=False,
            mcp=MCPConfig(deferred_loading_enabled=True),
        ),
    )
    mcp_task = asyncio.create_task(asyncio.sleep(0, result=[EchoTool()]))
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DummyLLM(),
        [],
        "system",
        mcp_task=mcp_task,
    )
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]
    state.turn_active = True

    await agent._finalize_mcp_load()

    update = state.inject_queue.get_nowait()
    assert update["user_visible"] is False
    assert "catalog discovery is complete" in update["content"]
    assert "Retry tool_search" in update["content"]
    assert "ordinary deferred schemas remain hidden" in update["content"]


@pytest.mark.asyncio
async def test_mcp_disconnect_does_not_remove_stable_tools_in_deferred_mode(
    tmp_path,
    monkeypatch,
):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_sub_agent=False,
            mcp=MCPConfig(deferred_loading_enabled=True),
        ),
    )
    stable = EchoTool()
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [stable], "system")
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]

    async def disconnect(_name):
        return {"success": True, "removedTools": ["echo", "tool_search"]}

    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.disconnect_mcp_server",
        disconnect,
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.get_all_mcp_tools",
        lambda: [],
    )

    result = await agent.extMethod("mcp/disconnect", {"name": "demo"})

    assert result["success"] is True
    assert any(tool.name == "echo" for tool in agent._base_tools)
    assert state.agent.tools["echo"] is stable
    assert "tool_search" in state.agent.tools


@pytest.mark.asyncio
async def test_mcp_disconnect_restores_stable_tool_in_eager_mode(
    tmp_path,
    monkeypatch,
):
    from box_agent.tools.setup import sync_mcp_tool_list, sync_mcp_tools

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_sub_agent=False,
            mcp=MCPConfig(deferred_loading_enabled=False),
        ),
    )
    stable = EchoTool()
    remote = EchoTool()
    remote.mcp_tool_id = "mcp:demo/echo"
    remote.server_name = "demo"
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [stable], "system")
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]
    sync_mcp_tool_list(
        agent._base_tools,
        [remote],
        agent._base_mcp_fallback_tools,
    )
    sync_mcp_tools(
        state.agent.tools,
        [remote],
        state.mcp_fallback_tools,
    )
    assert agent._base_tools[0] is remote
    assert state.agent.tools["echo"] is remote

    async def disconnect(_name):
        return {"success": True, "removedTools": ["echo"]}

    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.disconnect_mcp_server",
        disconnect,
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.get_all_mcp_tools",
        lambda: [],
    )

    result = await agent.extMethod("mcp/disconnect", {"name": "demo"})

    assert result["success"] is True
    assert agent._base_tools == [stable]
    assert state.agent.tools["echo"] is stable


@pytest.mark.asyncio
async def test_mcp_reconnect_failure_injects_non_availability_state(
    tmp_path,
    monkeypatch,
):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system")
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
    state = agent._sessions[session.sessionId]
    state.turn_active = True

    async def reconnect(_name):
        return {"success": False, "error": "connection failed"}

    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.reconnect_mcp_server",
        reconnect,
    )

    result = await agent.extMethod("mcp/reconnect", {"name": "demo"})

    assert result["success"] is False
    update = state.inject_queue.get_nowait()
    assert update["user_visible"] is False
    assert "did not connect" in update["content"]
    assert "connection failed" not in update["content"]


class CountingWebSearchTool(Tool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "web_search"

    @property
    def description(self):
        return "Searches the web"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, query: str = ""):
        self.calls += 1
        return ToolResult(success=True, content=f"result:{query}")


class WebBudgetLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls <= 25:
            index = self.calls - 1
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id=f"web-{index}",
                        type="function",
                        function=FunctionCall(name="web_search", arguments={"query": f"q{index}"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="final from gathered evidence")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="final from gathered evidence", finish_reason="stop")


@pytest.fixture
def acp_agent(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DummyLLM(), [EchoTool()], "system")
    return agent, conn


@pytest.mark.asyncio
async def test_acp_binds_cumulative_real_user_text_to_bash_env(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "原始事实 A"}])
    )
    await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "补充事实 B"}])
    )

    bash_env = agent._sessions[session.sessionId].agent.tools["bash"]._subprocess_env
    source_text = base64.b64decode(bash_env["BOX_AGENT_SOURCE_TEXT_B64"]).decode("utf-8")
    assert source_text == "原始事实 A\n\n补充事实 B"


@pytest.mark.asyncio
async def test_acp_prompt_reclaims_background_bash_at_turn_end(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, BackgroundBashLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "run server"}])
    )

    assert response.stopReason == "end_turn"
    assert BackgroundShellManager.get_available_ids(session.sessionId) == []


@pytest.mark.asyncio
async def test_acp_prompt_discards_uncommitted_staged_writes_at_turn_end(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, StagedBeginLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "start write"}])
    )

    assert response.stopReason == "end_turn"
    assert list(tmp_path.rglob("*.part")) == []


@pytest.mark.asyncio
async def test_acp_restored_session_binds_user_history_without_assistant_text(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[
                {
                    "text": (
                        "以下是当前会话最近的上下文，请在此基础上继续回答：\n"
                        "用户:\ntext: 项目为：某连锁零售集团智能客服升级。\n\n"
                        "助手:\ntext: 内部推断，不应成为事实来源。\n\n"
                        "用户问题：继续完成 PPT"
                    )
                }
            ],
        )
    )

    bash_env = agent._sessions[session.sessionId].agent.tools["bash"]._subprocess_env
    source_text = base64.b64decode(bash_env["BOX_AGENT_SOURCE_TEXT_B64"]).decode("utf-8")
    assert source_text == (
        "项目为：某连锁零售集团智能客服升级。\n\n继续完成 PPT"
    )
    assert "内部推断" not in source_text


@pytest.mark.asyncio
async def test_acp_turn_executes_tool(acp_agent):
    agent, conn = acp_agent
    # Explicit session_mode is consumed at session creation; DummyLLM's first
    # response is consumed by the main agent loop as designed.
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    prompt = SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "end_turn"
    assert response.field_meta["ok"] is True
    assert "errorCode" not in response.field_meta
    assert "errorCategory" not in response.field_meta
    assert any("tool:ping" in str(update) for update in conn.updates)
    llm_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "llm_output"
    ]
    assert [item["finish_reason"] for item in llm_outputs] == ["tool", "stop"]
    assert llm_outputs[0]["content"] == "calling tool"
    assert llm_outputs[0]["thinking"] == "calling echo"
    assert llm_outputs[0]["tool_calls"][0]["function"]["name"] == "echo"
    assert llm_outputs[1]["content"] == "done"
    message_chunks = [
        (i, update.update.content.text)
        for i, update in enumerate(conn.updates)
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    ]
    assert "calling tool" in [text for _, text in message_chunks]
    tool_index = _first_tool_call_index(conn.updates)
    assert tool_index != -1
    assert next(i for i, text in message_chunks if text == "calling tool") < tool_index
    progress_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "agent_progress"
    ]
    assert progress_outputs == []
    await agent.cancel(SimpleNamespace(sessionId=session.sessionId))
    assert agent._sessions[session.sessionId].cancelled


@pytest.mark.asyncio
async def test_acp_prompt_exposes_structured_points_insufficient_error(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, PointsInsufficientLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "hello"}])
    )

    assert response.stopReason == "end_turn"
    assert response.field_meta["ok"] is False
    assert response.field_meta["errorCode"] == 1000007
    assert response.field_meta["errorCategory"] == "quota"


@pytest.mark.asyncio
async def test_acp_emits_skills_usage_raw_output(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=4, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, SkillUsageLLM(), [SkillTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "make infographic"}])
    )

    assert response.stopReason == "end_turn"
    skill_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "skills_usage"
    ]
    assert skill_outputs == [
        {
            "type": "skills_usage",
            "skills": ["theme-factory"],
            "current": "theme-factory",
        },
        {
            "type": "skills_usage",
            "skills": ["theme-factory", "html-templates"],
            "current": "html-templates",
        },
    ]
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert any(item["skills"] == ["theme-factory"] for item in turn_usage_outputs)
    assert turn_usage_outputs[-1]["skills"] == ["theme-factory", "html-templates"]
    assert turn_usage_outputs[-1]["version"] == 3
    assert [
        {
            key: invocation[key]
            for key in ("skillName", "activationSource", "status")
        }
        for invocation in turn_usage_outputs[-1]["skillInvocations"]
    ] == [
        {
            "skillName": "theme-factory",
            "activationSource": "get_skill",
            "status": "succeeded",
        },
        {
            "skillName": "html-templates",
            "activationSource": "get_skill",
            "status": "succeeded",
        },
    ]
    assert len(
        {
            invocation["invocationId"]
            for invocation in turn_usage_outputs[-1]["skillInvocations"]
        }
    ) == 2


@pytest.mark.asyncio
async def test_acp_skill_invocations_are_idempotent_and_keep_context(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "paid-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: paid-skill\n"
        "description: A versioned installable skill.\n"
        "metadata:\n"
        '  version: "1.2.3"\n'
        "---\n"
        "# Paid skill instructions\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=5, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(
        conn,
        config,
        RepeatedSkillUsageLLM(),
        [SkillTool()],
        "system",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "session_id": "office-session-skill",
                "expert": {"id": "expert-a", "name": "Expert A"},
                "expert_team": {"id": "team-a", "name": "Team A"},
            },
        )
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "use paid skill twice, then try a missing skill"}],
            field_meta={"turnId": "office-turn-skill"},
        )
    )

    assert response.stopReason == "end_turn"
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    final_invocations = turn_usage_outputs[-1]["skillInvocations"]
    assert len(final_invocations) == 1
    invocation = final_invocations[0]
    assert invocation["skillName"] == "paid-skill"
    assert invocation["skillVersion"] == "1.2.3"
    assert invocation["skillSource"] == "builtin"
    assert invocation["activationSource"] == "get_skill"
    assert invocation["status"] == "succeeded"
    assert invocation["usageRole"] == "primary"
    assert invocation["contextExpertId"] == "expert-a"
    assert invocation["contextTeamId"] == "team-a"
    assert invocation["instructionDigest"] == sha256(skill_file.read_bytes()).hexdigest()
    invocation_ids = {
        item["invocationId"]
        for payload in turn_usage_outputs
        for item in payload["skillInvocations"]
    }
    assert invocation_ids == {invocation["invocationId"]}
    assert all(
        item["skillName"] != "missing-skill"
        for payload in turn_usage_outputs
        for item in payload["skillInvocations"]
    )


@pytest.mark.asyncio
async def test_acp_emits_turn_usage_for_tools_mcp_and_tokens(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(
        conn,
        config,
        CapabilityUsageLLM(),
        [EchoTool(), FakeMCPTool()],
        "system",
    )

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "session_id": "office-session-a",
            },
        )
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "open and echo"}],
            field_meta={"turnId": "office-turn-1"},
        )
    )

    assert response.stopReason == "end_turn"
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert any(item["tools"] == [{"name": "echo", "count": 1}] for item in turn_usage_outputs)
    assert any(
        item["mcp"]
        == [{"server": "browser", "tool": "open", "name": "browser.open", "count": 1}]
        for item in turn_usage_outputs
    )
    assert turn_usage_outputs[-1]["sessionId"] == "office-session-a"
    assert turn_usage_outputs[-1]["session_id"] == "office-session-a"
    assert turn_usage_outputs[-1]["acpSessionId"] == session.sessionId
    assert turn_usage_outputs[-1]["turnId"] == "office-turn-1"
    assert turn_usage_outputs[-1]["turn_id"] == "office-turn-1"
    assert turn_usage_outputs[-1]["tokenUsage"] == {
        "promptTokens": 12,
        "completionTokens": 6,
        "totalTokens": 18,
        "calls": 2,
    }


@pytest.mark.asyncio
async def test_acp_threads_session_turn_and_title_to_llm(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(tmp_path),
            enable_memory_extraction=False,
        ),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    llm = CorrelationCaptureLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "session_id": "office-session-a",
                "title": "Quarterly review",
            },
        )
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "summarize"}],
            field_meta={"turnId": "office-turn-1"},
        )
    )

    stream_calls = [call for call in llm.calls if call["mode"] == "stream"]
    assert stream_calls[0]["session_id"] == "office-session-a"
    assert stream_calls[0]["turn_id"] == "office-turn-1"
    assert stream_calls[0]["title"] == "Quarterly review"

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "continue"}],
            field_meta={"sessionTitle": "Updated review"},
        )
    )

    stream_calls = [call for call in llm.calls if call["mode"] == "stream"]
    assert stream_calls[1]["session_id"] == "office-session-a"
    assert stream_calls[1]["turn_id"] == f"{session.sessionId}-turn-2"
    assert stream_calls[1]["title"] == "Updated review"


@pytest.mark.asyncio
async def test_acp_goal_ext_method_injects_active_goal_into_prompt(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(tmp_path),
            goal_autopilot_enabled=False,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    set_result = await agent.extMethod(
        "goal",
        {
            "sessionId": session.sessionId,
            "action": "set",
            "objective": "Make the ACP goal flow verifiable",
        },
    )
    assert set_result["ok"] is True
    assert set_result["goal"]["status"] == "active"

    await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "continue"}]))

    latest_user = [content for role, content in llm.calls[-1] if role == "user"][-1]
    assert "## Active Goal" in latest_user
    assert "Make the ACP goal flow verifiable" in latest_user
    assert "## Latest User Message" in latest_user
    assert "continue" in latest_user


@pytest.mark.asyncio
async def test_acp_goal_pause_stops_prompt_injection(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Keep this goal available", "status": "active"},
            },
        )
    )
    pause_result = await agent.extMethod("goal", {"sessionId": session.sessionId, "action": "pause"})
    assert pause_result["goal"]["status"] == "paused"

    await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "side question"}]))

    latest_user = [content for role, content in llm.calls[-1] if role == "user"][-1]
    assert latest_user == "side question"


@pytest.mark.asyncio
async def test_acp_new_session_goal_meta_restores_goal_state(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {
                    "objective": "Restore this officev3 goal",
                    "status": "paused",
                    "progress": ["Host persisted progress"],
                    "evidence": ["Host persisted evidence"],
                    "blockedReason": "Host paused for review",
                },
            },
        )
    )

    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.objective == "Restore this officev3 goal"
    assert state.agent.goal.status == "paused"
    assert state.agent.goal.progress == ["Host persisted progress"]
    assert state.agent.goal.evidence == ["Host persisted evidence"]
    assert state.agent.goal.blocked_reason == "Host paused for review"
    assert session.field_meta["goal"]["status"] == "paused"
    assert session.field_meta["goal"]["progress"] == ["Host persisted progress"]
    assert session.field_meta["goal"]["blockedReason"] == "Host paused for review"


@pytest.mark.asyncio
async def test_acp_goal_write_tool_completes_goal_and_emits_snapshot(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, GoalCompleteLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Let the model complete this goal", "status": "active"},
            },
        )
    )
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "finish it"}]))

    assert response.stopReason == "end_turn"
    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.status == "complete"
    assert state.agent.goal.evidence == ["ACP goal completion test passed"]
    assert state.agent.goal.completed_by == "model"
    goal_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "goal_snapshot"
    ]
    assert goal_outputs
    assert goal_outputs[-1]["action"] == "complete"
    assert goal_outputs[-1]["goal"]["status"] == "complete"
    assert goal_outputs[-1]["goal"]["evidence"] == ["ACP goal completion test passed"]
    assert goal_outputs[-1]["goal"]["completedBy"] == "model"


@pytest.mark.asyncio
async def test_acp_goal_autopilot_continues_active_goal_until_complete(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(tmp_path),
            goal_autopilot_max_turns=2,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = GoalAutopilotCompleteLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Keep working until model completes", "status": "active"},
            },
        )
    )
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "start"}]))

    assert response.stopReason == "end_turn"
    assert response.field_meta["goalAutopilot"]["continuations"] == 1
    assert response.field_meta["goalAutopilot"]["budgetExhausted"] is False
    assert llm.calls == 3
    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.status == "complete"
    assert state.agent.goal.evidence == ["autopilot continuation verified completion"]
    continuation_user = [content for role, content in llm.messages[1] if role == "user"][-1]
    assert "Goal autopilot continuation 1/2" in continuation_user
    assert "## Active Goal" in continuation_user


@pytest.mark.asyncio
async def test_acp_goal_autopilot_stops_when_budget_exhausted(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(tmp_path),
            goal_autopilot_max_turns=1,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    llm = GoalAutopilotNeverCompleteLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Remain active after budget", "status": "active"},
            },
        )
    )
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "start"}]))

    assert response.stopReason == "max_turn_requests"
    assert response.field_meta["goalAutopilot"]["continuations"] == 1
    assert response.field_meta["goalAutopilot"]["budgetExhausted"] is True
    assert response.field_meta["goalAutopilot"]["noProgressExhausted"] is False
    assert llm.calls == 2
    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.status == "active"


@pytest.mark.asyncio
async def test_acp_goal_autopilot_stops_after_repeated_no_progress(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(tmp_path),
            goal_autopilot_max_turns=3,
            goal_autopilot_no_progress_turns=2,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    llm = GoalAutopilotNeverCompleteLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Remain active with no progress", "status": "active"},
            },
        )
    )
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "start"}]))

    assert response.stopReason == "max_turn_requests"
    assert response.field_meta["goalAutopilot"]["continuations"] == 2
    assert response.field_meta["goalAutopilot"]["budgetExhausted"] is False
    assert response.field_meta["goalAutopilot"]["noProgressExhausted"] is True
    assert response.field_meta["goalAutopilot"]["noProgressTurns"] == 2
    assert llm.calls == 3
    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.status == "active"


@pytest.mark.asyncio
async def test_acp_goal_autopilot_stops_when_goal_blocks(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(tmp_path),
            goal_autopilot_max_turns=3,
        ),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    llm = GoalAutopilotBlockLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "goal": {"objective": "Validate provider integration", "status": "active"},
            },
        )
    )
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "start"}]))

    assert response.stopReason == "end_turn"
    assert response.field_meta["goalAutopilot"]["continuations"] == 1
    assert response.field_meta["goalAutopilot"]["budgetExhausted"] is False
    assert llm.calls == 3
    state = agent._sessions[session.sessionId]
    assert state.agent.goal is not None
    assert state.agent.goal.status == "blocked"
    assert state.agent.goal.blocked_reason == "Waiting for third-party credentials"
    assert state.agent.goal.progress == ["Detected provider authentication gap"]


@pytest.mark.asyncio
async def test_acp_project_artifact_mode_does_not_create_output(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system {SANDBOX_INFO}")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"artifact_mode": "project"})
    )
    state = agent._sessions[session.sessionId]

    assert not (tmp_path / "output").exists()
    assert state.output_dir is None
    assert state.artifact_mode == "project"
    assert "Do not create or use an `output/` folder" in state.agent.system_prompt
    assert "当前工作区/代码项目根目录" in state.agent.system_prompt
    assert "{SANDBOX_INFO}" not in state.agent.system_prompt
    assert session.field_meta["artifact_mode"] == "project"


@pytest.mark.asyncio
async def test_acp_project_artifact_mode_keeps_host_execution_gate(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"artifact_mode": "project"})
    )
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[
                {
                    "text": (
                        "用户问题：Implement the assigned code task.\n"
                        '<host_execution_contract acceptance_criteria_count="2">\n'
                        "Report every criterion before ending.\n"
                        "</host_execution_contract>"
                    )
                }
            ],
        )
    )

    assert response.stopReason == "end_turn"
    gate = captured["gate"]
    assert isinstance(gate, CompletionGate)
    assert "report_execution_result" in gate.required_tools
    assert gate.execution_result_criteria_count == 2


@pytest.mark.asyncio
async def test_acp_default_artifact_mode_creates_output(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system {SANDBOX_INFO}")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]

    assert (tmp_path / "output").is_dir()
    assert state.output_dir == str(tmp_path / "output")
    assert state.artifact_mode == "output"
    assert "cwd 已是 `{workspace}/output/`" in state.agent.system_prompt
    assert session.field_meta is None


@pytest.mark.asyncio
async def test_acp_injects_standard_box_agent_image_generation_policy(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
        image_generation=ImageGenerationConfig(
            endpoint="https://image.example.test/api/web/llm/v2/images/gen"
        ),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"})
    )
    prompt = agent._sessions[session.sessionId].agent.system_prompt

    assert "## Native Image Generation" in prompt
    assert "CLI 与 ACP 共用" in prompt
    assert "不由宿主 `env_context` 控制" in prompt
    assert "当前生图服务：已配置" in prompt


@pytest.mark.asyncio
async def test_acp_uses_host_artifact_root_dir_for_output_mode(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(allow_full_access=True),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")
    artifact_root = tmp_path / "session-a" / "output"

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "session_id": "office-session-a",
                "workspace_layout": {"artifact_root_dir": str(artifact_root)},
            },
        )
    )
    state = agent._sessions[session.sessionId]
    sandbox_tool = state.agent.tools["execute_code"]

    assert artifact_root.is_dir()
    assert state.output_dir == str(artifact_root.resolve())
    assert state.upstream_session_id == "office-session-a"
    assert not (tmp_path / "output").exists()
    assert sandbox_tool._get_workspace("ignored") == artifact_root.resolve()


def test_acp_artifact_raw_output_gets_session_metadata():
    output = _tool_result_raw_output(
        {"type": "artifact", "filename": "deck.pptx", "abs_path": "/tmp/deck.pptx"},
        "[OK] done",
        None,
        session_id="office-session-a",
        output_dir="/tmp/session-a/output",
    )

    assert output["session_id"] == "office-session-a"
    assert output["sessionId"] == "office-session-a"
    assert output["output_dir"] == "/tmp/session-a/output"


@pytest.mark.asyncio
async def test_acp_streams_long_plain_answer_chunks(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, LongAnswerLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "介绍李白"}])
    )

    assert response.stopReason == "end_turn"
    message_chunks = [
        update
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    ]
    assert message_chunks
    streamed_text = "".join(chunk.update.content.text for chunk in message_chunks)
    assert "李白是唐代诗人" in streamed_text
    assert "后世称他为诗仙" in streamed_text


@pytest.mark.asyncio
async def test_acp_normalizes_action_hint_chunks_across_activity(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, MalformedActionHintLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "你好"}])
    )

    assert response.stopReason == "end_turn"
    message_chunks = [
        update
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    ]
    streamed_text = "".join(chunk.update.content.text for chunk in message_chunks)
    assert "```action_hint {" not in streamed_text
    assert streamed_text.startswith("好的。```action_hint\n")
    payload = streamed_text.split("```action_hint\n", 1)[1].removesuffix("\n```")
    expected_hint = {
        "action": "open_settings",
        "params": {"tab": "onboarding"},
        "display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。",
    }
    assert json.loads(payload) == expected_hint

    llm_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "llm_output"
    ]
    assert "```action_hint {" not in llm_outputs[-1]["content"]
    assert json.loads(
        llm_outputs[-1]["content"].split("```action_hint\n", 1)[1].removesuffix("\n```")
    ) == expected_hint

    assistant_messages = [
        msg.content
        for msg in agent._sessions[session.sessionId].agent.messages
        if msg.role == "assistant"
    ]
    assert "```action_hint {" not in assistant_messages[-1]
    assert json.loads(
        assistant_messages[-1].split("```action_hint\n", 1)[1].removesuffix("\n```")
    ) == expected_hint


@pytest.mark.asyncio
async def test_acp_marks_injected_message_at_step_boundary(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    await state.inject_queue.put({"id": "inj-1", "content": "生成10页就可以了"})

    stop_reason = await agent._run_turn(state, session.sessionId)

    assert stop_reason == "end_turn"
    rendered = "\n".join(str(update) for update in conn.updates)
    assert "[Injected:inj-1] 生成10页就可以了" in rendered
    assert "done" in rendered


@pytest.mark.asyncio
async def test_acp_auto_completion_gate_continues_until_ppt_artifact(tmp_path):
    old = tmp_path / "output" / "old.html"
    old.parent.mkdir()
    old.write_text("old")
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=5, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = PrematurePptLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "做一份 15 页售前竞标方案，导出 PPTX 文件"}],
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 3
    assert (tmp_path / "output" / "bid-proposal.pptx").read_text() == "fake pptx payload"
    rendered = "\n".join(str(update) for update in conn.updates)
    assert "PPT 已生成" in rendered
    assert "尚未满足完成条件" not in rendered


@pytest.mark.asyncio
async def test_acp_completion_gate_ignores_historical_deliverable_on_plain_continue(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=5, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = DoneLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[
                {
                    "text": (
                        "以下是当前会话最近的上下文，请在此基础上继续回答：\n"
                        "用户:\ntext: 做一份 15 页售前竞标方案 PPT\n\n"
                        "助手:\ngenerate: 我已经完成了初稿。\n\n"
                        "用户问题：继续"
                    )
                }
            ],
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 1
    rendered = "\n".join(str(update) for update in conn.updates)
    assert "尚未满足完成条件" not in rendered


def test_pending_completion_gate_resume_detection_is_session_scoped():
    assert should_resume_pending_completion_gate(
        "输出 HTML",
        waiting_for_user_input=False,
    )
    assert should_resume_pending_completion_gate(
        "TAM 120 亿元，SAM 30 亿元，SOM 3 亿元",
        waiting_for_user_input=True,
    )
    assert not should_resume_pending_completion_gate(
        "这个主题色是什么意思？",
        waiting_for_user_input=False,
    )
    assert not should_resume_pending_completion_gate(
        "取消，不用继续",
        waiting_for_user_input=True,
    )
    assert not should_resume_pending_completion_gate(
        "优化这个“继续制作 PPT 并输出 HTML”的提示词",
        waiting_for_user_input=True,
    )


def test_recover_controlled_presentation_gate_from_deep_research_checkpoint(tmp_path):
    research = tmp_path / "output" / "research"
    research.mkdir(parents=True)
    (research / "topic_insight.md").write_text("validated evidence", encoding="utf-8")

    gate = recover_completion_gate(tmp_path)

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.workflow_options["research_mode"] == "deep"


def test_recover_controlled_presentation_gate_from_source_first_outline(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")

    gate = recover_completion_gate(tmp_path)

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.workflow_options["research_mode"] == "auto"


def test_recover_controlled_presentation_gate_does_not_reopen_complete_deck(tmp_path):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    (output / "index.html").write_text("<html></html>", encoding="utf-8")
    for report_name in (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    ):
        (qa / report_name).write_text('{"ok": true}', encoding="utf-8")

    gate = recover_completion_gate(tmp_path)

    assert gate is None


@pytest.mark.asyncio
async def test_acp_recovers_controlled_deck_after_new_session_from_filesystem(tmp_path):
    research = tmp_path / "output" / "research"
    research.mkdir(parents=True)
    (research / "topic_insight.md").write_text("validated evidence", encoding="utf-8")
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[
                {
                    "text": (
                        "继续完成 PPT。沿用已经通过 QA 的 research，不要重复搜索；"
                        "从当前文件系统检查点继续，交付 index.html。"
                    )
                }
            ],
        )
    )

    assert response.stopReason == "end_turn"
    assert captured["gate"] is not None
    assert captured["gate"] is state.pending_completion_gate
    assert state.pending_completion_gate.workflow_options["research_mode"] == "deep"


@pytest.mark.asyncio
async def test_acp_output_html_followup_reuses_pending_presentation_gate(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    pending_gate = CompletionGate(
        required_changed_artifact_globs=("output/**/*.html",),
        workflow_checkpoint_kind="controlled_presentation",
    )
    state.pending_completion_gate = pending_gate
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "输出 HTML"}],
        )
    )

    assert response.stopReason == "end_turn"
    assert captured["gate"] is pending_gate
    assert state.pending_completion_gate is pending_gate
    assert response.field_meta["deliveryStatus"] == "incomplete"
    assert response.field_meta["recoverable"] is True
    assert response.field_meta["deliveryGaps"]
    assert response.field_meta["ok"] is True
    assert response.field_meta["runStatus"] == "incomplete"
    assert response.field_meta["completed"] is False


@pytest.mark.asyncio
async def test_acp_checkpoint_pause_overrides_incomplete_and_maps_metadata(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    state.pending_completion_gate = CompletionGate(
        required_changed_artifact_globs=("output/**/*.pptx",),
        workflow_checkpoint_kind="controlled_presentation",
    )

    async def checkpoint_pause(state_arg, session_id, **kwargs):
        state_arg.last_checkpoint = {
            "type": "context_checkpoint",
            "checkpointId": "checkpoint-1",
            "workflowKind": "controlled_presentation",
            "recoverable": True,
        }
        return "checkpoint_paused"

    agent._run_turn = checkpoint_pause  # type: ignore[method-assign]

    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "继续输出 PPTX"}],
        )
    )

    assert response.stopReason == "end_turn"
    assert response.field_meta["ok"] is True
    assert response.field_meta["runStatus"] == "paused"
    assert response.field_meta["completed"] is False
    assert response.field_meta["paused"] is True
    assert response.field_meta["deliveryStatus"] == "paused"
    assert response.field_meta["deliveryGaps"] == []
    assert response.field_meta["recoverable"] is True
    assert response.field_meta["checkpoint"]["checkpointId"] == "checkpoint-1"
    assert response.field_meta["lastStopReason"] == "checkpoint_paused"


@pytest.mark.asyncio
async def test_acp_run_turn_maps_context_checkpoint_event_to_host_metadata(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]

    async def checkpoint_events(*args, **kwargs):
        yield ContextCheckpointEvent(
            checkpoint_id="checkpoint-1",
            workflow_kind="controlled_presentation",
            adapter_id="box-agent.controlled-presentation.v1",
            schema_version=1,
            workspace_identity="workspace-hash",
            path=str(tmp_path / ".box-agent" / "checkpoints" / "checkpoint.json"),
            stage="outline",
            artifact_count=2,
            artifact_set_sha256="artifact-hash",
        )
        yield DoneEvent(
            stop_reason=StopReason.CHECKPOINT_PAUSED,
            final_content="Progress saved.",
        )

    state.agent.run_events = checkpoint_events  # type: ignore[method-assign]

    reason = await agent._run_turn(state, session.sessionId)

    assert reason == "checkpoint_paused"
    assert state.last_error is None
    assert state.last_checkpoint is not None
    assert state.last_checkpoint["checkpointId"] == "checkpoint-1"
    assert state.last_checkpoint["status"] == "paused"
    assert any("context_checkpoint" in str(update) for update in conn.updates)


@pytest.mark.asyncio
async def test_acp_resumes_controlled_deck_after_required_user_input(tmp_path):
    output_dir = tmp_path / "output"
    completion_tool = CompleteDeckTool(output_dir)
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=6, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = ClarifyThenResumePptLLM()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [completion_tool],
        "system",
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    first = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[
                {
                    "text": (
                        "制作一份融资路演 PPT，市场规模必须使用用户提供的真实数字"
                    )
                }
            ],
        )
    )
    state = agent._sessions[session.sessionId]

    assert first.stopReason == "end_turn"
    assert state.pending_completion_gate is not None
    assert state.waiting_for_user_input is True
    assert not (output_dir / "index.html").exists()
    assert first.field_meta["deliveryStatus"] == "waiting_for_user"
    assert first.field_meta["recoverable"] is True
    assert first.field_meta["deliveryGaps"]

    second = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "TAM 120 亿元，SAM 30 亿元，SOM 3 亿元"}],
        )
    )

    assert second.stopReason == "end_turn"
    assert llm.calls == 4
    assert completion_tool.calls == 1
    assert (output_dir / "index.html").is_file()
    assert state.pending_completion_gate is None
    assert state.waiting_for_user_input is False
    assert second.field_meta["deliveryStatus"] == "complete"
    assert second.field_meta["recoverable"] is False
    assert second.field_meta["deliveryGaps"] == []
    assert "TAM 120 亿元" in state.source_text
    rendered = "\n".join(str(update) for update in conn.updates)
    assert "已根据补充数据继续完成 HTML" in rendered
    assert "尚未满足完成条件" not in rendered


@pytest.mark.asyncio
async def test_acp_preloads_matched_pptx_skill_for_deliverable(tmp_path):
    skills_dir = tmp_path / "skills"
    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir(parents=True)
    (pptx_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "keywords: [ppt, pptx, powerpoint, slide]\n"
        "---\n"
        "# PPTX FULL RULES\n"
        "Use the editable deck workflow.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = CaptureMessagesLLM()
    prompt_capture = ParentPromptCaptureTool()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [prompt_capture],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"}],
        )
    )

    first_system_prompt = llm.calls[0][0][1]
    assert "## Auto-Loaded Skill Instructions" in first_system_prompt
    assert "# Skill: pptx" in first_system_prompt
    assert "# PPTX FULL RULES" in first_system_prompt
    assert prompt_capture.parent_system_prompt == first_system_prompt
    assert agent._sessions[session.sessionId].preloaded_skill_names == ["pptx"]


@pytest.mark.asyncio
async def test_acp_unloads_auto_preloaded_skill_after_it_is_disabled(tmp_path):
    skills_dir = tmp_path / "skills"
    settings_path = tmp_path / "skill-settings.json"
    settings_path.write_text('{"disabledSkillNames": []}', encoding="utf-8")
    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir(parents=True)
    pptx_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "capabilities: [presentation.authoring]\n"
        "workflow: controlled_presentation\n"
        "---\n"
        "# PPTX FULL RULES\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(
        skills_dir,
        skill_settings_path=settings_path,
    )
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    async def capture_run_turn(state_arg, session_id, **kwargs):
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "生成一份季度汇报 PPT"}],
        )
    )
    state = agent._sessions[session.sessionId]
    assert state.preloaded_skill_names == ["pptx"]
    assert "# PPTX FULL RULES" in state.agent.system_prompt

    settings_path.write_text('{"disabledSkillNames": ["pptx"]}', encoding="utf-8")
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "再生成一份产品介绍 PPT"}],
        )
    )

    assert state.preloaded_skill_names == []
    assert "## Auto-Loaded Skill Instructions" not in state.agent.system_prompt
    assert "# PPTX FULL RULES" not in state.agent.system_prompt


@pytest.mark.asyncio
async def test_acp_host_presentation_config_guarantees_presentation_provider(tmp_path):
    skills_dir = tmp_path / "skills"
    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir(parents=True)
    (pptx_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "capabilities: [presentation.authoring]\n"
        "workflow: controlled_presentation\n"
        "---\n"
        "# PPTX FULL RULES\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "继续"}],
            field_meta={
                "presentation_config": {
                    "schema_version": 1,
                    "intent": "create",
                    "confirmed_by": "user",
                }
            },
        )
    )

    gate = captured["gate"]
    assert isinstance(gate, CompletionGate)
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert agent._sessions[session.sessionId].preloaded_skill_names == ["pptx"]


@pytest.mark.asyncio
async def test_acp_explicit_external_skill_gets_lifecycle_and_stays_preloaded_on_resume(
    tmp_path,
):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "ppt-master"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: ppt-master\n"
        "description: Generate editable PPTX presentations.\n"
        "---\n"
        "# PPT MASTER RULES\n"
        "Ask for confirmation when required, then create and export the deck.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader([(skills_dir, "user")])
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    captured: list[CompletionGate | None] = []

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured.append(kwargs.get("completion_gate"))
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "请使用 /ppt-master 介绍内马尔最辉煌的1个赛季"}],
        )
    )
    first_gate = captured[-1]
    assert isinstance(first_gate, CompletionGate)
    assert first_gate.workflow_checkpoint_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert first_gate.required_changed_artifact_globs == ("output/**/*.pptx",)
    assert first_gate.workflow_options["skill_name"] == "ppt-master"
    state = agent._sessions[session.sessionId]
    assert state.preloaded_skill_names == ["ppt-master"]
    assert "# PPT MASTER RULES" in state.agent.system_prompt

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "确认制作"}],
        )
    )
    resumed_gate = captured[-1]
    assert isinstance(resumed_gate, CompletionGate)
    assert resumed_gate.workflow_checkpoint_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert resumed_gate.workflow_options["skill_name"] == "ppt-master"
    assert state.preloaded_skill_names == ["ppt-master"]


@pytest.mark.asyncio
async def test_acp_plain_skill_mention_does_not_force_external_lifecycle(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "ppt-master"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: ppt-master\n"
        "description: Generate editable PPTX presentations.\n"
        "---\n"
        "# PPT MASTER RULES\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader([(skills_dir, "user")])
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "解释一下 ppt-master 这个 Skill 的名字"}],
        )
    )

    gate = captured["gate"]
    assert gate is None or not isinstance(gate, CompletionGate) or (
        gate.workflow_checkpoint_kind != EXTERNAL_SKILL_WORKFLOW_KIND
    )


@pytest.mark.asyncio
async def test_acp_generic_ppt_request_does_not_preload_matched_lark_slides(tmp_path):
    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"
    lark_dir = user_root / "lark-slides"
    lark_dir.mkdir(parents=True)
    lark_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: lark-slides\n"
        "description: Create and edit Lark or Feishu PPT slides.\n"
        "capabilities: [presentation.authoring]\n"
        "---\n"
        "# LARK SLIDES RULES\n",
        encoding="utf-8",
    )
    pptx_dir = builtin_root / "pptx"
    pptx_dir.mkdir(parents=True)
    pptx_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "capabilities: [presentation.authoring]\n"
        "workflow: controlled_presentation\n"
        "---\n"
        "# PPTX FULL RULES\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(
        [(user_root, "user"), (builtin_root, "builtin")]
    )
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    async def capture_run_turn(state_arg, session_id, **kwargs):
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "制作一份哈利波特主题介绍 PPT"}],
            field_meta={
                "presentation_config": {
                    "schema_version": 1,
                    "intent": "create",
                    "confirmed_by": "implicit",
                }
            },
        )
    )

    state = agent._sessions[session.sessionId]
    assert state.preloaded_skill_names == ["pptx"]
    assert "# PPTX FULL RULES" in state.agent.system_prompt
    assert "# LARK SLIDES RULES" not in state.agent.system_prompt


@pytest.mark.asyncio
async def test_acp_host_config_uses_matched_legacy_presentation_skill(tmp_path):
    user_root = tmp_path / "user-skills"
    builtin_root = tmp_path / "builtin-skills"
    legacy_dir = user_root / "legacy-slides"
    legacy_dir.mkdir(parents=True)
    legacy_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: legacy-slides\n"
        "description: 创建和编辑飞书幻灯片\n"
        "---\n"
        "# LEGACY SLIDES RULES\n",
        encoding="utf-8",
    )
    pptx_dir = builtin_root / "pptx"
    pptx_dir.mkdir(parents=True)
    pptx_dir.joinpath("SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "capabilities: [presentation.authoring]\n"
        "workflow: controlled_presentation\n"
        "---\n"
        "# PPTX FULL RULES\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(
        [(user_root, "user"), (builtin_root, "builtin")]
    )
    skill_loader.discover_skills()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(
        DummyConn(),
        config,
        DoneLLM(),
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )
    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    captured: dict[str, object] = {}

    async def capture_run_turn(state_arg, session_id, **kwargs):
        captured["gate"] = kwargs.get("completion_gate")
        return "end_turn"

    agent._run_turn = capture_run_turn  # type: ignore[method-assign]

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "用飞书创建幻灯片"}],
            field_meta={
                "presentation_config": {
                    "schema_version": 1,
                    "intent": "create",
                    "confirmed_by": "implicit",
                }
            },
        )
    )

    gate = captured["gate"]
    assert isinstance(gate, CompletionGate)
    assert gate.workflow_checkpoint_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert gate.workflow_options["skill_name"] == "legacy-slides"
    assert gate.max_tool_calls == 128
    assert gate.completion_reserve_tool_calls == 0
    assert gate.required_changed_artifact_globs == ()
    assert agent._sessions[session.sessionId].preloaded_skill_names == [
        "legacy-slides"
    ]


@pytest.mark.asyncio
async def test_acp_does_not_repeat_preloaded_skill_in_get_skill_tool_context(tmp_path):
    skills_dir = tmp_path / "skills"
    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir(parents=True)
    (pptx_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "keywords: [ppt, pptx, powerpoint, slide]\n"
        "---\n"
        "# PPTX FULL RULES\n"
        "Use the editable deck workflow.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = PreloadedSkillThenGetSkillLLM()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        create_skill_tools(sources=[(skills_dir, "builtin")])[0],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"}],
        )
    )

    assert agent._sessions[session.sessionId].preloaded_skill_names == ["pptx"]
    assert len(llm.calls) == 2
    tool_messages = [content for role, content in llm.calls[1] if role == "tool"]
    assert tool_messages == [
        "Skill 'pptx' is already preloaded in this session. "
        "Follow its system instructions directly."
    ]
    assert "# PPTX FULL RULES" not in tool_messages[0]

    other_session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    first_state = agent._sessions[session.sessionId]
    other_state = agent._sessions[other_session.sessionId]
    first_get_skill = first_state.agent.tools["get_skill"]
    other_get_skill = other_state.agent.tools["get_skill"]
    assert first_get_skill is not other_get_skill
    assert first_get_skill.preloaded_skill_hashes is first_state.preloaded_skill_hashes
    assert other_get_skill.preloaded_skill_hashes is other_state.preloaded_skill_hashes
    assert other_state.preloaded_skill_hashes == {}


@pytest.mark.asyncio
async def test_acp_preloads_hyperframes_skill_when_runtime_available(tmp_path):
    skills_dir = tmp_path / "skills"
    hyperframes_dir = skills_dir / "hyperframes-video"
    hyperframes_dir.mkdir(parents=True)
    (hyperframes_dir / "SKILL.md").write_text(
        "---\n"
        "name: hyperframes-video\n"
        "description: Create and render MP4 videos with the host HyperFrames runtime.\n"
        "keywords: [video, mp4, animation, hyperframes, 生成视频, 做视频]\n"
        "---\n"
        "# HYPERFRAMES VIDEO FULL RULES\n"
        "Use the bundled HyperFrames runtime and render with strict validation.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = CaptureMessagesLLM()
    prompt_capture = ParentPromptCaptureTool()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [prompt_capture],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "env_context": {
                    "hyperframes": {
                        "installed": True,
                        "available": True,
                        "template": True,
                        "ffmpeg": True,
                        "ffprobe": True,
                    }
                },
            },
        )
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "把这张图做成一个 8 秒 MP4 视频动画"}],
        )
    )

    state = agent._sessions[session.sessionId]
    first_system_prompt = llm.calls[0][0][1]
    assert "hyperframes-video" in state.skill_selector.matched_skill_names
    assert "## Auto-Loaded Skill Instructions" in first_system_prompt
    assert "# Skill: hyperframes-video" in first_system_prompt
    assert "# HYPERFRAMES VIDEO FULL RULES" in first_system_prompt
    assert prompt_capture.parent_system_prompt == first_system_prompt
    assert state.preloaded_skill_names == ["hyperframes-video"]
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert turn_usage_outputs[-1]["skills"] == ["hyperframes-video"]
    assert [
        {
            key: invocation[key]
            for key in ("skillName", "activationSource", "status")
        }
        for invocation in turn_usage_outputs[-1]["skillInvocations"]
    ] == [
        {
            "skillName": "hyperframes-video",
            "activationSource": "preloaded",
            "status": "succeeded",
        }
    ]


@pytest.mark.asyncio
async def test_acp_does_not_preload_hyperframes_skill_when_runtime_unavailable(tmp_path):
    skills_dir = tmp_path / "skills"
    hyperframes_dir = skills_dir / "hyperframes-video"
    hyperframes_dir.mkdir(parents=True)
    (hyperframes_dir / "SKILL.md").write_text(
        "---\n"
        "name: hyperframes-video\n"
        "description: Create and render MP4 videos with the host HyperFrames runtime.\n"
        "keywords: [video, mp4, animation, hyperframes, 生成视频, 做视频]\n"
        "---\n"
        "# HYPERFRAMES VIDEO FULL RULES\n"
        "Use the bundled HyperFrames runtime and render with strict validation.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={
                "session_mode": "general",
                "env_context": {
                    "hyperframes": {
                        "installed": True,
                        "available": False,
                        "template": True,
                        "ffmpeg": True,
                        "ffprobe": True,
                    }
                },
            },
        )
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "把这张图做成一个 8 秒 MP4 视频动画"}],
        )
    )

    state = agent._sessions[session.sessionId]
    first_system_prompt = llm.calls[0][0][1]
    assert "hyperframes-video" in state.skill_selector.matched_skill_names
    assert "## Auto-Loaded Skill Instructions" not in first_system_prompt
    assert "# HYPERFRAMES VIDEO FULL RULES" not in first_system_prompt
    assert state.preloaded_skill_names == []


@pytest.mark.asyncio
async def test_acp_preloads_required_skill_for_document_deliverable(tmp_path):
    skills_dir = tmp_path / "skills"
    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir(parents=True)
    (pptx_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "keywords: [ppt, pptx, powerpoint, slide]\n"
        "required_skills: [html-templates]\n"
        "---\n"
        "# PPTX FULL RULES\n"
        "Use the editable deck workflow.\n",
        encoding="utf-8",
    )
    html_templates_dir = skills_dir / "html-templates"
    html_templates_dir.mkdir()
    (html_templates_dir / "SKILL.md").write_text(
        "---\n"
        "name: html-templates\n"
        "description: Select visual style constraints for HTML slide decks.\n"
        "keywords: [html, template, visual]\n"
        "---\n"
        "# HTML TEMPLATE RULES\n"
        "Select a Visual DNA profile.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"}],
        )
    )

    first_system_prompt = llm.calls[0][0][1]
    assert "# Skill: pptx" in first_system_prompt
    assert "# Skill: html-templates" in first_system_prompt
    assert "# HTML TEMPLATE RULES" in first_system_prompt
    assert agent._sessions[session.sessionId].preloaded_skill_names == [
        "pptx",
        "html-templates",
    ]
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert turn_usage_outputs[-1]["skills"] == ["pptx", "html-templates"]
    assert [
        {
            key: invocation.get(key)
            for key in ("skillName", "usageRole", "dependencyOf")
        }
        for invocation in turn_usage_outputs[-1]["skillInvocations"]
    ] == [
        {
            "skillName": "pptx",
            "usageRole": "primary",
            "dependencyOf": None,
        },
        {
            "skillName": "html-templates",
            "usageRole": "dependency",
            "dependencyOf": "pptx",
        },
    ]


@pytest.mark.asyncio
async def test_acp_preloads_pptx_when_catalog_filter_drops_it(tmp_path):
    skills_dir = tmp_path / "skills"
    prompt = "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"

    for index in range(16):
        noise_dir = skills_dir / f"lark-noise-{index}"
        noise_dir.mkdir(parents=True)
        (noise_dir / "SKILL.md").write_text(
            "---\n"
            f"name: lark-noise-{index}\n"
            "description: 做一份 新员工 入职 培训 可编辑 会议室 HR 友好 流程 清单\n"
            "keywords: [做一份, 新员工, 入职, 培训, 可编辑, 会议室, HR]\n"
            "---\n"
            f"# Noise {index}\n",
            encoding="utf-8",
        )

    pptx_dir = skills_dir / "pptx"
    pptx_dir.mkdir()
    (pptx_dir / "SKILL.md").write_text(
        "---\n"
        "name: pptx\n"
        "description: Create editable PowerPoint PPTX slide decks.\n"
        "keywords: [ppt, pptx, powerpoint, slide]\n"
        "---\n"
        "# PPTX FULL RULES\n"
        "Use the editable deck workflow.\n",
        encoding="utf-8",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()
    assert "pptx" not in [skill.name for skill in skill_loader.filter_by_query(prompt)]

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(
        conn,
        config,
        llm,
        [],
        f"system\n\n{SKILL_SLOT_SENTINEL}",
        skill_loader=skill_loader,
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": prompt}])
    )

    state = agent._sessions[session.sessionId]
    first_system_prompt = llm.calls[0][0][1]
    assert "pptx" not in state.skill_selector.matched_skill_names
    assert "## Auto-Loaded Skill Instructions" in first_system_prompt
    assert "# Skill: pptx" in first_system_prompt
    assert "# PPTX FULL RULES" in first_system_prompt
    assert state.preloaded_skill_names == ["pptx"]
    turn_usage_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert turn_usage_outputs[-1]["skills"] == ["pptx"]


@pytest.mark.asyncio
async def test_acp_hides_internal_web_search_budget_injection(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=30, workspace_dir=str(tmp_path)),
        tool_limits=ToolLimitsConfig(web_search={"total_calls": 24}),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    web_search = CountingWebSearchTool()
    agent = BoxACPAgent(conn, config, WebBudgetLLM(), [web_search], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]

    stop_reason = await agent._run_turn(state, session.sessionId)

    assert stop_reason == "end_turn"
    assert web_search.calls == 24
    rendered = "\n".join(str(update) for update in conn.updates)
    assert "final from gathered evidence" in rendered
    assert "web_search 调用已达到预算上限" not in rendered
    assert "Tool call budget reached" not in rendered
    assert "Search batch controller update" not in rendered
    assert "[Injected" not in rendered


@pytest.mark.asyncio
async def test_acp_can_cancel_pending_injected_message(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    state.turn_active = True

    injected = await agent.extMethod(
        "inject",
        {
            "sessionId": session.sessionId,
            "text": "生成10页就可以了",
            "injectionId": "inj-2",
        },
    )
    cancelled = await agent.extMethod(
        "cancel_inject",
        {"sessionId": session.sessionId, "injectionId": "inj-2"},
    )

    assert injected == {"ok": True, "injectionId": "inj-2"}
    assert cancelled == {"ok": True}
    assert state.inject_queue.empty()


@pytest.mark.asyncio
async def test_acp_inject_same_id_is_idempotent(tmp_path):
    """Retrying inject with the same injectionId must not enqueue/run twice."""
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, DoneLLM(), [], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    state.turn_active = True

    args = {"sessionId": session.sessionId, "text": "只做5页", "injectionId": "dup-1"}

    first = await agent.extMethod("inject", args)
    second = await agent.extMethod("inject", dict(args))  # retry, same id

    assert first == {"ok": True, "injectionId": "dup-1"}
    assert second == {"ok": True, "injectionId": "dup-1", "deduplicated": True}
    # Still exactly one queued item despite two calls.
    assert state.inject_queue.qsize() == 1

    # Even after the item is consumed (drained by the loop), a retry stays deduped.
    consumed = state.inject_queue.get_nowait()
    assert _inject_item_id(consumed) == "dup-1"
    third = await agent.extMethod("inject", dict(args))
    assert third == {"ok": True, "injectionId": "dup-1", "deduplicated": True}
    assert state.inject_queue.empty()

    # An explicit cancel clears the id so the host may deliberately re-inject it.
    await agent.extMethod(
        "cancel_inject",
        {"sessionId": session.sessionId, "injectionId": "dup-1"},
    )
    fourth = await agent.extMethod("inject", dict(args))
    assert fourth == {"ok": True, "injectionId": "dup-1"}
    assert state.inject_queue.qsize() == 1


@pytest.mark.asyncio
async def test_acp_new_session_injects_core_memory_without_returning_it(tmp_path):
    memory_mgr = MemoryManager(memory_dir=str(tmp_path / "memory"))
    memory_mgr.write_core("- User prefers concise Chinese responses\n- User works on officev3")
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path), memory_dir=str(tmp_path / "memory")),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system", memory_manager=memory_mgr)

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )

    assert session.field_meta is None
    assert "--- MEMORY START ---" in agent._sessions[session.sessionId].agent.system_prompt
    assert "User prefers concise Chinese responses" in agent._sessions[session.sessionId].agent.system_prompt


@pytest.mark.asyncio
async def test_acp_invalid_session(acp_agent):
    """Auto-creates session when sessionId is not found (compatibility)."""
    agent, _ = acp_agent
    # Auto-created sessions without metadata stay on the general prompt. The
    # DummyLLM returns one normal assistant response, so we only check stopReason.
    prompt = SimpleNamespace(sessionId="missing", prompt=[{"text": "?"}])
    response = await agent.prompt(prompt)
    assert response.stopReason == "end_turn"


@pytest.mark.asyncio
async def test_acp_prompt_lists_officev3_allowed_directories(tmp_path):
    allowed = tmp_path / "Documents"
    allowed.mkdir()
    workspace = tmp_path / "workspace"

    officev3 = Officev3Config(
        permissions=Officev3Permissions(
            filesystem=FilesystemPermissions(
                scope="session_workspace",
                allowed_directories=[str(allowed)],
            )
        ),
        paths=Officev3Paths(session_workspace_root=str(tmp_path / "office-raccoon")),
    )
    officev3._present = True
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(),
        officev3=officev3,
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(workspace), field_meta={"session_mode": "general"})
    )
    prompt = agent._sessions[session.sessionId].agent.system_prompt

    assert "## File Access Context" in prompt
    assert "configured allowed directories are allowed" in prompt
    assert str(allowed) in prompt
    assert "Do not claim you can only access the workspace" in prompt


@pytest.mark.asyncio
async def test_acp_default_permission_mode_replaces_global_allowed_directories(tmp_path):
    global_allowed = tmp_path / "global"
    session_allowed = tmp_path / "session-allowed"
    workspace = tmp_path / "workspace"
    global_allowed.mkdir()
    session_allowed.mkdir()

    officev3 = Officev3Config(
        permissions=Officev3Permissions(
            filesystem=FilesystemPermissions(
                scope="user_home",
                allowed_directories=[str(global_allowed)],
            )
        ),
        paths=Officev3Paths(session_workspace_root=str(tmp_path / "legacy-root")),
    )
    officev3._present = True
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
        officev3=officev3,
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={
                "permission_mode": "default",
                "filesystem_policy": {
                    "filesystem_scope": "session_workspace",
                    "session_workspace_root": str(workspace),
                    "allowed_directories": [str(session_allowed)],
                },
            },
        )
    )
    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]

    assert bash_tool.allow_full_access is False
    assert bash_tool._perm is not None
    assert bash_tool._perm.policy.filesystem_scope == "session_workspace"
    assert bash_tool._perm.policy.allowed_directories == (str(session_allowed),)
    assert "execute_code" not in agent._sessions[session.sessionId].agent.tools


@pytest.mark.asyncio
async def test_acp_default_permission_mode_without_filesystem_policy_fails_closed(tmp_path):
    global_allowed = tmp_path / "global"
    workspace = tmp_path / "workspace"
    global_allowed.mkdir()

    officev3 = Officev3Config(
        permissions=Officev3Permissions(
            filesystem=FilesystemPermissions(
                scope="user_home",
                allowed_directories=[str(global_allowed)],
            )
        ),
        paths=Officev3Paths(session_workspace_root=str(tmp_path / "legacy-root")),
    )
    officev3._present = True
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
        officev3=officev3,
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={"permission_mode": "default"},
        )
    )
    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]

    assert bash_tool.allow_full_access is False
    assert bash_tool._perm is not None
    assert bash_tool._perm.policy.filesystem_scope == "session_workspace"
    assert bash_tool._perm.policy.session_workspace_root == str(workspace)
    assert bash_tool._perm.policy.allowed_directories == ()
    assert "execute_code" not in agent._sessions[session.sessionId].agent.tools


@pytest.mark.asyncio
async def test_acp_default_mode_applies_session_directories_without_global_policy(tmp_path):
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "session-allowed"
    allowed.mkdir()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={
                "permission_mode": "default",
                "filesystem_policy": {
                    "filesystem_scope": "session_workspace",
                    "session_workspace_root": str(workspace),
                    "allowed_directories": [str(allowed)],
                },
            },
        )
    )
    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]

    assert bash_tool._perm is not None
    assert bash_tool._perm.policy.allowed_directories == (str(allowed),)
    assert "execute_code" not in agent._sessions[session.sessionId].agent.tools
    assert "仅 `execute_code` 沙箱可用" not in agent._sessions[session.sessionId].agent.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_mode", [[], {}, 1])
async def test_acp_non_string_permission_mode_fails_closed(tmp_path, invalid_mode):
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={"permission_mode": invalid_mode},
        )
    )
    tools = agent._sessions[session.sessionId].agent.tools

    assert tools["bash"].allow_full_access is False
    assert tools["bash"]._perm is not None
    assert "execute_code" not in tools


@pytest.mark.asyncio
async def test_acp_full_access_request_respects_server_cap(tmp_path):
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={"permission_mode": "full_access"},
        )
    )
    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]
    session_tools = agent._sessions[session.sessionId].agent.tools

    assert bash_tool.allow_full_access is False
    assert bash_tool._perm is not None
    assert "execute_code" not in session_tools
    assert "sandbox_status" not in session_tools


@pytest.mark.asyncio
async def test_acp_full_access_mode_requires_server_opt_in(tmp_path):
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={"permission_mode": "full_access"},
        )
    )
    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]
    session_tools = agent._sessions[session.sessionId].agent.tools

    assert bash_tool.allow_full_access is True
    assert bash_tool._perm is None
    assert "execute_code" in session_tools
    assert "sandbox_status" in session_tools


@pytest.mark.asyncio
async def test_acp_prompt_includes_skill_runtime_context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    sandbox_base = tmp_path / "sandbox-runtime"
    python_path = sandbox_base / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python_path.chmod(0o755)

    from box_agent.tools.jupyter_tool import SandboxEnvironment

    monkeypatch.setattr(
        "box_agent.tools.runtime.SandboxEnvironment",
        lambda: SandboxEnvironment(base_dir=sandbox_base),
    )
    monkeypatch.setattr("box_agent.tools.runtime.DEFAULT_NODE_RUNTIME_ROOT", tmp_path / "missing-node")
    monkeypatch.setenv(
        "BOX_AGENT_OFFICE_NODE_RUNTIME_ROOT",
        str(tmp_path / "missing-office-node"),
    )

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(workspace), field_meta={"session_mode": "general"})
    )
    prompt = agent._sessions[session.sessionId].agent.system_prompt

    assert "## Skill Runtime Context" in prompt
    assert "$BOX_AGENT_PYTHON" in prompt
    assert "- Node:" in prompt
    assert "不可用" in prompt
    assert "npm install -g" in prompt
    assert "$BOX_AGENT_SKILL_TOOLS_ROOT" in prompt

    bash_tool = agent._sessions[session.sessionId].agent.tools["bash"]
    assert bash_tool._subprocess_env["BOX_AGENT_PYTHON"] == str(python_path)
    assert bash_tool._subprocess_env["BOX_AGENT_PYTHON3"] == str(python_path)


@pytest.mark.asyncio
async def test_acp_prompt_and_bash_env_include_self_managed_node_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    node_root = tmp_path / ".box-agent" / "runtimes" / "node"
    node_bin = node_root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    node = node_bin / "node"
    npm = node_bin / "npm"
    npx = node_bin / "npx"
    for path in (node, npm, npx):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    (node_root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("box_agent.tools.runtime.DEFAULT_NODE_RUNTIME_ROOT", node_root)

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(workspace), field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    prompt = state.agent.system_prompt
    bash_tool = state.agent.tools["bash"]

    assert "- Node:" in prompt
    assert "标准 `node`/`npm`/`npx`" in prompt
    assert "box_agent" in prompt
    assert "$BOX_AGENT_NODE" in prompt
    assert bash_tool._subprocess_env["BOX_AGENT_NODE"] == str(node)
    assert bash_tool._subprocess_env["BOX_AGENT_NPM"] == str(npm)
    assert bash_tool._subprocess_env["BOX_AGENT_NPX"] == str(npx)
    skill_tools = Path.home() / ".box-agent" / "skill-tools"
    assert bash_tool._subprocess_env["NODE_PATH"].split(os.pathsep) == [
        str(skill_tools / "lib" / "node_modules"),
        str(node_root / "sandbox" / "node_modules"),
    ]
    assert bash_tool._subprocess_env["NPM_CONFIG_CACHE"] == str(skill_tools / "npm-cache")
    assert bash_tool._subprocess_env["NPM_CONFIG_PREFIX"] == str(skill_tools)


@pytest.mark.asyncio
async def test_acp_frozen_mode_still_discovers_self_managed_node_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    node_root = tmp_path / "node-runtime"
    node_bin = node_root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    node = node_bin / "node"
    npm = node_bin / "npm"
    npx = node_bin / "npx"
    for path in (node, npm, npx):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    (node_root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("box_agent.tools.runtime.DEFAULT_NODE_RUNTIME_ROOT", node_root)
    monkeypatch.setattr("box_agent.tools.runtime.sys.frozen", True, raising=False)
    monkeypatch.setattr("box_agent.tools.setup.sys.frozen", True, raising=False)

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(workspace), field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]

    assert "box_agent" in state.agent.system_prompt
    assert "execute_code" in state.agent.system_prompt or "shell python" in state.agent.system_prompt
    assert state.agent.tools["bash"]._subprocess_env["BOX_AGENT_NODE"] == str(node)


@pytest.mark.asyncio
async def test_acp_host_env_context_feeds_bash_and_execute_code_runtime_env(tmp_path):
    workspace = tmp_path / "workspace"
    python_path = tmp_path / "officev3" / "python" / "python.exe"
    node_path = tmp_path / "officev3" / "node" / "node.exe"
    npm_path = tmp_path / "officev3" / "node" / "npm.cmd"
    npx_path = tmp_path / "officev3" / "node" / "npx.cmd"
    node_modules = tmp_path / "officev3" / "node_modules"
    for runtime_path in (python_path, node_path, npm_path, npx_path):
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime_path.chmod(0o755)
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=True),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [EchoTool()], "system")

    env_context = {
        "runtimes": {
            "python": {
                "path": str(python_path),
                "ready": True,
                "provider": "officev3",
            },
            "node": {
                "path": str(node_path),
                "npm": str(npm_path),
                "npx": str(npx_path),
                "node_modules": str(node_modules),
                "ready": True,
                "provider": "officev3",
            },
        }
    }

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={"session_mode": "general", "env_context": env_context},
        )
    )
    state = agent._sessions[session.sessionId]
    bash_env = state.agent.tools["bash"]._subprocess_env
    execute_code_env = state.agent.tools["execute_code"].runtime_env

    assert bash_env["BOX_AGENT_PYTHON"] == str(python_path)
    assert bash_env["BOX_AGENT_SANDBOX_PYTHON"] == str(python_path)
    assert bash_env["BOX_AGENT_NODE"] == str(node_path)
    assert bash_env["BOX_AGENT_NPM"] == str(npm_path)
    assert bash_env["BOX_AGENT_NPX"] == str(npx_path)
    assert bash_env["NODE_PATH"].split(os.pathsep)[-1] == str(node_modules)
    assert bash_env["NPM_CONFIG_PREFIX"] == str(
        Path.home() / ".box-agent" / "skill-tools"
    )
    assert execute_code_env["BOX_AGENT_SANDBOX_PYTHON"] == str(python_path)


@pytest.mark.asyncio
async def test_acp_emits_todo_snapshot_raw_output(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, TodoLLM(), [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "更新 todo 状态"}])
    )

    assert response.stopReason == "end_turn"
    todo_snapshots = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and update.update.rawOutput.get("type") == "todo_snapshot"
    ]
    assert any(
        snapshot["action"] == "set"
        and [item["task"] for item in snapshot["items"]]
        == ["Plan host integration", "Verify host snapshot"]
        and snapshot["summary"]
        == {"total": 2, "completed": 0, "in_progress": 1, "pending": 1}
        for snapshot in todo_snapshots
    )
    assert any(
        snapshot["action"] == "transition"
        and [item["id"] for item in snapshot["items"]] == ["1", "2"]
        and [item["status"] for item in snapshot["items"]]
        == ["completed", "in_progress"]
        and snapshot["transition"]
        == {"completed_id": "1", "in_progress_id": "2"}
        for snapshot in todo_snapshots
    )


@pytest.mark.asyncio
async def test_acp_emits_plan_snapshot_raw_output(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, PlanLLM(), [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "开发一个 React 个人介绍网站，先做规划"}])
    )

    assert response.stopReason == "end_turn"
    plan_update_indexes = [
        index
        for index, update in enumerate(conn.updates)
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
    ]
    assert plan_update_indexes
    first_plan_update = conn.updates[plan_update_indexes[0]].update
    first_plan_tool_id = first_plan_update.toolCallId
    assert first_plan_update.status == "completed"
    assert any(
        index < plan_update_indexes[0]
        and getattr(update.update, "sessionUpdate", None) == "tool_call"
        and update.update.toolCallId == first_plan_tool_id
        for index, update in enumerate(conn.updates)
    )
    plan_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
    ]
    assert plan_outputs[0]["action"] == "start"
    assert plan_outputs[0]["plan"]["status"] == "draft"
    assert any(
        output.get("action") == "set"
        and output["plan"]["title"] == "Plan host integration"
        and output["summary"]["steps"] == 1
        for output in plan_outputs
    )


@pytest.mark.asyncio
async def test_acp_session_meta_force_plan_start_hint_is_one_shot(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanAfterRetryLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=None,
            field_meta={"session_mode": "general", "forcePlanStart": True},
        )
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "介绍一下太阳系"}],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    state = agent._sessions[session.sessionId]
    assert state.force_plan_start is False
    assert state.pending_plan_approval is None
    assert llm.calls == 1
    assert not any(
        getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_prompt_meta_force_plan_start_is_hint_only(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanAfterRetryLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "介绍一下太阳系"}],
            field_meta={"forcePlanStart": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert agent._sessions[session.sessionId].pending_plan_approval is None
    assert llm.calls == 1
    assert not any(
        getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_history_plan_snapshot_does_not_force_plan_for_short_reply(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanAfterRetryLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    prompt = """以下是当前会话最近的上下文，请在此基础上继续回答：
用户:
text: 可以开始

助手:
plan: {"title": "执行计划", "steps": [{"title": "旧步骤"}]}

用户问题：ok"""
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": prompt}],
            field_meta={"forcePlanStart": True, "requirePlanApproval": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 1
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is None
    assert not any(
        getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_history_plan_snapshot_does_not_force_plan_from_host_hint(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanAfterRetryLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    prompt = """以下是当前会话最近的上下文，请在此基础上继续回答：
用户:
text: 可以开始

助手:
plan: {"title": "执行计划", "steps": [{"title": "旧步骤"}]}"""
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": prompt}],
            field_meta={"forcePlanStart": True, "requirePlanApproval": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 1
    assert agent._sessions[session.sessionId].pending_plan_approval is None
    assert not any(
        getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_latest_user_plan_request_still_triggers_plan_with_history(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanAfterRetryLLM()
    agent = BoxACPAgent(conn, config, llm, [], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    prompt = """以下是当前会话最近的上下文，请在此基础上继续回答：
用户:
text: ok

助手:
plan: {"title": "旧计划", "steps": []}

用户问题：先出一个计划"""
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": prompt}],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 2
    assert agent._sessions[session.sessionId].pending_plan_approval is not None
    assert any(
        getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        and update.update.rawOutput.get("action") == "start"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_prompt_text_plan_request_waits_for_approval_without_meta(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanApprovalThenEchoLLM()
    agent = BoxACPAgent(conn, config, llm, [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "使用plan 生成一个内马尔图片"}],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 1
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is not None
    assert state.pending_plan_approval["state"] == "pending"
    assert any(
        getattr(update.update, "rawOutput", None)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        and update.update.rawOutput.get("approval", {}).get("state") == "pending"
        for update in conn.updates
    )
    assert not any(
        getattr(update.update, "toolCallId", "") == "echo-after-approval"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_prompt_text_plan_request_auto_approves_with_meta(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanApprovalThenEchoLLM()
    agent = BoxACPAgent(conn, config, llm, [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "使用plan 生成一个内马尔图片"}],
            field_meta={"autoApprovePlan": True, "skipPlanApproval": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 3
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is None
    plan_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
    ]
    assert any(output.get("action") == "set" for output in plan_outputs)
    assert not any(output.get("approval", {}).get("state") == "pending" for output in plan_outputs)
    assert any(
        getattr(update.update, "toolCallId", "") == "echo-after-approval"
        and getattr(update.update, "status", None) == "completed"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_organic_plan_write_waits_for_approval_without_meta(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanApprovalThenEchoLLM()
    agent = BoxACPAgent(conn, config, llm, [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "生成一份分析报告"}],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls == 1
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is not None
    assert state.pending_plan_approval["state"] == "pending"
    assert any(
        getattr(update.update, "rawOutput", None)
        and update.update.rawOutput.get("type") == "plan_snapshot"
        and update.update.rawOutput.get("approval", {}).get("state") == "pending"
        for update in conn.updates
    )
    assert not any(
        getattr(update.update, "toolCallId", "") == "echo-after-approval"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_organic_plan_write_auto_approves_with_meta(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=10, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    llm = PlanApprovalThenEchoLLM()
    agent = BoxACPAgent(conn, config, llm, [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "生成一份分析报告"}],
            field_meta={"autoApprovePlan": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert llm.calls >= 3
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is None
    assert any(
        getattr(update.update, "toolCallId", "") == "echo-after-approval"
        and getattr(update.update, "status", None) == "completed"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_plan_approval_text_continues_pending_plan(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, PlanApprovalThenEchoLLM(), [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    first = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "先出计划"}],
            field_meta={"forcePlanStart": True, "requirePlanApproval": True},
        )
    )

    assert first.stopReason == "end_turn"
    state = agent._sessions[session.sessionId]
    assert state.pending_plan_approval is not None
    assert state.pending_plan_approval["state"] == "pending"
    plan_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "plan_snapshot"
    ]
    assert any(
        output.get("approval", {}).get("state") == "pending"
        and output["plan"]["title"] == "Approval-gated plan"
        for output in plan_outputs
    )

    approval_prompt = """以下是当前会话最近的上下文，请在此基础上继续回答：
用户:
text: 先出计划

助手:
plan: {"title": "Approval-gated plan", "steps": []}

用户问题：继续执行"""
    second = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": approval_prompt}])
    )

    assert second.stopReason == "end_turn"
    assert state.pending_plan_approval is None
    assert any(
        getattr(update.update, "toolCallId", "") == "echo-after-approval"
        and getattr(update.update, "status", None) == "completed"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_acp_sub_agent_progress_has_stable_grouping_fields(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=5, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, SubAgentLLM(), [EchoTool()], "system")

    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    response = await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "delegate"}]))

    assert response.stopReason == "end_turn"
    progress = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "rawOutput", None)
        and isinstance(update.update.rawOutput, dict)
        and update.update.rawOutput.get("type") == "sub_agent_progress"
    ]
    assert progress
    assert {item["parent_tool_call_id"] for item in progress} == {"sub1"}
    assert all(item["sub_agent_id"].startswith("subagent-") for item in progress)
    assert {item["sub_agent_id"] for item in progress}
    # Short distinct label is forwarded for host-side rendering.
    assert all(item["title"] == "file probe" for item in progress)
    assert any(item["event"] == "tool_start" and item["tool_name"] == "echo" for item in progress)
    assert any(item["event"] == "llm_output" and item["content"] == "child summary" for item in progress)


@pytest.mark.asyncio
async def test_acp_prompt_response_reports_turn_token_total(tmp_path):
    """The prompt response carries the per-turn token total in _meta.usage."""
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, UsageLLM(per_call_total=30), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "hello"}],
            field_meta={"turn_id": "turn-response-1"},
        )
    )

    assert response.stopReason == "end_turn"
    assert response.field_meta["usage"] == {
        "totalTokens": 30,
        "sessionId": session.sessionId,
        "session_id": session.sessionId,
        "turnId": "turn-response-1",
        "turn_id": "turn-response-1",
    }


@pytest.mark.asyncio
async def test_acp_prompt_response_marks_done_error_as_failure(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(
        conn,
        config,
        EmptyFinalAnswerLLM(),
        [EchoTool()],
        "system",
    )

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "Use echo and summarize the result"}],
        )
    )

    assert response.stopReason == "end_turn"
    assert response.field_meta["ok"] is False
    assert response.field_meta["error"]
    assert response.field_meta["lastStopReason"] == "error"


@pytest.mark.asyncio
async def test_acp_token_meter_resets_between_turns(tmp_path):
    """Each turn reports only its own tokens, not a cumulative running sum."""
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, UsageLLM(per_call_total=25), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    first = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "one"}])
    )
    second = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "two"}])
    )

    assert first.field_meta["usage"]["totalTokens"] == 25
    assert second.field_meta["usage"]["totalTokens"] == 25
    assert first.field_meta["usage"]["sessionId"] == session.sessionId
    assert second.field_meta["usage"]["sessionId"] == session.sessionId
    assert first.field_meta["usage"]["turnId"] == f"{session.sessionId}-turn-1"
    assert second.field_meta["usage"]["turnId"] == f"{session.sessionId}-turn-2"


class SpeakThenToolLLM:
    """Emits a short visible preface before any tool."""

    async def generate_stream(self, messages, tools, **_):
        if not getattr(self, "_spoke", False):
            self._spoke = True
            yield StreamEvent(type="text", delta="我先打开页面检查一下。")
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="tool1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "ping"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None):
        return LLMResponse(content="done", finish_reason="stop")


def _first_tool_call_index(updates):
    for i, update in enumerate(updates):
        if getattr(update.update, "sessionUpdate", None) == "tool_call":
            return i
    return -1


@pytest.mark.asyncio
async def test_acp_streams_short_model_preface_before_tool(tmp_path):
    """Short model-authored pre-tool text is streamed before the tool call."""
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=3, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_todo=False, enable_sub_agent=False),
    )
    conn = DummyConn()
    agent = BoxACPAgent(conn, config, SpeakThenToolLLM(), [EchoTool()], "system")

    session = await agent.newSession(
        SimpleNamespace(cwd=None, field_meta={"session_mode": "general"})
    )
    response = await agent.prompt(
        SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "打开页面"}])
    )

    assert response.stopReason == "end_turn"
    message_chunks = [
        (i, update.update.content.text)
        for i, update in enumerate(conn.updates)
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    ]
    preface_index = next(
        i for i, text in message_chunks if text == "我先打开页面检查一下。"
    )
    tool_index = _first_tool_call_index(conn.updates)
    assert tool_index != -1
    assert preface_index < tool_index
