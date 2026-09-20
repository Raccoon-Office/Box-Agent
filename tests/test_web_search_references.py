import json

import pytest

from box_agent.tools.web_search_runtime import _extract_web_search_payload


@pytest.mark.parametrize("source_id", ["opaque-source-id", "ref_opaque-source-id"])
def test_search_refs_preserve_source_id_as_alias_for_display_sort_id(source_id):
    payload = _extract_web_search_payload("web_search", json.dumps({
        "Result": {"WebResults": [{
            "Id": source_id, "SortId": 8,
            "Title": "Source", "Url": "https://example.com/weather",
        }]},
    }))
    assert payload is not None
    ref = payload["refs"][0]
    assert ref["reference_tag"] == "ref_8"
    assert ref["reference_aliases"] == ["ref_opaque-source-id"]
    assert ref["url"] == "https://example.com/weather"


@pytest.mark.parametrize("source_id", [None, "", "invalid id", "x" * 257])
def test_invalid_source_ids_do_not_create_citation_aliases(source_id):
    payload = _extract_web_search_payload("web_search", json.dumps({
        "Result": {"WebResults": [{
            "Id": source_id, "SortId": 2, "Url": "https://example.com",
        }]},
    }))
    assert payload is not None
    assert "reference_aliases" not in payload["refs"][0]


def test_search_result_without_url_does_not_register_a_clickable_source():
    assert _extract_web_search_payload("web_search", json.dumps({
        "Result": {"WebResults": [{"Id": "opaque-id", "SortId": 2}]},
    })) is None
