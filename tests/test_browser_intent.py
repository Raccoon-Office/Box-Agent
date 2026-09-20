from __future__ import annotations

import pytest

from box_agent.events import ToolCallResult
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.browser_intent import (
    BrowserToolIntentPolicy,
    has_explicit_current_page_intent,
    has_human_browser_handoff_intent,
)


@pytest.mark.parametrize(
    "user_text",
    [
        "看一下当前页面里的两个选项",
        "帮我总结这个网页",
        "帮我看看这个网站",
        "看看我打开的那个网页",
        "分析我现在看的这个页面",
        "总结我刚刚打开的那个网站",
        "summarize this website",
        "读取我浏览器里打开的页面",
        "对比我已经登录后的页面",
        "总结这篇公众号文章",
        "summarize the current browser tab",
        "compare the options on this page",
    ],
)
def test_current_page_intent_accepts_explicit_page_references(user_text: str) -> None:
    assert has_explicit_current_page_intent(user_text)


@pytest.mark.parametrize(
    "user_text",
    [
        "看一下gpt sol 极高和最高的区别",
        "帮我看看这个问题",
        "查一下最新资料",
        "了解一下这个模型",
        "打开 https://example.com 并总结",
        "做一个网页",
        "帮我看看",
        "看看那个问题",
        "帮我分析一个网站",
        "compare GPT modes",
    ],
)
def test_current_page_intent_rejects_generic_requests(user_text: str) -> None:
    assert not has_explicit_current_page_intent(user_text)


@pytest.mark.parametrize(
    "user_text",
    [
        "填写这个表单，填好让我检查，最后我点击提交",
        "填表然后看着，最后人点击",
        "fill the form and let me review and submit it",
    ],
)
def test_human_handoff_intent_requires_visible_browser(user_text: str) -> None:
    assert has_human_browser_handoff_intent(user_text)


@pytest.mark.parametrize(
    "user_text",
    [
        "检索公开网页",
        "填写一个不会提交的计算器表单并返回结果",
        "批量抓取十个页面",
    ],
)
def test_human_handoff_intent_rejects_background_tasks(user_text: str) -> None:
    assert not has_human_browser_handoff_intent(user_text)


@pytest.mark.parametrize("user_text", [
    "继续看", "继续帮我分析一下", "请接着帮我总结一下吧", "帮我看看",
    "再看一下好吗？", "继续对比一下", "continue",
])
def test_browser_continuation_requires_recent_successful_browser_context(user_text: str) -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="看看当前页面"),
        Message(role="assistant", content=""),
        Message(
            role="tool",
            name="user_browser_read_current_page",
            tool_call_id="browser-1",
            content='{"ok": true}',
        ),
        Message(role="user", content=user_text),
    ]

    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text=user_text,
        messages=messages,
    )

    assert policy.allow_current_page is True
    assert policy.is_tool_visible("user_browser_read_current_page") is True
    assert policy.tool_call_error("user_browser_read_page", {}) is None
    assert BrowserToolIntentPolicy.for_turn(
        current_turn_text=user_text, messages=[],
    ).allow_current_page is False
    messages[-2] = messages[-2].model_copy(update={"content": "Error: source_unavailable"})
    failed_policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text=user_text,
        messages=messages,
    )
    assert failed_policy.allow_current_page is False


@pytest.mark.parametrize("user_text", [
    "继续帮我分析一下这个模型", "帮我看看代码", "继续写代码", "查一下天气",
])
def test_browser_history_does_not_authorize_unrelated_followups(user_text: str) -> None:
    messages = [
        Message(role="user", content="看看当前页面"),
        Message(role="tool", name="user_browser_read_current_page",
                tool_call_id="browser-1", content='{"ok": true}'),
        Message(role="user", content=user_text),
    ]
    policy = BrowserToolIntentPolicy.for_turn(current_turn_text=user_text, messages=messages)
    assert policy.is_tool_visible("user_browser_read_current_page") is False
    assert policy.tool_call_error("user_browser_read_page", {}) is not None


def test_browser_continuation_does_not_reuse_context_across_an_unrelated_turn() -> None:
    messages = [
        Message(role="user", content="看看当前页面"),
        Message(role="tool", name="user_browser_read_current_page",
                tool_call_id="browser-1", content='{"ok": true}'),
        Message(role="user", content="解释一下冒泡排序"),
        Message(role="assistant", content="冒泡排序通过比较相邻元素排序。"),
        Message(role="user", content="继续帮我分析一下"),
    ]
    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="继续帮我分析一下", messages=messages,
    )
    assert policy.allow_current_page is False


def test_browser_continuation_does_not_hide_either_backend() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="抓取公开网页"),
        Message(role="assistant", content=""),
        Message(
            role="tool",
            name="managed_browser_navigate",
            tool_call_id="browser-1",
            content='{"ok": true}',
        ),
        Message(role="user", content="继续"),
    ]

    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="继续",
        messages=messages,
    )

    assert policy.is_tool_visible("managed_browser_navigate") is True
    assert policy.is_tool_visible("user_browser_open_tab_and_read") is True


def test_submit_continuation_allows_connector_only_after_human_confirmation() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="填写当前页面"),
        Message(role="assistant", content=""),
        Message(
            role="tool",
            name="user_browser_fill",
            tool_call_id="browser-1",
            content='{"ok": true}',
        ),
        Message(role="user", content="现在提交吧"),
    ]

    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="现在提交吧",
        messages=messages,
    )

    assert policy.human_handoff is False
    assert policy.is_tool_visible("user_browser_submit") is True


def test_browser_policy_blocks_current_page_tools_without_explicit_intent() -> None:
    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="看一下gpt sol 极高和最高的区别",
        messages=[],
    )

    assert policy.is_tool_visible("user_browser_read_current_page") is False
    assert policy.is_tool_visible("user_browser_snapshot") is False
    assert policy.is_tool_visible("user_browser_open_tab_and_read") is True
    assert policy.tool_call_error("user_browser_read_page", {}) is not None
    assert policy.tool_call_error("user_browser_read_page", {"url": "https://example.com"}) is None
    assert (
        policy.tool_call_error(
            "user_browser_session_call",
            {"action": "read_article", "args": {}},
        )
        is not None
    )


def test_browser_policy_applies_same_guards_to_public_namespaces() -> None:
    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="检索一个公开网页",
        messages=[],
    )

    assert policy.is_tool_visible("managed_browser_navigate") is True
    assert policy.is_tool_visible("user_browser_read_current_page") is False
    assert policy.is_tool_visible("user_browser_snapshot") is False
    assert policy.tool_call_error("user_browser_read_page", {}) is not None
    assert (
        policy.tool_call_error(
            "user_browser_read_page",
            {"url": "https://example.com"},
        )
        is None
    )


def test_browser_policy_exposes_both_backends_for_public_retrieval() -> None:
    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="用爬虫批量抓取这些公开网页",
        messages=[],
    )

    assert policy.is_tool_visible("managed_browser_navigate") is True
    assert policy.is_tool_visible("managed_browser_snapshot") is True
    assert policy.is_tool_visible("user_browser_open_tab_and_read") is True
    assert policy.is_tool_visible("user_browser_read_page") is True
    assert policy.tool_call_error("user_browser_open_tab_and_read", {"url": "https://example.com"}) is None


def test_browser_policy_keeps_human_review_in_real_browser_without_submitting() -> None:
    policy = BrowserToolIntentPolicy.for_turn(
        current_turn_text="填写这个表单，填好让我检查，最后我点击提交",
        messages=[],
    )

    assert policy.allow_current_page is True
    assert policy.human_handoff is True
    assert policy.is_tool_visible("user_browser_snapshot") is True
    assert policy.is_tool_visible("user_browser_fill") is True
    assert policy.is_tool_visible("user_browser_submit") is False
    assert policy.is_tool_visible("managed_browser_navigate") is True
    assert policy.tool_call_error("user_browser_submit", {"confirmed": True}) is not None
    assert policy.tool_call_error("managed_browser_navigate", {"url": "https://example.com"}) is None


class _CapturingLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.tool_name_calls: list[list[str]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.tool_name_calls.append([tool.name for tool in tools or []])
        response = self._responses.pop(0)
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(
            type="finish",
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
        )


class _CountingCurrentPageTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "user_browser_read_current_page"

    @property
    def description(self) -> str:
        return "Read the current browser page"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content='{"ok": true}')


class _CountingNamedBrowserTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Test browser tool: {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "additionalProperties": True}

    async def execute(self, **_kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content='{"ok": true}')


def _current_page_call() -> ToolCall:
    return ToolCall(
        id="browser-call",
        type="function",
        function=FunctionCall(name="user_browser_read_current_page", arguments={}),
    )


def _browser_call(name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(
        id="browser-call",
        type="function",
        function=FunctionCall(name=name, arguments=arguments or {}),
    )


async def _collect_events(iterator):
    return [event async for event in iterator]


@pytest.mark.asyncio
async def test_core_hides_and_denies_current_page_tool_for_generic_request() -> None:
    tool = _CountingCurrentPageTool()
    llm = _CapturingLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_current_page_call()],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="直接回答", finish_reason="stop"),
        ]
    )

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="system"),
                Message(role="user", content="看一下gpt sol 极高和最高的区别"),
            ],
            tools={tool.name: tool},
            max_steps=3,
            current_turn_text="看一下gpt sol 极高和最高的区别",
        )
    )

    assert "user_browser_read_current_page" not in llm.tool_name_calls[0]
    assert tool.calls == 0
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is False
    assert result.user_visible is False
    assert "CURRENT_PAGE_INTENT_REQUIRED" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", [
    "看一下当前页面里的两个选项", "看看我打开的那个网页", "帮我看看这个网站",
    "继续帮我分析一下", "帮我看看",
])
async def test_core_exposes_and_executes_current_page_tool_for_explicit_request(user_text: str) -> None:
    tool = _CountingCurrentPageTool()
    llm = _CapturingLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_current_page_call()],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="已读取", finish_reason="stop"),
        ]
    )

    messages = [Message(role="system", content="system")]
    if user_text in {"继续帮我分析一下", "帮我看看"}:
        messages.extend([
            Message(role="user", content="读取当前页面"),
            Message(role="assistant", content="", tool_calls=[_current_page_call()]),
            Message(role="tool", name=tool.name, tool_call_id="browser-call",
                    content='{"ok": true}'),
            Message(role="assistant", content="已读取页面"),
        ])
    messages.append(Message(role="user", content=user_text))
    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={tool.name: tool},
            max_steps=3,
            current_turn_text=user_text,
        )
    )

    assert "user_browser_read_current_page" in llm.tool_name_calls[0]
    assert tool.calls == 1
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is True
    assert result.user_visible is True


@pytest.mark.asyncio
async def test_core_exposes_and_executes_either_backend_for_public_retrieval() -> None:
    connector = _CountingNamedBrowserTool("user_browser_open_tab_and_read")
    playwright = _CountingNamedBrowserTool("managed_browser_navigate")
    llm = _CapturingLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _browser_call(
                        "user_browser_open_tab_and_read",
                        {"url": "https://example.com"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="已完成", finish_reason="stop"),
        ]
    )

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="system"),
                Message(role="user", content="用爬虫抓取这个公开网页"),
            ],
            tools={
                connector.name: connector,
                playwright.name: playwright,
            },
            max_steps=3,
            current_turn_text="用爬虫抓取这个公开网页",
        )
    )

    assert "user_browser_open_tab_and_read" in llm.tool_name_calls[0]
    assert "managed_browser_navigate" in llm.tool_name_calls[0]
    assert connector.calls == 1
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is True
    assert result.user_visible is True


@pytest.mark.asyncio
async def test_core_stops_before_submit_when_user_wants_human_handoff() -> None:
    fill = _CountingNamedBrowserTool("user_browser_fill")
    submit = _CountingNamedBrowserTool("user_browser_submit")
    playwright = _CountingNamedBrowserTool("managed_browser_navigate")
    llm = _CapturingLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _browser_call(
                        "user_browser_submit",
                        {"confirmed": True},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="请检查后手动提交", finish_reason="stop"),
        ]
    )

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="system"),
                Message(
                    role="user",
                    content="填写这个表单，填好让我检查，最后我点击提交",
                ),
            ],
            tools={
                fill.name: fill,
                submit.name: submit,
                playwright.name: playwright,
            },
            max_steps=3,
            current_turn_text="填写这个表单，填好让我检查，最后我点击提交",
        )
    )

    assert "user_browser_fill" in llm.tool_name_calls[0]
    assert "user_browser_submit" not in llm.tool_name_calls[0]
    assert "managed_browser_navigate" in llm.tool_name_calls[0]
    assert submit.calls == 0
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is False
    assert result.user_visible is False
    assert "HUMAN_FINAL_ACTION_REQUIRED" in (result.error or "")
