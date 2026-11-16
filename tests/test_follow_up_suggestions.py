
from tests.acp_helpers import acp_params
from acp.schema import SessionNotification
import asyncio
from copy import copy
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.acp import follow_up_suggestions as suggestions_module
from box_agent.acp.follow_up_suggestions import (
    FollowUpSuggestionsStreamExtractor,
    build_follow_up_suggestions_generation_system_prompt,
    build_follow_up_suggestions_prompt,
    normalize_follow_up_suggestions,
    parse_follow_up_suggestions_response,
)
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall


class _RecordingConn:
    def __init__(self) -> None:
        self.updates = []

    async def session_update(self, session_id, update, **meta):
        payload = SessionNotification(session_id=session_id, update=update, field_meta=meta or None)
        self.updates.append(payload)


class _FollowUpLLM:
    fenced = True
    prefix = "已完成方案。\n"

    async def generate(self, messages, tools=None):
        return LLMResponse(content="完成。", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        yield StreamEvent(
            type="text",
            delta=(
                self.prefix + ("```" if self.fenced else "") + "follow_up_suggestions\n"
                '{"suggestions":["把方案拆成'
            ),
        )
        yield StreamEvent(
            type="activity",
            activity={"protocol": "agent_activity_v1", "phase": "provider_stream"},
        )
        yield StreamEvent(
            type="text",
            delta='今日待办", "给我一份风险清单"]}' + ("\n```" if self.fenced else ""),
        )
        yield StreamEvent(type="finish", finish_reason="stop")


class _DedicatedFollowUpLLM:
    model = "follow-up-model"
    max_output_tokens = 100_000

    def __init__(self) -> None:
        self.generate_calls = []
        self.release_suggestions = asyncio.Event()

    def for_model(self, model, *, max_output_tokens=None):
        cloned = copy(self)
        cloned.model = model
        if max_output_tokens is not None:
            cloned.max_output_tokens = max_output_tokens
        return cloned

    async def generate(self, messages, tools=None, **_):
        if _.get("call_kind") == "turn_continuation_judge":
            return LLMResponse(content='{"continue":false}', finish_reason="stop")
        self.generate_calls.append({**_, "max_tokens": self.max_output_tokens,
                                    "messages": [message.model_copy(deep=True) for message in messages]})
        await self.release_suggestions.wait()
        return LLMResponse(
            content='{"suggestions":["核对项目当前的 React 版本", "查看 React 19.2 的新增内容"]}',
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, **_):
        yield StreamEvent(type="text", delta="React 当前 npm 稳定最新版是 19.2.7。")
        yield StreamEvent(type="finish", finish_reason="stop")


class _JudgedFollowUpLLM(_FollowUpLLM):
    def __init__(self):
        self.generate_calls = []

    async def generate(self, messages, tools=None, **kwargs):
        self.generate_calls.append(kwargs.get("call_kind"))
        assert kwargs.get("call_kind") == "turn_continuation_judge"
        return LLMResponse(content='{"continue":false}', finish_reason="stop")


class _DecisionThenStopLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools=None, **_):
        return LLMResponse(content="done", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="choose-delivery-scope",
                        type="function",
                        function=FunctionCall(
                            name="request_user_decision",
                            arguments={
                                "question": "请选择交付范围。",
                                "decision_kind": "delivery_scope",
                                "options": [
                                    {"id": "full", "label": "保持完整版本"},
                                    {"id": "prototype", "label": "先做精简版本"},
                                ],
                                "default_option_id": "full",
                                "requested_auto_submit_seconds": 30,
                                "risk_level": "low",
                                "reversible": True,
                                "preserves_user_intent": True,
                            },
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(
            type="text",
            delta=(
                "请在选择卡片中继续。\n```follow_up_suggestions\n"
                '{"suggestions":["选择完整版本"]}\n```'
            ),
        )
        yield StreamEvent(type="finish", finish_reason="stop")


def test_extractor_hides_split_metadata_block_and_keeps_visible_answer() -> None:
    extractor = FollowUpSuggestionsStreamExtractor()

    first = extractor.push("已整理好执行方案。\n```follow_up_sugg")
    second = extractor.push(
        'estions\n{"suggestions":["把方案拆成今日待办", "给我一份风险清单"]}\n```'
    )
    final = extractor.finish()

    assert "".join(first + second + final) == "已整理好执行方案。\n"
    assert extractor.suggestions == ["把方案拆成今日待办", "给我一份风险清单"]


def test_extractor_drops_invalid_or_unfinished_metadata() -> None:
    extractor = FollowUpSuggestionsStreamExtractor()

    assert extractor.push("回答。\n```follow_up_suggestions\n{bad json}\n```") == ["回答。\n"]
    assert extractor.suggestions == []

    extractor.push("\n```follow_up_suggestions\n")
    assert extractor.finish() == []


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_extractor_hides_bare_metadata_at_every_split(newline) -> None:
    prefix = "小鸟图片已生成完成。" + newline
    metadata = 'follow_up_suggestions' + newline * 2 + '{"suggestions":["换个风格", "生成空中的小鸟"]}'
    text = prefix + metadata + newline + "后续正文"
    for split in range(len(text) + 1):
        extractor = FollowUpSuggestionsStreamExtractor()
        chunks = extractor.push(text[:split]) + extractor.push(text[split:]) + extractor.finish()
        assert "".join(chunks) == prefix + newline + "后续正文", split
        assert extractor.suggestions == ["换个风格", "生成空中的小鸟"], split
    extractor = FollowUpSuggestionsStreamExtractor()
    visible = []
    for char in text:
        visible.extend(extractor.push(char))
    visible.extend(extractor.finish())
    assert "".join(visible) == prefix + newline + "后续正文"
    assert extractor.suggestions == ["换个风格", "生成空中的小鸟"]


@pytest.mark.parametrize("text", [
    '正文提到 follow_up_suggestions\n{"suggestions":["继续"]}',
    'follow_up_suggestions 是协议名称。',
    'follow_up_suggestions_extra\n{"suggestions":["继续"]}',
    'follow_up_suggestions\n普通正文',
    'follow_up_suggestions\n{"other":["继续"]}',
    'follow_up_suggestions\n{"suggestions":"继续"}',
    'follow_up_suggestions\n{bad json}',
    'follow_up_suggestions\n{"suggestions":["未完成',
    'follow_up_sugg',
])
def test_extractor_preserves_non_metadata_bare_text(text) -> None:
    extractor = FollowUpSuggestionsStreamExtractor()
    visible = []
    for char in text:
        visible.extend(extractor.push(char))
    visible.extend(extractor.finish())
    assert "".join(visible) == text
    assert extractor.suggestions == []


def test_extractor_releases_oversized_unconfirmed_bare_text() -> None:
    for suffix in [" " * 17_000, '\n{"unfinished":"' + "x" * 17_000]:
        text = "follow_up_suggestions" + suffix
        extractor = FollowUpSuggestionsStreamExtractor()
        assert "".join(extractor.push(text)) == text
        assert extractor.finish() == []
        assert extractor.suggestions == []


@pytest.mark.parametrize("candidate", ["{bad json}", '{"suggestions":["未完成'])
def test_extractor_recovers_fenced_suggestions_after_bad_bare_candidate(candidate):
    visible = f"回答。\nfollow_up_suggestions\n{candidate}\n"
    fenced = '```follow_up_suggestions\n{"suggestions":["继续"]}\n```'
    extractor = FollowUpSuggestionsStreamExtractor()
    chunks = extractor.push(visible)
    for char in fenced:
        chunks.extend(extractor.push(char))
    assert "".join(chunks) == visible
    assert extractor.suggestions == ["继续"]
    assert extractor.finish() == []


def test_extractor_recovers_bare_suggestions_after_bad_candidate_at_every_split():
    visible = 'follow_up_suggestions\n{"unterminated"\n'
    text = visible + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    for split in range(len(text) + 1):
        extractor = FollowUpSuggestionsStreamExtractor()
        chunks = extractor.push(text[:split]) + extractor.push(text[split:]) + extractor.finish()
        assert "".join(chunks) == visible, split
        assert extractor.suggestions == ["继续"], split


def test_extractor_applies_bare_limit_independently_of_chunk_size():
    prefix = 'follow_up_suggestions\n{"suggestions":["继续"],"extra":"'
    limit = 16_384
    for size in (limit, limit + 1):
        text = prefix + "x" * (size - len(prefix) - 2) + '"}'
        for split in (0, limit - 1, limit, len(text)):
            extractor = FollowUpSuggestionsStreamExtractor()
            chunks = extractor.push(text[:split]) + extractor.push(text[split:]) + extractor.finish()
            assert "".join(chunks) == ("" if size == limit else text)
            assert extractor.suggestions == (["继续"] if size == limit else [])


@pytest.mark.parametrize("opening, closing", [
    ("```text", "```"),
    ("  ~~~~json", "  ~~~~~  "),
    ("````text\r\n```", "````"),
])
def test_extractor_preserves_markdown_examples_at_every_split(opening, closing):
    example = (
        f"示例：\r\n{opening}\r\n"
        'follow_up_suggestions\r\n{"suggestions":["示例"]}\r\n'
        f"{closing}\r\n"
    )
    text = example + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    for split in range(len(text) + 1):
        extractor = FollowUpSuggestionsStreamExtractor()
        chunks = extractor.push(text[:split]) + extractor.push(text[split:]) + extractor.finish()
        assert "".join(chunks) == example, split
        assert extractor.suggestions == ["继续"], split


@pytest.mark.parametrize("example", [
    "- ```text\n  follow_up_suggestions\n  {\"suggestions\":[\"示例\"]}\n  ```\n\n",
    "10. ~~~text\n    示例\n    ~~~\n\n",
    "1.  ```text\n    示例\n\n",
    "- ```text\n\t示例\n  ```\n\n",
    "- ```text\n \t示例\n  ```\n\n",
    "- ```text\n  示例\n\n```text\nfollow_up_suggestions\n"
    "{\"suggestions\":[\"示例\"]}\n```\n\n",
])
def test_extractor_accepts_top_level_metadata_after_list_code_fence(example):
    text = example + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    for split in range(len(text) + 1):
        extractor = FollowUpSuggestionsStreamExtractor()
        chunks = extractor.push(text[:split]) + extractor.push(text[split:]) + extractor.finish()
        assert "".join(chunks) == example, split
        assert extractor.suggestions == ["继续"], split


@pytest.mark.parametrize("extra", [
    pytest.param("9" * 5000, id="oversized-integer"),
    pytest.param("NaN", id="invalid-constant"),
])
def test_extractor_preserves_bare_payload_on_decoder_errors(extra):
    text = 'follow_up_suggestions\n{"suggestions":["继续"],"extra":' + extra + '}\n后续正文。'
    extractor = FollowUpSuggestionsStreamExtractor()
    assert "".join(extractor.push(text) + extractor.finish()) == text
    assert extractor.suggestions == []


def test_extractor_preserves_bare_payload_if_decoder_hits_recursion_limit(monkeypatch):
    def decode(*args):
        raise RecursionError

    # The native decoder's recursion limit varies by Python version and platform.
    monkeypatch.setattr(suggestions_module._BARE_DECODER, "raw_decode", decode)
    text = 'follow_up_suggestions\n{"suggestions":["继续"]}'
    extractor = FollowUpSuggestionsStreamExtractor()
    assert "".join(extractor.push(text) + extractor.finish()) == text
    assert extractor.suggestions == []


def test_extractor_waits_for_split_number_that_completes_as_a_float():
    prefix = 'follow_up_suggestions\n{"suggestions":["继续"],"extra":' + "9" * 5000
    extractor = FollowUpSuggestionsStreamExtractor()
    assert extractor.push(prefix) == []
    assert extractor.push("e-5000}") + extractor.finish() == []
    assert extractor.suggestions == ["继续"]


def test_extractor_bare_json_handles_escaped_quotes_braces_and_empty_suggestions() -> None:
    extractor = FollowUpSuggestionsStreamExtractor()
    text = 'follow_up_suggestions\n{"suggestions":["解释 } 和 \\"引号\\"", "次へ", "Continue"]}'
    assert extractor.push(text) + extractor.finish() == []
    assert extractor.suggestions == ['解释 } 和 "引号"', '次へ', 'Continue']
    extractor = FollowUpSuggestionsStreamExtractor()
    assert extractor.push('follow_up_suggestions\n{"suggestions":[]}') + extractor.finish() == []
    assert extractor.suggestions == []


def _assert_extraction_at_every_split(text, expected, suggestions):
    chunkings = [[text[:split], text[split:]] for split in range(len(text) + 1)]
    chunkings.append(list(text))
    for chunks in chunkings:
        extractor = FollowUpSuggestionsStreamExtractor()
        visible = [piece for chunk in chunks for piece in extractor.push(chunk)]
        visible.extend(extractor.finish())
        assert "".join(visible) == expected, chunks
        assert extractor.suggestions == suggestions, chunks


@pytest.mark.parametrize("opening, closing", [
    ("<follow_up_suggestions>", "</follow_up_suggestions>"),
    ("follow_up_suggestions\n", "</follow_up_suggestions>"),
    ("```follow_up_suggestions\n", "</follow_up_suggestions>"),
    ("<follow_up_suggestions>\n", "```"),
    ("```follow_up_suggestions\n", "```"),
])
def test_extractor_hides_xml_and_mixed_metadata_at_every_split(opening, closing):
    text = '回答。\n' + opening + '{"suggestions":["继续"]}\n' + closing + '\n后续正文。'
    _assert_extraction_at_every_split(text, '回答。\n\n后续正文。', ["继续"])


@pytest.mark.parametrize("opening, closing", [
    ("```follow_up_suggestions\n", "```"),
    ("<follow_up_suggestions>", "</follow_up_suggestions>"),
    ("follow_up_suggestions\n", "</follow_up_suggestions>"),
])
def test_extractor_does_not_close_on_delimiters_inside_json_strings(opening, closing):
    text = (
        '回答。\n' + opening
        + '{"suggestions":["解释 ``` 和 </follow_up_suggestions> 的差异"]}\n'
        + closing + '\n后续正文。'
    )
    _assert_extraction_at_every_split(
        text, '回答。\n\n后续正文。', ["解释 ``` 和 </follow_up_suggestions> 的差异"],
    )


@pytest.mark.parametrize("text", [
    '````markdown\n```follow_up_suggestions\n{"suggestions":["示例"]}\n```\n````',
    '~~~text\n<follow_up_suggestions>{"suggestions":["示例"]}</follow_up_suggestions>\n~~~',
    '```follow_up_suggestions_example\n{"suggestions":["示例"]}\n```',
    '`<follow_up_suggestions>{"suggestions":["示例"]}</follow_up_suggestions>`',
    '正文中的 ```follow_up_suggestions\n{"suggestions":["示例"]}\n```',
    '> <follow_up_suggestions>{"suggestions":["示例"]}</follow_up_suggestions>',
    '    <follow_up_suggestions>{"suggestions":["示例"]}</follow_up_suggestions>',
    '<follow_up_suggestions_example>{"suggestions":["示例"]}</follow_up_suggestions_example>',
])
def test_extractor_preserves_metadata_examples_and_similar_names(text):
    _assert_extraction_at_every_split(text, text, [])


@pytest.mark.parametrize("opening", ["```follow_up_suggestions\n", "<follow_up_suggestions>"])
def test_extractor_keeps_answer_after_metadata_missing_its_close(opening):
    text = '回答。\n' + opening + '{"suggestions":["继续"]}\n后续正文。'
    _assert_extraction_at_every_split(text, '回答。\n\n后续正文。', ["继续"])


@pytest.mark.parametrize("opening", ["```follow_up_suggestions\n", "<follow_up_suggestions>"])
@pytest.mark.parametrize("next_opening", ["```follow_up_suggestions\n", "<follow_up_suggestions>"])
def test_extractor_recovers_a_new_block_after_truncated_explicit_metadata(opening, next_opening):
    text = (
        '回答。\n' + opening + '{"suggestions":["未完成\n'
        + next_opening + '{"suggestions":["继续"]}\n</follow_up_suggestions>\n后续正文。'
    )
    _assert_extraction_at_every_split(text, '回答。\n\n后续正文。', ["继续"])


def test_extractor_bounds_oversized_explicit_metadata_without_leaking_its_tail():
    prefix = '回答。\n```follow_up_suggestions\n{"suggestions":["'
    text = prefix + "x" * 17_000 + '</follow_up_suggestions> ``` more"]}\n```\n后续正文。'
    for chunks in ([text], [text[:16_384], text[16_384:]], [text[i:i + 31] for i in range(0, len(text), 31)]):
        extractor = FollowUpSuggestionsStreamExtractor()
        visible = []
        for chunk in chunks:
            visible.extend(extractor.push(chunk))
            assert len(extractor._buffer) <= 16_384
        visible.extend(extractor.finish())
        assert "".join(visible) == '回答。\n\n后续正文。'
        assert extractor.suggestions == []


@pytest.mark.parametrize("suffix", [
    '\n<',
    '\n</follow_up_sugg',
    '\n```\nfollow_up_suggestions\n{"suggestions":["代码示例"]}\n```',
])
def test_extractor_preserves_code_and_unconfirmed_closers_after_bare_json(suffix):
    text = 'follow_up_suggestions\n{"suggestions":["继续"]}' + suffix
    _assert_extraction_at_every_split(text, suffix, ["继续"])


@pytest.mark.parametrize("payload", ["123", "1e20", "-1.25", "null", '["not an object"]'])
def test_extractor_discards_invalid_explicit_scalar_payloads_at_every_split(payload):
    text = '回答。\n```follow_up_suggestions\n' + payload + '\n```\n后续正文。'
    _assert_extraction_at_every_split(text, '回答。\n\n后续正文。', [])


@pytest.mark.parametrize("opening, closing", [
    ("<pre>", "</pre>"),
    ("<code>", "</code>"),
    ("<pre><code>", "</code></pre>"),
    ('<pre class="example"><code data-language="json">', "</code></pre>"),
    ('<PRE class="example">', "</PRE>"),
])
@pytest.mark.parametrize("metadata", [
    'follow_up_suggestions\n{"suggestions":["示例"]}',
    '```follow_up_suggestions\n{"suggestions":["示例"]}\n```',
    '<follow_up_suggestions>{"suggestions":["示例"]}</follow_up_suggestions>',
])
def test_extractor_preserves_html_code_containers_at_every_split(opening, closing, metadata):
    example = '示例：\n' + opening + '\n' + metadata + '\n' + closing + '\n正文。\n'
    text = example + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    _assert_extraction_at_every_split(text, example, ["继续"])


def test_extractor_ignores_html_tags_inside_markdown_code_examples():
    example = '```html\n<pre>\n```\n'
    text = example + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    _assert_extraction_at_every_split(text, example, ["继续"])


@pytest.mark.parametrize("literal", [
    '{"suggestions":["解释 </pre> 和 </code> 的作用"]}',
    '{"suggestions":["解释 \\"</pre>\\" 和 </code> 的作用"]}',
])
def test_extractor_preserves_quoted_html_closers_inside_code_containers(literal):
    block = 'follow_up_suggestions\n{"suggestions":["继续"]}'
    example = '<pre><code>\n' + literal + '\n' + block + '\n</code></pre>\n'
    _assert_extraction_at_every_split(example + block, example, ["继续"])


def test_extractor_accepts_crlf_closing_fence_at_every_split():
    text = '回答。\r\n```follow_up_suggestions\r\n{"suggestions":["继续"]}\r\n```\r\n后续正文。'
    _assert_extraction_at_every_split(text, '回答。\r\n\r\n后续正文。', ["继续"])


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_extractor_accepts_markdown_fence_indentation_at_every_split(indent):
    text = '回答。\n' + indent + '```follow_up_suggestions\n{"suggestions":["继续"]}\n' + indent + '```\n后续正文。'
    _assert_extraction_at_every_split(text, '回答。\n\n后续正文。', ["继续"])


@pytest.mark.parametrize("example", [
    '    ```follow_up_suggestions\n    {"suggestions":["示例"]}\n    ```\n',
    '\t```follow_up_suggestions\n\t{"suggestions":["示例"]}\n\t```\n',
    '````markdown\n ```follow_up_suggestions\n{"suggestions":["示例"]}\n ```\n````\n',
])
def test_extractor_preserves_indented_and_nested_fence_examples(example):
    _assert_extraction_at_every_split(example, example, [])


@pytest.mark.parametrize("opening, closing", [
    ("Example: <code>", "</code>"),
    ("Here's <code>", "</code>"),
    ('Example: <pre class="example"><code>', "</code></pre>"),
    ("<div><pre>", "</pre></div>"),
    ("<code><code>inner</code>", "</code>"),
    ("<code>\nconst tag = '</code>';", "</code>"),
    ("<code>\nconst tag = `</code>`;", "</code>"),
    ('<pre data-title="a > </pre> b">', "</pre>"),
])
def test_extractor_preserves_html_code_after_inline_prose(opening, closing):
    example = opening + '\nfollow_up_suggestions\n{"suggestions":["Example"]}\n' + closing + '\n'
    _assert_extraction_at_every_split(example, example, [])


@pytest.mark.parametrize("prefix", [
    'Example: ` <code> `\n',
    'Example: `` <code> ``\n',
    'Example: \\<code>\n',
    'Example: &lt;code&gt;\n',
])
def test_extractor_does_not_treat_quoted_or_escaped_tags_as_html_containers(prefix):
    text = prefix + 'follow_up_suggestions\n{"suggestions":["继续"]}'
    _assert_extraction_at_every_split(text, prefix, ["继续"])


@pytest.mark.parametrize("prefix", [
    '<span title="<code>">Example</span>\n',
    "<span title='<pre> > <code>'>Example</span>\n",
    '<!-- <code> -->\n',
    '<!-- > <pre> and <code> -->\n',
])
def test_extractor_does_not_open_code_containers_from_attributes_or_comments(prefix):
    text = prefix + 'follow_up_suggestions\n{"suggestions":["Next"]}'
    _assert_extraction_at_every_split(text, prefix, ["Next"])


@pytest.mark.parametrize("tag", ["pre", "code"])
@pytest.mark.parametrize("word", ["don't", "users'"])
def test_extractor_does_not_treat_apostrophes_in_html_code_as_quoted_strings(tag, word):
    prefix = f'Example: <{tag}>{word}</{tag}>\n'
    text = prefix + '```follow_up_suggestions\n{"suggestions":["Next"]}\n```'
    _assert_extraction_at_every_split(text, prefix, ["Next"])


@pytest.mark.parametrize("ticks", ["`", "``"])
def test_extractor_preserves_metadata_inside_closed_multiline_inline_code(ticks):
    example = 'Example: ' + ticks + '\nfollow_up_suggestions\n{"suggestions":["Example"]}\n' + ticks + '\n'
    text = example + 'follow_up_suggestions\n{"suggestions":["Next"]}'
    _assert_extraction_at_every_split(text, example, ["Next"])


@pytest.mark.parametrize("ticks", ["`", "``"])
@pytest.mark.parametrize("fenced", [False, True])
def test_extractor_accepts_metadata_after_unmatched_inline_backticks(ticks, fenced):
    prefix = 'An unmatched ' + ticks + ' is ordinary text.\n'
    metadata = 'follow_up_suggestions\n{"suggestions":["Next"]}'
    text = prefix + ('```' + metadata + '\n```' if fenced else metadata)
    _assert_extraction_at_every_split(text, prefix, ["Next"])


def test_extractor_confirms_inline_closing_run_at_end_of_response():
    example = 'Example: ``\nfollow_up_suggestions\n{"suggestions":["Explain ` ticks"]}\n``'
    _assert_extraction_at_every_split(example, example, [])


def test_extractor_bounds_waiting_for_an_unmatched_inline_code_span():
    prefix = 'Example: `\n'
    metadata = 'follow_up_suggestions\n{"suggestions":["Next"]}'
    suffix = '\n' + 'x' * 17_000
    text = prefix + metadata + suffix
    chunkings = [[text], [text[:16_384], text[16_384:]], [text[i:i + 97] for i in range(0, len(text), 97)]]
    for chunks in chunkings:
        extractor = FollowUpSuggestionsStreamExtractor()
        visible = []
        for chunk in chunks:
            visible.extend(extractor.push(chunk))
            assert len(extractor._buffer) <= 16_384
        visible.extend(extractor.finish())
        assert ''.join(visible) == prefix + suffix
        assert extractor.suggestions == ["Next"]


def test_extractor_gives_fenced_blocks_priority_over_unmatched_inline_backticks():
    example = 'Example: `\n```python\nfollow_up_suggestions\n{"suggestions":["Example"]}\n```\n'
    text = example + '```follow_up_suggestions\n{"suggestions":["Next"]}\n```'
    _assert_extraction_at_every_split(text, example, ["Next"])


@pytest.mark.parametrize("prefix, suffix", [
    ('Example: `\n\n', '\n`'),
    ('Example: `\n', '\n\n`'),
    ('Example: `\n', '\n```python\ntext `\n```'),
])
def test_extractor_does_not_extend_inline_code_across_block_boundaries(prefix, suffix):
    text = prefix + 'follow_up_suggestions\n{"suggestions":["Next"]}' + suffix
    _assert_extraction_at_every_split(text, prefix + suffix, ["Next"])


@pytest.mark.parametrize("closing", ["``` ", "````\t\r"])
def test_extractor_recovers_from_oversized_truncated_metadata_with_padded_closer(closing):
    text = '回答。\n```follow_up_suggestions\n{"suggestions":["' + "x" * 17_000 + '\n' + closing + '\n后续正文。'
    expected = '回答。\n' + ('\r' if closing.endswith('\r') else '') + '\n后续正文。'
    chunkings = [[text], list(text), [text[index:index + 97] for index in range(0, len(text), 97)]]
    close_start = text.index(closing)
    chunkings.extend([text[:split], text[split:]] for split in range(close_start, len(text) + 1))
    for chunks in chunkings:
        extractor = FollowUpSuggestionsStreamExtractor()
        visible = [part for chunk in chunks for part in extractor.push(chunk)]
        visible.extend(extractor.finish())
        assert ''.join(visible) == expected, list(map(len, chunks))
        assert extractor.suggestions == []


def test_normalize_follow_up_suggestions_limits_and_deduplicates() -> None:
    assert normalize_follow_up_suggestions(
        ["  整理成待办  ", "整理成待办", "再生成风险清单", "补充负责人", "第四条"]
    ) == ["整理成待办", "再生成风险清单", "补充负责人"]


def test_parser_accepts_json_and_accidental_fences() -> None:
    assert parse_follow_up_suggestions_response(
        '```json\n{"suggestions":["整理迁移步骤"]}\n```'
    ) == ["整理迁移步骤"]
    assert parse_follow_up_suggestions_response("不是 JSON") == []


def test_prompt_requires_a_structured_follow_up_block_only_after_completion() -> None:
    prompt = build_follow_up_suggestions_prompt()

    assert "```follow_up_suggestions" in prompt
    assert "任务失败" in prompt
    assert '"suggestions"' in build_follow_up_suggestions_generation_system_prompt()


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_enabled", [False, True])
@pytest.mark.parametrize("fenced, invalid_bare", [
    (False, ""),
    (True, ""),
    pytest.param(
        True,
        'follow_up_suggestions\n{"suggestions":["忽略"],"extra":' + "9" * 5000 + '}\n后续正文。\n',
        id="fenced-after-decoder-error",
    ),
])
async def test_acp_strips_model_metadata_across_activity_and_emits_suggestions(
    tmp_path, judge_enabled, fenced, invalid_bare,
) -> None:
    conn = _RecordingConn()
    llm = _JudgedFollowUpLLM() if judge_enabled else _FollowUpLLM()
    llm.fenced = fenced
    llm.prefix += invalid_bare
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.new_session(
        **acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"follow_up_suggestions": True}))
    )

    assert agent._sessions[session.sessionId].follow_up_suggestions_enabled is True
    response = await agent.prompt(
        **acp_params(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "做完了吗"}]))
    )

    assert response.stopReason == "end_turn"
    if judge_enabled:
        assert llm.generate_calls == ["turn_continuation_judge"]
        assert agent._sessions[session.sessionId].follow_up_suggestions_task is None
    visible_text = "".join(
        update.update.content.text
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
    )
    assert visible_text == "已完成方案。\n" + invalid_bare
    suggestion_updates = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert suggestion_updates == [
        {
            "type": "follow_up_suggestions",
            "suggestions": ["把方案拆成今日待办", "给我一份风险清单"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_skill_catalog", [False, True])
async def test_acp_generates_suggestions_in_background_without_blocking_prompt(
    tmp_path,
    monkeypatch,
    with_skill_catalog,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conn = _RecordingConn()
    llm = _DedicatedFollowUpLLM()
    loader = None
    if with_skill_catalog:
        from box_agent.tools.skill_loader import SkillLoader
        folder = tmp_path / "skills" / "react-helper"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: react-helper\ndescription: React version helper\nkeywords: [React]\n---\nMethod\n",
            encoding="utf-8",
        )
        loader = SkillLoader(folder.parent)
        loader.discover_skills()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
        skill_loader=loader,
    )
    session = await agent.new_session(
        **acp_params(SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "follow_up_suggestions": True,
                "session_id": "office-session-1",
                "title": "React 版本查询",
            },
        ))
    )

    response = await agent.prompt(
        **acp_params(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "React 最新版是多少"}],
            field_meta={"turn_id": "office-turn-1", "title": "React 版本查询"},
        ))
    )

    assert response.stopReason == "end_turn"
    suggestion_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert suggestion_task is not None
    assert not suggestion_task.done()
    assert not any(
        getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
        for update in conn.updates
    )

    llm.release_suggestions.set()
    await suggestion_task
    suggestion_input = "\n".join(str(message.content) for message in llm.generate_calls[-1]["messages"])
    assert "React 最新版是多少" in suggestion_input
    assert "[Host Skill catalog:" not in suggestion_input
    if with_skill_catalog:
        assert any(message.source == "runtime" and "[Host Skill catalog:" in str(message.content)
                   for message in agent._sessions[session.sessionId].agent.messages)
    assert llm.generate_calls[-1]["max_tokens"] == 8_192
    assert llm.max_output_tokens == 100_000
    suggestion_updates = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert suggestion_updates == [
        {
            "type": "follow_up_suggestions",
            "turn_id": "office-turn-1",
            "suggestions": ["核对项目当前的 React 版本", "查看 React 19.2 的新增内容"],
        }
    ]
    assert llm.generate_calls[-1]["session_id"] == "office-session-1"
    assert llm.generate_calls[-1]["turn_id"] == "office-turn-1"
    assert llm.generate_calls[-1]["title"] == "React 版本查询"


@pytest.mark.asyncio
async def test_acp_cancels_stale_background_suggestions_when_next_turn_starts(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    conn = _RecordingConn()
    llm = _DedicatedFollowUpLLM()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.new_session(
        **acp_params(SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"follow_up_suggestions": True, "session_id": "office-session-1"},
        ))
    )

    await agent.prompt(
        **acp_params(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "第一轮"}],
            field_meta={"turn_id": "office-turn-1"},
        ))
    )
    first_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert first_task is not None

    await agent.prompt(
        **acp_params(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "第二轮"}],
            field_meta={"turn_id": "office-turn-2"},
        ))
    )
    second_task = agent._sessions[session.sessionId].follow_up_suggestions_task
    assert second_task is not None
    assert second_task is not first_task
    await asyncio.sleep(0)
    assert first_task.done()

    llm.release_suggestions.set()
    await second_task
    suggestion_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert [output["turn_id"] for output in suggestion_outputs] == ["office-turn-2"]


@pytest.mark.asyncio
async def test_acp_does_not_mix_follow_up_suggestions_with_user_decision(tmp_path) -> None:
    conn = _RecordingConn()
    llm = _DecisionThenStopLLM()
    agent = BoxACPAgent(
        conn,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(max_steps=8, workspace_dir=str(tmp_path)),
            tools=ToolsConfig(),
        ),
        llm,
        [],
        "system",
    )
    session = await agent.new_session(
        **acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"follow_up_suggestions": True}))
    )

    response = await agent.prompt(
        **acp_params(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "制作演示文稿"}]))
    )

    assert response.stopReason == "end_turn"
    assert agent._sessions[session.sessionId].waiting_for_user_input is True
    raw_outputs = [
        update.update.rawOutput
        for update in conn.updates
        if getattr(update.update, "sessionUpdate", None) == "tool_call_update"
        and isinstance(getattr(update.update, "rawOutput", None), dict)
    ]
    assert any(output.get("type") == "user_decision_request" for output in raw_outputs)
    assert not any(output.get("type") == "follow_up_suggestions" for output in raw_outputs)

    resumed = await agent.prompt(
        **acp_params(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "我不选这两个方案，改成六页并突出案例。"}],
        ))
    )

    assert resumed.stopReason == "end_turn"
    assert agent._sessions[session.sessionId].waiting_for_user_input is False
