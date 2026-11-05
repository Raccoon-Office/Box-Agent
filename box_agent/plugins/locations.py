"""One profile location for management processes and Agent startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..user_paths import state_path

_PACKAGE_ROOTS: set[Path] = set()


def plugin_package_roots() -> frozenset[Path]:
    root = state_path("plugins", os.environ.get("BOX_AGENT_PLUGIN_STATE_DIR") or None)
    return frozenset({(root / "packages").resolve(), *_PACKAGE_ROOTS})


def plugin_settings(config: Any = None):
    settings = getattr(config, "plugin_platform", None)
    if settings is None:
        # Read only platform settings, without requiring usable model credentials.
        import yaml
        from ..config import Config, PluginPlatformConfig

        config_path = Config.find_config_file("config.yaml")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path else {}
        section = raw.get("plugin_platform", {}) if isinstance(raw, dict) else {}
        if not isinstance(section, dict):
            section = {}
        # Management must remain usable when unrelated startup/deployment
        # fields are invalid. Runtime startup validates the full host mapping.
        section = {key: value for key, value in section.items()
                   if key in PluginPlatformConfig.model_fields}
        for key, variable in (("state_dir", "BOX_AGENT_PLUGIN_STATE_DIR"),
                              ("target_id", "BOX_AGENT_PLUGIN_TARGET_ID"),
                              ("profile_id", "BOX_AGENT_PLUGIN_PROFILE_ID")):
            if value := os.environ.get(variable):
                section[key] = value
        settings = PluginPlatformConfig(**section)
    return settings


def plugin_location(config: Any = None) -> tuple[Path, str, str]:
    settings = plugin_settings(config)
    root = state_path("plugins", os.environ.get("BOX_AGENT_PLUGIN_STATE_DIR") or settings.state_dir)
    target = os.environ.get("BOX_AGENT_PLUGIN_TARGET_ID") or settings.target_id
    profile = os.environ.get("BOX_AGENT_PLUGIN_PROFILE_ID") or settings.profile_id
    _PACKAGE_ROOTS.add((root / "packages").resolve())
    return root, target, profile
