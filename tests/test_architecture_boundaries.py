"""Executable dependency rules for the three-layer architecture."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture_imports import forbidden_adapter_layer_imports


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "box_agent"
CORE_BRIDGE = Path("runtime.py")
APPLICATION_ADAPTER_MODULES = ("box_agent.acp", "box_agent.cli", "acp", "cli")
PRESENTATION_WORKFLOW_TOKENS = (
    "controlled_presentation",
    "presentation_research_mode",
    "controlled_presentation_stage",
    "pptx",
    "powerpoint",
    "slide deck",
    "演示文稿",
    "幻灯片",
)


def _stable_kernel_paths() -> tuple[Path, ...]:
    """Return legacy stable modules plus every Python file in ``kernel``."""
    legacy_paths = (
        PACKAGE_ROOT / "core.py",
        PACKAGE_ROOT / "loop_guards.py",
        PACKAGE_ROOT / "runtime.py",
    )
    kernel_root = PACKAGE_ROOT / "kernel"
    return tuple(legacy_paths) + tuple(sorted(kernel_root.rglob("*.py")))


def _direct_core_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "box_agent.core"
                or alias.name.startswith("box_agent.core.")
                for alias in node.names
            ):
                violations.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                module == "box_agent.core"
                or module.startswith("box_agent.core.")
                or (node.level > 0 and (module == "core" or module.startswith("core.")))
                or (
                    module == "box_agent"
                    and any(alias.name == "core" for alias in node.names)
                )
                or (
                    node.level > 0
                    and not module
                    and any(alias.name == "core" for alias in node.names)
                )
            ):
                violations.append(node.lineno)
    return violations


def _is_application_adapter_module(name: str) -> bool:
    return any(
        name == module or name.startswith(f"{module}.")
        for module in APPLICATION_ADAPTER_MODULES
    )


def _application_adapter_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] if module else []
            if module in {"", "box_agent"}:
                names.extend(alias.name for alias in node.names)
        else:
            continue
        for name in names:
            if _is_application_adapter_module(name):
                violations.append(f"{name}:{node.lineno}")
    return violations


def _outer_composition_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules = [module]
            if module == "box_agent":
                modules.extend(f"box_agent.{alias.name}" for alias in node.names)
            elif node.level > 0 and not module:
                modules.extend(alias.name for alias in node.names)
        else:
            continue
        for module in modules:
            if (
                module == "box_agent.plugins"
                or module.startswith("box_agent.plugins.")
                or module == "plugins"
                or module.startswith("plugins.")
                or module == "box_agent.composition"
                or module == "composition"
            ):
                violations.append(f"{module}:{node.lineno}")
    return violations


def test_only_runtime_bridge_imports_core_implementation() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PACKAGE_ROOT)
        if relative_path in {CORE_BRIDGE, Path("core.py")}:
            continue
        for lineno in _direct_core_imports(path):
            violations.append(f"{relative_path}:{lineno}")

    assert violations == [], (
        "Application and capability modules must use Agent/run_events, "
        "box_agent.runtime, artifacts, or policy modules instead of importing "
        f"box_agent.core directly: {violations}"
    )


def test_core_does_not_depend_on_application_adapters() -> None:
    core_path = PACKAGE_ROOT / "core.py"
    forbidden = _application_adapter_imports(core_path)
    assert forbidden == [], f"Core must not import application adapters: {forbidden}"


def test_core_has_no_workflow_state_machine_dependency() -> None:
    core_path = PACKAGE_ROOT / "core.py"
    tree = ast.parse(core_path.read_text(encoding="utf-8"), filename=str(core_path))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.append(module)
            if module in {"", "box_agent"}:
                imported_modules.extend(alias.name for alias in node.names)

    assert not any(
        module == "box_agent.workflows"
        or module.startswith("box_agent.workflows.")
        or module == "workflows"
        or module.startswith("workflows.")
        for module in imported_modules
    ), "Core must not depend on the removed workflow packages"

    source = core_path.read_text(encoding="utf-8")
    assert "controlled_presentation" not in source
    assert "WorkflowPolicy" not in source
    assert "CompletionGate" not in source


def test_stable_kernel_contains_no_concrete_presentation_workflow() -> None:
    violations: list[str] = []
    for path in _stable_kernel_paths():
        relative_path = path.relative_to(PACKAGE_ROOT)
        source = path.read_text(encoding="utf-8").lower()
        for token in PRESENTATION_WORKFLOW_TOKENS:
            if token in source:
                violations.append(f"{relative_path}:{token}")

    assert violations == [], (
        "Presentation routing and state must stay outside the stable kernel: "
        f"{violations}"
    )


def test_stable_kernel_has_no_application_adapter_or_core_dependency() -> None:
    adapter_violations: list[str] = []
    core_violations: list[str] = []
    for path in _stable_kernel_paths():
        relative_path = path.relative_to(PACKAGE_ROOT)
        for violation in _application_adapter_imports(path):
            adapter_violations.append(f"{relative_path}:{violation}")
        if "kernel" in relative_path.parts:
            for lineno in _direct_core_imports(path):
                core_violations.append(f"{relative_path}:{lineno}")

    assert adapter_violations == [], (
        "Stable kernel files must not import application adapters: "
        f"{adapter_violations}"
    )
    assert core_violations == [], (
        "Kernel package files must not import box_agent.core: "
        f"{core_violations}"
    )


def test_kernel_has_no_outer_composition_dependency_at_any_scope() -> None:
    violations = [
        f"{path.relative_to(PACKAGE_ROOT)}:{violation}"
        for path in sorted((PACKAGE_ROOT / "kernel").rglob("*.py"))
        for violation in _outer_composition_imports(path)
    ]

    assert violations == [], (
        "Outer composition and plugins depend on kernel-owned ports; kernel "
        "modules must not import either layer, including inside functions: "
        f"{violations}"
    )


def test_outer_composition_detector_rejects_package_member_imports(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "forbidden_outer_imports.py"
    sample.write_text(
        "from box_agent import plugins\n"
        "from box_agent import composition\n"
        "from . import plugins\n"
        "from . import composition\n",
        encoding="utf-8",
    )

    assert _outer_composition_imports(sample) == [
        "box_agent.plugins:1",
        "box_agent.composition:2",
        "plugins:3",
        "composition:4",
    ]


def test_adapter_import_detector_handles_absolute_and_relative_forms(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "adapter.py"
    sample.write_text(
        "import box_agent.kernel.loop\n"
        "from box_agent.core import run_agent_loop\n"
        "from box_agent import plugins\n"
        "from .kernel import AgentLoopKernel\n"
        "from ..kernel import ports\n"
        "from . import composition\n"
        "from .. import core\n"
        "from .action_hints import normalize\n"
        "from ..events import DoneEvent\n",
        encoding="utf-8",
    )

    assert forbidden_adapter_layer_imports(
        sample,
        inspected_module="box_agent.acp.adapter",
    ) == [
        "box_agent.kernel.loop:1",
        "box_agent.core:2",
        "box_agent.plugins:3",
        ".kernel:4",
        "..kernel:5",
        ".composition:6",
        "..core:7",
    ]


def test_generic_tools_contain_no_presentation_workflow_lifecycle() -> None:
    forbidden_tokens = (
        "controlled_presentation",
        "CompletionGate",
        "WorkflowPolicy",
        "runtime_workflow_actions",
    )
    generic_tool_paths = (
        PACKAGE_ROOT / "tools" / "base.py",
        PACKAGE_ROOT / "tools" / "bash_tool.py",
        PACKAGE_ROOT / "tools" / "file_tools.py",
        PACKAGE_ROOT / "tools" / "jupyter_tool.py",
    )

    violations = [
        f"{path.name}:{token}"
        for path in generic_tool_paths
        for token in forbidden_tokens
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_pptx_tool_safety_has_no_workflow_lifecycle_dependency() -> None:
    safety_path = PACKAGE_ROOT / "tools" / "pptx_safety.py"
    source = safety_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(safety_path))
    imported_modules = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ]

    assert not any(
        module == "box_agent.workflows"
        or module.startswith("box_agent.workflows.")
        or module == "workflows"
        or module.startswith("workflows.")
        for module in imported_modules
    )
    assert "controlled_presentation" not in source
    assert "CompletionGate" not in source
    assert "WorkflowPolicy" not in source


def test_acp_has_no_legacy_workflow_lifecycle() -> None:
    acp_path = PACKAGE_ROOT / "acp" / "__init__.py"
    tree = ast.parse(acp_path.read_text(encoding="utf-8"), filename=str(acp_path))
    concrete_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        concrete_imports.extend(
            module
            for module in modules
            if module.startswith("box_agent.workflows.presentation_")
        )

    source = acp_path.read_text(encoding="utf-8")
    assert concrete_imports == []
    assert '"controlled_presentation"' not in source
    assert "CompletionGate" not in source
    assert "WorkflowPolicy" not in source
    assert "ContextCheckpointEvent" not in source


def test_legacy_workflow_provider_modules_are_removed() -> None:
    removed_files = (
        "completion.py",
        "workflow_policy.py",
        "workflow_checkpoint_store.py",
        "workflow_owner_store.py",
        "delivery.py",
    )

    assert all(
        not (PACKAGE_ROOT / relative_path).exists()
        for relative_path in removed_files
    )
    assert not list((PACKAGE_ROOT / "workflows").glob("*.py"))


def test_application_adapter_detector_rejects_submodule_imports(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "forbidden_imports.py"
    sample.write_text(
        "import box_agent.acp.session\n"
        "from box_agent.acp.protocol import Request\n"
        "from .acp.transport import Connection\n"
        "from box_agent import cli\n"
        "from . import acp\n",
        encoding="utf-8",
    )

    assert _application_adapter_imports(sample) == [
        "box_agent.acp.session:1",
        "box_agent.acp.protocol:2",
        "acp.transport:3",
        "cli:4",
        "acp:5",
    ]
