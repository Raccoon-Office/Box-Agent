"""Read a prepared expert package into the existing full-profile contract.

This module does not download, install or execute package resources. Download
size and digest validation belongs to the package preparation step.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_FRONTMATTER = re.compile(r"^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)")


class ExpertPackageError(ValueError):
    """Safe, machine-readable error without download URL or credential data."""

    def __init__(self, message: str, code: str = "EXPERT_PACKAGE_INVALID") -> None:
        super().__init__(message)
        self.code = code
        self.detail = message


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExpertPackageError("Cannot read expert package JSON resource") from exc


def _resource(root: Path, relative: Any) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or ":" in relative or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts):
        raise ExpertPackageError("Expert resource must be a relative package path")
    try:
        target = (root / relative).resolve()
        inside = target.is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExpertPackageError("Cannot resolve expert resource path") from exc
    if not inside:
        raise ExpertPackageError("Expert resource is outside its package")
    return target


def _body(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ExpertPackageError("Missing expert instructions") from exc
    body = _FRONTMATTER.sub("", text, count=1).strip()
    if not body:
        raise ExpertPackageError("Expert instructions must not be empty")
    return body


def _skill_name(directory: Path) -> str:
    try:
        text = _resource(directory, "SKILL.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
        data = yaml.safe_load(match[1]) if match else None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ExpertPackageError("Cannot read expert Skill metadata") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ExpertPackageError("Expert Skill is missing its declared name")
    return name


def _market_bindings(package: Path, definitions: dict[str, dict], required: set[str]) -> dict[str, dict]:
    if not definitions:
        return {}
    path = _resource(package, "dependencies.lock.json")
    if not path.is_file():
        if definitions.keys() & required:
            raise ExpertPackageError("Required market Skill has no package lock identity")
        return {}
    lock = _read_json(path)
    if (not isinstance(lock, dict) or not isinstance(lock.get("skills"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("key"), str) for item in lock["skills"])):
        raise ExpertPackageError("Invalid expert package dependency lock")
    indexed = {item["key"]: item for item in lock["skills"]}
    if len(indexed) != len(lock["skills"]):
        raise ExpertPackageError("Duplicate expert package dependency lock key")
    bindings = {}
    for key, definition in definitions.items():
        record = indexed.get(key)
        skill_id = definition.get("skillId")
        if record is None and key not in required:
            continue
        if (not isinstance(skill_id, str) or not skill_id.strip() or not record
                or record.get("skillId") != skill_id
                or not isinstance(record.get("slug"), str) or not _NAME.fullmatch(record["slug"])):
            raise ExpertPackageError("Market Skill definition and lock identity do not match")
        bindings[key] = {"skillId": skill_id, "slug": record["slug"], "source": "MARKET", "key": key}
    return bindings


def load_expert_profile(package: Path, kind: str, metadata: dict[str, Any], digest: str) -> dict[str, Any]:
    """Parse one local package; selected IDs are distinct from manifest package IDs."""
    if (kind not in {"expert", "team"} or not isinstance(metadata, dict)
            or not isinstance(metadata.get("id"), str) or not _ID.fullmatch(metadata["id"])
            or not isinstance(metadata.get("name"), str) or not metadata["name"].strip()
            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        raise ExpertPackageError("Invalid expert package selection identity")
    package = Path(package).resolve()
    manifest = _read_json(_resource(package, ".box-agent-plugin/plugin.json"))
    expected = "agent" if kind == "expert" else "team"
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != "1.0"
            or manifest.get("expertType") != expected
            or not isinstance(manifest.get("agents"), list)
            or not isinstance(manifest.get("skills"), list)):
        raise ExpertPackageError("Expert package type or manifest is invalid")
    agents = manifest["agents"]
    if not agents or any(not isinstance(agent, dict) or not isinstance(agent.get("id"), str) or not agent["id"]
                         or not isinstance(agent.get("skillBindings", []), list) for agent in agents):
        raise ExpertPackageError("Invalid expert agent definitions")
    if len({agent["id"] for agent in agents}) != len(agents):
        raise ExpertPackageError("Duplicate expert agent ID")
    bindings = [binding for agent in agents for binding in agent.get("skillBindings", [])]
    if any(not isinstance(binding, dict) or not isinstance(binding.get("key"), str) or not binding["key"]
           for binding in bindings):
        raise ExpertPackageError("Invalid expert Skill bindings")
    required = {binding["key"] for binding in bindings if binding.get("required") is not False}
    definitions = {}
    for definition in manifest["skills"]:
        if (not isinstance(definition, dict) or not isinstance(definition.get("key"), str)
                or not definition["key"] or definition["key"] in definitions):
            raise ExpertPackageError("Invalid or duplicate expert Skill key")
        definitions[definition["key"]] = definition
    if required - definitions.keys():
        raise ExpertPackageError("Expert package is missing required Skill definitions")
    selected_keys = {binding["key"] for binding in bindings}

    files_path = _resource(package, "files.json")
    files = _read_json(files_path) if files_path.is_file() else []
    if not isinstance(files, list) or any(not isinstance(record, dict) for record in files):
        raise ExpertPackageError("Invalid expert resource file list")
    missing = [record.get("path") for record in files if not _resource(package, record.get("path")).is_file()]
    skills = []
    for key, definition in definitions.items():
        if key not in selected_keys:
            continue
        if definition.get("source") == "HUB":
            continue
        if definition.get("source") != "BUNDLED":
            raise ExpertPackageError("Unknown expert Skill source")
        relative = definition.get("path")
        directory = _resource(package, relative)
        incomplete = any(path == relative or path.startswith(relative.rstrip("/") + "/") for path in missing)
        if not _resource(directory, "SKILL.md").is_file() or incomplete:
            if key in required:
                raise ExpertPackageError("Missing required expert Skill resources")
            continue
        skills.append({"key": key, "slug": _skill_name(directory), "directory": str(directory)})
    skill_prefixes = [d["path"].rstrip("/") + "/" for d in definitions.values()
                      if d.get("source") == "BUNDLED" and isinstance(d.get("path"), str)]
    if any(not any(path.startswith(prefix) for prefix in skill_prefixes) for path in missing):
        raise ExpertPackageError("Expert package is missing a required resource")
    indexed = {skill["key"]: {"skillId": None, "slug": skill["slug"], "source": "BUNDLED", "key": skill["key"]}
               for skill in skills}
    indexed.update(_market_bindings(package, {key: d for key, d in definitions.items()
                                           if key in selected_keys and d.get("source") == "HUB"}, required))
    profiles = {}
    for agent in agents:
        body = _body(_resource(package, agent.get("file")))
        declared = [indexed[binding["key"]] for binding in agent.get("skillBindings", []) if binding["key"] in indexed]
        tools = agent.get("toolRequests", [])
        if not isinstance(tools, list) or any(not isinstance(tool, str) or not _NAME.fullmatch(tool) for tool in tools):
            raise ExpertPackageError("Invalid expert tool requests")
        profiles[agent["id"]] = {
            "id": agent["id"], "name": agent["id"], "role": "", "instructions": [body],
            "defaultSkills": [binding["slug"] for binding in declared] + tools,
            "skillBindings": declared + [{"slug": tool, "skillId": None, "source": "TOOL"} for tool in tools],
        }
    common = {key: metadata[key] for key in ("id", "name", "description", "starterPrompt", "tags") if key in metadata}
    common.update(source="builtin", revision=f"package:{digest}",
                  packageSnapshot={"digest": digest, "directory": str(package), "manifest": manifest, "skills": skills})
    if expected == "agent":
        entry_agent = manifest.get("entryAgent")
        lead = profiles.get(entry_agent) if isinstance(entry_agent, str) else None
        if lead is None:
            raise ExpertPackageError("Expert package is missing its entry Agent")
        return {**common, **lead, "id": metadata["id"], "name": metadata["name"],
                "description": metadata.get("description", ""), "role": metadata.get("description") or metadata["name"],
                "visibleRules": lead["instructions"], "constraints": []}
    team = manifest.get("team")
    if (not isinstance(team, dict) or not isinstance(team.get("leadAgent"), str)
            or team["leadAgent"] not in profiles or not isinstance(team.get("memberAgents"), list)
            or any(not isinstance(name, str) for name in team["memberAgents"])):
        raise ExpertPackageError("Expert team is missing its leader or members")
    member_ids = [name for name in team["memberAgents"] if name != team["leadAgent"]]
    if any(name not in profiles for name in member_ids) or len(member_ids) != len(set(member_ids)):
        raise ExpertPackageError("Expert team member definitions do not match")
    mode = team.get("executionMode")
    if not isinstance(mode, str) or mode not in {"advisory", "orchestrated", "review_panel"}:
        raise ExpertPackageError("Invalid expert team execution mode")
    workflow = _body(_resource(package, team.get("workflow")))
    return {**common, "leader": profiles[team["leadAgent"]], "members": [profiles[name] for name in member_ids],
            "workflow": [workflow], "visibleRules": [workflow], "reviewRules": [],
            "executionMode": mode, "teamPersona": metadata.get("description", "")}
