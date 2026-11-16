"""Updated business catalog management states and compatibility."""
from types import SimpleNamespace

import pytest

from box_agent.llm.reasoning import normalize_raccoon_thinking, unified_thinking_params

SUPPORTED = {
    "supported": True, "can_disable": True, "default_level": "off",
    "display_levels": [{"value": "level_1", "label": "Minimal"},
                       {"value": "level_5", "label": "Max"}],
}
UNMANAGED = {"managed": False, "supported": False, "can_disable": False, "display_levels": []}


@pytest.mark.parametrize("managed", [None, "false", "true", 0, 1, [], {}])
def test_invalid_management_type_does_not_enable_unified_requests(managed):
    assert normalize_raccoon_thinking({**SUPPORTED, "managed": managed}) is None


@pytest.mark.parametrize("patch", [
    {"supported": True}, {"can_disable": True}, {"default_level": None},
    {"default_level": "off"}, {"display_levels": [{"value": "level_1", "label": "On"}]},
])
def test_unmanaged_requires_complete_unmanaged_shape(patch):
    assert normalize_raccoon_thinking({**UNMANAGED, **patch}) is None


def test_unmanaged_is_valid_but_preserves_legacy_provider_route():
    assert normalize_raccoon_thinking(UNMANAGED) == UNMANAGED
    client = SimpleNamespace(model="test-entry", raccoon_thinking_models={"test-entry": UNMANAGED},
                             raccoon_thinking_level="level_5")
    assert unified_thinking_params(client, True) is None


@pytest.mark.parametrize("cap", [SUPPORTED, {**SUPPORTED, "managed": True}])
def test_missing_management_is_backward_compatible_only_for_valid_managed_shape(cap):
    client = SimpleNamespace(model="model", raccoon_thinking_models={"model": cap},
                             raccoon_thinking_level="level_4")
    assert unified_thinking_params(client, True) == {"raccoon_thinking_level": "level_4"}
    assert unified_thinking_params(client, False) == {}


def test_managed_unsupported_suppresses_all_reasoning_parameters():
    cap = {"managed": True, "supported": False, "can_disable": True,
           "display_levels": [], "default_level": "off"}
    client = SimpleNamespace(model="model", raccoon_thinking_models={"model": cap},
                             raccoon_thinking_level="level_5")
    assert unified_thinking_params(client, True) == {}
