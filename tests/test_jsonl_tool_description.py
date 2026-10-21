"""Model-facing query examples must be valid calls to the real tool."""
import json

import pytest

from box_agent.tools.file.jsonl_tool import JsonlQueryTool
from box_agent.tools.file.read_tool import MAX_READ_CHARS, ReadTool


async def test_query_jsonl_documented_call_filters_and_projects_nested_fields(tmp_path):
    tool = JsonlQueryTool(workspace_dir=str(tmp_path))
    example_text = tool.description.split("Example: ", 1)[1]
    arguments, _ = json.JSONDecoder().raw_decode(example_text)
    (tmp_path / arguments["path"]).write_text(
        json.dumps({"id": 7, "data": {"error": "other"}}) + "\n"
        + json.dumps({"id": 42, "data": {"error": "target"}}) + "\n",
        encoding="utf-8",
    )

    result = await tool.execute(**arguments)

    assert result.success is True
    records = json.loads(result.content)["records"]
    assert len(records) == 1
    assert records[0]["data"] == {"/id": 42, "/data/error": "target"}


async def test_query_jsonl_line_location_can_retrieve_verbatim_source_record(tmp_path):
    source_line = '{ "id" : 42, "data" : { "error" : "中文 \\u0061" } }'
    original = ('{"id":7}\n' + source_line + '\n').encode("utf-8")
    source = tmp_path / "events.jsonl"
    source.write_bytes(original)
    query = JsonlQueryTool(workspace_dir=str(tmp_path))

    result = await query.execute(
        path="events.jsonl", fields=["/id", "/data/error"], where={"/id": 42},
    )

    assert result.success is True
    record = json.loads(result.content)["records"][0]
    assert record["data"] == {"/id": 42, "/data/error": "中文 a"}
    assert json.dumps(record["data"], ensure_ascii=False) != source_line
    read = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="events.jsonl", offset=record["line"], limit=1,
    )
    assert read.success is True
    assert f"2|{source_line}" in read.content
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "record_chars,can_read", [(MAX_READ_CHARS, True), (MAX_READ_CHARS + 1, False)],
)
async def test_query_jsonl_locates_records_on_both_sides_of_raw_read_limit(
    tmp_path, record_chars, can_read,
):
    prefix, suffix = '{"id":42,"payload":"', '"}\n'
    original = (prefix + "x" * (record_chars - len(prefix) - len(suffix)) + suffix).encode()
    source = tmp_path / "events.jsonl"
    source.write_bytes(original)
    query = await JsonlQueryTool(workspace_dir=str(tmp_path)).execute(
        path="events.jsonl", fields=["/id"], where={"/id": 42},
    )

    assert query.success is True
    record = json.loads(query.content)["records"][0]
    assert record["data"] == {"/id": 42}
    read = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="events.jsonl", offset=record["line"], limit=1,
    )

    assert read.success is can_read
    if can_read:
        assert f"1|{original.decode().rstrip()}" in read.content
    else:
        assert read.content == ""
        assert "safety limit" in read.error
    assert source.read_bytes() == original


@pytest.mark.parametrize("invalid", ["id", "#id", " /id", "data.error"])
@pytest.mark.parametrize("parameter", ["fields", "where"])
async def test_query_jsonl_keeps_rejecting_undocumented_pointer_syntax(tmp_path, invalid, parameter):
    tool = JsonlQueryTool(workspace_dir=str(tmp_path))
    arguments = {parameter: [invalid] if parameter == "fields" else {invalid: 42}}

    result = await tool.execute(path="events.jsonl", **arguments)

    assert result.success is False
    assert "beginning with '/'" in result.error
