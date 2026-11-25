"""Shared Python and package pins for runtime installation and builds.

Regenerate constraints from a source checkout with:
    uv run python -c 'from box_agent.tools.runtime_versions import update_constraints; update_constraints()'
Existing environments are reused; new package installs on Python 3.12.6 use these pins.
"""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement

from box_agent.resource_paths import bundled_resource_dir

RUNTIME_PYTHON_VERSION = "3.12.6"


def constraints_path() -> Path:
    return bundled_resource_dir("config") / "runtime-constraints.txt"


def validate_packages(packages: list[str]) -> None:
    """Validate package requests; pip/uv owns version and marker resolution."""
    for package in packages:
        requirement = Requirement(package)
        if requirement.url or requirement.extras or requirement.marker:
            raise ValueError(f"PACKAGE_REQUIREMENT_NOT_ALLOWED: {package}")


def update_constraints() -> None:
    """Developer entry point; installation only reads the committed constraints."""
    import subprocess
    import tempfile

    from .jupyter_tool import ALLOWED_RUNTIME_PACKAGES, SANDBOX_DEFAULT_PACKAGES

    project_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="box-agent-runtime-pins-") as temporary:
        directory = Path(temporary)
        project_constraints = directory / "project.txt"
        requirements = directory / "runtime.in"
        requirements.write_text("\n".join(sorted(
            ALLOWED_RUNTIME_PACKAGES | set(SANDBOX_DEFAULT_PACKAGES) | {"ipykernel", "pip"}
        )) + "\n", encoding="utf-8")
        subprocess.run([
            "uv", "export", "--frozen", "--all-extras", "--no-dev",
            "--no-emit-project", "--no-hashes", "--output-file", str(project_constraints),
        ], cwd=project_root, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([
            "uv", "pip", "compile", str(requirements), "--constraint", str(project_constraints),
            "--python-version", RUNTIME_PYTHON_VERSION, "--universal", "--no-annotate",
            "--custom-compile-command",
            "uv run python -c 'from box_agent.tools.runtime_versions import update_constraints; update_constraints()'",
            "--output-file", str(project_root / "box_agent/config/runtime-constraints.txt"),
        ], cwd=project_root, check=True)
