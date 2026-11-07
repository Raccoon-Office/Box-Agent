"""Prepare one request through the existing exposure and visibility policies."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..base import Tool, build_tool_name_index
from .contracts import PreparedTools, ToolDefinitionView

if TYPE_CHECKING:
    from ..mcp_tool_search import MCPToolExposureManager


def prepare_tools(
    candidates: Iterable[Tool],
    *,
    tool_exposure: MCPToolExposureManager | None = None,
    is_tool_visible: Callable[[Tool], bool] | None = None,
) -> PreparedTools:
    """Freeze offered definitions and bind calls to their actual tool objects."""

    offered = list(candidates)
    if tool_exposure is not None and getattr(tool_exposure, "stable_prefix", False):
        from ..local_tool_exposure import DISCOVERABLE_LOCAL_NAMES
        from ..browser_intent import _BROWSER_CONTEXT_TOOLS
        tool_exposure._stable_candidates = {tool.name: tool for tool in offered}
        if not tool_exposure._deferred_mcp and tool_exposure._scope_parent is None:
            groups = {}
            for tool in offered:
                if getattr(tool, "mcp_tool_id", None) is not None:
                    server = getattr(tool, "server_name", None) or getattr(tool, "_server_name", "")
                    groups.setdefault(server, []).append(tool)
            existing = {entry.server_name for entry in tool_exposure._catalog.snapshot()}
            for server in existing - groups.keys():
                tool_exposure._catalog.remove_server(server)
            for server, members in groups.items():
                current = [entry.tool for entry in tool_exposure._catalog.snapshot() if entry.server_name == server]
                if sorted(map(id, current)) != sorted(map(id, members)):
                    tool_exposure._catalog.replace_server(server, members)
        def visible_name(name: str) -> bool:
            target = tool_exposure._stable_candidates.get(name)
            if target is None:
                target = next((entry.tool for entry in tool_exposure._catalog.snapshot()
                               if entry.model_name == name), None)
            return target is not None and is_tool_visible(target)

        tool_exposure._stable_visibility = visible_name if is_tool_visible is not None else None
        if tool_exposure._stable_direct is None:
            direct_local = {"bash_output", "bash_kill"}
            if "read_file" in tool_exposure._stable_candidates:
                direct_local.add("query_jsonl")
            direct_local.update(tool.name for tool in offered
                                if getattr(tool, "_model_exposure_direct", False))
            tool_exposure._stable_direct = {
                tool.name: (tool, ToolDefinitionView.from_tool(tool)) for tool in offered
                if getattr(tool, "mcp_tool_id", None) is None
                and (tool.name not in DISCOVERABLE_LOCAL_NAMES or tool.name in direct_local)
                and tool.name not in _BROWSER_CONTEXT_TOOLS
            }
        direct = tool_exposure._stable_direct
        valid_refs = set(tool_exposure._search_refs)
        for text in tool_exposure._bootstrap_acked:
            try:
                valid_refs.add(json.loads(text)["tool_ref"])
            except (ValueError, KeyError, TypeError):
                continue
        route_refs, routed_targets, routed_definitions = {}, {}, {}
        generations = dict(tool_exposure._direct_generations)
        for ref in valid_refs:
            tool, definition, generation = tool_exposure._stable_refs[ref]
            if (tool.name in direct or not tool_exposure.is_current_target(tool.name, tool)
                    or definition.description != tool.description or not definition.matches_target(tool)
                    or (getattr(tool, "mcp_tool_id", None) is not None
                        and tool_exposure.validate_call(tool.name, generation, tool) is not None)):
                continue
            route_refs[ref] = tool.name
            routed_targets[tool.name] = tool
            routed_definitions[tool.name] = definition
            if isinstance(generation, int) and getattr(tool, "mcp_tool_id", None) is not None:
                generations[tool.name] = generation
        targets = {name: pair[0] for name, pair in direct.items()}
        return PreparedTools(
            definitions=tuple(pair[1] for pair in direct.values()), targets=targets,
            call_names={name: tool.name for name, tool in build_tool_name_index(targets.values()).items()},
            mcp_generations=generations, routed_targets=routed_targets,
            routed_definitions=routed_definitions, route_refs=route_refs,
            route_visible=lambda name: tool_exposure._stable_visibility is None or tool_exposure._stable_visibility(name),
            _tool_exposure=tool_exposure,
            _registered_names=frozenset(tool_exposure._stable_candidates),
        )
    registered_names = frozenset(tool.name for tool in offered)
    generations: dict[str, int] = {}
    if tool_exposure is not None:
        exposure = tool_exposure.prepare_tools(offered)
        offered = exposure.tools
        generations = dict(exposure.mcp_generations)
    else:
        generations = {
            tool.name: tool.mcp_generation
            for tool in offered
            if getattr(tool, "mcp_tool_id", None) is not None
            and isinstance(getattr(tool, "mcp_generation", None), int)
        }
    if is_tool_visible is not None:
        offered = [tool for tool in offered if is_tool_visible(tool)]
    name_index = build_tool_name_index(offered)
    targets = {tool.name: tool for tool in offered}
    return PreparedTools(
        definitions=tuple(ToolDefinitionView.from_tool(tool) for tool in offered),
        targets=targets,
        call_names={name: tool.name for name, tool in name_index.items()},
        mcp_generations={
            name: generation
            for name, generation in generations.items()
            if name in targets
        },
        _tool_exposure=tool_exposure,
        _registered_names=registered_names,
    )
