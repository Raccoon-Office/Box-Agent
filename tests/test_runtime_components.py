from box_agent.tools.runtime_components import runtime_component_required


def test_missing_component_contract_is_independent_of_video_and_contains_no_installer_commands():
    result = runtime_component_required("document-preview", code="COMPONENT_MISSING", message="Unavailable")
    assert not result.success
    assert result.error == "COMPONENT_MISSING: Unavailable"
    assert result.raw_output == {"type": "runtime_component_required", "schema_version": 1,
                                 "component_id": "document-preview", "code": "COMPONENT_MISSING"}
    assert "host" in result.model_context
