"""Expert preflight observes the same native image registration rules."""
from types import SimpleNamespace

import pytest

from box_agent.tools.setup import image_generation_tool_settings


@pytest.fixture(autouse=True)
def image_environment(monkeypatch):
    for name in (
        "BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "BOX_AGENT_IMAGE_GEN_ENDPOINT",
        "RACCOON_PRIVATE_DEPLOYMENT", "RACCOON_PRIVATE_OFFICE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def config(endpoint="", *, managed=False):
    return SimpleNamespace(
        image_generation=SimpleNamespace(endpoint=endpoint),
        officev3=SimpleNamespace(use_default_image_generation_preset=managed),
    )


def test_unconfigured_image_tool_is_not_available_for_expert_preflight():
    assert image_generation_tool_settings(config()) == (
        False, None, "no image_generation.endpoint configured",
    )


def test_explicit_image_endpoint_is_available_without_creating_a_tool():
    endpoint = "https://images.example.test/v1/images/generations"
    settings = config(endpoint)
    available, actual, _ = image_generation_tool_settings(settings)
    assert available is True
    assert actual == endpoint
    assert settings.image_generation.endpoint == endpoint


@pytest.mark.parametrize("variable", ["BOX_AGENT_IMAGE_GENERATION_ENDPOINT", "BOX_AGENT_IMAGE_GEN_ENDPOINT"])
def test_environment_only_image_tool_keeps_native_constructor_fallback(monkeypatch, variable):
    monkeypatch.setenv(variable, "https://images.example.test/v1/images/generations")
    available, endpoint, _ = image_generation_tool_settings(config())
    assert available is True
    # Non-managed native registration passes None so the constructor resolves env.
    assert endpoint is None


@pytest.mark.parametrize("managed,expected_host", [
    (True, "code-test.xiaohuanxiong.com"),
    (False, "xiaohuanxiong.com"),
])
def test_hosted_alignment_applies_only_to_default_preset(managed, expected_host):
    original = "https://xiaohuanxiong.com/api/web/llm/v2/images/gen"
    llm = SimpleNamespace(api_base="https://code-test.xiaohuanxiong.com/api/web/llm/v2")
    available, endpoint, _ = image_generation_tool_settings(config(original, managed=managed), llm)
    assert available is True
    assert endpoint == f"https://{expected_host}/api/web/llm/v2/images/gen"


@pytest.mark.parametrize("in_config", [True, False])
def test_private_default_preset_uses_trusted_configured_service(monkeypatch, in_config):
    base = "https://tenant.example/office"
    endpoint = base + "/api/web/llm/v2/images/gen"
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", base)
    monkeypatch.setenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", endpoint)
    llm = SimpleNamespace(api_base=base + "/api/web/llm/v2")
    available, actual, _ = image_generation_tool_settings(config(endpoint if in_config else "", managed=True), llm)
    assert available is True
    assert actual == endpoint


@pytest.mark.parametrize("in_config", [True, False])
@pytest.mark.parametrize("endpoint,private_base", [
    ("https://code-test.xiaohuanxiong.com/api/web/llm/v2/images/gen", "https://tenant.example/office"),
    ("https://other.example/custom", "https://tenant.example/office"),
    ("https://tenant.example/office/images/gen", "https://tenant.example/office/images/gen"),
])
def test_private_default_preset_rejects_endpoints_outside_service_boundary(monkeypatch, in_config, endpoint, private_base):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", private_base)
    monkeypatch.setenv("BOX_AGENT_IMAGE_GENERATION_ENDPOINT", endpoint)
    llm = SimpleNamespace(api_base="https://tenant.example/office/api/web/llm/v2")
    available, actual, reason = image_generation_tool_settings(config(endpoint if in_config else "", managed=True), llm)
    assert available is False
    assert actual == ""
    assert reason == "private managed image endpoint is outside the service boundary"


def test_private_explicit_custom_image_endpoint_keeps_existing_registration(monkeypatch):
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    endpoint = "https://custom.example/v1/images/generations"
    llm = SimpleNamespace(api_base="https://tenant.example/office/api/web/llm/v2")
    available, actual, _ = image_generation_tool_settings(config(endpoint), llm)
    assert available is True
    assert actual == endpoint
