"""Prepared file links reach the model; bad references never consume a turn."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import ResourceContentBlock

from box_agent.input_resources import normalize_file_inputs
from box_agent.injections import InjectionManager
from tests.acp_helpers import acp_params
from tests.test_acp_history_edit import CaptureLLM, adapter, edit, request


def link(path: Path, **fields):
    return {"type": "resource_link", "uri": path.as_uri(), "name": path.name,
            "mimeType": "text/csv", **fields}


def test_all_files_keep_names_and_encoded_paths_without_duplicate_entries(tmp_path):
    paths = [tmp_path / f"资料 {index}%#?.csv" for index in range(9)]
    for path in paths:
        path.write_text("id,value\n1,37\n")
    files = normalize_file_inputs([*[link(path) for path in paths], link(paths[0])], tmp_path)
    assert len(files) == 9
    assert [item["name"] for item in files] == [path.name for path in paths]
    assert [item["path"] for item in files] == [str(path) for path in paths]
    assert files[-1]["mimeType"] == "text/csv"


@pytest.mark.parametrize("uri", [
    "https://example.com/file.csv", "file://remote-host/file.csv", "file:relative.csv",
    "file:///tmp/../file.csv", "file:///tmp/%2e%2e/file.csv", "file:///tmp/%FF.csv",
    "file:///tmp/%ZZ.csv", "file:///tmp/%00.csv", "file:///tmp/a.csv?query=1",
    "file:///tmp/a.csv#fragment", "file:///tmp/a%5cb.csv", "file:///tmp/a\nb.csv",
])
def test_invalid_or_remote_uris_are_explicitly_rejected(tmp_path, uri):
    with pytest.raises(ValueError, match="INVALID_INPUT_RESOURCE"):
        normalize_file_inputs([link(tmp_path / "x", uri=uri)], tmp_path)


def test_other_session_symlink_hardlink_and_directory_are_rejected(tmp_path):
    owned = tmp_path / "current"
    owned.mkdir()
    other = tmp_path / "other.csv"
    other.write_text("foreign")
    escape = owned / "escape.csv"
    escape.symlink_to(other)
    hardlink = owned / "hard.csv"
    hardlink.hardlink_to(other)
    for path in [other, escape, hardlink, owned, owned / "missing"]:
        with pytest.raises(ValueError, match="INVALID_INPUT_RESOURCE"):
            normalize_file_inputs([link(path)], owned)


def test_permission_policy_and_actual_open_are_both_required(tmp_path, monkeypatch):
    path = tmp_path / "file.csv"
    path.write_text("37")
    deny = SimpleNamespace(check=lambda **_: SimpleNamespace(allowed=False))
    with pytest.raises(ValueError, match="permission_denied"):
        normalize_file_inputs([link(path)], tmp_path, deny)
    def denied(*args, **kwargs):
        raise PermissionError("file not readable")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError, match="file not readable"):
        normalize_file_inputs([link(path)], tmp_path)


def test_resource_count_limit_does_not_silently_drop_files(tmp_path):
    with pytest.raises(ValueError, match="too_many_files"):
        normalize_file_inputs([link(tmp_path / "a")] * 521, tmp_path)


@pytest.mark.asyncio
async def test_model_receives_complete_file_facts_and_user_text_unchanged(tmp_path):
    paths = [tmp_path / f"中文 文件 {index}.txt" for index in range(8)]
    for index, path in enumerate(paths):
        path.write_text(str(index + 17))
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "file-test"})))
        text = "Compare all files. <desktop_response_format>literal</desktop_response_format>"
        await host.prompt(session_id=session.session_id, prompt=[{"type": "text", "text": text},
                          *[link(path) for path in paths]], turn_id="files",
                          input_context={"schemaVersion": 1, "userRequest": text})
        model_text = next(str(message.content) for message in llm.requests[0] if message.role == "user")
        facts, _ = json.JSONDecoder().raw_decode(model_text[model_text.index('{"files":'):])
        assert [item["name"] for item in facts["files"]] == [path.name for path in paths]
        assert host._sessions[session.session_id].source_text == text
        assert text in str(next(m.content for m in reversed(llm.requests[-1]) if m.role == "user" and m.source == "user"))
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("revise", [False, True])
async def test_missing_file_cannot_accept_or_revise_history_and_same_id_can_retry(tmp_path, revise):
    llm = CaptureLLM()
    host = adapter(tmp_path, llm)
    try:
        session = await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_id": "file-test"})))
        await host.prompt(**acp_params(request(session, "original", "t1")))
        log = host._sessions[session.session_id].agent.session_log
        path = tmp_path / "missing.txt"
        req = request(session, "replace with file", "t2", edit("t1") if revise else None)
        req.prompt.append(ResourceContentBlock.model_validate(link(path)))
        before = log.path.read_bytes()
        calls_before = len(llm.requests)
        with pytest.raises(ValueError, match="INVALID_INPUT_RESOURCE"):
            await host.prompt(**acp_params(req))
        assert log.path.read_bytes() == before
        assert not log.has_turn_binding("t2")
        assert len(llm.requests) == calls_before
        path.write_text("random-value,73")
        await host.prompt(**acp_params(req))
        assert log.has_turn_binding("t2")
        assert "original" not in str(next(m.content for m in reversed(llm.requests[-1]) if m.role == "user" and m.source == "user"))
        assert path.name in str(next(m.content for m in reversed(llm.requests[-1]) if m.role == "user" and m.source == "user"))
    finally:
        await host.aclose()


def test_file_only_supplement_uses_shared_projection_and_identity(tmp_path):
    path = tmp_path / "only.csv"
    path.write_text("81")
    context = {"files": normalize_file_inputs([link(path)], tmp_path)}
    item = {"id": "supplement", "content": "", "input_context": context}
    manager = InjectionManager()
    assert manager.submit(item)
    assert not manager.submit(deepcopy(item))
    changed = deepcopy(item)
    changed["input_context"]["files"][0]["name"] = "another.csv"
    with pytest.raises(ValueError, match="INJECTION_IDEMPOTENCY_CONFLICT"):
        manager.submit(changed)
    messages = []
    manager.apply_next(messages)
    assert "请分析这些文件" in messages[-1].content
    assert path.name in messages[-1].content
