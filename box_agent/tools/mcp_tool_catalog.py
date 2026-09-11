"""Process-wide catalog of tools discovered from connected MCP servers.

The catalog owns discovery metadata only.  Which catalog entries are visible to
an LLM is decided per Agent session by :mod:`mcp_tool_search`.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import RLock

from .base import Tool


def _normalize(value: str) -> str:
    value = value.strip().lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value)


def _tokenize(value: str) -> tuple[str, ...]:
    """Split searchable metadata into lowercase word-like terms."""
    return tuple(re.findall(r"[^\W_]+", _normalize(value), flags=re.UNICODE))


def _prefix_term_frequency(term: str, field_terms: tuple[str, ...]) -> int:
    return sum(candidate.startswith(term) for candidate in field_terms)


def _bm25_term_score(
    *,
    term_frequency: int,
    document_frequency: int,
    document_count: int,
    field_length: int,
    average_field_length: float,
) -> float:
    """Return a small dependency-free BM25 relevance component."""
    if term_frequency <= 0 or document_frequency <= 0 or document_count <= 0:
        return 0.0
    k1 = 1.2
    b = 0.7
    inverse_document_frequency = math.log(
        1 + (document_count - document_frequency + 0.5)
        / (document_frequency + 0.5)
    )
    normalized_length = (
        field_length / average_field_length if average_field_length > 0 else 1.0
    )
    return inverse_document_frequency * (
        term_frequency * (k1 + 1)
        / (term_frequency + k1 * (1 - b + b * normalized_length))
    )


@dataclass(frozen=True, slots=True)
class MCPToolEntry:
    tool_id: str
    model_name: str
    server_name: str
    description: str
    tool: Tool
    generation: int
    always_load: bool
    connector_id: str | None = None
    connector_name: str | None = None
    remote_name: str | None = None
    name_conflict: bool = False


class MCPToolCatalog:
    """Thread-safe current snapshot of connected MCP tools."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, MCPToolEntry] = {}
        self._server_generations: dict[str, int] = {}
        self._loading = False
        self._refreshing_servers: set[str] = set()
        self._ready_event: asyncio.Event | None = None

    def _has_pending_discovery(self) -> bool:
        return self._loading or bool(self._refreshing_servers)

    def _ensure_pending_event(self) -> None:
        if self._ready_event is None or self._ready_event.is_set():
            self._ready_event = asyncio.Event()

    def mark_loading(self) -> None:
        """Mark initial discovery as incomplete without resetting live waiters."""
        with self._lock:
            if self._loading:
                return
            was_pending = self._has_pending_discovery()
            self._loading = True
            if not was_pending:
                self._ensure_pending_event()

    def mark_ready(self) -> None:
        """Publish discovery completion and release pending catalog searches."""
        with self._lock:
            self._loading = False
            ready_event = (
                self._ready_event if not self._has_pending_discovery() else None
            )
        if ready_event is not None:
            ready_event.set()

    def mark_server_loading(self, server_name: str) -> None:
        """Mark one hot-reloading server unavailable for catalog searches."""
        with self._lock:
            if server_name in self._refreshing_servers:
                return
            was_pending = self._has_pending_discovery()
            self._refreshing_servers.add(server_name)
            if not was_pending:
                self._ensure_pending_event()

    def mark_server_ready(self, server_name: str) -> None:
        """Finish one hot-reload and release waiters when discovery is stable."""
        with self._lock:
            self._refreshing_servers.discard(server_name)
            ready_event = (
                self._ready_event if not self._has_pending_discovery() else None
            )
        if ready_event is not None:
            ready_event.set()

    @property
    def initial_loading(self) -> bool:
        """Distinguish cold discovery from independent server refreshes."""
        with self._lock:
            return self._loading

    @property
    def loading(self) -> bool:
        with self._lock:
            return self._has_pending_discovery()

    async def wait_until_ready(self, timeout: float) -> bool:
        """Wait for active discovery, returning False on a bounded timeout."""
        with self._lock:
            if not self._has_pending_discovery():
                return True
            ready_event = self._ready_event
        if ready_event is None:
            return True
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    def replace_server(self, server_name: str, tools: Iterable[Tool]) -> int:
        """Replace one server snapshot and return its new generation."""
        with self._lock:
            generation = self._server_generations.get(server_name, 0) + 1
            self._server_generations[server_name] = generation
            self._entries = {
                tool_id: entry
                for tool_id, entry in self._entries.items()
                if entry.server_name != server_name
            }
            for tool in tools:
                raw_name = tool.name
                tool_id = f"mcp:{server_name}/{raw_name}"
                # MCPTool exposes these attributes.  Keeping the catalog
                # tolerant of Tool doubles makes focused tests inexpensive.
                tool._mcp_generation = generation
                self._entries[tool_id] = MCPToolEntry(
                    tool_id=tool_id,
                    model_name=raw_name,
                    server_name=server_name,
                    description=tool.description,
                    tool=tool,
                    generation=generation,
                    always_load=bool(getattr(tool, "mcp_always_load", False)),
                    connector_id=getattr(tool, "mcp_connector_id", None),
                    connector_name=getattr(tool, "mcp_connector_name", None),
                    remote_name=getattr(tool, "remote_name", raw_name),
                )
            self._rebuild_conflicts()
            return generation

    def remove_server(self, server_name: str) -> None:
        with self._lock:
            self._entries = {
                tool_id: entry
                for tool_id, entry in self._entries.items()
                if entry.server_name != server_name
            }
            self._rebuild_conflicts()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._server_generations.clear()
            self._loading = False
            self._refreshing_servers.clear()
            ready_event = self._ready_event
            self._ready_event = None
        if ready_event is not None:
            ready_event.set()

    def snapshot(self) -> tuple[MCPToolEntry, ...]:
        with self._lock:
            return tuple(sorted(self._entries.values(), key=lambda entry: entry.tool_id))

    def get(self, tool_id: str) -> MCPToolEntry | None:
        with self._lock:
            return self._entries.get(tool_id)

    def get_by_model_name(self, model_name: str) -> MCPToolEntry | None:
        matches = [entry for entry in self.snapshot() if entry.model_name == model_name]
        if len(matches) != 1 or matches[0].name_conflict:
            return None
        return matches[0]

    def resolve_connector_id(
        self,
        reference: str,
        *,
        entry_filter: Callable[[MCPToolEntry], bool] | None = None,
    ) -> str | None:
        """Resolve a connector ID or unique display-name fragment."""
        normalized_reference = _normalize(reference)
        if not normalized_reference:
            return None
        identities: dict[str, set[str]] = {}
        for entry in self.snapshot():
            if not entry.connector_id or not self._visible(entry, entry_filter):
                continue
            identities.setdefault(entry.connector_id, set()).add(entry.connector_name or "")

        exact_ids = {
            connector_id
            for connector_id in identities
            if _normalize(connector_id) == normalized_reference
        }
        if len(exact_ids) == 1:
            return next(iter(exact_ids))

        matching_ids = {
            connector_id
            for connector_id, names in identities.items()
            if any(
                normalized_reference
                in _normalize(f"{connector_id} {connector_name}")
                for connector_name in names
            )
        }
        return next(iter(matching_ids)) if len(matching_ids) == 1 else None

    @staticmethod
    def _visible(
        entry: MCPToolEntry,
        entry_filter: Callable[[MCPToolEntry], bool] | None,
    ) -> bool:
        return entry_filter is None or entry_filter(entry)

    def search(
        self,
        query: str,
        *,
        server_name: str | None = None,
        top_k: int = 5,
        entry_filter: Callable[[MCPToolEntry], bool] | None = None,
    ) -> list[MCPToolEntry]:
        ranked = self._ranked_search(
            query,
            server_name=server_name,
            entry_filter=entry_filter,
        )
        return [entry for *_, entry in ranked[: max(1, top_k)]]

    def search_many(
        self,
        queries: Iterable[str],
        *,
        server_name: str | None = None,
        top_k: int = 5,
        entries: Iterable[MCPToolEntry] | None = None,
        entry_filter: Callable[[MCPToolEntry], bool] | None = None,
    ) -> list[MCPToolEntry]:
        """Merge independent keyword searches using each tool's best rank."""
        search_entries = tuple(entries) if entries is not None else None
        normalized_queries: list[str] = []
        seen_queries: set[str] = set()
        for query in queries:
            normalized_query = _normalize(query)
            if not normalized_query or normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)
            normalized_queries.append(query)

        best_by_tool: dict[
            str,
            tuple[int, float, int, int, str, MCPToolEntry],
        ] = {}
        for query_index, query in enumerate(normalized_queries):
            for exact_priority, neg_relevance, neg_matched, tool_id, entry in (
                self._ranked_search(
                    query,
                    server_name=server_name,
                    entries=search_entries,
                    entry_filter=entry_filter,
                )
            ):
                candidate = (
                    exact_priority,
                    neg_relevance,
                    neg_matched,
                    query_index,
                    tool_id,
                    entry,
                )
                current = best_by_tool.get(tool_id)
                if current is None or candidate[:4] < current[:4]:
                    best_by_tool[tool_id] = candidate

        ranked = sorted(best_by_tool.values(), key=lambda item: item[:5])
        return [entry for *_, entry in ranked[: max(1, top_k)]]

    def lookup_exact(
        self,
        tool_names: Iterable[str],
        *,
        server_name: str | None = None,
        entry_filter: Callable[[MCPToolEntry], bool] | None = None,
    ) -> tuple[list[MCPToolEntry], list[str]]:
        """Resolve exact catalog IDs, model names, or original MCP names."""
        entries = [
            entry
            for entry in self.snapshot()
            if server_name is None or entry.server_name == server_name
            if self._visible(entry, entry_filter)
        ]
        resolved: list[MCPToolEntry] = []
        resolved_ids: set[str] = set()
        missing: list[str] = []
        for requested_name in tool_names:
            normalized_name = _normalize(requested_name)
            if not normalized_name:
                continue
            matches = [
                entry
                for entry in entries
                if normalized_name
                in {
                    _normalize(entry.tool_id),
                    _normalize(f"{entry.server_name}/{entry.model_name}"),
                    _normalize(f"{entry.server_name}:{entry.model_name}"),
                    _normalize(entry.model_name),
                }
            ]
            if not matches:
                remote_matches = [
                    entry
                    for entry in entries
                    if entry.remote_name
                    and normalized_name
                    in {
                        _normalize(entry.remote_name),
                        _normalize(f"{entry.server_name}/{entry.remote_name}"),
                        _normalize(f"{entry.server_name}:{entry.remote_name}"),
                    }
                ]
                if len(remote_matches) == 1:
                    matches = remote_matches
            if not matches:
                missing.append(requested_name)
                continue
            for entry in matches:
                if entry.tool_id in resolved_ids:
                    continue
                resolved_ids.add(entry.tool_id)
                resolved.append(entry)
        return resolved, missing

    def _ranked_search(
        self,
        query: str,
        *,
        server_name: str | None = None,
        entries: Iterable[MCPToolEntry] | None = None,
        entry_filter: Callable[[MCPToolEntry], bool] | None = None,
    ) -> list[tuple[int, float, int, str, MCPToolEntry]]:
        normalized_query = _normalize(query)
        query_terms = tuple(dict.fromkeys(_tokenize(query)))
        if not normalized_query or not query_terms:
            return []
        documents = []
        for entry in self.snapshot() if entries is None else entries:
            if server_name and entry.server_name != server_name:
                continue
            if not self._visible(entry, entry_filter):
                continue
            aliases = tuple(getattr(entry.tool, "aliases", ()))
            names = (entry.model_name, *aliases)
            model_terms = tuple(dict.fromkeys(_tokenize(" ".join(names))))
            model_term_set = set(model_terms)
            remote_terms = tuple(
                term
                for term in dict.fromkeys(_tokenize(entry.remote_name or ""))
                if term not in model_term_set
            )
            remote_term_set = set(remote_terms)
            server_terms = tuple(
                term
                for term in dict.fromkeys(_tokenize(entry.server_name))
                if term not in model_term_set and term not in remote_term_set
            )
            connector_terms = tuple(
                dict.fromkeys(_tokenize(f"{entry.connector_id or ''} {entry.connector_name or ''}"))
            )
            documents.append(
                (
                    entry,
                    tuple(_normalize(name) for name in names),
                    _normalize(entry.remote_name or ""),
                    _normalize(entry.tool_id),
                    _normalize(f"{entry.server_name} {entry.model_name}"),
                    model_terms,
                    remote_terms,
                    server_terms,
                    connector_terms,
                    tuple(dict.fromkeys(_tokenize(entry.description))),
                )
            )
        if not documents:
            return []

        average_model_name_length = sum(len(item[5]) for item in documents) / len(
            documents
        )
        average_remote_name_length = sum(len(item[6]) for item in documents) / len(
            documents
        )
        average_server_name_length = sum(len(item[7]) for item in documents) / len(
            documents
        )
        average_connector_length = sum(len(item[8]) for item in documents) / len(documents)
        average_description_length = sum(len(item[9]) for item in documents) / len(
            documents
        )
        document_frequencies = {
            term: (
                sum(_prefix_term_frequency(term, item[5]) > 0 for item in documents),
                sum(_prefix_term_frequency(term, item[6]) > 0 for item in documents),
                sum(_prefix_term_frequency(term, item[7]) > 0 for item in documents),
                sum(_prefix_term_frequency(term, item[8]) > 0 for item in documents),
                sum(_prefix_term_frequency(term, item[9]) > 0 for item in documents),
            )
            for term in query_terms
        }

        ranked: list[tuple[int, float, int, str, MCPToolEntry]] = []
        for (
            entry,
            normalized_names,
            normalized_remote_name,
            normalized_id,
            normalized_server_name,
            model_name_terms,
            remote_name_terms,
            server_name_terms,
            connector_terms,
            description_terms,
        ) in documents:
            exact_priority = 3
            if normalized_query in (*normalized_names, normalized_remote_name):
                exact_priority = 0
            elif normalized_query == normalized_id:
                exact_priority = 1
            elif normalized_query == normalized_server_name:
                exact_priority = 2
            elif any(
                len(name) >= 3 and name in normalized_query
                for name in (*normalized_names, normalized_remote_name)
            ):
                # Preserve main's compound-query behavior: when the model names
                # a concrete tool inside a longer request, rank that explicit
                # selection ahead of fuzzy BM25 matches.
                exact_priority = 2

            relevance = 0.0
            matched_terms = 0
            for term in query_terms:
                model_name_frequency = _prefix_term_frequency(term, model_name_terms)
                remote_name_frequency = _prefix_term_frequency(term, remote_name_terms)
                server_name_frequency = _prefix_term_frequency(term, server_name_terms)
                connector_frequency = _prefix_term_frequency(term, connector_terms)
                description_frequency = _prefix_term_frequency(term, description_terms)
                if (
                    model_name_frequency
                    or remote_name_frequency
                    or server_name_frequency
                    or connector_frequency
                    or description_frequency
                ):
                    matched_terms += 1
                (
                    model_name_document_frequency,
                    remote_name_document_frequency,
                    server_name_document_frequency,
                    connector_document_frequency,
                    description_document_frequency,
                ) = document_frequencies[term]
                relevance += 2.0 * _bm25_term_score(
                    term_frequency=model_name_frequency,
                    document_frequency=model_name_document_frequency,
                    document_count=len(documents),
                    field_length=len(model_name_terms),
                    average_field_length=average_model_name_length,
                )
                relevance += 2.0 * _bm25_term_score(
                    term_frequency=remote_name_frequency,
                    document_frequency=remote_name_document_frequency,
                    document_count=len(documents),
                    field_length=len(remote_name_terms),
                    average_field_length=average_remote_name_length,
                )
                relevance += 0.5 * _bm25_term_score(
                    term_frequency=server_name_frequency,
                    document_frequency=server_name_document_frequency,
                    document_count=len(documents),
                    field_length=len(server_name_terms),
                    average_field_length=average_server_name_length,
                )
                relevance += 0.75 * _bm25_term_score(
                    term_frequency=connector_frequency,
                    document_frequency=connector_document_frequency,
                    document_count=len(documents),
                    field_length=len(connector_terms),
                    average_field_length=average_connector_length,
                )
                relevance += _bm25_term_score(
                    term_frequency=description_frequency,
                    document_frequency=description_document_frequency,
                    document_count=len(documents),
                    field_length=len(description_terms),
                    average_field_length=average_description_length,
                )
            if exact_priority == 3 and matched_terms == 0:
                continue
            ranked.append(
                (exact_priority, -relevance, -matched_terms, entry.tool_id, entry)
            )
        ranked.sort(key=lambda item: item[:4])
        return ranked

    def _rebuild_conflicts(self) -> None:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.model_name] = counts.get(entry.model_name, 0) + 1
        self._entries = {
            tool_id: MCPToolEntry(
                tool_id=entry.tool_id,
                model_name=entry.model_name,
                server_name=entry.server_name,
                description=entry.description,
                tool=entry.tool,
                generation=entry.generation,
                always_load=entry.always_load,
                connector_id=entry.connector_id,
                connector_name=entry.connector_name,
                remote_name=entry.remote_name,
                name_conflict=counts[entry.model_name] > 1,
            )
            for tool_id, entry in self._entries.items()
        }


_CATALOG = MCPToolCatalog()


def get_mcp_tool_catalog() -> MCPToolCatalog:
    return _CATALOG
