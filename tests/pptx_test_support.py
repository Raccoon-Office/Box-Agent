"""Optional host-runtime checks shared by PPTX integration test runners."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


PPT_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins/ppt"
PPT_ALIASES = {"pptx": "pptx", "ppt-fast": "ppt_fast", **{
    f"sn-ppt-{name}": f"sn_ppt_{name}"
    for name in ("entry", "story", "standard", "dazzle", "tools", "doctor")}}


def ppt_plugin_loader(settings_path: Path):
    """Compose the real package with explicit compatibility aliases for tests.

    Installation/immutability is covered separately by package tests. Content
    contract tests read plugin sources instead of the core-upgrade compatibility copies.
    """
    from box_agent.plugins.manifest import load_manifest
    from box_agent.plugins.skill_sources import build_skill_plan
    from box_agent.tools.skill_loader import SkillLoader

    manifest = load_manifest(PPT_PLUGIN_ROOT)
    values, errors = manifest.normalize_config({})
    assert not errors
    plan = build_skill_plan({"generation": "source-test", "instances": {"ppt": {
        "manifest": manifest.data, "package_root": str(PPT_PLUGIN_ROOT),
        "package_digest": "0" * 64, "config_revision": 1, "config": values,
        "enabled": True, "aliases": PPT_ALIASES,
    }}})
    builtin = Path(__file__).resolve().parents[1] / "box_agent/skills"
    loader = SkillLoader(sources=[(builtin, "builtin")], skill_settings_path=settings_path,
                         **plan.loader_arguments())
    loader.discover_skills()
    return loader


def skip_unavailable_pptx_runtime(result: subprocess.CompletedProcess[str]) -> None:
    """Skip missing host dependencies, not script, rendering, or QA failures."""
    if result.returncode == 0:
        return
    output = result.stdout + result.stderr
    # Preflight reports converter damage alongside missing browser dependencies.
    # A broken Skill install must fail even when the optional host is absent.
    if "Missing bundled converter." in output:
        return
    unavailable = (
        "Cannot find module 'playwright'",
        "Missing dependency: playwright",
        "Missing npm package: playwright",
        "Executable doesn't exist",
        "Playwright Chromium is not available",
        "Chromium executable not found at ",
        "Chromium executable not found under ",
    )
    if any(marker in output for marker in unavailable):
        pytest.skip("Managed Playwright browser is unavailable")
    if any(marker in output for marker in (
        "Cannot find module '@napi-rs/canvas'",
        "Missing dependency: @napi-rs/canvas",
    )):
        pytest.skip("Managed Canvas dependency is unavailable")
