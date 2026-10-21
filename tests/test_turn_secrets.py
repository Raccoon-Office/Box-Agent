"""Credential notice: narrow detection, masked output, placed after the user's words."""

import pytest

from box_agent.turn_hints import turn_hints, with_turn_hints
from box_agent.turn_secrets import credential_notice, detect_credentials, mask_credential

KEY = "sk-live-7f3a9c2e5b8d41f6a0c4e2b9d7f15a36"
TOKEN = "ghp_fixturefixturefixturefixturefixturefixture"
PASSWORD = "fixture-password-not-real"


@pytest.mark.parametrize("text,expected", [
    (f"帮我看下这个 API key 格式对不对：{KEY}。", [KEY]),
    (f'curl -H "Authorization: Bearer {TOKEN}" https://api.example.invalid/v1/me', [TOKEN]),
    (f"我们测试库的密码是 {PASSWORD}，主机 db.internal:5432", [PASSWORD]),
    ("password: hunter2-long, user: app", ["hunter2-long"]),
    ("AWS key AKIAIOSFODNN7EXAMPLE leaked", ["AKIAIOSFODNN7EXAMPLE"]),
    ("token eyJfixturefixturefixture.fixturefixturefixture.fixturefixturefixture", [
        "eyJfixturefixturefixture.fixturefixturefixture.fixturefixturefixture"]),
])
def test_detects_common_credential_shapes(text, expected):
    assert detect_credentials(text) == expected


@pytest.mark.parametrize("text", [
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 broke the build",
    "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "use scikit-learn and sk-learn style pipelines",
    "密码策略要求至少 12 位",
    "the password is required on first login",
    "帮我写一段产品介绍",
])
def test_ignores_hashes_and_ordinary_text(text):
    assert detect_credentials(text) == []
    assert credential_notice(text) is None


def test_masks_never_reveal_short_secrets_or_full_values():
    assert mask_credential(KEY) == "sk-live-…5a36"
    assert mask_credential(PASSWORD) == "****"
    notice = credential_notice(f"key {KEY} and 密码是 {PASSWORD}")
    assert KEY not in notice and PASSWORD not in notice
    assert "`sk-live-…5a36`" in notice and "`****`" in notice


def test_turn_hints_combine_language_and_credential_notes():
    english = f"Can you check whether this API key looks valid: {KEY}? It should be sk-live- and 32 hex chars."
    hints = turn_hints(english)
    assert len(hints) == 2 and hints[0].startswith("[Deliverable language") and hints[1].startswith("[敏感信息")
    assert with_turn_hints("帮我写一段产品介绍") == "帮我写一段产品介绍"
    assert with_turn_hints(english).startswith(english)


@pytest.mark.asyncio
async def test_acp_prompt_places_credential_notice_after_the_request(tmp_path):
    from types import SimpleNamespace

    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from tests.test_acp import _EMPTY_CONNECTOR_CONTEXT, CaptureMessagesLLM, DummyConn

    config = Config(llm=LLMConfig(api_key="test-key"),
                    agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)), tools=ToolsConfig())
    llm = CaptureMessagesLLM()
    agent = BoxACPAgent(DummyConn(), config, llm, [], "system")
    session = await agent.newSession(SimpleNamespace(cwd=None, field_meta={"session_mode": "general"}))
    text = f"帮我看下这个 API key 格式对不对：{KEY}"

    await agent.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": text}], field_meta={}))

    [user_message] = [content for role, content in llm.calls[0] if role == "user"]
    assert user_message.startswith(text)
    assert "[敏感信息：本条消息含疑似凭据（`sk-live-…5a36`）" in user_message
    assert user_message.endswith(_EMPTY_CONNECTOR_CONTEXT)


@pytest.mark.parametrize('text', [
    'password: "fixture-password-long"',
    "password: 'fixture-password-long'",
    '"password": "fixture-password-long"',
    'password: "fixture password with spaces"',
])
def test_quoted_passwords_receive_a_fully_masked_notice(text):
    [value] = detect_credentials(text)
    notice = credential_notice(text)
    assert value not in notice
    assert '`****`' in notice


def test_long_password_does_not_disclose_prefix_or_suffix():
    password = 'ThisIsAReallyLongDatabasePassword1234'
    notice = credential_notice('password: ' + password)
    assert password[:8] not in notice and password[-4:] not in notice
    assert '`****`' in notice


@pytest.mark.parametrize('reference', ['${DB_PASSWORD}', '$DB_PASSWORD', '%DB_PASSWORD%', 'process.env.DB_PASSWORD', 'os.environ["DB_PASSWORD"]'])
def test_environment_references_are_not_credentials(reference):
    assert detect_credentials('password: ' + reference) == []
    assert credential_notice('password: ' + reference) is None


@pytest.mark.parametrize('reference', ['process.env.API_TOKEN', 'process.env.ACCESS_TOKEN'])
def test_bearer_environment_references_do_not_trigger_credential_notice(reference):
    text = f'Authorization: Bearer {reference}'
    assert detect_credentials(text) == []
    assert credential_notice(text) is None


@pytest.mark.parametrize('value', [TOKEN + ' with spaces', 'sk-live-' + 'fixture' * 6 + '=='])
def test_token_shaped_password_is_detected_in_full_and_fully_masked(value):
    text = f'password: "{value}"'
    assert detect_credentials(text) == [value]
    notice = credential_notice(text)
    assert value[:8] not in notice and value[-4:] not in notice
    assert '`****`' in notice


def test_token_also_used_as_part_of_a_password_receives_a_fully_masked_notice():
    text = f'API key: {TOKEN}\npassword: "{TOKEN} with spaces"'
    notice = credential_notice(text)
    assert TOKEN[:8] not in notice and TOKEN[-4:] not in notice
    assert '`****`' in notice


@pytest.mark.parametrize('password_later', [False, True])
def test_many_credentials_produce_a_bounded_notice_without_truncating_the_request(password_later):
    tokens = [TOKEN] + ['sk-fixture' + format(i, '08d') + 'x' * 16 for i in range(1000)]
    text = '\n'.join(tokens)
    if password_later:
        text += f'\npassword: "{TOKEN} with spaces"'
    notice = credential_notice(text)
    assert len(notice) < 1000
    assert len(detect_credentials(text)) <= 8
    assert '任何凭据' in notice
    assert with_turn_hints(text).startswith(text)
    if password_later:
        assert TOKEN[:8] not in notice and TOKEN[-4:] not in notice


@pytest.mark.parametrize('password_first', [False, True])
@pytest.mark.parametrize('password_length', [8, 12])
def test_token_containing_a_separate_password_is_fully_masked(password_length, password_first):
    key = 'sk-live-' + 'a' * 32
    password = key[:password_length]
    other_tokens = ['sk-fixture' + format(i, '08d') + 'x' * 16 for i in range(1000)]
    lines = [key, *other_tokens]
    lines.insert(0 if password_first else len(lines), f'password: "{password}"')
    text = '\n'.join(lines)
    notice = credential_notice(text)
    assert key[:8] not in notice and key[-4:] not in notice
    assert '`****`' in notice
