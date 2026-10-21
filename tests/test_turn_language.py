"""Deliverable-language hint: only for clearly non-Chinese requests."""

import pytest

from box_agent.turn_language import deliverable_language_hint, request_language, with_deliverable_language_hint


@pytest.mark.parametrize("text,expected", [
    ("Write a short product blurb based on product.md and save it as blurb.md.", "English"),
    ("Please summarize the attached report into three bullet points for the board.", "English"),
    ("帮我写一段产品介绍，存成 intro.md。", None),
    ("把 report.md 翻译成英文 summary，存成 summary_en.md。", None),
    ("ok", None),
    ("continue", None),
    ("continue after restart", None),
    ("/pptx explain this method", None),
    ("/pptx Build a ten-slide deck from notes.md for the quarterly board review.", "English"),
    ("OK继续", None),
    ("`src/box_agent/very/long/path/name.py` ```print('hello world from code')``` 修一下", None),
    ("product.md をもとに短い紹介文を書いて blurb.md に保存してください。", None),
])
def test_request_language(text, expected):
    assert request_language(text) == expected


def test_hint_only_for_non_chinese_and_appended_after_the_request():
    assert deliverable_language_hint("帮我写一段产品介绍") is None
    english = "Write a short product blurb based on product.md and save it as blurb.md."
    message = with_deliverable_language_hint(english)
    assert message.startswith(english)
    assert message.endswith("unless the user asks for a specific language.]")
    assert with_deliverable_language_hint("帮我写一段产品介绍") == "帮我写一段产品介绍"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,hinted", [
    ("Write a short product blurb based on product.md and save it as blurb.md.", True),
    ("根据 product.md 写一段产品介绍，存成 intro.md。", False),
])
async def test_acp_prompt_places_hint_beside_the_user_request(tmp_path, text, hinted):
    """Real ACP turn: hint follows the user's words; host's English prefixes do not trigger it."""
    from types import SimpleNamespace

    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from tests.test_acp import _EMPTY_CONNECTOR_CONTEXT, CaptureMessagesLLM, DummyConn

    config = Config(llm=LLMConfig(api_key="test-key"),
                    agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)), tools=ToolsConfig())
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")
    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))

    await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": text}],
                                       field_meta={"ui_language": "zh"}))

    [user_message] = [content for role, content in llm.calls[0] if role == "user"]
    assert user_message.startswith("[Host UI language: Chinese.")
    assert user_message.endswith(_EMPTY_CONNECTOR_CONTEXT)
    assert ("[Deliverable language: English." in user_message) is hinted
    if hinted:
        assert user_message.index(text) < user_message.index("[Deliverable language")


@pytest.mark.parametrize('text', [
    'Escribe una breve descripción del producto basada en product.md y guárdala como blurb.md.',
    'Bitte schreibe eine kurze Produktbeschreibung und speichere sie in blurb.md.',
    'Rédige une courte description du produit dans blurb.md pour notre équipe.',
    'Escreva uma descrição breve do produto e salve em blurb.md.',
    'On a besoin de ton avis, tu as carte blanche pour ce document.',
    'On a besoin de ton aide, tu as une minute pour faire ce document ?',
])
def test_other_latin_script_languages_are_not_forced_to_english(text):
    assert request_language(text) is None
    assert with_deliverable_language_hint(text) == text


def test_common_english_question_is_recognized():
    assert request_language('Can you check whether this API key looks valid? It should use the documented format.') == 'English'


@pytest.mark.parametrize('phrase', ['こんにちは', '"こんにちは"', '"' + 'こんにちは' * 20 + '"'])
def test_english_request_referencing_japanese_does_not_gain_a_japanese_hint(phrase):
    text = f'Write a short note about the phrase {phrase} for the board and save it as greeting.md.'
    assert request_language(text) is None
    assert with_deliverable_language_hint(text) == text


@pytest.mark.parametrize('phrase', ['こんにちは', '"こんにちは"'])
def test_chinese_request_referencing_japanese_does_not_gain_a_japanese_hint(phrase):
    text = f'请解释一下{phrase}这句话是什么意思。'
    assert request_language(text) is None
    assert with_deliverable_language_hint(text) == text


@pytest.mark.parametrize('opening,closing', [('', ''), ('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』')])
def test_chinese_request_quoting_japanese_paragraph_is_not_forced_to_japanese(opening, closing):
    paragraph = ('こちらの商品は、みなさまにもっと便利にお使いいただけるように、'
                 'さまざまな新しい機能をそなえています。ご利用になるまえに、'
                 'こちらの説明をよくお読みください。')
    text = f'请解释这段日文的意思，并保存成说明文档：{opening}{paragraph}{closing}'
    assert request_language(text) is None
    assert with_deliverable_language_hint(text) == text


def test_japanese_request_uses_the_system_language_contract_without_extra_inference():
    text = 'product.md をもとに「新しい商品」の短い紹介文を書いて blurb.md に保存してください。'
    assert request_language(text) is None
    assert with_deliverable_language_hint(text) == text


@pytest.mark.parametrize('character', ['f', '<', '“'])
def test_large_plain_text_language_detection_completes(character):
    import subprocess
    import sys
    from pathlib import Path

    # A quadratic path regex took minutes on this input. A subprocess keeps a
    # regression bounded while allowing ample time for slow CI hosts.
    script = ("import runpy; request_language = runpy.run_path('box_agent/turn_language.py')['request_language']; "
              f"text = 'Write a short note for the board and save it as note.md. ' + {character!r} * 262144; "
              "assert request_language(text) == 'English'")
    subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                   check=True, timeout=10, capture_output=True, text=True)
