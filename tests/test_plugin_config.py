"""Configuration and composition boundaries for optional plugins."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from pydantic import ValidationError

from box_agent.config import Config
from box_agent.plugins.cua.config import CuaConfig


def _write_config(path: Path, *, plugins: object) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "api_key": "sk-test-key",
                "api_base": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "provider": "openai",
                "plugins": plugins,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_core_config_passes_plugin_namespaces_through_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    plugin_data = {
        "cua": {"server_name": "computer-use", "feed_screenshots": False},
        "future.plugin": {"opaque": {"value": 1}, "enabled": True},
    }
    _write_config(path, plugins=plugin_data)

    config = Config.from_yaml(path)

    assert config.plugins == plugin_data
    assert not hasattr(config.tools, "cua")


@pytest.mark.parametrize(
    ("plugins", "message"),
    [
        ([], "plugins must be a mapping"),
        ({"cua": []}, "plugins.cua must be a mapping"),
        ({"cua": None}, "plugins.cua must be a mapping"),
        ({"": {}}, "plugin names must be non-empty strings"),
    ],
)
def test_core_config_rejects_invalid_plugin_namespace_shapes(
    tmp_path: Path, plugins: object, message: str,
) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, plugins=plugins)

    with pytest.raises(ValueError, match=message):
        Config.from_yaml(path)


def test_cua_config_validates_only_the_cua_namespace() -> None:
    assert CuaConfig.model_validate({}).model_dump() == {
        "server_name": "computer-use",
        "feed_screenshots": True,
    }
    assert CuaConfig.model_validate(
        {"server_name": "  my-computer-use  ", "feed_screenshots": False}
    ).server_name == "my-computer-use"

    with pytest.raises(ValidationError):
        CuaConfig.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        CuaConfig.model_validate({"server_name": "   "})


def test_bundled_catalog_is_explicit_and_can_be_excluded() -> None:
    from box_agent.plugins.catalog import bundled_plugin_descriptors
    from box_agent.plugins.runtime import PluginRuntime

    bundled_ids = {descriptor.plugin_id for descriptor in bundled_plugin_descriptors()}
    assert "run.cua" in bundled_ids

    runtime = PluginRuntime(include_bundled_plugins=False)
    try:
        discovered_ids = {descriptor.plugin_id for descriptor in runtime.host.discover()}
        assert "run.cua" not in discovered_ids
    finally:
        # No activation is needed for this composition-only assertion; closing
        # still exercises the normal runtime lifecycle.
        import asyncio

        asyncio.run(runtime.aclose())


def test_runtime_without_bundled_plugins_does_not_import_cua(tmp_path: Path) -> None:
    """The core runtime remains usable when the optional package is absent."""

    script = r'''
import asyncio
import importlib.abc
from pathlib import Path
import sys


class RejectCuaImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "box_agent.plugins.cua" or fullname.startswith("box_agent.plugins.cua."):
            raise AssertionError("optional CUA package was imported")
        return None


sys.meta_path.insert(0, RejectCuaImport())
from box_agent.config import Config
from box_agent.plugins.runtime import PluginRuntime

config = Config.from_yaml(Path(sys.argv[1]))
assert config.plugins == {"future.plugin": {"opaque": True}}
runtime = PluginRuntime(include_bundled_plugins=False)
assert "run.cua" not in {item.plugin_id for item in runtime.host.discover()}
runtime.host.validate()
asyncio.run(runtime.aclose())
'''
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, plugins={"future.plugin": {"opaque": True}})
    environment = os.environ.copy()
    environment.pop("BOX_AGENT_HOME", None)

    completed = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
