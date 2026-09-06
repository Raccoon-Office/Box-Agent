"""Shared AST import checks for adapter-boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_ADAPTER_LAYER_MEMBERS = frozenset(
    {"core", "kernel", "plugins", "composition"}
)


def _resolves_to_forbidden_layer(module_name: str) -> bool:
    return any(
        module_name == f"box_agent.{member}"
        or module_name.startswith(f"box_agent.{member}.")
        for member in FORBIDDEN_ADAPTER_LAYER_MEMBERS
    )


def _resolve_relative_module(
    *,
    inspected_module: str,
    inspected_path: Path,
    level: int,
    module: str,
) -> str:
    package_parts = inspected_module.split(".")
    if inspected_path.name != "__init__.py":
        package_parts = package_parts[:-1]
    parent_count = max(0, level - 1)
    if parent_count:
        package_parts = package_parts[:-parent_count]
    if module:
        package_parts.extend(module.split("."))
    return ".".join(package_parts)


def forbidden_adapter_layer_imports(
    path: Path,
    *,
    inspected_module: str,
) -> list[str]:
    """Return direct adapter imports of Core, Kernel, plugins, or composition.

    Relative imports are both resolved from the inspected module and checked
    conservatively by their first member. This catches forms such as
    ``from .kernel`` and ``from . import composition`` even when a same-named
    local module would otherwise resolve below the adapter package.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _resolves_to_forbidden_layer(alias.name):
                    violations.append(f"{alias.name}:{node.lineno}")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        module = node.module or ""
        if node.level == 0:
            candidates = [module]
            if module == "box_agent":
                candidates.extend(
                    f"box_agent.{alias.name}" for alias in node.names
                )
            for candidate in candidates:
                if _resolves_to_forbidden_layer(candidate):
                    violations.append(f"{candidate}:{node.lineno}")
            continue

        relative_prefix = "." * node.level
        resolved = _resolve_relative_module(
            inspected_module=inspected_module,
            inspected_path=path,
            level=node.level,
            module=module,
        )
        relative_members = [module.split(".", 1)[0]] if module else []
        relative_members.extend(alias.name.split(".", 1)[0] for alias in node.names)
        if (
            _resolves_to_forbidden_layer(resolved)
            or any(
                member in FORBIDDEN_ADAPTER_LAYER_MEMBERS
                for member in relative_members
            )
        ):
            rendered = (
                f"{relative_prefix}{module}"
                if module
                else f"{relative_prefix}{node.names[0].name}"
            )
            violations.append(f"{rendered}:{node.lineno}")
    return violations


__all__ = ["forbidden_adapter_layer_imports"]
