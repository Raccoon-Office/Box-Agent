from box_agent.llm.openai_client import (
    _DEFAULT_SENSENOVA_MODEL_PREFIXES,
    _is_sensenova_model,
    _sensenova_model_prefixes,
)


def test_default_sensenova_prefixes(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_SENSENOVA_MODEL_PREFIXES", raising=False)
    assert _sensenova_model_prefixes() == _DEFAULT_SENSENOVA_MODEL_PREFIXES
    assert _is_sensenova_model("sensenova-v6")
    assert _is_sensenova_model("SN-SenseNova-Turbo")
    # Case-insensitive; a SenseNova-Flash model already matches the built-ins.
    assert _is_sensenova_model("SenseNova-Flash-Lite-20260727")
    assert not _is_sensenova_model("deepseek-v4")
    assert not _is_sensenova_model(None)
    assert not _is_sensenova_model("")


def test_env_extends_prefixes_without_dropping_builtins(monkeypatch):
    monkeypatch.setenv("BOX_AGENT_SENSENOVA_MODEL_PREFIXES", "deepseek-, flash-")
    prefixes = _sensenova_model_prefixes()
    # Built-ins are always retained.
    for builtin in _DEFAULT_SENSENOVA_MODEL_PREFIXES:
        assert builtin in prefixes
    assert "deepseek-" in prefixes
    assert "flash-" in prefixes
    assert _is_sensenova_model("deepseek-v4")
    assert _is_sensenova_model("Flash-X")
    assert _is_sensenova_model("sensenova-v6")


def test_blank_env_falls_back_to_builtins(monkeypatch):
    monkeypatch.setenv("BOX_AGENT_SENSENOVA_MODEL_PREFIXES", "   ")
    assert _sensenova_model_prefixes() == _DEFAULT_SENSENOVA_MODEL_PREFIXES
