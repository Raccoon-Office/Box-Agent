"""Bounded host connector discovery; catalog visibility never grants execution."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any


VERSION = 1
RESULT_BUDGET = 2400
PROMPT = (
    "For videos, short videos or storyboard stories, suggest SEKO Canvas "
    "unless the user chose otherwise. "
    "For SEKO, first use tool_search with only connector=\"seko\". If absent, offer an available route. "
    "Load HyperFrames only on the chosen local route. "
    "Find external connectors via tool_search. Candidates grant no access. For a needed unready connector, "
    "call tool_search with only its exact connector ID, in a standalone call. The setup receipt pauses the task "
    "for the user's choice. Only the user may connect, authorize or select it. Use fresh ready results. "
    "Never set up connectors through other tools."
)
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STATES = frozenset({"disconnected", "configuring", "authorizing", "connecting", "connected",
                     "unauthorized", "error", "cancelling", "disconnecting", "disabled", "unknown"})
_ENTRY_FIELDS = frozenset({"id", "name", "description", "kind", "auth_mode", "skills", "keywords",
                           "configured", "enabled", "status", "reason_code", "execution_ready"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string(value: Any, limit: int, field: str, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(character) < 32 for character in value)):
        raise ValueError(f"Invalid connector discovery {field}")
    return value.strip()


def parse_snapshot(value: Any) -> dict[str, Any]:
    """Validate and copy only the versioned, credential-free host contract."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "catalog_revision", "state_revision", "available", "entries",
    }:
        raise ValueError("Invalid connector discovery snapshot")
    if type(value["schema_version"]) is not int or value["schema_version"] != VERSION:
        raise ValueError("Unsupported connector discovery version")
    revision = value["state_revision"]
    if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
        raise ValueError("Invalid connector discovery state_revision")
    if type(value["available"]) is not bool:
        raise ValueError("Invalid connector discovery availability")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > 256:
        raise ValueError("Invalid connector discovery entries")
    parsed = []
    seen = set()
    for entry in entries:
        required = _ENTRY_FIELDS - {"keywords", "reason_code"}
        if not isinstance(entry, dict) or not required <= entry.keys() or entry.keys() - _ENTRY_FIELDS:
            raise ValueError("Invalid connector discovery entry")
        item = {key: _string(entry.get(key), limit, key, empty=key == "description") for key, limit in (
            ("id", 64), ("name", 128), ("description", 1024), ("auth_mode", 32),
        )}
        if not _ID.fullmatch(item["id"]) or item["id"] in seen:
            raise ValueError("Invalid or duplicate connector discovery id")
        seen.add(item["id"])
        if (entry["kind"] not in ("mcp", "cli") or not isinstance(entry["status"], str)
                or entry["status"] not in _STATES):
            raise ValueError("Invalid connector discovery kind or status")
        item.update(kind=entry["kind"], status=entry["status"])
        for key in ("configured", "enabled", "execution_ready"):
            if type(entry[key]) is not bool:
                raise ValueError(f"Invalid connector discovery {key}")
            item[key] = entry[key]
        for key, limit in (("skills", 128), ("keywords", 64)):
            values = entry.get(key, [])
            if not isinstance(values, list) or len(values) > 16:
                raise ValueError(f"Invalid connector discovery {key}")
            item[key] = [_string(text, limit, key) for text in values]
        if "reason_code" in entry:
            item["reason_code"] = _string(entry["reason_code"], 64, "reason_code")
        parsed.append(item)
    return {"schema_version": VERSION,
            "catalog_revision": _string(value["catalog_revision"], 128, "catalog_revision"),
            "state_revision": revision, "available": value["available"], "entries": parsed}


def resolve_discovery_mode(log, *, requested: bool, new_session: bool, tool_version: int) -> bool:
    """Resolve the pinned mode without changing the log during construction."""
    contracts = list(log.events_of_type("connectors/discovery-contract")) if log else []
    if contracts:
        if any(event["data"] != {"version": VERSION} for event in contracts):
            from box_agent.session_log import SessionLogIncompatible
            raise SessionLogIncompatible("Unsupported connector discovery contract")
        return tool_version == 2
    return new_session and requested and tool_version == 2


def bind_discovery_mode(log, *, requested: bool, new_session: bool, tool_version: int) -> bool:
    """Persist only the mode in Session Log; older readers may ignore this event."""
    enabled = resolve_discovery_mode(log, requested=requested, new_session=new_session, tool_version=tool_version)
    if enabled and log is not None and not any(log.events_of_type("connectors/discovery-contract")):
        log.append("connectors/discovery-contract", {"version": VERSION}, ignorable=True)
        log.flush()
    return enabled


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text: str) -> set[str]:
    terms = set()
    for term in re.findall(r"[a-z0-9]+|[\u3040-\u9fff]+", _normalize(text)[:512]):
        if re.fullmatch(r"[\u3040-\u9fff]+", term):
            terms.update(term[index:index + 2] for index in range(len(term) - 1))
        elif term not in {"the", "a", "an", "and", "or", "to", "of", "in", "for", "with", "my"}:
            terms.add(term)
    return terms


class ConnectorDiscovery:
    """Per-session catalog and receipts, refreshed only from trusted host metadata."""

    def __init__(self, selected: Callable[[], set[str]], *, host_supported: bool = True):
        self._selected = selected
        self.host_supported = host_supported
        self.available = False
        self.state_revision = 0
        self.catalog_revision = ""
        self.entries: dict[str, dict[str, Any]] = {}
        self.skipped_connector_id: str | None = None
        self.ready_only = False
        self._snapshot: dict[str, Any] | None = None
        self._visible: set[str] = set()
        self._issued: set[str] = set()
        self._result_calls: dict[str, set[str]] = {}

    def update(self, value: Any, *, skipped_connector_id: str | None = None,
               ready_only: bool = False) -> None:
        self.skipped_connector_id = skipped_connector_id
        self.ready_only = ready_only or skipped_connector_id is not None
        if value is None or not self.host_supported:
            self.available = False
            return
        try:
            snapshot = parse_snapshot(value)
            if self._snapshot is not None and (
                snapshot["state_revision"] < self.state_revision
                or (snapshot["state_revision"] == self.state_revision and snapshot != self._snapshot)
            ):
                raise ValueError("Stale or conflicting connector discovery revision")
        except ValueError:
            self.available = False
            raise
        self._snapshot = snapshot
        self.available = snapshot["available"]
        self.state_revision = snapshot["state_revision"]
        self.catalog_revision = snapshot["catalog_revision"]
        self.entries = {entry["id"]: entry for entry in snapshot["entries"]}

    def permitted(self, connector_id: str) -> bool:
        entry = self.entries.get(connector_id)
        return bool(self.available and entry and connector_id != self.skipped_connector_id
                    and connector_id in self._selected()
                    and entry["configured"] and entry["enabled"] and entry["execution_ready"]
                    and entry["status"] == "connected")

    def action(self, entry: dict[str, Any], live_mcp_ids: frozenset[str]) -> str | None:
        if (not self.available or entry["status"] == "unknown"
                or entry.get("reason_code", "").upper().startswith("UNSUPPORTED")):
            return "recheck"
        if entry["status"] in {"configuring", "authorizing", "connecting", "cancelling", "disconnecting"}:
            return "wait"
        if entry["status"] == "unauthorized":
            return "reauthorize"
        if not entry["configured"]:
            return "connect"
        if not entry["enabled"] or entry["status"] == "disabled":
            return "enable"
        if entry["status"] != "connected" or not entry["execution_ready"]:
            return "retry"
        if entry["id"] not in self._selected():
            return "select"
        if entry["kind"] == "mcp" and entry["id"] not in live_mcp_ids:
            return "retry"
        return None

    def resolve(self, reference: str) -> dict[str, Any] | None:
        reference = _normalize(reference.strip())
        if reference in self.entries:
            return self.entries[reference]
        matches = [entry for entry in self.entries.values() if reference and reference in _normalize(entry["name"])]
        return matches[0] if len(matches) == 1 else None

    def search(self, queries: list[str], live_mcp_ids: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        terms = set().union(*(_terms(query) for query in queries[:12])) if queries else set()
        ranked = []
        for entry in self.entries.values():
            if self.ready_only and (
                entry["id"] == self.skipped_connector_id or self.action(entry, live_mcp_ids) is not None
            ):
                continue
            fields = ((entry["id"], 8), (entry["name"], 8), (" ".join(entry["keywords"]), 5),
                      (entry["description"], 1))
            score = sum(weight * len(terms & _terms(text)) for text, weight in fields)
            if score:
                ranked.append((score, entry["id"], entry))
        return [entry for _, _, entry in sorted(ranked, key=lambda item: (-item[0], item[1]))[:3]]

    def _description(self, entry: dict[str, Any]) -> dict[str, Any]:
        description = {"id": entry["id"], "name": entry["name"], "kind": entry["kind"],
                       "purpose": entry["description"][:160]}
        # Bound actual JSON, including escaping, while retaining the exact ID/name.
        while len(json.dumps(description, ensure_ascii=False)) > 600:
            description["purpose"] = description["purpose"][:-1]
        return description

    def augment(self, payload: dict, *, queries: list[str], connector: str | None,
                exact_request: bool, live_mcp_ids: frozenset[str]) -> dict[str, Any] | None:
        if not self.host_supported:
            return None
        resolved = self.resolve(connector) if connector else None
        if self.ready_only and connector and (
            resolved is None or resolved["id"] == self.skipped_connector_id
            or self.action(resolved, live_mcp_ids) is not None
        ):
            if not payload.get("activated"):
                payload["notice"] = (
                    "Continue the original task with currently available tools or answer directly. "
                    "Do not request connector setup in this continuation. Explain any remaining limitation."
                )
            return None
        entries = ([resolved] if resolved else []) if connector else self.search(queries, live_mcp_ids)
        activated_ids = {item.get("connector_id") for item in payload.get("activated", [])}
        candidates = []
        ready_skills = {}
        receipt = None
        for entry in entries:
            action = self.action(entry, live_mcp_ids)
            if action is None and entry["id"] in activated_ids:
                continue
            description = self._description(entry)
            signature = _json(description)
            candidate = ({"id": entry["id"]} if signature in self._visible else description)
            candidate.update(status="ready" if action is None else entry["status"] if self.available else "unknown",
                             action=action, state_revision=self.state_revision)
            if action is None and entry["kind"] == "cli":
                ready_skills[entry["id"]] = entry["skills"]
            candidates.append(candidate)
            if "purpose" in candidate:
                self._issued.add(signature)
            if exact_request and action is not None:
                receipt = {"type": "connector_action_required", "version": VERSION,
                           "connector_id": entry["id"], "action": action,
                           "state_revision": self.state_revision, "wait_for_user": True}
        if candidates:
            payload["connector_candidates"] = candidates
            payload["success"] = True
            if receipt:
                payload["notice"] = "The task is waiting for the user's connector choice."
            elif not payload.get("activated") and not self.ready_only:
                payload["notice"] = "Connector candidates are not callable tools. Request setup with only connector=<exact id>."
            # Reserve all candidate summaries before adding optional CLI method names.
            for candidate in candidates:
                if ready_skills.get(candidate["id"]):
                    candidate.update(skills=[], skills_omitted_count=len(ready_skills[candidate["id"]]))
            for candidate in candidates:
                skills = ready_skills.get(candidate["id"], [])
                if not skills:
                    continue
                for skill in skills:
                    candidate["skills"].append(skill)
                    candidate["skills_omitted_count"] -= 1
                    extra = {"connector_candidates": candidates, "notice": payload.get("notice", "")}
                    if len(json.dumps(extra, ensure_ascii=False)) > RESULT_BUDGET:
                        candidate["skills"].pop()
                        candidate["skills_omitted_count"] += 1
                        break
                if not candidate["skills_omitted_count"]:
                    del candidate["skills_omitted_count"]
        return receipt

    def record_result(self, call_id: str, content: str) -> None:
        descriptions = self._descriptions(content) & self._issued
        if descriptions:
            self._result_calls[call_id] = descriptions

    @staticmethod
    def _descriptions(content: str) -> set[str]:
        try:
            rows = json.loads(content).get("connector_candidates", [])
            return {_json({key: row[key] for key in ("id", "name", "kind", "purpose")})
                    for row in rows if isinstance(row, dict) and all(key in row for key in ("id", "name", "kind", "purpose"))}
        except (TypeError, ValueError, AttributeError):
            return set()

    def acknowledge(self, messages: Iterable[Any]) -> None:
        visible = set()
        for message in messages:
            if message.role == "tool" and message.name == "tool_search" and isinstance(message.content, str):
                visible.update(self._descriptions(message.content)
                               & self._result_calls.get(message.tool_call_id, set()))
        self._visible = visible
