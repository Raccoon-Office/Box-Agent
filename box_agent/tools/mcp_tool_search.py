"""Session-scoped local/MCP discovery and deferred tool exposure."""

from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import Tool, ToolResult, build_tool_name_index
from .mcp_tool_catalog import MCPToolCatalog, MCPToolEntry, _normalize

if TYPE_CHECKING:
    from ..schema import Message
    from .engine.contracts import ToolDefinitionView

TOOL_SEARCH_NAME = "tool_search"
_DISCOVERY_ENTRY_BUDGET_CHARS = 48_000
_ACTIVATION_COMPANIONS: dict[str, tuple[str, ...]] = {
    # Playwright navigation returns metadata before the page-body snapshot.
    # Exposing one without the other creates a runtime state the model cannot
    # complete once a workflow requires exact-page evidence.
    "managed_browser_navigate": ("managed_browser_snapshot",),
}


@dataclass(frozen=True, slots=True)
class ActivatedMCPTool:
    tool_id: str
    model_name: str
    generation: int


@dataclass(frozen=True, slots=True)
class ToolExposure:
    tools: list[Tool]
    offered_names: frozenset[str]
    mcp_generations: dict[str, int]


class ToolSearchTool(Tool):
    """Search allowed local/MCP tools and activate usable session hits."""

    reserved_deferred_mcp_search = True

    def __init__(
        self,
        catalog: MCPToolCatalog,
        activated: OrderedDict[str, ActivatedMCPTool],
        *,
        protected_names_provider: Callable[[], frozenset[str]] | None = None,
        allowed_connector_ids_provider: Callable[[], frozenset[str]] | None = None,
        readiness_timeout: float = 15.0,
        local_tools_provider: Callable[[], Iterable[Tool]] | None = None,
        activated_local_tools: OrderedDict[str, Tool] | None = None,
    ) -> None:
        self._stable_manager = None
        self._catalog = catalog
        self._activated = activated
        self._protected_names_provider = protected_names_provider
        self._allowed_connector_ids_provider = allowed_connector_ids_provider
        self._readiness_timeout = readiness_timeout
        self._local_tools_provider = local_tools_provider
        self._activated_local = (
            activated_local_tools if activated_local_tools is not None else OrderedDict()
        )

    def _entry_is_allowed(self, entry) -> bool:
        if entry.connector_id is None:
            return True
        if self._allowed_connector_ids_provider is None:
            return True
        return entry.connector_id in self._allowed_connector_ids_provider()

    @property
    def name(self) -> str:
        return TOOL_SEARCH_NAME

    @property
    def description(self) -> str:
        if self._stable_manager is not None:
            return "Search allowed tools. Returned complete schemas and tool_ref values may be used with call_tool on the next model step."
        return (
            "Search allowed local utilities and the connected deferred MCP catalog "
            "by capability. Local utilities include file append, diagnostics, "
            "plans, progress, goals, memory, scheduling and integrations when available. Every hit "
            "returned by this call is immediately activated for this session and "
            "only those activated hits are added to the next model step; other "
            "deferred catalog tools are not exposed, while alwaysLoad tools remain "
            "visible without search. Use query for one keyword search, queries for "
            "independent bilingual or synonymous searches, or tool_names to activate "
            "only exact catalog IDs, model-facing names, or unique original MCP names. "
            "Passing only server_name activates all tools from that one enabled server. "
            "Provide at least one non-empty query, queries, tool_names, server_name, or "
            "connector input; they may be combined. Prefer short capability, server, "
            "connector, or tool "
            "keywords; task-specific operands are tolerated but should be omitted "
            "when possible. Passing only connector activates all tools owned by "
            "the uniquely matched enabled connector. Set top_k to however many "
            "matching tool schemas the task actually needs, including ten or more "
            "when appropriate. "
            "A query hit may activate a protocol-required companion, such as the "
            "snapshot paired with managed browser navigation, in addition to top_k. "
            "The response reports catalog_tool_count for the applied server scope, "
            "matched_count after query limits, and activated_count after conflict "
            "filtering; these counts remain MCP-only, with local counts reported "
            "separately. server_name selects only MCP tools. Never infer the catalog "
            "total from top_k or matched_count. "
            "This search activates tools but does not execute them."
        )

    @property
    def parameters(self) -> dict:
        # Some providers reject top-level unions. execute() checks that at
        # least one search input is non-empty before accessing the catalog.
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "One capability, action, server, or tool keyword query. Kept "
                        "for compatibility; prefer queries for independent alternatives."
                    ),
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Independent keyword queries merged by each tool's best "
                        "relevance. Use separate Chinese, English, synonym, or exact "
                        "capability phrases. Prefix matches are supported and "
                        "unmatched task operands are tolerated."
                    ),
                },
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Exact tool IDs, model-facing names, or unique original MCP names "
                        "to activate, such as mcp:server/tool, server/tool, or an "
                        "MCP tools/list name. No fuzzy fallback is used, and unlisted "
                        "catalog tools remain hidden."
                    ),
                },
                "server_name": {
                    "type": "string",
                    "description": (
                        "Exact internal MCP server name. On its own, activates all "
                        "tools from that enabled server; with query, connector, or "
                        "tool_names, it narrows the normal discovery scope."
                    ),
                },
                "connector": {
                    "type": "string",
                    "description": (
                        "Connector ID or unique display-name text from "
                        "<connector-status>, such as pkulaw or 北大法宝. On its "
                        "own, activates all tools owned by that enabled connector; "
                        "with query, server_name, or tool_names, it narrows the "
                        "normal discovery scope."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": (
                        "Exact maximum number of query matches to return and activate; "
                        "ignored for tool_names and server-only activation. Choose any positive count required by "
                        "the task; there is no small fixed cap. Protocol-required "
                        "companions may be activated in addition to these query matches. "
                        "Other unreturned catalog tools remain hidden."
                    ),
                },
            },
            "additionalProperties": False,
        }

    async def execute(
        self,
        query: str | None = None,
        queries: list[str] | None = None,
        tool_names: list[str] | None = None,
        server_name: str | None = None,
        connector: str | None = None,
        top_k: int = 1,
    ) -> ToolResult:
        normalized_server_name = (
            server_name.strip()
            if isinstance(server_name, str) and server_name.strip()
            else None
        )
        normalized_queries = [
            item
            for item in ([query] if query else []) + (queries or [])
            if isinstance(item, str) and item.strip()
        ]
        normalized_tool_names = [
            item
            for item in tool_names or []
            if isinstance(item, str) and item.strip()
        ]
        normalized_connector = (
            connector.strip()
            if isinstance(connector, str) and connector.strip()
            else None
        )
        search_input = {
            "query": query,
            "queries": normalized_queries,
            "tool_names": normalized_tool_names,
            "server_name": normalized_server_name,
            "connector": normalized_connector,
        }
        connector_direct = bool(
            normalized_connector
            and not normalized_server_name
            and not normalized_queries
            and not normalized_tool_names
        )
        server_direct = bool(
            normalized_server_name and not normalized_queries and not normalized_tool_names
        )
        if (
            not normalized_queries
            and not normalized_tool_names
            and not server_direct
            and not connector_direct
        ):
            payload = {
                "success": False,
                **search_input,
                "catalog_tool_count": None,
                "matched_count": 0,
                "companion_count": 0,
                "activated_count": 0,
                "activated": [],
                "conflicts": [],
                "missing": [],
                "notice": "Provide query, queries, exact tool_names, server_name, or connector.",
            }
            return ToolResult(
                success=False,
                content=json.dumps(payload, ensure_ascii=False),
                error="Tool search input is empty.",
            )

        local_tools = {
            tool.name: tool
            for tool in (
                self._local_tools_provider() if self._local_tools_provider is not None else ()
            )
            if getattr(tool, "mcp_tool_id", None) is None and tool.name != TOOL_SEARCH_NAME
            and (self._stable_manager is None or self._stable_manager.is_discoverable(tool))
        }
        local_index = {
            _normalize(name): tool
            for name, tool in build_tool_name_index(local_tools.values()).items()
        }
        # A partial MCP source must never hold up a local query. Legacy MCP-only
        # callers keep the existing readiness wait, including explicit server queries.
        mcp_ready = (
            not self._catalog.loading
            if (
                self._local_tools_provider is not None
                and normalized_server_name is None
                and normalized_connector is None
            )
            else await self._catalog.wait_until_ready(self._readiness_timeout)
        )

        resolved_connector_id = (
            self._catalog.resolve_connector_id(
                normalized_connector,
                entry_filter=self._entry_is_allowed,
            )
            if normalized_connector
            else None
        )

        def entry_is_in_scope(entry) -> bool:
            if not self._entry_is_allowed(entry):
                return False
            if self._stable_manager is not None and not self._stable_manager.is_discoverable(entry.tool):
                return False
            if normalized_connector and (
                resolved_connector_id is None
                or entry.connector_id != resolved_connector_id
            ):
                return False
            return normalized_server_name is None or entry.server_name == normalized_server_name

        search_input["resolved_connector_id"] = resolved_connector_id
        scoped_locals = (
            local_tools if normalized_server_name is None and normalized_connector is None else {}
        )
        local_entries = {
            name: MCPToolEntry(
                tool_id=f"local:{name}", model_name=name, server_name="",
                description=tool.description, tool=tool, generation=0, always_load=False,
            )
            for name, tool in scoped_locals.items()
        }
        mcp_entries = tuple(
            entry for entry in self._catalog.snapshot()
            if mcp_ready and entry_is_in_scope(entry)
        )
        catalog_tool_count = len(mcp_entries) if mcp_ready else None
        missing: list[str] = []
        pending: list[str] = []
        hits: list[MCPToolEntry] = []
        if connector_direct or server_direct:
            hits = list(mcp_entries)
        elif normalized_tool_names:
            seen_ids = set()
            for requested_name in normalized_tool_names:
                local = local_index.get(_normalize(requested_name))
                matches = (
                    [local_entries[local.name]]
                    if local is not None and local.name in local_entries else []
                )
                if mcp_ready:
                    matches.extend(self._catalog.lookup_exact(
                        [requested_name], server_name=normalized_server_name,
                        entry_filter=entry_is_in_scope,
                    )[0])
                if not matches:
                    (missing if mcp_ready else pending).append(requested_name)
                for entry in matches:
                    if entry.tool_id not in seen_ids:
                        hits.append(entry)
                        seen_ids.add(entry.tool_id)
        else:
            hits = self._catalog.search_many(
                normalized_queries, server_name=normalized_server_name, top_k=top_k,
                entries=(*mcp_entries, *local_entries.values()),
                entry_filter=entry_is_in_scope,
            )
        query_matched_count = sum(not entry.tool_id.startswith("local:") for entry in hits)
        local_matched_count = len(hits) - query_matched_count
        hit_ids = {entry.tool_id for entry in hits}
        companion_entries = []
        if not normalized_tool_names and not server_direct and not connector_direct:
            for entry in tuple(hits):
                if entry.tool_id.startswith("local:"):
                    continue
                for companion_name in _ACTIVATION_COMPANIONS.get(entry.model_name, ()):
                    companions, _ = self._catalog.lookup_exact(
                        [companion_name],
                        server_name=entry.server_name,
                        entry_filter=entry_is_in_scope,
                    )
                    for companion in companions:
                        if companion.tool_id in hit_ids:
                            continue
                        hit_ids.add(companion.tool_id)
                        hits.append(companion)
                        companion_entries.append(companion)
        activated_results = []
        conflicts = []
        protected_names = frozenset(build_tool_name_index(local_tools.values())) | (
            self._protected_names_provider()
            if self._protected_names_provider is not None
            else frozenset()
        )
        local_activated_count = 0
        omitted_count = 0
        oversized_count = 0
        entry_chars = 0
        for entry in hits:
            local = entry.tool_id.startswith("local:")
            if not local and (entry.name_conflict or entry.model_name == TOOL_SEARCH_NAME
                              or entry.model_name in protected_names):
                conflicts.append({
                    "tool_id": entry.tool_id, "name": entry.model_name,
                    "server_name": entry.server_name,
                    "error": ("reserved deferred-search tool name" if entry.model_name == TOOL_SEARCH_NAME
                              else "conflicts with stable core tool" if entry.model_name in protected_names
                              else "duplicate model-facing tool name"),
                })
                continue
            if local:
                already_active = self._activated_local.get(entry.model_name) is entry.tool
                item = {"name": entry.model_name, "source": "local",
                        "description": entry.description, "already_active": already_active}
            else:
                previous = self._activated.get(entry.tool_id)
                already_active = previous is not None and previous.generation == entry.generation
                item = {"name": entry.model_name, "server_name": entry.server_name,
                        "connector_id": entry.connector_id, "connector_name": entry.connector_name,
                        "remote_name": entry.remote_name, "description": entry.description,
                        "already_active": already_active}
            if self._stable_manager is not None:
                placeholder = "tool:v1:" + "x" * 32
                preview = {**item, "tool_ref": placeholder, "input_schema": entry.tool.parameters,
                           "call_example": {"tool_ref": placeholder, "arguments_json": "{}"}}
                size = len(json.dumps(preview, ensure_ascii=False)) + 2
                if size > _DISCOVERY_ENTRY_BUDGET_CHARS:
                    oversized_count += 1
                    continue
                if entry_chars + size > _DISCOVERY_ENTRY_BUDGET_CHARS:
                    omitted_count += 1
                    continue
                entry_chars += size
                item.update(self._stable_manager.describe(entry.tool, entry.generation))
                self._stable_manager._remember_search_descriptor(item)
            if local:
                self._activated_local[entry.model_name] = entry.tool
                local_activated_count += 1
            else:
                self._activated[entry.tool_id] = ActivatedMCPTool(
                    tool_id=entry.tool_id, model_name=entry.model_name, generation=entry.generation,
                )
            activated_results.append(item)
        payload = {
            "success": not conflicts or bool(activated_results),
            **search_input,
            "catalog_tool_count": catalog_tool_count,
            "matched_count": query_matched_count,
            "companion_count": len(companion_entries),
            "activated_count": len(activated_results) - local_activated_count,
            "activated": activated_results,
            "conflicts": conflicts,
            "missing": missing,
            "notice": (
                f"Activated {len(activated_results)} returned or required companion "
                "tool(s) for this session. Only these hits, their protocol-required "
                "companions, and explicit eager tools are callable by their real name "
                "on the next step; all other catalog tools remain hidden."
                if activated_results
                else (
                    "Matching tools have conflicting model-facing names."
                    if conflicts
                    else (
                        "No exact MCP tools found for the requested tool_names."
                        if normalized_tool_names
                        else "No matching MCP tools found."
                    )
                )
            ),
        }
        if self._stable_manager is not None:
            payload["omitted_for_size_count"] = omitted_count
            payload["oversized_schema_count"] = oversized_count
            if oversized_count or omitted_count:
                payload["notice"] = (
                    "Some complete tool schemas were omitted due to the size limit. "
                    "Search exact tool_names or use a smaller top_k; an individually oversized schema cannot be delivered."
                )
            elif activated_results:
                payload["notice"] = "Use the returned tool_ref with call_tool on the next model step."
        if self._local_tools_provider is not None:
            payload.update(
                local_tool_count=len(scoped_locals),
                local_matched_count=local_matched_count,
                local_activated_count=local_activated_count,
                mcp_state="ready" if mcp_ready else "loading",
            )
        if self._stable_manager is not None:
            # Echoed query and diagnostic metadata must not defeat the result budget.
            for key in ("query", "server_name", "connector", "resolved_connector_id"):
                if isinstance(payload.get(key), str):
                    payload[key] = payload[key][:256]
            for key in ("queries", "tool_names", "missing", "conflicts"):
                values = payload.get(key)
                if not isinstance(values, list):
                    continue
                payload[f"{key}_total"] = len(values)
                payload[key] = [
                    {k: v[:256] if isinstance(v, str) else v for k, v in item.items()}
                    if isinstance(item, dict) else item[:256] if isinstance(item, str) else item
                    for item in values[:12]
                ]
        if not mcp_ready:
            payload.update(
                success=bool(activated_results),
                state="partial_catalog_loading" if activated_results else "catalog_loading",
                pending_tool_names=([name[:256] for name in pending[:12]] if self._stable_manager is not None else pending),
                notice=(
                    "Local discovery completed; the MCP catalog is still loading. "
                    "Retry MCP searches after the runtime readiness update."
                ),
            )
            if not activated_results:
                return ToolResult(
                    success=False, content=json.dumps(payload, ensure_ascii=False),
                    error="MCP catalog is still loading; retry tool_search shortly.",
                )
        conflict_error = None
        if conflicts and not activated_results:
            conflict_error = (
                "MCP tool name conflict; adjust MCP configuration before activation."
            )
        return ToolResult(
            success=conflict_error is None,
            content=json.dumps(payload, ensure_ascii=False),
            error=conflict_error,
        )


class MCPToolExposureManager:
    """Build one step's visible tool set from a session activation store."""

    def __init__(
        self,
        catalog: MCPToolCatalog,
        activated: OrderedDict[str, ActivatedMCPTool],
        *,
        activated_local_tools: OrderedDict[str, Tool] | None = None,
        deferred_local_names_provider: Callable[[], frozenset[str]] | None = None,
        deferred_mcp: bool = True,
        allowed_connector_ids_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._catalog = catalog
        self._activated = activated
        self._activated_local = (
            activated_local_tools if activated_local_tools is not None else OrderedDict()
        )
        self._deferred_local_names_provider = deferred_local_names_provider
        self._deferred_mcp = deferred_mcp
        self._allowed_connector_ids_provider = allowed_connector_ids_provider
        self.stable_prefix = False
        self._stable_direct: dict[str, tuple[Tool, ToolDefinitionView]] | None = None
        self._stable_refs: dict[str, tuple[Tool, ToolDefinitionView, int | None]] = {}
        self._search_refs: set[str] = set()
        self._pending_search_descriptors: dict[str, set[str]] = {}
        self._search_result_calls: dict[str, set[tuple[str, str]]] = {}
        self._stable_candidates: dict[str, Tool] = {}
        self._live_candidates: Callable[[], Mapping[str, Tool]] = lambda: self._stable_candidates
        self._stable_visibility: Callable[[str], bool] | None = None
        self._bootstrap_acked: set[str] = set()
        self._bootstrap_issued: set[str] = set()
        self._scope_parent: MCPToolExposureManager | None = None
        self._scope_upstreams: dict[str, Tool] = {}
        self._direct_generations: dict[str, int] = {}

    def for_inherited_tools(self, child_tools: dict[str, Tool]) -> MCPToolExposureManager:
        """Freeze a child's resolved subset; borrow only parent validation authority."""
        from .engine.contracts import ToolDefinitionView

        allowed = self.inherited_tools(dict(self._live_candidates()))
        scoped = MCPToolExposureManager(
            self._catalog, OrderedDict(), deferred_mcp=self._deferred_mcp,
            allowed_connector_ids_provider=self._allowed_connector_ids_provider,
        )
        scoped.stable_prefix = True
        scoped._scope_parent = self
        scoped._stable_direct = {}
        for name, tool in child_tools.items():
            upstream = allowed.get(name)
            if upstream is None or name in {TOOL_SEARCH_NAME, "call_tool"}:
                continue
            scoped._scope_upstreams[name] = upstream
            scoped._stable_direct[name] = (tool, ToolDefinitionView.from_tool(tool))
            if getattr(upstream, "mcp_tool_id", None) is not None:
                entry = self._catalog.get_by_model_name(name)
                scoped._direct_generations[name] = entry.generation if entry is not None and entry.tool is upstream else -1
        return scoped

    def enable_stable_prefix(self, search_tool: ToolSearchTool) -> None:
        self.stable_prefix = True
        search_tool._stable_manager = self
        # Do not persist only a truncated description while issuing its reference.
        search_tool.max_result_size_chars = float("inf")
        search_tool._local_tools_provider = lambda: tuple(self._stable_candidates.values())

    def is_discoverable(self, tool: Tool) -> bool:
        return (tool.name not in {"tool_search", "call_tool"}
                and tool.name not in (self._stable_direct or {})
                and (self._stable_visibility is None or self._stable_visibility(tool.name)))

    def describe(self, tool: Tool, generation: int | None) -> dict:
        from .engine.contracts import ToolDefinitionView
        definition = ToolDefinitionView.from_tool(tool)
        # Reuse a ref only while both the object and definition remain current.
        for ref, (old_tool, old_definition, old_generation) in self._stable_refs.items():
            if old_tool is tool and old_generation == generation and old_definition.description == tool.description and old_definition.matches_target(tool):
                break
        else:
            ref = "tool:v1:" + secrets.token_urlsafe(24)
            self._stable_refs[ref] = (tool, definition, generation)
        return {"tool_ref": ref, "input_schema": definition.parameters,
                "call_example": {"tool_ref": ref, "arguments_json": "{}"}}

    @staticmethod
    def _canonical_descriptor(item: dict) -> str:
        return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _remember_search_descriptor(self, item: dict) -> None:
        self._pending_search_descriptors.setdefault(item["tool_ref"], set()).add(
            self._canonical_descriptor(item)
        )

    def record_search_result(self, call_id: str, content: str) -> None:
        """Bind descriptors to a tool-search call committed by the engine."""
        if not call_id:
            return
        matches = self._matching_search_descriptors(content)
        if matches:
            self._search_result_calls[call_id] = matches

    def _matching_search_descriptors(self, content: str) -> set[tuple[str, str]]:
        def unique_pairs(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError("duplicate key")
                output[key] = value
            return output
        try:
            payload = json.loads(content, object_pairs_hook=unique_pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
            items = payload.get("activated") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return set()
            matched = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("tool_ref"), str):
                    continue
                ref = item["tool_ref"]
                canonical = self._canonical_descriptor(item)
                if canonical in self._pending_search_descriptors.get(ref, ()):
                    matched.add((ref, canonical))
            return matched
        except (TypeError, ValueError, RecursionError):
            return set()

    def acknowledge_search_results(self, messages: Iterable[Message]) -> None:
        """Acknowledge complete issued descriptions in final projected tool messages.

        Only call IDs recorded when an actual search result was committed qualify.
        Already-prepared request snapshots are immutable and gain no new refs.
        """
        for message in messages:
            if (getattr(message, "role", None) != "tool"
                    or getattr(message, "name", None) != TOOL_SEARCH_NAME
                    or not isinstance(getattr(message, "content", None), str)):
                continue
            issued = self._search_result_calls.get(getattr(message, "tool_call_id", None), set())
            for ref, _ in self._matching_search_descriptors(message.content) & issued:
                self._search_refs.add(ref)

    def is_current_target(self, name: str, tool: Tool) -> bool:
        if self._scope_parent is not None:
            upstream = self._scope_upstreams.get(name)
            return (self._live_candidates().get(name) is tool and upstream is not None
                    and self._scope_parent.is_current_target(name, upstream))
        if getattr(tool, "mcp_tool_id", None) is not None:
            current = self._catalog.get_by_model_name(name)
            return current is not None and current.tool is tool and self._entry_is_allowed(current)
        return self._live_candidates().get(name) is tool

    def pending_bootstrap_descriptions(self, visible_history: Iterable[Message] | None = None) -> tuple[str, ...]:
        from .local_tool_exposure import DISCOVERABLE_LOCAL_NAMES

        visible = (self._bootstrap_acked if visible_history is None else {
            message.content for message in visible_history
            if getattr(message, "role", None) == "user"
            and getattr(message, "source", None) == "runtime"
            and getattr(message, "name", None) == "tool_descriptions"
            and isinstance(getattr(message, "content", None), str)
        })
        if visible_history is not None:
            self._bootstrap_acked.intersection_update(visible)
        pending = []
        pending_size = 0
        omitted = 0
        def queue(text: str) -> None:
            nonlocal pending_size, omitted
            if text in visible:
                return
            if pending_size + len(text) > _DISCOVERY_ENTRY_BUDGET_CHARS - 1024:
                omitted += 1
                return
            pending.append(text)
            pending_size += len(text)

        def render(name: str, description: str, tool: Tool, generation: int | None) -> str:
            placeholder = "tool:v1:" + "x" * 32
            preview = {"name": name, "description": description, "tool_ref": placeholder,
                       "input_schema": tool.parameters,
                       "call_example": {"tool_ref": placeholder, "arguments_json": "{}"}}
            if len(json.dumps(preview, ensure_ascii=False)) > _DISCOVERY_ENTRY_BUDGET_CHARS:
                return json.dumps({"name": name[:256], "error": "Tool schema exceeds the discovery size limit and cannot be delivered."}, ensure_ascii=False)
            return json.dumps({"name": name, "description": description,
                               **self.describe(tool, generation)}, ensure_ascii=False)
        if self._scope_parent is not None:
            return ()
        if self._deferred_local_names_provider is not None:
            deferred = self._deferred_local_names_provider()
            for name, tool in self._stable_candidates.items():
                if (name not in DISCOVERABLE_LOCAL_NAMES or name in deferred
                        or getattr(tool, "mcp_tool_id", None) is not None
                        or not self.is_discoverable(tool) or not self.is_current_target(name, tool)):
                    continue
                text = render(name, tool.description, tool, 0)
                queue(text)
        for entry in self._catalog.snapshot():
            if not entry.always_load or entry.name_conflict or not self._entry_is_allowed(entry) or not self.is_discoverable(entry.tool):
                continue
            text = render(entry.model_name, entry.description, entry.tool, entry.generation)
            queue(text)
        if omitted:
            notice = json.dumps({"error": "Some tool descriptions were omitted due to the bootstrap size limit. Search exact tool_names to obtain individual schemas.", "omitted_count": omitted}, ensure_ascii=False)
            if notice not in visible:
                pending.append(notice)
        self._bootstrap_issued.update(pending)
        return tuple(pending)

    def ack_bootstrap_descriptions(self, descriptions: Iterable[str]) -> None:
        self._bootstrap_acked.update(text for text in descriptions if text in self._bootstrap_issued)

    def restore_compacted_descriptions(self, messages: list[Message]) -> list[Message]:
        """Replay acknowledged, still-valid discoveries without issuing new refs."""
        from ..schema import Message

        visible: set[str] = set()
        for message in messages:
            if not isinstance(message.content, str):
                continue
            if message.role == "tool" and message.name == TOOL_SEARCH_NAME:
                issued = self._search_result_calls.get(message.tool_call_id, set())
                visible.update(text for _, text in self._matching_search_descriptors(message.content) & issued)
            elif message.role == "user" and message.source == "runtime" and message.name == "tool_descriptions":
                visible.add(message.content)
        restored = list(messages)
        for ref, (tool, definition, generation) in self._stable_refs.items():
            mcp_generation = generation if getattr(tool, "mcp_tool_id", None) is not None else None
            if (ref not in self._search_refs or not self.is_current_target(tool.name, tool)
                    or not self.is_discoverable(tool) or not definition.matches_target(tool)
                    or definition.description != tool.description
                    or self.validate_call(tool.name, mcp_generation, tool) is not None):
                continue
            descriptions = self._pending_search_descriptors.get(ref, ())
            if any(text in visible for text in descriptions):
                continue
            # Search may have returned the same ref more than once. Deliver
            # one complete, deterministic descriptor, not all search receipts.
            if descriptions:
                restored.append(Message(role="user", source="runtime", name="tool_descriptions",
                                        content=min(descriptions)))
        return restored

    def _entry_is_allowed(self, entry) -> bool:
        return self._connector_is_allowed(entry.connector_id)

    def _connector_is_allowed(self, connector_id: str | None) -> bool:
        if connector_id is None:
            return True
        if self._allowed_connector_ids_provider is None:
            return True
        return connector_id in self._allowed_connector_ids_provider()

    def activate_server(self, server_name: str) -> int:
        """Activate every currently discovered tool owned by one MCP server.

        This is a host-facing capability for opt-in integrations that own a
        whole MCP server (for example a CUA plugin). It changes only the
        session activation map; normal name/conflict/permission checks still
        run when the next tool exposure is prepared.
        """
        activated = 0
        for entry in self._catalog.snapshot():
            if (
                entry.server_name != server_name
                or entry.name_conflict
                or entry.model_name == TOOL_SEARCH_NAME
                or not self._entry_is_allowed(entry)
            ):
                continue
            previous = self._activated.get(entry.tool_id)
            if previous is not None and previous.generation == entry.generation:
                continue
            self._activated[entry.tool_id] = ActivatedMCPTool(
                tool_id=entry.tool_id,
                model_name=entry.model_name,
                generation=entry.generation,
            )
            activated += 1
        return activated

    def prepare_tools(self, candidates: list[Tool]) -> ToolExposure:
        if self._scope_parent is not None:
            available = {tool.name: tool for tool in candidates}
            scoped = [tool for name, (tool, _) in self._stable_direct.items()
                      if available.get(name) is tool and self.is_current_target(name, tool)]
            return ToolExposure(scoped, frozenset(tool.name for tool in scoped),
                                {name: generation for name, generation in self._direct_generations.items()
                                 if name in {tool.name for tool in scoped}})
        # ``candidates`` is the session's stable core-tool registry. Ordinary
        # MCP tools live only in the process catalog and are appended here
        # after an explicit activation (or alwaysLoad). Keeping the two stores
        # separate makes it impossible for a loaded MCP schema to leak into a
        # provider request merely because it was connected successfully.
        visible: OrderedDict[str, Tool] = OrderedDict()
        generations: dict[str, int] = {}
        local_tools = {
            tool.name: tool for tool in candidates
            if getattr(tool, "mcp_tool_id", None) is None
        }
        protected_names = frozenset(build_tool_name_index(local_tools.values()))
        deferred_names = (
            self._deferred_local_names_provider()
            if self._deferred_local_names_provider is not None else frozenset()
        )
        for name, target in tuple(self._activated_local.items()):
            if local_tools.get(name) is not target:
                self._activated_local.pop(name, None)
        for tool in candidates:
            if getattr(tool, "mcp_tool_id", None) is not None:
                if not self._deferred_mcp and self._connector_is_allowed(
                    getattr(tool, "mcp_connector_id", None)
                ):
                    visible[tool.name] = tool
                    generation = getattr(tool, "mcp_generation", None)
                    if isinstance(generation, int):
                        generations[tool.name] = generation
            elif tool.name not in deferred_names or self._activated_local.get(tool.name) is tool:
                visible[tool.name] = tool

        for entry in self._catalog.snapshot() if self._deferred_mcp else ():
            if not self._entry_is_allowed(entry):
                continue
            if (
                entry.name_conflict
                or entry.model_name == TOOL_SEARCH_NAME
                or entry.model_name in protected_names
            ):
                continue
            activation = self._activated.get(entry.tool_id)
            activated = activation is not None and activation.generation == entry.generation
            if not entry.always_load and not activated:
                continue
            visible[entry.model_name] = entry.tool
            generations[entry.model_name] = entry.generation
        tools = list(visible.values())
        return ToolExposure(tools, frozenset(visible), generations)

    def inherited_tools(self, tool_map: dict[str, Tool]) -> dict[str, Tool]:
        """Retain allowed local capabilities and only activated/eager remote tools."""
        exposure = self.prepare_tools(list(tool_map.values()))
        if self._scope_parent is not None:
            return {tool.name: tool for tool in exposure.tools}
        return {
            tool.name: tool
            for tool in [*tool_map.values(), *exposure.tools]
            if tool.name not in {TOOL_SEARCH_NAME, "call_tool"} and (
                getattr(tool, "mcp_tool_id", None) is None
                or tool.name in exposure.offered_names
            )
        }

    def validate_call(
        self,
        name: str,
        offered_generation: int | None,
        target_tool: Tool | None = None,
    ) -> str | None:
        if self._scope_parent is not None:
            upstream = self._scope_upstreams.get(name)
            if upstream is None or not self._scope_parent.is_current_target(name, upstream):
                return f"Tool '{name}' is no longer available to this child."
            return self._scope_parent.validate_call(name, offered_generation, upstream)
        if target_tool is not None and not self._connector_is_allowed(
            getattr(target_tool, "mcp_connector_id", None)
        ):
            return f"MCP tool '{name}' is not enabled for this conversation; search again."
        if offered_generation is None:
            return None
        current = self._catalog.get_by_model_name(name)
        if current is not None and not self._entry_is_allowed(current):
            return f"MCP tool '{name}' is not enabled for this conversation; search again."
        if not self._deferred_mcp:
            if (
                target_tool is not None
                and getattr(target_tool, "mcp_generation", None) != offered_generation
            ):
                return f"MCP tool '{name}' execution target changed after it was offered; prepare tools again."
            return None
        if current is None:
            return f"MCP tool '{name}' is unavailable or has a name conflict; search again."
        if current.generation != offered_generation:
            return f"MCP tool '{name}' changed after it was offered; search again."
        if (
            target_tool is not None
            and getattr(target_tool, "mcp_generation", None) != offered_generation
        ):
            return f"MCP tool '{name}' execution target changed after it was offered; search again."
        return None
