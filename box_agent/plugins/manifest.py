"""Static, non-executing contracts for external Skill resource packages."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urldefrag, urlsplit

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

PLUGIN_API_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SKILL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
PACKAGE_ID = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$"


class PluginError(ValueError):
    """A stable management error, safe for a host adapter to serialize."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PluginError("invalid_json", "Value must be finite JSON data") from exc


def load_yaml(text: str) -> Any:
    """Reject duplicate mapping keys instead of silently choosing a declaration."""
    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise yaml.constructor.ConstructorError(None, None, "Duplicate or non-string mapping key", key_node.start_mark)
                result[key] = self.construct_object(value_node, deep=deep)
            return result
    return yaml.load(text, Loader=UniqueLoader)


def contained_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    """Resolve only portable, literal package paths; never follow symlinks."""
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise PluginError("invalid_path", "Expected a nonempty package-relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ":" in relative or any(part in {".", ".."} for part in relative.split("/")):
        raise PluginError("invalid_path", "Package paths must not escape their root", {"path": relative})
    root = root.resolve()
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise PluginError("invalid_path", "Package symlinks are not supported", {"path": relative})
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise PluginError("invalid_path", "Package path escaped its root", {"path": relative})
    if must_exist and not resolved.exists():
        raise PluginError("missing_resource", "Package resource is missing", {"path": relative})
    return resolved


_STRING_LIST = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "uniqueItems": True, "maxItems": 128}
MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "id", "version", "requires", "runtime", "config_schema", "ui", "contributes"],
    "properties": {
        "schema_version": {"const": 1}, "id": {"type": "string", "pattern": PACKAGE_ID, "maxLength": 128},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", "maxLength": 64},
        "display_name": {"type": "string", "maxLength": 256},
        "description": {"type": "string", "maxLength": 4096},
        "requires": {"type": "object", "additionalProperties": False,
                     "required": ["box_agent", "plugin_api"], "properties": {
                         "box_agent": {"type": "string", "minLength": 1, "maxLength": 128},
                         "plugin_api": {"const": PLUGIN_API_VERSION},
                         "runtime_profile": {"type": "string", "pattern": SKILL_NAME},
                         "plugins": {"type": "object", "maxProperties": 0},
                     }},
        "runtime": {"type": "object", "additionalProperties": False,
                    "required": ["kind"], "properties": {"kind": {"const": "resources"}}},
        "config_schema": {"type": "string", "minLength": 1, "maxLength": 512},
        "ui": {"type": "object", "additionalProperties": False, "required": ["config"], "properties": {
            "config": {"type": "object", "additionalProperties": False,
                       "required": ["entry", "bridge_version"], "properties": {
                           "entry": {"type": "string", "minLength": 1, "maxLength": 512},
                           "bridge_version": {"const": 1},
                       }}}},
        "contributes": {"type": "object", "additionalProperties": False, "required": ["skills"],
                        "properties": {"skills": {"type": "array", "minItems": 1, "maxItems": 128,
                            "items": {"type": "object", "additionalProperties": False,
                                      "required": ["id", "path"], "properties": {
                                          "id": {"type": "string", "pattern": IDENTIFIER},
                                          "path": {"type": "string", "minLength": 1, "maxLength": 512},
                                          "config_keys": _STRING_LIST, "requires_host_tools": _STRING_LIST,
                                          "enabled_by": {"type": "array", "minItems": 1, "maxItems": 128,
                                                         "uniqueItems": True,
                                                         "items": {"type": "string", "pattern": IDENTIFIER}},
                                      }}}}},
    },
}


def validate_enabled_by(declarations: list[dict[str, Any]]) -> None:
    """Validate same-package usage links independently of aliases or user state."""
    entries = {entry["id"]: entry for entry in declarations}
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise PluginError("invalid_manifest", "Skill enabled_by must not contain self references or cycles")
        if key in visited:
            return
        visiting.add(key)
        for target in entries[key].get("enabled_by", []):
            if target not in entries:
                raise PluginError("invalid_manifest", "Skill enabled_by references an undeclared contribution",
                                  {"skill": key, "target": target})
            visit(target)
        visiting.remove(key)
        visited.add(key)

    for key in entries:
        visit(key)


def _validation_errors(validator: Draft202012Validator, value: Any) -> list[dict[str, str]]:
    try:
        return [{"path": "/" + "/".join(str(x) for x in e.absolute_path), "message": e.message}
                for e in sorted(validator.iter_errors(value), key=lambda e: str(list(e.absolute_path)))][:64]
    except Exception as exc:
        raise PluginError("invalid_schema", "Configuration schema could not be evaluated") from exc


def _stable_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", value)
    if not match:
        raise PluginError("invalid_version", "Expected a semantic version", {"version": value})
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def check_host_version(expression: str, version: str) -> None:
    actual = _stable_version(version)
    tokens = expression.split()
    if not tokens:
        raise PluginError("invalid_version", "Host version requirement is empty")
    for token in tokens:
        match = re.fullmatch(r"(==|>=|<=|>|<)(\d+\.\d+\.\d+)", token)
        if not match:
            raise PluginError("invalid_version", "Version ranges use space-separated comparisons", {"range": expression})
        op, wanted = match.groups()
        expected = _stable_version(wanted)
        valid = {"==": actual == expected, ">=": actual >= expected, "<=": actual <= expected,
                 ">": actual > expected, "<": actual < expected}[op]
        if not valid:
            raise PluginError("incompatible_host", "Plugin does not support this Box-Agent version",
                              {"required": expression, "actual": version})


@dataclass(frozen=True)
class ResourceManifest:
    root: Path
    data: dict[str, Any]
    config_schema: dict[str, Any]
    schema_digest: str
    schema_documents: dict[str, Any]

    def _validator(self) -> Draft202012Validator:
        base = contained_path(self.root, self.data["config_schema"]).as_uri()
        # A local resource URI gives relative $ref a base without granting retrieval.
        return Draft202012Validator({"$ref": base}, registry=self._registry())

    def _registry(self) -> Registry:
        registry: Registry = Registry()
        for uri, schema in self.schema_documents.items():
            registry = registry.with_resource(uri, Resource.from_contents(schema, default_specification=DRAFT202012))
        return registry

    def normalize_config(self, config: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
        if not isinstance(config, dict):
            return {}, [{"path": "/", "message": "Configuration must be an object"}]
        if len(json_bytes(config)) > MAX_CONFIG_BYTES:
            raise PluginError("config_too_large", "Configuration exceeds 64 KiB")
        candidate = copy.deepcopy(config)

        base = contained_path(self.root, self.data["config_schema"]).as_uri()

        def defaults(schema: Any, value: Any, resolver: Any, depth: int = 0) -> None:
            if depth > 32 or not isinstance(schema, dict):
                return
            if "$ref" in schema:
                resolved = resolver.lookup(schema["$ref"])
                defaults(resolved.contents, value, resolved.resolver, depth + 1)
            if isinstance(value, list):
                for element in value:
                    defaults(schema.get("items"), element, resolver, depth + 1)
            if not isinstance(value, dict):
                return
            for name, child in schema.get("properties", {}).items():
                if not isinstance(child, dict):
                    continue
                default_schema = child
                seen: set[str] = set()
                child_resolver = resolver
                while "default" not in default_schema and "$ref" in default_schema:
                    reference = default_schema["$ref"]
                    if reference in seen or len(seen) > 32:
                        break
                    seen.add(reference)
                    resolved = child_resolver.lookup(reference)
                    default_schema, child_resolver = resolved.contents, resolved.resolver
                    if not isinstance(default_schema, dict):
                        default_schema = {}
                        break
                if name not in value and "default" in default_schema:
                    value[name] = copy.deepcopy(default_schema["default"])
                if name in value:
                    defaults(child, value[name], resolver, depth + 1)

        try:
            defaults(self.config_schema, candidate, self._registry().resolver(base))
        except Exception as exc:
            raise PluginError("invalid_schema", "Configuration schema defaults could not be resolved") from exc
        if len(json_bytes(candidate)) > MAX_CONFIG_BYTES:
            raise PluginError("config_too_large", "Normalized configuration exceeds 64 KiB")
        return candidate, _validation_errors(self._validator(), candidate)


def load_manifest(root: Path, *, host_version: str = "1.0.0") -> ResourceManifest:
    root = root.resolve()
    path = contained_path(root, "plugin.yaml")
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise PluginError("invalid_manifest", "Manifest is missing or exceeds 256 KiB")
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
        json_bytes(data)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise PluginError("invalid_manifest", "Manifest is not valid UTF-8 YAML") from exc
    if isinstance(data, dict) and (
        set(data.get("contributes", {}) if isinstance(data.get("contributes"), dict) else {}) - {"skills"}
        or isinstance(data.get("runtime"), dict) and data["runtime"].get("kind") != "resources"
    ):
        raise PluginError("unsupported_feature", "This runtime supports external Skill resource packages only")
    errors = _validation_errors(Draft202012Validator(MANIFEST_SCHEMA), data)
    if errors:
        raise PluginError("invalid_manifest", "Manifest validation failed", {"errors": errors})
    check_host_version(data["requires"]["box_agent"], host_version)
    ui_path = contained_path(root, data["ui"]["config"]["entry"])
    if ui_path.suffix.lower() != ".html" or not ui_path.is_file() or ui_path.stat().st_size > 2 * 1024 * 1024:
        raise PluginError("invalid_ui", "Configuration UI must be an HTML file of at most 2 MiB")
    try:
        ui_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PluginError("invalid_ui", "Configuration UI must be UTF-8 HTML") from exc
    schema_path = contained_path(root, data["config_schema"])
    documents: dict[str, Any] = {}
    schema_hashes: dict[str, str] = {}

    def read_schema(current: Path) -> Any:
        uri = current.as_uri()
        if uri in documents:
            return documents[uri]
        if len(documents) >= 32 or not current.is_file() or current.stat().st_size > MAX_SCHEMA_BYTES:
            raise PluginError("invalid_schema", "Schema documents exceed package limits")
        try:
            raw = current.read_bytes()
            schema = json.loads(raw)
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise PluginError("invalid_schema", "Invalid configuration JSON Schema",
                              {"path": current.relative_to(root).as_posix()}) from exc
        documents[uri] = schema
        schema_hashes[current.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()

        def visit(node: Any, depth: int = 0) -> None:
            if depth > 64:
                raise PluginError("invalid_schema", "Schema nesting exceeds 64 levels")
            if isinstance(node, dict):
                if "$id" in node:
                    raise PluginError("invalid_schema", "Use package-relative $ref without custom $id")
                for key in ("$ref", "$dynamicRef"):
                    if key in node:
                        ref = node[key]
                        if not isinstance(ref, str):
                            raise PluginError("invalid_schema", "Schema reference must be a string")
                        filename, _ = urldefrag(ref)
                        if filename:
                            parts = urlsplit(filename)
                            if parts.scheme or parts.netloc or parts.query:
                                raise PluginError("invalid_schema", "Remote schema references are forbidden")
                            target_relative = (current.parent.relative_to(root) / unquote(filename)).as_posix()
                            read_schema(contained_path(root, target_relative))
                for value in node.values():
                    visit(value, depth + 1)
            elif isinstance(node, list):
                for value in node:
                    visit(value, depth + 1)
        visit(schema)
        return schema

    config_schema = read_schema(schema_path)
    if not isinstance(config_schema, dict) or config_schema.get("type") != "object" or config_schema.get("additionalProperties") is not False:
        raise PluginError("invalid_schema", "Configuration schema must be an object with additionalProperties:false")
    skill_ids: set[str] = set()
    skill_paths: set[str] = set()
    for skill in data["contributes"]["skills"]:
        if skill["id"] in skill_ids or skill["path"] in skill_paths:
            raise PluginError("duplicate_skill", "Skill IDs and paths must be unique")
        skill_ids.add(skill["id"])
        skill_paths.add(skill["path"])
        skill_dir = contained_path(root, skill["path"])
        if not skill_dir.is_dir():
            raise PluginError("missing_resource", "Skill path must be a directory", {"skill": skill["id"]})
        body = contained_path(root, skill["path"] + "/SKILL.md")
        if not body.is_file() or body.stat().st_size > 2 * 1024 * 1024:
            raise PluginError("invalid_skill", "Skill body must be a file of at most 2 MiB")
        for key in skill.get("config_keys", []):
            if key not in config_schema.get("properties", {}):
                raise PluginError("invalid_manifest", "Skill config_keys references an undeclared configuration field",
                                  {"skill": skill["id"], "field": key})
    validate_enabled_by(data["contributes"]["skills"])
    return ResourceManifest(root, data, config_schema,
                            hashlib.sha256(json_bytes(schema_hashes)).hexdigest(), documents)
