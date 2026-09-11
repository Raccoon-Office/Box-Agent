"""Integration tests for explicit ACP session modes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import LLMResponse


class _DummyConn:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


class _TrackingLLM:
    """LLM stub that records whether ACP uses extra non-stream LLM calls."""

    def __init__(self, mode_label: str = "general"):
        self.generate_calls = 0
        self.main_calls = 0
        self._mode_label = mode_label

    async def generate(self, messages, tools=None):
        self.generate_calls += 1
        return LLMResponse(content="ok", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        self.main_calls += 1
        from box_agent.schema import StreamEvent
        yield StreamEvent(type="text", delta="ok")
        yield StreamEvent(type="finish", finish_reason="stop")


def _make_agent(tmp_path, llm: _TrackingLLM) -> tuple[BoxACPAgent, _DummyConn]:
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    conn = _DummyConn()
    agent = BoxACPAgent(conn, config, llm, [], "base system")
    return agent, conn


@pytest.mark.asyncio
async def test_explicit_mode_uses_requested_prompt(tmp_path):
    llm = _TrackingLLM(mode_label="ppt_outline")
    agent, _ = _make_agent(tmp_path, llm)

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "data_analysis"})
    )
    state = agent._sessions[session.sessionId]
    assert state.session_mode == "data_analysis"

    prompt = SimpleNamespace(
        sessionId=session.sessionId, prompt=[{"text": "show me a chart"}]
    )
    await agent.prompt(prompt)

    assert llm.generate_calls == 0, "session mode must not add one-shot LLM calls"
    assert state.session_mode == "data_analysis"


@pytest.mark.asyncio
async def test_missing_mode_stays_general_without_extra_llm_call(tmp_path):
    llm = _TrackingLLM(mode_label="ppt_outline")
    agent, _ = _make_agent(tmp_path, llm)

    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path)))
    state = agent._sessions[session.sessionId]
    assert state.session_mode is None

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "帮我做个 AI 主题的 PPT 大纲"}],
        )
    )

    assert llm.generate_calls == 0
    assert state.session_mode is None
    # System message remains general (base prompt was "base system").
    assert state.agent.messages[0].role == "system"


@pytest.mark.asyncio
async def test_missing_mode_never_auto_promotes_to_data_analysis(tmp_path):
    llm = _TrackingLLM(mode_label="data_analysis")
    agent, _ = _make_agent(tmp_path, llm)

    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path)))

    for msg in ["分析第一个表格", "再看看第二份数据"]:
        await agent.prompt(
            SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": msg}])
        )

    assert llm.generate_calls == 0
    assert agent._sessions[session.sessionId].session_mode is None


def test_data_analysis_prompt_includes_plot_contract_and_general_prompt_does_not(tmp_path):
    llm = _TrackingLLM(mode_label="general")
    agent, _ = _make_agent(tmp_path, llm)
    agent._system_prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    general_prompt = agent._build_session_prompt(
        "general", workspace=tmp_path, enable_general_directory_policy=True
    )
    analysis_prompt = agent._build_session_prompt("data_analysis", workspace=tmp_path)

    assert "多文件交付" in general_prompt
    assert "zip -r bundle.zip" in general_prompt
    assert "Interactive Chart Data Output" not in general_prompt
    assert "<!--PLOT_DATA:" not in general_prompt

    assert "多文件交付" in analysis_prompt
    assert "Interactive Chart Data Output" in analysis_prompt
    assert "<!--PLOT_DATA:" in analysis_prompt
    assert "sandbox:/mnt/data/<filename>" in analysis_prompt


def test_code_agent_prompt_includes_software_engineering_contract(tmp_path):
    llm = _TrackingLLM(mode_label="general")
    agent, _ = _make_agent(tmp_path, llm)
    agent._system_prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    general_prompt = agent._build_session_prompt("general", workspace=tmp_path)
    code_prompt = agent._build_session_prompt(
        "code_agent",
        workspace=tmp_path,
    )

    assert "Software Engineering Mode (code_agent)" not in general_prompt
    assert "Software Engineering Mode (code_agent)" in code_prompt
    assert "文件搜索默认使用 `search_files`" in general_prompt
    assert "只有当前会话的专属提示明确要求其他方式时" in general_prompt
    assert "`rg --files`" not in general_prompt
    assert "本会话的代码搜索优先使用独立的 `rg` 工具" in code_prompt
    assert "`mode='files'`" in code_prompt
    assert "`mode='content'`" in code_prompt
    assert "不要为常规文件发现或内容检索拼接 Bash 命令" in code_prompt
    assert "`rg --files`" not in code_prompt
    assert "`rg -n`" not in code_prompt
    assert "`--hidden`" in code_prompt
    assert "`--no-ignore`" in code_prompt
    assert "回退到 `search_files`" in code_prompt
    assert "代码工作区就是交付位置" in code_prompt
    assert "不要默认创建或使用 `output/`" in code_prompt
    assert "`git diff`/`git status` 失败不能当作已确认" in code_prompt
    assert "JS 引用的 id/selector 与 HTML 一致" in code_prompt
    assert "引用具体函数或代码片段时" in code_prompt
    assert "`file_path:line_number`" in code_prompt
    assert "无法确认精确行号时应明确说明" in code_prompt
    assert "不得猜测" in code_prompt
    assert "Project Startup Context" in code_prompt
    assert "cwd 已是 `{workspace}/output/`" not in code_prompt
    assert "不要在最终文本手写或猜测 `local-file://`" in code_prompt
    assert "工作目录内相对位置即可" in code_prompt


def test_code_agent_prompt_reads_workspace_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Project Rules\n\n- Run focused tests before reporting done.\n",
        encoding="utf-8",
    )
    llm = _TrackingLLM(mode_label="general")
    agent, _ = _make_agent(tmp_path, llm)
    agent._system_prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    general_prompt = agent._build_session_prompt("general", workspace=tmp_path)
    code_prompt = agent._build_session_prompt(
        "code_agent",
        workspace=tmp_path,
    )

    assert "Project Startup Context" not in general_prompt
    assert "Project Instructions" in code_prompt
    assert "AGENTS.md" in code_prompt
    assert "Run focused tests before reporting done." in code_prompt
    assert "project instructions apply only when they do not conflict" in code_prompt


def test_code_agent_prompt_includes_git_status_summary(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    llm = _TrackingLLM(mode_label="general")
    agent, _ = _make_agent(tmp_path, llm)
    agent._system_prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    code_prompt = agent._build_session_prompt(
        "code_agent",
        workspace=tmp_path,
    )

    assert "Git repository: yes" in code_prompt
    assert "Status: 1 changed entry" in code_prompt
    assert "?? README.md" in code_prompt
