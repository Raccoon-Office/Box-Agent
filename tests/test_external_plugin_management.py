"""Observable cold-install/configuration boundaries without an Agent or model."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest
import yaml

from box_agent.plugins.manifest import PluginError, load_manifest
from box_agent.plugins.package_store import PackageStore, package_digest
from box_agent.plugins.settings_service import PluginSettingsService


@pytest.fixture
def package(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "skills" / "guide").mkdir(parents=True)
    (root / "ui").mkdir()
    (root / "ui" / "config.html").write_text('<!doctype html><h1>Settings</h1>', encoding="utf-8")
    (root / "skills" / "guide" / "SKILL.md").write_text(
        '---\nname: guide\ndescription: Test guide\n---\nRead the supplied language preference.\n', encoding="utf-8")
    schema = {"type": "object", "additionalProperties": False, "required": ["language"],
              "properties": {"language": {"type": "string", "enum": ["zh-CN", "en-US"], "default": "zh-CN"}}}
    (root / "config.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    manifest = {"schema_version": 1, "id": "org.example.guide", "version": "1.0.0",
                "requires": {"box_agent": ">=1.0.0 <2.0.0", "plugin_api": 1},
                "runtime": {"kind": "resources"}, "config_schema": "config.schema.json",
                "ui": {"config": {"entry": "ui/config.html", "bridge_version": 1}},
                "contributes": {"skills": [{"id": "guide", "path": "skills/guide", "config_keys": ["language"]}]}}
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


@pytest.fixture
def service(tmp_path: Path) -> PluginSettingsService:
    return PluginSettingsService(tmp_path / "managed")


def install(service: PluginSettingsService, package: Path, **kwargs):
    return service.install("team", str(package), enabled=True, request_id="install-1", **kwargs)


def save(service: PluginSettingsService, previous, *, value="en-US", request_id="save-1"):
    return service.save_config("team", {"language": value}, previous["saved_revision"], request_id,
                               previous["package_digest"], previous["schema_digest"])


def activate(service: PluginSettingsService, request_id="apply-1"):
    activation = service.prepare_apply(service.activation_status()["profile_revision"], request_id)
    service.confirm_apply(activation["activation_id"])
    return activation


def test_audit_identifies_local_operator_scope_and_revisions_without_config_values(service, package):
    first = install(service, package)
    saved = save(service, first)
    service.set_enabled("team", False, saved["saved_revision"], "disable-audit")
    rows = service.store.read()["audit"]
    assert [row["event"] for row in rows] == ["package.installed", "config.saved", "instance.enabled_changed"]
    assert [(row["old_revision"], row["new_revision"]) for row in rows] == [(0, 1), (1, 2), (2, 3)]
    for row in rows:
        assert row["actor"] == {"kind": "local_os", "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None}
        assert row["target_id"] == service.target_id
        assert row["profile_id"] == service.profile_id
        assert row["instance_id"] == "team"
        assert row["active_generation"] == service.store.read()["active"]["generation"]
    assert rows[1]["fields"] == ["language"]
    assert len(rows[1]["config_digest"]) == 64
    assert "en-US" not in json.dumps(rows) and "zh-CN" not in json.dumps(rows)
    # A retried request has the same receipt and must not create another event.
    assert save(service, first) == saved
    assert service.store.read()["audit"] == rows
    with pytest.raises(PluginError, match="Unknown management fields"):
        service.dispatch("config.save", {"actor": "other-user"})
    assert service.store.read()["audit"] == rows


def test_install_is_pending_and_copies_an_immutable_package(service, package):
    view = install(service, package)
    assert view["config"] == {"language": "zh-CN"}
    assert view["desired_enabled"] and not view["active_enabled"]
    assert view["active_revision"] is None and view["restart_required"]
    assert service.active_plugin_sources() == []
    installed = service.packages.root / view["package_digest"]
    assert installed != package and package_digest(installed) == view["package_digest"]
    (package / "skills/guide/SKILL.md").write_text('changed', encoding="utf-8")
    assert 'Read the supplied' in (installed / "skills/guide/SKILL.md").read_text()
    assert not (installed / "plugin.yaml").stat().st_mode & 0o222


def test_saved_configuration_only_enters_sources_after_confirmed_restart(service, package):
    first = install(service, package)
    active = activate(service)
    new = save(service, first)
    assert new["saved_revision"] == 2 and new["active_revision"] == 1
    assert service.active_plugin_sources()[0]["config"]["language"] == "zh-CN"
    candidate = service.prepare_apply(new["profile_revision"], "apply-2")
    assert candidate["generation"] != active["generation"]
    assert service.active_plugin_sources(candidate["activation_id"])[0]["config"]["language"] == "en-US"
    assert service.active_plugin_sources()[0]["config"]["language"] == "zh-CN"
    service.confirm_apply(candidate["activation_id"])
    assert service.get_config("team")["active_revision"] == 2
    assert not service.get_config("team")["restart_required"]
    reloaded = PluginSettingsService(service.root)
    assert reloaded.active_plugin_sources()[0]["config"]["language"] == "en-US"


def test_apply_freezes_snapshot_even_when_more_changes_are_saved(service, package):
    first = install(service, package)
    candidate = service.prepare_apply(first["profile_revision"], "apply-1")
    save(service, first)
    service.confirm_apply(candidate["activation_id"])
    view = service.get_config("team")
    assert view["active_revision"] == 1 and view["saved_revision"] == 2
    assert view["restart_required"] and service.activation_status()["restart_required"]
    assert service.get_snapshot()["generation"] == candidate["candidate_generation"]


def test_failed_candidate_does_not_replace_active_and_keeps_saved_edits(service, package):
    first = install(service, package)
    old = activate(service)
    next_config = save(service, first)
    candidate = service.prepare_apply(next_config["profile_revision"], "apply-2")
    failure = service.fail_apply(candidate["activation_id"], {"code": "runtime_missing", "message": "Missing Node"})
    assert failure["generation"] == old["generation"]
    assert failure["activation"]["status"] == "failed"
    assert service.get_config("team")["config"]["language"] == "en-US"
    with pytest.raises(PluginError, match="Failed candidates"):
        service.confirm_apply(candidate["activation_id"])


def test_retry_same_request_returns_receipt_but_reusing_id_with_other_content_is_rejected(service, package):
    first = install(service, package)
    new = save(service, first)
    assert save(service, first) == new
    assert service.get_config("team")["saved_revision"] == 2
    with pytest.raises(PluginError) as caught:
        save(service, first, value="zh-CN")
    assert caught.value.code == "request_id_reused"
    with pytest.raises(PluginError) as caught:
        save(service, first, request_id="different-id")
    assert caught.value.code == "config_conflict"


def test_validation_has_field_errors_and_does_not_persist(service, package):
    first = install(service, package)
    validation = service.validate_config("team", {"language": "bad"}, first["package_digest"], first["schema_digest"])
    assert not validation["valid"] and validation["errors"][0]["path"] == "/language"
    with pytest.raises(PluginError) as caught:
        save(service, first, value="bad")
    assert caught.value.code == "invalid_config"
    assert service.get_config("team")["saved_revision"] == 1
    with pytest.raises(PluginError) as caught:
        service.save_config("team", {}, 1, "old-page", "0" * 64, first["schema_digest"])
    assert caught.value.code == "stale_package"


def test_disabled_corrupt_package_can_be_removed_without_reading_it(service, package):
    first = install(service, package, aliases={"guide": "guide"})
    activate(service)
    installed_manifest = service.packages.root / first["package_digest"] / "plugin.yaml"
    installed_manifest.chmod(0o600)
    installed_manifest.write_text("invalid: [", encoding="utf-8")
    listed = service.list_plugins()["plugins"][0]
    assert listed["error"]["code"] == "invalid_manifest"
    stopped = service.set_enabled("team", False, 1, "disable")
    activation = service.prepare_apply(stopped["profile_revision"], "disable-apply")
    snapshot = service.get_snapshot(activation["activation_id"])
    assert snapshot["instances"]["team"]["aliases"] == {"guide": "guide"}
    assert not snapshot["instances"]["team"]["enabled"]
    service.confirm_apply(activation["activation_id"])
    assert service.active_plugin_sources() == []


def test_runtime_unavailability_is_visible_without_changing_saved_configuration(service, package):
    install(service, package)
    activation = activate(service)
    service.record_runtime_status(activation["generation"], False, [{"code": "missing_runtime", "instance_id": "team"}])
    assert service.list_plugins()["runtime"]["healthy"] is False
    assert service.get_config("team")["active_revision"] == 1
    with pytest.raises(PluginError) as caught:
        service.record_runtime_status("old-generation", True, [])
    assert caught.value.code == "stale_generation"


def test_rollback_prepares_previous_snapshot_and_preserves_newer_desired_values(service, package):
    first = install(service, package)
    activate(service)
    second = save(service, first)
    activate(service, "apply-2")
    rollback = service.rollback(second["profile_revision"], "rollback")
    assert service.active_plugin_sources()[0]["config"]["language"] == "en-US"
    assert service.active_plugin_sources(rollback["activation_id"])[0]["config"]["language"] == "zh-CN"
    service.confirm_apply(rollback["activation_id"])
    assert service.get_config("team")["config"]["language"] == "en-US"
    assert service.get_config("team")["restart_required"]


def test_management_rejects_wrong_target_unknown_fields_and_nonexistent_candidate(service, package):
    install(service, package)
    for method, params, code in [
        ("plugins.list", {"target_id": "remote"}, "scope_mismatch"),
        ("plugins.list", {"secret": "bad"}, "invalid_request"),
        ("run.model", {}, "unsupported_method"),
    ]:
        with pytest.raises(PluginError) as caught:
            service.dispatch(method, params)
        assert caught.value.code == code
    other = PluginSettingsService(service.root, profile_id="another")
    assert other.list_plugins()["plugins"] == []
    with pytest.raises(PluginError) as caught:
        service.get_snapshot("missing")
    assert caught.value.code == "activation_not_found"


def test_ui_read_returns_only_bound_static_assets(service, package):
    (package / "ui/style.css").write_text('body { color: black }', encoding="utf-8")
    (package / "ui/notes.txt").write_text('not a UI asset', encoding="utf-8")
    first = install(service, package)
    ui = service.get_ui("team", first["package_digest"], first["schema_digest"])
    assert ui["html"] == '<!doctype html><h1>Settings</h1>'
    assert list(ui["assets"]) == ["style.css"]
    assert ui["assets"]["style.css"]["mime_type"] == "text/css"


@pytest.mark.parametrize("mutation,code", [
    (lambda m: m["runtime"].update(kind="python-worker"), "unsupported_feature"),
    (lambda m: m["contributes"].update(tools=[]), "unsupported_feature"),
    (lambda m: m.update(unknown_field=True), "invalid_manifest"),
    (lambda m: m.pop("ui"), "invalid_manifest"),
    (lambda m: m["ui"]["config"].update(entry="../settings.html"), "invalid_path"),
    (lambda m: m["contributes"]["skills"][0].update(config_keys=["undeclared"]), "invalid_manifest"),
    (lambda m: m["requires"].update(box_agent=">=2.0.0"), "incompatible_host"),
])
def test_manifest_rejects_unsupported_or_unsafe_declarations(package, mutation, code):
    path = package / "plugin.yaml"
    data = yaml.safe_load(path.read_text())
    mutation(data)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(PluginError) as caught:
        load_manifest(package)
    assert caught.value.code == code


def test_schema_local_reference_resolves_without_network_and_remote_reference_is_rejected(package):
    root = package / "config.schema.json"
    schema = json.loads(root.read_text())
    schema["properties"]["language"] = {"$ref": "fields.json#/$defs/language", "default": "zh-CN"}
    (package / "fields.json").write_text(json.dumps({"$defs": {"language": {"enum": ["zh-CN", "en-US"]}}}))
    root.write_text(json.dumps(schema))
    manifest = load_manifest(package)
    values, errors = manifest.normalize_config({})
    assert values == {"language": "zh-CN"} and not errors
    schema["properties"]["language"]["$ref"] = "https://example.org/schema.json"
    root.write_text(json.dumps(schema))
    with pytest.raises(PluginError) as caught:
        load_manifest(package)
    assert caught.value.code == "invalid_schema"


def test_installer_rejects_symlinks_and_detects_modified_installed_bytes(tmp_path, package):
    target = tmp_path / "outside.txt"
    target.write_text("private")
    (package / "escape").symlink_to(target)
    store = PackageStore(tmp_path / "packages")
    with pytest.raises(PluginError) as caught:
        store.install(package)
    assert caught.value.code == "invalid_path"
    (package / "escape").unlink()
    installed = store.install(package)
    body = Path(installed["package_root"]) / "skills/guide/SKILL.md"
    body.chmod(0o600)
    body.write_text("changed")
    with pytest.raises(PluginError) as caught:
        store.load(installed["package_digest"])
    assert caught.value.code == "package_corrupt"


def _competing_save(root: str, first: dict, name: str, queue) -> None:
    service = PluginSettingsService(Path(root))
    try:
        save(service, first, request_id=name)
        queue.put("saved")
    except PluginError as exc:
        queue.put(exc.code)


def test_two_processes_cannot_overwrite_the_same_configuration_revision(service, package):
    first = install(service, package)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=_competing_save, args=(str(service.root), first, str(i), queue)) for i in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in processes) == ["config_conflict", "saved"]
    assert service.get_config("team")["saved_revision"] == 2


def test_apply_retry_survives_restart_and_blocks_a_second_candidate(service, package):
    first = install(service, package)
    candidate = service.prepare_apply(first["profile_revision"], "prepare")
    recovered = PluginSettingsService(service.root)
    assert recovered.prepare_apply(first["profile_revision"], "prepare") == candidate
    assert recovered.activation_status(candidate["activation_id"])["activation"]["status"] == "prepared"
    for method in (recovered.prepare_apply, recovered.rollback):
        with pytest.raises(PluginError) as caught:
            method(first["profile_revision"], "other")
        assert caught.value.code == "activation_busy"
    with pytest.raises(PluginError) as caught:
        recovered.prepare_apply(first["profile_revision"] + 1, "prepare")
    assert caught.value.code == "request_id_reused"
    failure = {"code": "startup_failed", "message": "Process exited before verification"}
    failed = recovered.fail_apply(candidate["activation_id"], failure, "failed")
    assert recovered.fail_apply(candidate["activation_id"], failure, "failed") == failed
    with pytest.raises(PluginError) as caught:
        recovered.fail_apply(candidate["activation_id"], {**failure, "message": "other"}, "failed")
    assert caught.value.code == "request_id_reused"
    second = recovered.prepare_apply(first["profile_revision"], "prepare-2")
    assert second["activation_id"] != candidate["activation_id"]
    assert recovered.activation_status(candidate["activation_id"])["activation"]["status"] == "failed"


def test_rollback_receipt_survives_confirmation_and_restart(service, package):
    first = install(service, package)
    activate(service)
    second = save(service, first)
    activate(service, "apply-2")
    rollback = service.rollback(second["profile_revision"], "rollback")
    confirmed = service.confirm_apply(rollback["activation_id"], "confirm-rollback")
    recovered = PluginSettingsService(service.root)
    assert recovered.rollback(second["profile_revision"], "rollback") == rollback
    assert recovered.confirm_apply(rollback["activation_id"], "confirm-rollback") == confirmed
    assert recovered.active_plugin_sources()[0]["config"]["language"] == "zh-CN"


def test_preflight_is_static_and_confirm_refuses_modified_candidate_bytes(service, package):
    first = install(service, package)
    activation = service.prepare_apply(first["profile_revision"], "apply")
    check = service.preflight_snapshot(activation["activation_id"])
    assert check["valid"] and check["runtime_verified"] is False
    assert service.active_plugin_sources() == []
    body = service.packages.root / first["package_digest"] / "skills/guide/SKILL.md"
    body.chmod(0o600)
    body.write_text("changed after prepare")
    with pytest.raises(PluginError) as caught:
        service.confirm_apply(activation["activation_id"])
    assert caught.value.code == "package_corrupt"
    assert service.activation_status()["activation"]["status"] == "prepared"
    assert service.get_snapshot()["generation"] == "builtin"


def test_atomic_replace_failure_preserves_saved_active_and_receipts(service, package, monkeypatch):
    from box_agent.plugins import config_store
    first = install(service, package)
    activate(service)
    before = service.store.path.read_bytes()
    replace = config_store.os.replace

    def fail_replace(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(config_store.os, "replace", fail_replace)
    with pytest.raises(PluginError) as caught:
        save(service, first)
    assert caught.value.code == "config_write_failed"
    assert service.store.path.read_bytes() == before
    assert not list(service.store.root.glob(".state-*"))
    monkeypatch.setattr(config_store.os, "replace", replace)
    assert save(service, first)["saved_revision"] == 2


def _write_deployment(tmp_path, package):
    manifest = load_manifest(package)
    lock = {"schema_version": 1, "packages": {"guide": {"package_id": manifest.data["id"],
        "version": manifest.data["version"], "package_digest": package_digest(package),
        "schema_digest": manifest.schema_digest, "path": str(package)}}}
    document = {"model_api_key": "must-not-appear-in-plugin-state", "plugin_platform": {
        "lock_file": "plugins.lock.json", "instances": {"team": {"package": "guide", "enabled": True, "config": {}}},
        "bindings": {"skills": {"public-guide": "team/guide"}}}}
    config = tmp_path / "deployment.yaml"
    config.write_text(yaml.safe_dump(document))
    (tmp_path / "plugins.lock.json").write_text(json.dumps(lock))
    return config, document, lock


def test_explicit_deployment_import_export_never_activates_or_reads_host_secrets(service, package, tmp_path):
    config, document, _ = _write_deployment(tmp_path, package)
    imported = service.dispatch("profile.import", {"config_path": str(config), "expected_profile_revision": 0,
        "request_id": "import-1", "initialize_only": True})
    assert imported["imported_instances"] == ["team"] and imported["restart_required"]
    assert service.active_plugin_sources() == []
    assert service.get_config("team")["config"] == {"language": "zh-CN"}
    assert "must-not-appear" not in service.store.path.read_text()
    assert service.import_profile(str(config), 0, "import-1", True) == imported
    # Editing the original YAML does not affect authoritative settings on restart.
    document["plugin_platform"]["instances"]["team"]["enabled"] = False
    config.write_text(yaml.safe_dump(document))
    recovered = PluginSettingsService(service.root)
    assert recovered.get_config("team")["desired_enabled"]
    with pytest.raises(PluginError) as caught:
        recovered.import_profile(str(config), 1, "new-initialization", True)
    assert caught.value.code == "profile_initialized"
    exported = recovered.export_profile()
    config.write_text(yaml.safe_dump({"plugin_platform": exported["plugin_platform"]}))
    (tmp_path / "plugins.lock.json").write_text(json.dumps(exported["lock"]))
    other = PluginSettingsService(tmp_path / "another-target")
    other.import_profile(str(config), 0, "import-export")
    assert other.get_config("team")["config"] == recovered.get_config("team")["config"]
    assert other.store.read()["instances"]["team"]["aliases"] == {"public-guide": "guide"}


def test_invalid_import_does_not_partially_update_instances(service, package, tmp_path):
    config, document, lock = _write_deployment(tmp_path, package)
    document["plugin_platform"]["instances"]["broken"] = {"package": "guide", "config": {"language": "bad"}}
    config.write_text(yaml.safe_dump(document))
    with pytest.raises(PluginError) as caught:
        service.import_profile(str(config), 0, "import")
    assert caught.value.code == "invalid_config"
    assert service.list_plugins()["plugins"] == []
    assert service.activation_status()["profile_revision"] == 0
    del document["plugin_platform"]["instances"]["broken"]
    config.write_text(yaml.safe_dump(document))
    lock["packages"]["guide"]["package_digest"] = "0" * 64
    (tmp_path / "plugins.lock.json").write_text(json.dumps(lock))
    with pytest.raises(PluginError) as caught:
        service.import_profile(str(config), 0, "import")
    assert caught.value.code == "package_corrupt"
    assert service.list_plugins()["plugins"] == []


def test_mixed_host_and_deployment_import_preserves_bound_target_and_excludes_host_values(service, package, tmp_path):
    config, document, _ = _write_deployment(tmp_path, package)
    ignored_state_root = tmp_path / "must-not-be-used-as-state"
    document["plugin_platform"].update({
        "enabled": False, "state_dir": str(ignored_state_root), "target_id": "another-host",
        "profile_id": "another-profile", "runtime_profiles": {
            "host-only-check": {"python_modules": ["host_private_module"], "timeout_seconds": 15}},
    })
    document["llm"] = {"api_key": "another-main-config-secret"}
    config.write_text(yaml.safe_dump(document))
    imported = service.import_profile(str(config), 0, "mixed-import")
    assert (imported["target_id"], imported["profile_id"]) == ("local", "default")
    assert service.get_config("team")["desired_enabled"] is True
    assert service.active_plugin_sources() == []
    assert not ignored_state_root.exists()
    assert PluginSettingsService(service.root, "another-host", "another-profile").list_plugins()["plugins"] == []
    exported = service.export_profile()
    assert set(exported["plugin_platform"]) == {"lock_file", "instances", "bindings"}
    persisted_and_exported = service.store.path.read_text() + json.dumps(exported)
    for ignored in ("must-not-appear-in-plugin-state", "another-main-config-secret", "another-host",
                    "another-profile", "must-not-be-used-as-state", "host-only-check", "host_private_module"):
        assert ignored not in persisted_and_exported
    # Host-only edits do not alter the imported deployment or invalidate its receipt.
    document["plugin_platform"]["target_id"] = "third-host"
    config.write_text(yaml.safe_dump(document))
    assert service.import_profile(str(config), 0, "mixed-import") == imported


@pytest.mark.parametrize("field,value", [
    ("enabled", "not-a-boolean"),
    ("target_id", []),
    ("runtime_profiles", []),
    ("unknown_host_setting", "not-accepted"),
])
def test_import_rejects_invalid_or_unknown_host_fields_even_in_safe_mode(service, package, tmp_path, monkeypatch, field, value):
    config, document, _ = _write_deployment(tmp_path, package)
    document["plugin_platform"][field] = value
    config.write_text(yaml.safe_dump(document))
    monkeypatch.setenv("BOX_AGENT_DISABLE_PLUGINS", "1")
    with pytest.raises(PluginError) as caught:
        service.import_profile(str(config), 0, "invalid-host-import")
    assert caught.value.code == "invalid_config"
    assert service.activation_status()["profile_revision"] == 0
    assert service.list_plugins()["plugins"] == []


def test_reference_defaults_and_duplicate_manifest_keys(package):
    schema_path = package / "config.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"]["language"] = {"$ref": "fields.json#/$defs/language"}
    (package / "fields.json").write_text(json.dumps({"$defs": {"language": {"type": "string", "default": "zh-CN"}}}))
    schema_path.write_text(json.dumps(schema))
    assert load_manifest(package).normalize_config({}) == ({"language": "zh-CN"}, [])
    path = package / "plugin.yaml"
    path.write_text(path.read_text() + "runtime: {kind: resources}\n")
    with pytest.raises(PluginError) as caught:
        load_manifest(package)
    assert caught.value.code == "invalid_manifest"


def test_install_retry_returns_original_receipt_when_source_was_removed(service, package):
    first = install(service, package)
    # A retry identifies the original operation; it does not reinstall mutable input.
    (package / "plugin.yaml").unlink()
    assert install(service, package) == first
    with pytest.raises(PluginError) as caught:
        service.install("team", str(package), enabled=True, config={}, request_id="install-1")
    assert caught.value.code == "request_id_reused"


def test_durability_failure_after_replace_is_uncertain_and_retry_is_idempotent(service, package, monkeypatch):
    from box_agent.plugins import config_store
    first = install(service, package)
    sync = config_store.os.fsync
    calls = 0

    def fail_directory_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory sync failure")
        return sync(descriptor)

    monkeypatch.setattr(config_store.os, "fsync", fail_directory_sync)
    with pytest.raises(PluginError) as caught:
        save(service, first)
    assert caught.value.code == "config_write_uncertain"
    monkeypatch.setattr(config_store.os, "fsync", sync)
    recovered = PluginSettingsService(service.root)
    assert recovered.get_config("team")["saved_revision"] == 2
    assert save(recovered, first)["saved_revision"] == 2


def _competing_apply(root: str, revision: int, name: str, queue) -> None:
    try:
        PluginSettingsService(Path(root)).prepare_apply(revision, name)
        queue.put("prepared")
    except PluginError as exc:
        queue.put(exc.code)


def test_two_managers_cannot_prepare_distinct_candidates_at_once(service, package):
    first = install(service, package)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=_competing_apply, args=(str(service.root), first["profile_revision"], str(i), queue)) for i in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in processes) == ["activation_busy", "prepared"]


def _competing_install(root: str, source: str, barrier, queue) -> None:
    # Pause both writers immediately before publication to exercise rename races.
    rename = Path.rename

    def simultaneous_rename(self, destination):
        barrier.wait(timeout=10)
        return rename(self, destination)

    Path.rename = simultaneous_rename
    result = PackageStore(Path(root)).install(Path(source))
    queue.put(result["package_digest"])


def test_concurrent_package_publication_reuses_the_same_verified_bytes(tmp_path, package):
    root = tmp_path / "packages"
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_competing_install, args=(str(root), str(package), barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    hashes = [queue.get(timeout=2) for _ in processes]
    assert hashes == [package_digest(package)] * 2
    assert not list(root.glob(".install-*"))
    assert not (root / hashes[0]).stat().st_mode & 0o222
    assert PackageStore(root).load(hashes[0]).data["id"] == "org.example.guide"


@pytest.mark.parametrize("snapshot_name,field,value", [
    ("active", "aliases", None),
    ("active", "aliases", {"guide": []}),
    ("active", "manifest", []),
    ("active", "enabled", "false"),
    ("active", "expose", [None]),
    ("candidate", "config_revision", True),
    ("candidate", "config", []),
    ("candidate", "package_root", None),
])
def test_corrupt_snapshot_values_fail_with_typed_diagnostic(service, package, snapshot_name, field, value):
    first = install(service, package)
    activate(service)
    service.prepare_apply(first["profile_revision"], "next")
    state = json.loads(service.store.path.read_text())
    snapshot = state["active"] if snapshot_name == "active" else state["activation"]["snapshot"]
    snapshot["instances"]["team"][field] = value
    service.store.path.write_text(json.dumps(state))
    with pytest.raises(PluginError) as caught:
        service.get_snapshot()
    assert caught.value.code == "config_corrupt"
    # Reads never silently replace damaged state or activate a fallback snapshot.
    assert json.loads(service.store.path.read_text()) == state


def test_corrupt_active_aliases_are_quarantined_at_core_load(service, package):
    from types import SimpleNamespace
    from box_agent.plugins.skill_sources import load_plugin_skill_plan
    install(service, package)
    activate(service)
    state = json.loads(service.store.path.read_text())
    state["active"]["instances"]["team"]["aliases"] = None
    service.store.path.write_text(json.dumps(state))
    config = SimpleNamespace(plugin_platform=SimpleNamespace(enabled=True, state_dir=str(service.root),
        target_id="local", profile_id="default"))
    plan = load_plugin_skill_plan(config)
    assert plan.errors and not plan.skills
