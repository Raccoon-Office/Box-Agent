"""Frontend-independent plugin management. No Agent or model is needed here.

Callers authenticate the operator before exposing this service. Runtime-only
confirmation/reporting methods must never be exposed to a plugin configuration
page. Pages receive only the current instance's get/validate/save operations.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .config_store import PluginConfigStore
from .manifest import IDENTIFIER, SKILL_NAME, PluginError, ResourceManifest, contained_path, json_bytes, load_yaml
from .package_store import PackageStore

_UI_TYPES = {".js": "text/javascript", ".css": "text/css", ".json": "application/json",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
             ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".ico": "image/x-icon"}
_DEPLOYMENT_FIELDS = frozenset({"lock_file", "instances", "bindings"})
_HOST_CONFIGURATION_FIELDS = frozenset({"enabled", "state_dir", "target_id", "profile_id", "runtime_profiles"})


class PluginSettingsService:
    """Own package/configuration state for one explicit target and profile."""

    def __init__(self, root: Path, target_id: str = "local", profile_id: str = "default",
                 *, host_version: str = "1.0.0") -> None:
        for value in (target_id, profile_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
                raise PluginError("invalid_scope", "Invalid execution target or profile identifier")
        self.root = Path(root).expanduser().resolve()
        self.target_id = target_id
        self.profile_id = profile_id
        scope = hashlib.sha256(json_bytes([target_id, profile_id])).hexdigest()[:24]
        self.store = PluginConfigStore(self.root / "profiles" / scope, target_id, profile_id)
        self.packages = PackageStore(self.root / "packages", host_version=host_version)

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(params, (dict, type(None))):
            raise PluginError("invalid_request", "Management parameters must be an object")
        params = dict(params or {})
        for key, expected in (("target_id", self.target_id), ("profile_id", self.profile_id)):
            if key in params and params.pop(key) != expected:
                raise PluginError("scope_mismatch", "Request belongs to another execution target or profile")
        methods = {
            "plugins.list": (self.list_plugins, set()),
            "plugins.install": (self.install, {"instance_id", "package_path", "enabled", "config", "aliases", "expose", "expected_revision", "request_id"}),
            "plugins.ui": (self.get_ui, {"instance_id", "package_digest", "schema_digest"}),
            "config.get": (self.get_config, {"instance_id"}),
            "config.validate": (self.validate_config, {"instance_id", "config", "package_digest", "schema_digest"}),
            "config.save": (self.save_config, {"instance_id", "config", "expected_revision", "request_id", "package_digest", "schema_digest"}),
            "instance.set_enabled": (self.set_enabled, {"instance_id", "enabled", "expected_revision", "request_id"}),
            "profile.prepare_apply": (self.prepare_apply, {"expected_profile_revision", "request_id"}),
            "profile.confirm_apply": (self.confirm_apply, {"activation_id", "request_id"}),
            "profile.fail_apply": (self.fail_apply, {"activation_id", "error", "request_id"}),
            "profile.rollback": (self.rollback, {"expected_profile_revision", "request_id"}),
            "profile.import": (self.import_profile, {"config_path", "expected_profile_revision", "request_id", "initialize_only"}),
            "profile.export": (self.export_profile, set()),
            "profile.preflight": (self.preflight_snapshot, {"activation_id"}),
            "activation.status": (self.activation_status, {"activation_id"}),
            "runtime.report": (self.record_runtime_status, {"generation", "healthy", "errors"}),
        }
        if method not in methods:
            raise PluginError("unsupported_method", "Unknown plugin-management method", {"method": method})
        handler, fields = methods[method]
        if set(params) - fields:
            raise PluginError("invalid_request", "Unknown management fields", {"fields": sorted(set(params) - fields)})
        try:
            inspect.signature(handler).bind(**params)
        except TypeError as exc:
            raise PluginError("invalid_request", "Invalid or missing management arguments") from exc
        return handler(**params)

    def _identity(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"target_id": self.target_id, "profile_id": self.profile_id,
                "profile_revision": state["profile_revision"], "generation": state["active"]["generation"]}

    @staticmethod
    def _instance(state: dict[str, Any], instance_id: str) -> dict[str, Any]:
        if not isinstance(instance_id, str) or not re.fullmatch(IDENTIFIER, instance_id):
            raise PluginError("invalid_instance", "Invalid plugin instance identifier")
        try:
            return state["instances"][instance_id]
        except KeyError as exc:
            raise PluginError("instance_not_found", "Plugin instance does not exist", {"instance_id": instance_id}) from exc

    @staticmethod
    def _expect_revision(instance: dict[str, Any], expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision != instance["saved_revision"]:
            raise PluginError("config_conflict", "Configuration changed; reload before saving",
                              {"saved_revision": instance["saved_revision"]})

    @staticmethod
    def _expect_profile(state: dict[str, Any], expected: int) -> None:
        if type(expected) is not int or expected != state["profile_revision"]:
            raise PluginError("config_conflict", "Business configuration changed; reload before applying",
                              {"profile_revision": state["profile_revision"]})

    @staticmethod
    def _receipt(state: dict[str, Any], request_id: str, method: str, payload: Any) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise PluginError("invalid_request", "A request_id of at most 128 characters is required")
        digest = hashlib.sha256(json_bytes([method, payload])).hexdigest()
        old = state["receipts"].get(request_id)
        if old:
            if old["digest"] != digest:
                raise PluginError("request_id_reused", "request_id was already used with different content")
            return digest, copy.deepcopy(old["result"])
        return digest, None

    @staticmethod
    def _remember(state: dict[str, Any], request_id: str, digest: str, result: dict[str, Any]) -> None:
        receipts = state["receipts"]
        receipts[request_id] = {"digest": digest, "result": copy.deepcopy(result)}
        while len(receipts) > 128:
            del receipts[next(iter(receipts))]

    def _audit(self, state: dict[str, Any], event: str, **data: Any) -> None:
        # Local administration is authorized by the host OS principal. Do not
        # infer a frontend account, or accept an actor supplied by plugin HTML.
        state["audit"].append({
            "event": event, "time": time.time(), **data,
            "actor": {"kind": "local_os", "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None},
            "target_id": self.target_id, "profile_id": self.profile_id,
            "profile_revision": state["profile_revision"],
            "active_generation": state["active"]["generation"],
        })
        state["audit"] = state["audit"][-256:]

    @classmethod
    def _archive_activation(cls, state: dict[str, Any]) -> None:
        current = cls._activation_view(state)
        if current is not None:
            history = state.setdefault("activation_history", {})
            history[current["activation_id"]] = current
            while len(history) > 32:
                del history[next(iter(history))]

    def _manifest(self, instance: dict[str, Any], *, verify: bool = False) -> ResourceManifest:
        manifest = self.packages.load(instance["package_digest"], verify=verify)
        if manifest.schema_digest != instance["schema_digest"] or manifest.data != instance["manifest"]:
            raise PluginError("package_corrupt", "Installed metadata differs from its locked declaration")
        return manifest

    @staticmethod
    def _check_editor(instance: dict[str, Any], package_digest: str | None, schema_digest: str | None) -> None:
        if package_digest != instance["package_digest"] or schema_digest != instance["schema_digest"]:
            raise PluginError("stale_package", "The plugin package or schema changed; reload its configuration page")

    def _view(self, state: dict[str, Any], instance_id: str, *, include_config: bool = False) -> dict[str, Any]:
        item = self._instance(state, instance_id)
        active = state["active"]["instances"].get(instance_id)
        active_revision = active["config_revision"] if active else None
        active_enabled = bool(active and active["enabled"])
        manifest = item["manifest"]
        result = {**self._identity(state), "instance_id": instance_id,
                  **{k: item[k] for k in ("package_id", "package_version", "package_digest", "schema_digest", "saved_revision")},
                  "display_name": manifest.get("display_name", manifest["id"]),
                  "description": manifest.get("description", ""),
                  "desired_enabled": item["enabled"], "active_enabled": active_enabled,
                  "active_revision": active_revision,
                  "restart_required": active is None or active_revision != item["saved_revision"],
                  "ui": copy.deepcopy(manifest["ui"]["config"])}
        if include_config:
            loaded = self._manifest(item)
            result.update(config=copy.deepcopy(item["config"]), schema=copy.deepcopy(loaded.config_schema))
        return result

    def list_plugins(self) -> dict[str, Any]:
        state = self.store.read()
        plugins = []
        for instance_id in sorted(state["instances"]):
            item = self._view(state, instance_id)
            try:
                self._manifest(state["instances"][instance_id])
            except PluginError as exc:
                item["error"] = exc.to_dict()
            plugins.append(item)
        return {**self._identity(state), "plugins": plugins, "activation": self._activation_view(state),
                "runtime": state.get("runtime")}

    def get_config(self, instance_id: str) -> dict[str, Any]:
        return self._view(self.store.read(), instance_id, include_config=True)

    def install(self, instance_id: str, package_path: str, *, enabled: bool = False,
                config: dict[str, Any] | None = None, aliases: dict[str, str] | None = None,
                expose: list[str] | None = None, expected_revision: int | None = None,
                request_id: str) -> dict[str, Any]:
        if not isinstance(instance_id, str) or not re.fullmatch(IDENTIFIER, instance_id) or type(enabled) is not bool:
            raise PluginError("invalid_request", "Invalid instance identifier or enabled flag")
        if not isinstance(package_path, str):
            raise PluginError("invalid_request", "package_path must be a path on this execution target")
        payload = {"instance_id": instance_id, "package_path": package_path, "enabled": enabled,
                   "config": config, "aliases": aliases, "expose": expose, "expected_revision": expected_revision}
        with self.store.transaction() as state:
            _, receipt = self._receipt(state, request_id, "install", payload)
            if receipt is not None:
                return receipt
        # Copying may be slow; never hold the profile writer lock across it.
        installed = self.packages.install(Path(package_path))
        manifest = self.packages.load(installed["package_digest"], verify=False)
        values, errors = manifest.normalize_config(config if config is not None else {})
        if errors:
            raise PluginError("invalid_config", "Plugin configuration is invalid", {"errors": errors})
        ids = {s["id"] for s in manifest.data["contributes"]["skills"]}
        aliases = aliases if aliases is not None else {}
        expose = expose if expose is not None else sorted(ids)
        if (not isinstance(aliases, dict) or any(not isinstance(k, str) or not re.fullmatch(SKILL_NAME, k)
                or v not in ids for k, v in aliases.items())):
            raise PluginError("invalid_binding", "Skill aliases must map public names to declared contribution IDs")
        if not isinstance(expose, list) or any(x not in ids for x in expose) or len(set(expose)) != len(expose):
            raise PluginError("invalid_binding", "expose must list unique declared Skill IDs")
        if any(v not in expose for v in aliases.values()):
            raise PluginError("invalid_binding", "Public aliases must refer to exposed Skills")
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "install", payload)
            if receipt is not None:
                return receipt
            previous = state["instances"].get(instance_id)
            if previous:
                self._expect_revision(previous, expected_revision)
            elif expected_revision not in (None, 0):
                raise PluginError("config_conflict", "New instances start at revision zero")
            for other_id, other in state["instances"].items():
                if other_id != instance_id and set(other["aliases"]) & set(aliases):
                    raise PluginError("alias_conflict", "A public Skill alias is already bound to another instance")
            revision = previous["saved_revision"] + 1 if previous else 1
            state["instances"][instance_id] = {**installed, "manifest": manifest.data,
                "config": values, "enabled": enabled, "aliases": dict(aliases), "expose": list(expose),
                "saved_revision": revision}
            state["profile_revision"] += 1
            self._audit(state, "package.installed", instance_id=instance_id, package_digest=installed["package_digest"],
                        revision=revision, old_revision=revision - 1, new_revision=revision, request_id=request_id)
            result = self._view(state, instance_id, include_config=True)
            self._remember(state, request_id, digest, result)
            return result

    def validate_config(self, instance_id: str, config: Any, package_digest: str | None = None,
                        schema_digest: str | None = None) -> dict[str, Any]:
        state = self.store.read()
        item = self._instance(state, instance_id)
        self._check_editor(item, package_digest, schema_digest)
        values, errors = self._manifest(item).normalize_config(config)
        return {"valid": not errors, "errors": errors, "config": values}

    def save_config(self, instance_id: str, config: Any, expected_revision: int, request_id: str,
                    package_digest: str, schema_digest: str) -> dict[str, Any]:
        payload = {"instance_id": instance_id, "config": config, "expected_revision": expected_revision,
                   "package_digest": package_digest, "schema_digest": schema_digest}
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "save", payload)
            if receipt is not None:
                return receipt
            item = self._instance(state, instance_id)
            self._check_editor(item, package_digest, schema_digest)
            self._expect_revision(item, expected_revision)
            values, errors = self._manifest(item).normalize_config(config)
            if errors:
                raise PluginError("invalid_config", "Plugin configuration is invalid", {"errors": errors})
            item["config"] = values
            item["saved_revision"] += 1
            state["profile_revision"] += 1
            self._audit(state, "config.saved", instance_id=instance_id, revision=item["saved_revision"],
                        old_revision=expected_revision, new_revision=item["saved_revision"], request_id=request_id,
                        fields=sorted(values), config_digest=hashlib.sha256(json_bytes(values)).hexdigest())
            result = self._view(state, instance_id, include_config=True)
            self._remember(state, request_id, digest, result)
            return result

    def set_enabled(self, instance_id: str, enabled: bool, expected_revision: int, request_id: str) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise PluginError("invalid_request", "enabled must be a boolean")
        payload = {"instance_id": instance_id, "enabled": enabled, "expected_revision": expected_revision}
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "set_enabled", payload)
            if receipt is not None:
                return receipt
            item = self._instance(state, instance_id)
            self._expect_revision(item, expected_revision)
            item["enabled"] = enabled
            item["saved_revision"] += 1
            state["profile_revision"] += 1
            self._audit(state, "instance.enabled_changed", instance_id=instance_id, enabled=enabled,
                        revision=item["saved_revision"], old_revision=expected_revision,
                        new_revision=item["saved_revision"], request_id=request_id)
            # Disabling a corrupt package must remain possible without importing or reading it.
            result = self._view(state, instance_id)
            self._remember(state, request_id, digest, result)
            return result

    def import_profile(self, config_path: str, expected_profile_revision: int, request_id: str,
                       initialize_only: bool = False) -> dict[str, Any]:
        """Explicitly merge deployment YAML into saved state, never into active.

        Files and packages are read on this execution target. Unmentioned instances
        retain their settings; disable them explicitly before a later apply.
        Host fields may coexist in plugin_platform and are validated, but never
        imported. The service's already selected target/profile/store remain fixed.

        YAML: plugin_platform: {lock_file: plugins.lock.json, instances:
          {team: {package: guide, enabled: true, config: {}}}, bindings:
          {skills: {public-guide: team/guide}}}
        Lock: {schema_version: 1, packages: {guide: {package_id: org.example.guide,
          version: 1.0.0, package_digest: <sha256>, schema_digest: <sha256>,
          path: ./guide-package}}}. path is relative to the lock; omit it only
        when the content hash already exists in this target's package store.
        """
        if not isinstance(config_path, str) or type(initialize_only) is not bool:
            raise PluginError("invalid_request", "Import requires a configuration path and boolean initialize_only")
        source = Path(config_path).expanduser().resolve()
        try:
            if source.stat().st_size > 4 * 1024 * 1024:
                raise PluginError("config_too_large", "Deployment configuration exceeds 4 MiB")
            document = load_yaml(source.read_text(encoding="utf-8"))
            platform = document.get("plugin_platform") if isinstance(document, dict) else None
            if not isinstance(platform, dict) or set(platform) - (_DEPLOYMENT_FIELDS | _HOST_CONFIGURATION_FIELDS):
                raise PluginError("invalid_config", "Import requires a plugin_platform with lock_file, instances and optional bindings")
            from ..config import PluginPlatformConfig
            try:
                # Validate directly: from_host_mapping intentionally bypasses
                # broken startup configuration in safe mode, which must not
                # suppress validation of an explicit import operation.
                PluginPlatformConfig(**{key: value for key, value in platform.items()
                                        if key in _HOST_CONFIGURATION_FIELDS})
            except ValueError as exc:
                raise PluginError("invalid_config", "Host plugin settings in the deployment file are invalid") from exc
            platform = {key: value for key, value in platform.items() if key in _DEPLOYMENT_FIELDS}
            if not isinstance(platform.get("lock_file"), str) or not platform["lock_file"]:
                raise PluginError("invalid_config", "plugin_platform.lock_file is required")
            lock_path = (source.parent / platform["lock_file"]).resolve()
            if lock_path.stat().st_size > 1024 * 1024:
                raise PluginError("config_too_large", "Package lock exceeds 1 MiB")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            if isinstance(exc, PluginError):
                raise
            raise PluginError("invalid_config", "Deployment configuration or package lock cannot be read") from exc
        if (not isinstance(lock, dict) or set(lock) != {"schema_version", "packages"}
                or type(lock.get("schema_version")) is not int or lock["schema_version"] != 1
                or not isinstance(lock["packages"], dict)):
            raise PluginError("invalid_lock", "Expected a version 1 package lock")
        entries = platform.get("instances")
        bindings = platform.get("bindings", {})
        if (not isinstance(entries, dict) or len(entries) > 128 or not isinstance(bindings, dict)
                or set(bindings) - {"skills"} or not isinstance(bindings.get("skills", {}), dict)):
            raise PluginError("invalid_config", "Invalid instance or Skill binding mapping")
        public_aliases = bindings.get("skills", {})
        instance_aliases: dict[str, dict[str, str]] = {name: {} for name in entries}
        for public, target in public_aliases.items():
            if not isinstance(public, str) or not re.fullmatch(SKILL_NAME, public) or not isinstance(target, str) or target.count("/") != 1:
                raise PluginError("invalid_binding", "Skill bindings require public_name: instance_id/skill_id")
            owner, contribution = target.split("/")
            if owner not in entries:
                raise PluginError("invalid_binding", "Skill binding references an instance absent from this import")
            instance_aliases[owner][public] = contribution
        # Hash only imported deployment fields and their lock, never host settings or credentials.
        payload = {"platform": platform, "lock": lock, "source_directory": str(source.parent),
                   "expected_profile_revision": expected_profile_revision, "initialize_only": initialize_only}
        with self.store.transaction() as state:
            _, receipt = self._receipt(state, request_id, "import", payload)
            if receipt is not None:
                return receipt
            self._expect_profile(state, expected_profile_revision)
            if initialize_only and (state["profile_revision"] != 0 or state["instances"]):
                raise PluginError("profile_initialized", "Deployment initialization cannot overwrite saved settings")
        prepared: dict[str, dict[str, Any]] = {}
        for instance_id, spec in entries.items():
            if (not isinstance(instance_id, str) or not re.fullmatch(IDENTIFIER, instance_id)
                    or not isinstance(spec, dict) or set(spec) - {"package", "enabled", "config", "expose"}
                    or not isinstance(spec.get("package"), str) or type(spec.get("enabled", False)) is not bool):
                raise PluginError("invalid_config", "Invalid imported plugin instance")
            locked = lock["packages"].get(spec["package"])
            if (not isinstance(locked, dict) or set(locked) - {"package_id", "version", "package_digest", "schema_digest", "path"}
                    or not {"package_id", "version", "package_digest"} <= set(locked)
                    or any(not isinstance(v, str) for v in locked.values())):
                raise PluginError("invalid_lock", "Instance must reference a fully pinned package")
            package_hash = locked["package_digest"]
            try:
                manifest = self.packages.load(package_hash, verify=True)
            except PluginError as exc:
                if exc.code != "package_unavailable" or not locked.get("path"):
                    raise
                installed = self.packages.install((lock_path.parent / locked["path"]).resolve())
                if installed["package_digest"] != package_hash:
                    raise PluginError("package_corrupt", "Import package differs from its locked digest")
                manifest = self.packages.load(package_hash, verify=True)
            if (manifest.data["id"] != locked["package_id"] or manifest.data["version"] != locked["version"]
                    or locked.get("schema_digest", manifest.schema_digest) != manifest.schema_digest):
                raise PluginError("invalid_lock", "Installed package metadata differs from the lock")
            config, errors = manifest.normalize_config(spec.get("config", {}))
            if errors:
                raise PluginError("invalid_config", "Imported configuration is invalid", {"instance_id": instance_id, "errors": errors})
            skills = {skill["id"] for skill in manifest.data["contributes"]["skills"]}
            exposed = spec.get("expose", {"skills": sorted(skills)})
            if (not isinstance(exposed, dict) or set(exposed) != {"skills"} or not isinstance(exposed["skills"], list)
                    or any(not isinstance(skill, str) or skill not in skills for skill in exposed["skills"])
                    or len(set(exposed["skills"])) != len(exposed["skills"])
                    or any(value not in exposed["skills"] for value in instance_aliases[instance_id].values())):
                raise PluginError("invalid_binding", "Imported exposure and aliases must reference declared, exposed Skills")
            prepared[instance_id] = {"package_id": manifest.data["id"], "package_version": manifest.data["version"],
                "package_digest": package_hash, "schema_digest": manifest.schema_digest, "package_root": str(manifest.root),
                "manifest": manifest.data, "config": config, "enabled": spec.get("enabled", False),
                "expose": list(exposed["skills"]), "aliases": instance_aliases[instance_id]}
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "import", payload)
            if receipt is not None:
                return receipt
            self._expect_profile(state, expected_profile_revision)
            combined = {**state["instances"], **prepared}
            aliases: set[str] = set()
            for item in combined.values():
                if aliases & set(item["aliases"]):
                    raise PluginError("alias_conflict", "Import would assign a public Skill alias more than once")
                aliases.update(item["aliases"])
            for name, item in prepared.items():
                item["saved_revision"] = state["instances"].get(name, {}).get("saved_revision", 0) + 1
            state["instances"] = combined
            state["profile_revision"] += 1
            self._audit(state, "profile.imported", instances=sorted(prepared), request_id=request_id,
                        revisions={name: {"old_revision": item["saved_revision"] - 1,
                                          "new_revision": item["saved_revision"]} for name, item in prepared.items()})
            result = {**self._status(state), "imported_instances": sorted(prepared)}
            self._remember(state, request_id, digest, result)
            return result

    def export_profile(self) -> dict[str, Any]:
        """Return only plugin deployment data; do not read the main host config."""
        state = self.store.read()
        instances, packages, aliases = {}, {}, {}
        for name, item in sorted(state["instances"].items()):
            reference = item["package_id"] + "@" + item["package_digest"]
            packages[reference] = {"package_id": item["package_id"], "version": item["package_version"],
                "package_digest": item["package_digest"], "schema_digest": item["schema_digest"],
                "path": str(self.packages.root / item["package_digest"])}
            instances[name] = {"package": reference, "enabled": item["enabled"], "config": copy.deepcopy(item["config"]),
                               "expose": {"skills": list(item["expose"])}}
            aliases.update({public: name + "/" + skill for public, skill in item["aliases"].items()})
        return {**self._identity(state), "plugin_platform": {"lock_file": "./plugins.lock.json", "instances": instances,
                "bindings": {"skills": aliases}}, "lock": {"schema_version": 1, "packages": packages}}

    def _snapshot(self, state: dict[str, Any], generation: str) -> dict[str, Any]:
        instances = {}
        for instance_id, item in state["instances"].items():
            if item["enabled"]:
                manifest = self._manifest(item, verify=True)
                _, errors = manifest.normalize_config(item["config"])
                if errors:
                    raise PluginError("invalid_config", "Candidate configuration failed validation",
                                      {"instance_id": instance_id, "errors": errors})
            instances[instance_id] = {"manifest": copy.deepcopy(item["manifest"]),
                "package_root": str(self.packages.root / item["package_digest"]),
                "package_digest": item["package_digest"], "schema_digest": item["schema_digest"],
                "config": copy.deepcopy(item["config"]), "config_revision": item["saved_revision"],
                "enabled": item["enabled"], "expose": list(item["expose"]), "aliases": dict(item["aliases"])}
        return {"generation": generation, "profile_revision": state["profile_revision"], "instances": instances}

    def prepare_apply(self, expected_profile_revision: int, request_id: str) -> dict[str, Any]:
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "prepare_apply", {"expected_profile_revision": expected_profile_revision})
            if receipt is not None:
                return receipt
            self._expect_profile(state, expected_profile_revision)
            if state.get("activation") and state["activation"]["status"] == "prepared":
                raise PluginError("activation_busy", "Another frozen candidate is waiting for its startup result",
                                  self._activation_view(state))
            activation_id = str(uuid.uuid4())
            generation = "plugins-" + uuid.uuid4().hex
            snapshot = self._snapshot(state, generation)
            self._archive_activation(state)
            state["activation"] = {"activation_id": activation_id, "status": "prepared", "snapshot": snapshot,
                                   "snapshot_digest": hashlib.sha256(json_bytes(snapshot)).hexdigest(), "created_at": time.time()}
            self._audit(state, "activation.prepared", activation_id=activation_id, generation=generation,
                        profile_revision=state["profile_revision"])
            result = {**self._identity(state), **self._activation_view(state), "candidate_generation": generation}
            self._remember(state, request_id, digest, result)
            return result

    @staticmethod
    def _activation_view(state: dict[str, Any]) -> dict[str, Any] | None:
        activation = state.get("activation")
        if activation is None:
            return None
        snapshot = activation["snapshot"]
        return {"activation_id": activation["activation_id"], "status": activation["status"],
                "generation": snapshot["generation"], "profile_revision": snapshot["profile_revision"],
                "snapshot_digest": activation["snapshot_digest"], "error": activation.get("error")}

    def get_snapshot(self, activation_id: str | None = None) -> dict[str, Any]:
        """Return frozen metadata; use preflight_snapshot before consuming bytes."""
        state = self.store.read()
        if activation_id is None:
            return copy.deepcopy(state["active"])
        activation = state.get("activation")
        if not activation or activation["activation_id"] != activation_id or activation["status"] not in {"prepared", "active"}:
            raise PluginError("activation_not_found", "Candidate activation is not available")
        snapshot = activation["snapshot"]
        if hashlib.sha256(json_bytes(snapshot)).hexdigest() != activation["snapshot_digest"]:
            raise PluginError("config_corrupt", "Candidate snapshot differs from its frozen digest")
        return copy.deepcopy(snapshot)

    def _preflight(self, snapshot: dict[str, Any]) -> None:
        for instance_id, item in snapshot["instances"].items():
            if not item["enabled"]:
                continue
            manifest = self._manifest(item, verify=True)
            if Path(item["package_root"]).resolve() != manifest.root:
                raise PluginError("package_corrupt", "Snapshot package path differs from its managed root")
            normalized, errors = manifest.normalize_config(item["config"])
            if errors or normalized != item["config"]:
                raise PluginError("invalid_config", "Snapshot configuration is not normalized and valid",
                                  {"instance_id": instance_id, "errors": errors})

    def preflight_snapshot(self, activation_id: str | None = None) -> dict[str, Any]:
        """Static integrity only; this deliberately does not confirm runtime health."""
        snapshot = self.get_snapshot(activation_id)
        self._preflight(snapshot)
        return {"valid": True, "generation": snapshot["generation"],
                "snapshot_digest": hashlib.sha256(json_bytes(snapshot)).hexdigest(),
                "runtime_verified": False}

    def active_plugin_sources(self, activation_id: str | None = None) -> list[dict[str, Any]]:
        snapshot = self.get_snapshot(activation_id)
        return [{**item, "instance_id": instance_id, "generation": snapshot["generation"]}
                for instance_id, item in snapshot["instances"].items() if item["enabled"]]

    def confirm_apply(self, activation_id: str, request_id: str | None = None) -> dict[str, Any]:
        """Called by the trusted runtime only after actual list/read startup smoke."""
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "confirm_apply", {"activation_id": activation_id}) if request_id is not None else ("", None)
            if receipt is not None:
                return receipt
            activation = state.get("activation")
            if not activation or activation["activation_id"] != activation_id:
                raise PluginError("activation_not_found", "Candidate activation is not available")
            if activation["status"] == "active":
                result = self._status(state)
                if request_id is not None:
                    self._remember(state, request_id, digest, result)
                return result
            if activation["status"] != "prepared":
                raise PluginError("activation_failed", "Failed candidates must be prepared again")
            if hashlib.sha256(json_bytes(activation["snapshot"])).hexdigest() != activation["snapshot_digest"]:
                raise PluginError("config_corrupt", "Candidate snapshot differs from its frozen digest")
            self._preflight(activation["snapshot"])
            state["previous_active"] = copy.deepcopy(state["active"])
            state["active"] = copy.deepcopy(activation["snapshot"])
            activation["status"] = "active"
            state["runtime"] = {"generation": state["active"]["generation"], "healthy": True, "errors": [], "reported_at": time.time()}
            self._audit(state, "activation.confirmed", activation_id=activation_id, generation=state["active"]["generation"])
            result = self._status(state)
            if request_id is not None:
                self._remember(state, request_id, digest, result)
            return result

    def fail_apply(self, activation_id: str, error: Any, request_id: str | None = None) -> dict[str, Any]:
        if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str) or set(error) - {"code", "message", "details"}):
            raise PluginError("invalid_request", "Failure diagnostic requires code, message and optional details")
        encoded = json_bytes(error)
        diagnostic = copy.deepcopy(error) if len(encoded) <= 8192 else {"code": "startup_failed", "message": "Startup diagnostic exceeded its size limit"}
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "fail_apply", {"activation_id": activation_id, "error": error}) if request_id is not None else ("", None)
            if receipt is not None:
                return receipt
            activation = state.get("activation")
            if not activation or activation["activation_id"] != activation_id:
                raise PluginError("activation_not_found", "Candidate activation is not available")
            if activation["status"] == "active":
                raise PluginError("activation_already_active", "Use rollback after an activated runtime fails")
            if activation["status"] != "failed":
                activation["status"] = "failed"
                activation["error"] = diagnostic
                self._audit(state, "activation.failed", activation_id=activation_id,
                            phase="activation", error_code=diagnostic["code"])
            result = self._status(state)
            if request_id is not None:
                self._remember(state, request_id, digest, result)
            return result

    def rollback(self, expected_profile_revision: int, request_id: str) -> dict[str, Any]:
        """Prepare the previous verified snapshot; rollback still needs a restart."""
        with self.store.transaction() as state:
            digest, receipt = self._receipt(state, request_id, "rollback", {"expected_profile_revision": expected_profile_revision})
            if receipt is not None:
                return receipt
            self._expect_profile(state, expected_profile_revision)
            if state.get("activation") and state["activation"]["status"] == "prepared":
                raise PluginError("activation_busy", "Resolve the pending activation before rollback", self._activation_view(state))
            previous = state.get("previous_active")
            if previous is None:
                raise PluginError("rollback_unavailable", "There is no previous verified snapshot")
            candidate = copy.deepcopy(previous)
            # Preserve the desired configuration. Rolling back need not discard the user's edits.
            candidate["generation"] = "plugins-" + uuid.uuid4().hex
            self._preflight(candidate)
            activation_id = str(uuid.uuid4())
            self._archive_activation(state)
            state["activation"] = {"activation_id": activation_id, "status": "prepared", "snapshot": candidate,
                                   "snapshot_digest": hashlib.sha256(json_bytes(candidate)).hexdigest(), "created_at": time.time()}
            self._audit(state, "activation.rollback_prepared", activation_id=activation_id)
            result = {**self._identity(state), **self._activation_view(state), "candidate_generation": candidate["generation"]}
            self._remember(state, request_id, digest, result)
            return result

    def _status(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**self._identity(state), "active_profile_revision": state["active"]["profile_revision"],
                "restart_required": state["active"]["profile_revision"] != state["profile_revision"],
                "activation": self._activation_view(state), "runtime": state.get("runtime")}

    def activation_status(self, activation_id: str | None = None) -> dict[str, Any]:
        state = self.store.read()
        result = self._status(state)
        if activation_id is not None and (not result["activation"] or result["activation"]["activation_id"] != activation_id):
            historical = state.get("activation_history", {}).get(activation_id)
            if historical is None:
                raise PluginError("activation_not_found", "Activation status is not available")
            result["activation"] = copy.deepcopy(historical)
        return result

    def record_runtime_status(self, generation: str, healthy: bool, errors: list[dict[str, Any]]) -> dict[str, Any]:
        if type(healthy) is not bool or not isinstance(errors, list) or len(json_bytes(errors)) > 32 * 1024:
            raise PluginError("invalid_request", "Invalid runtime diagnostic")
        with self.store.transaction() as state:
            if generation != state["active"]["generation"]:
                raise PluginError("stale_generation", "Runtime report is not for the active generation")
            state["runtime"] = {"generation": generation, "healthy": healthy, "errors": copy.deepcopy(errors), "reported_at": time.time()}
            return self._status(state)

    def get_ui(self, instance_id: str, package_digest: str | None = None,
               schema_digest: str | None = None) -> dict[str, Any]:
        state = self.store.read()
        item = self._instance(state, instance_id)
        self._check_editor(item, package_digest, schema_digest)
        manifest = self._manifest(item, verify=True)
        entry = manifest.data["ui"]["config"]["entry"]
        path = contained_path(manifest.root, entry)
        html = path.read_text(encoding="utf-8")
        assets: dict[str, Any] = {}
        total = len(html.encode("utf-8"))
        for asset in sorted(path.parent.rglob("*")):
            if not asset.is_file() or asset == path or asset.suffix.lower() not in _UI_TYPES:
                continue
            checked = contained_path(manifest.root, asset.relative_to(manifest.root).as_posix())
            total += checked.stat().st_size
            if total > 8 * 1024 * 1024 or len(assets) >= 256:
                raise PluginError("ui_too_large", "Configuration UI exceeds 8 MiB or 256 assets")
            assets[asset.relative_to(path.parent).as_posix()] = {
                "mime_type": _UI_TYPES[asset.suffix.lower()], "encoding": "base64",
                "content": base64.b64encode(checked.read_bytes()).decode("ascii")}
        return {"instance_id": instance_id, "package_digest": item["package_digest"],
                "schema_digest": item["schema_digest"], "entry": entry, "bridge_version": 1,
                "html": html, "assets": assets}
