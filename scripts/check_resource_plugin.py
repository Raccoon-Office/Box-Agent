"""Run authoring_static_preflight against the real resource-plugin implementation.

This creates and removes a temporary package store. It never confirms an
activation, starts an Agent/model, executes plugin scripts, or opens a browser.
Success covers authoring checks, not target-runtime or business acceptance.
Run from a checkout with Box-Agent's normal development dependencies installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
NOT_VERIFIED = [
    "Actual target host version/features, tool permissions and runtime dependencies",
    "HTML JavaScript, MessageChannel interactions, sandbox and visual behavior",
    "Real host startup confirmation, cold apply, disable, rollback and recovery",
    "Model selection, configuration compliance and final business artifacts",
]


def _verify_skill_projection(skill: Any, snapshot: dict[str, Any],
                             instance_names: dict[str, str], reader: Any) -> int:
    """Compare actual prompt data/body with the frozen inputs without logging values."""
    identity = skill.metadata["plugin"]
    instance_id, contribution_id = identity["instance_id"], identity["contribution_id"]
    item = snapshot["instances"][instance_id]
    manifest = item["manifest"]
    declaration = next(entry for entry in manifest["contributes"]["skills"]
                       if entry["id"] == contribution_id)
    expected_identity = {
        "plugin_id": manifest["id"], "instance_id": instance_id,
        "contribution_id": contribution_id, "package_version": manifest["version"],
        "package_digest": item["package_digest"], "config_revision": item["config_revision"],
        "generation": snapshot["generation"],
    }
    # The host emits its settings as the third paragraph, followed by Skill
    # content. Fail explicitly if that format changes; do not scan arbitrary
    # business text for a convenient matching JSON object.
    sections = skill.content.split("\n\n", 3)
    if len(sections) != 4 or sections[0] != "## Plugin instance settings":
        raise ValueError(f"Missing host settings projection: {instance_id}/{contribution_id}")
    projection = json.loads(sections[2])
    expected = {
        **expected_identity,
        "values": {key: item["config"][key] for key in declaration.get("config_keys", [])
                   if key in item["config"]},
        "skill_names": instance_names,
    }
    # JSON comparison also distinguishes booleans from equal-valued integers.
    if identity != expected_identity or json.dumps(projection, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError(f"Settings projection differs from frozen instance inputs: {instance_id}/{contribution_id}")
    source = reader.load_skill(Path(item["package_root"]) / declaration["path"] / "SKILL.md", source="plugin")
    if source is None or source.broken:
        raise ValueError(f"Cannot read source Skill for reference verification: {instance_id}/{contribution_id}")
    expected_body = source.content
    references = 0
    for key, model_name in instance_names.items():
        placeholder = "{{skill:" + key + "}}"
        references += source.content.count(placeholder)
        expected_body = expected_body.replace(placeholder, model_name)
    if sections[3] != expected_body:
        raise ValueError(f"Skill body references differ from this instance's bindings: {instance_id}/{contribution_id}")
    return references


def check_package(package: Path, *, config: dict[str, Any] | None = None,
                  instance_id: str = "authoring_check",
                  aliases: dict[str, str] | None = None,
                  host_version: str | None = None) -> dict[str, Any]:
    """Use a fresh explicit store, ignoring all configured host/profile paths."""
    report: dict[str, Any] = {
        "success": False, "scope": "authoring_static_preflight", "checks": [],
        "not_verified": list(NOT_VERIFIED), "runtime_verified": False,
        "activation_confirmed": False,
    }
    stage = "load_host_implementation"
    temporary_root: Path | None = None
    try:
        from box_agent import __version__
        from box_agent.plugins.settings_service import PluginSettingsService
        from box_agent.plugins.skill_sources import build_skill_plan
        from box_agent.tools.skill_loader import SkillLoader

        report["host"] = {
            "implementation": "current_source_checkout",
            "reported_version": __version__,
            "manifest_check_version": host_version or __version__,
            "version_note": "A version string is not proof of target feature support",
        }
        with tempfile.TemporaryDirectory(prefix="box-plugin-authoring-") as temporary:
            temporary_root = Path(temporary)
            service = PluginSettingsService(Path(temporary) / "store",
                                           target_id="authoring", profile_id="isolated",
                                           host_version=host_version or __version__)
            stage = "install_package"
            first = service.install(instance_id, str(package.absolute()), enabled=True,
                                    config=config, aliases=aliases, request_id="install-primary")
            report["package"] = {key: first[key] for key in (
                "package_id", "package_version", "package_digest", "schema_digest")}
            report["checks"].append(stage)

            stage = "validate_and_save_configuration"
            editor = service.get_config(instance_id)
            validation = service.validate_config(instance_id, editor["config"],
                                                 editor["package_digest"], editor["schema_digest"])
            if not validation["valid"]:
                raise ValueError("Installed configuration failed validation")
            saved = service.save_config(instance_id, editor["config"], editor["saved_revision"],
                                        "save-primary", editor["package_digest"], editor["schema_digest"])
            report["configuration"] = {"keys": sorted(saved["config"]),
                                       "saved_revision": saved["saved_revision"]}
            report["checks"].append(stage)

            stage = "read_configuration_page_assets"
            ui = service.get_ui(instance_id, saved["package_digest"], saved["schema_digest"])
            report["ui"] = {"entry": ui["entry"], "asset_count": len(ui["assets"]),
                            "html_bytes": len(ui["html"].encode("utf-8")),
                            "javascript_executed": False}
            report["checks"].append(stage)

            stage = "install_second_instance"
            peer_id = "authoring_peer" if instance_id != "authoring_peer" else "authoring_peer_two"
            service.install(peer_id, str(package.absolute()), enabled=True,
                            config=saved["config"], request_id="install-peer")
            report["checks"].append(stage)

            stage = "candidate_static_integrity"
            candidate = service.prepare_apply(service.activation_status()["profile_revision"],
                                              "prepare-authoring-check")
            service.preflight_snapshot(candidate["activation_id"])
            snapshot = service.get_snapshot(candidate["activation_id"])
            report["checks"].append(stage)

            stage = "skill_plan_and_same_instance_references"
            plan = build_skill_plan(snapshot)
            loader = SkillLoader(sources=[], skill_settings_path=Path(os.devnull),
                                 **plan.loader_arguments())
            loader.discover_skills()
            names = [skill.name for skill in plan.skills]
            if len(names) != len(set(names)):
                raise ValueError("Two plugin instances resolved to duplicate Skill names")
            report["checks"].append(stage)
            stage = "skill_projection_and_reference_binding"
            names_by_instance: dict[str, dict[str, str]] = {}
            for skill in plan.skills:
                identity = skill.metadata["plugin"]
                names_by_instance.setdefault(identity["instance_id"], {})[identity["contribution_id"]] = skill.name
            source_reader = SkillLoader(sources=[], skill_settings_path=Path(os.devnull))
            summaries = []
            for skill in plan.skills:
                loaded = loader.get_skill(skill.name)
                if loaded is None or not loaded.to_prompt().strip():
                    raise ValueError("A candidate Skill could not be read through SkillLoader")
                identity = skill.metadata["plugin"]
                declaration = next(item for item in snapshot["instances"][identity["instance_id"]]
                                   ["manifest"]["contributes"]["skills"]
                                   if item["id"] == identity["contribution_id"])
                reference_count = _verify_skill_projection(
                    loaded, snapshot, names_by_instance[identity["instance_id"]], source_reader)
                summaries.append({
                    "instance_id": identity["instance_id"],
                    "contribution_id": identity["contribution_id"],
                    "model_name": skill.name,
                    "config_revision": identity["config_revision"],
                    "config_keys": declaration.get("config_keys", []),
                    "user_visible": skill.user_visible,
                    "enabled_by": list(skill.plugin_enabled_by),
                    "projection_verified": True,
                    "reference_binding_verified": True,
                    "reference_count": reference_count,
                    "prompt_bytes": len(loaded.to_prompt().encode("utf-8")),
                })
            report["skills"] = summaries
            report["required_runtime_profiles"] = list(plan.runtime_profiles)
            report["required_host_tools"] = sorted({
                tool for skill in plan.skills for tool in skill.metadata.get("requires_host_tools", [])
            })
            report["checks"].append(stage)

            stage = "no_activation_confirmation"
            if service.active_plugin_sources() or service.get_config(instance_id)["active_revision"] is not None:
                raise ValueError("Authoring checks unexpectedly changed the active snapshot")
            report["checks"].append(stage)
        report["success"] = True
    except Exception as error:
        code = getattr(error, "code", "authoring_check_failed")
        details = getattr(error, "details", None)
        if code == "invalid_config" and isinstance(details, dict):
            # JSON Schema error messages can quote rejected parameter values.
            # Keep locations useful to authors without copying their input.
            details = {"invalid_fields": [item.get("path", "") for item in details.get("errors", [])]}
        report["error"] = {"stage": stage, "code": code, "message": str(error), "details": details}
    finally:
        if temporary_root is not None:
            report["temporary_store_removed"] = not temporary_root.exists()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Extracted plugin package directory")
    parser.add_argument("--config", type=Path, help="Non-sensitive configuration JSON object file")
    parser.add_argument("--instance-id", default="authoring_check")
    parser.add_argument("--alias", action="append", default=[], metavar="PUBLIC_NAME=CONTRIBUTION_ID")
    parser.add_argument("--host-version", help="Simulate the manifest version check only; does not select another host implementation")
    parser.add_argument("--json", action="store_true", help="Emit one JSON report; exit 0 only when authoring checks pass")
    args = parser.parse_args(argv)
    aliases: dict[str, str] = {}
    for item in args.alias:
        public, separator, contribution = item.partition("=")
        if not separator or not public or not contribution or public in aliases:
            parser.error("Each --alias must be a unique PUBLIC_NAME=CONTRIBUTION_ID")
        aliases[public] = contribution
    # Keep stdout machine-readable even if a host import emits diagnostics.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
            if args.config is not None and not isinstance(config, dict):
                raise ValueError("--config must contain a JSON object")
            report = check_package(args.package, config=config, instance_id=args.instance_id,
                                   aliases=aliases, host_version=args.host_version)
        except (OSError, UnicodeError, ValueError) as error:
            report = {"success": False, "scope": "authoring_static_preflight",
                      "error": {"stage": "read_config", "code": "invalid_config_file", "message": str(error)},
                      "not_verified": list(NOT_VERIFIED), "runtime_verified": False,
                      "activation_confirmed": False}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("PASS: authoring static preflight" if report["success"] else "FAIL: authoring static preflight")
        for name in report.get("checks", []):
            print(f"  PASS {name}")
        if "error" in report:
            print(json.dumps(report["error"], ensure_ascii=False))
        for name in report["not_verified"]:
            print(f"  NOT VERIFIED {name}")
        if report.get("temporary_store_removed"):
            print("The temporary store was removed; no plugin remains installed by this check.")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
