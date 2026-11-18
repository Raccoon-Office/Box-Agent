"""Resolve configured Skill sources without moving or changing user directories."""

import json
import os
from pathlib import Path

from .config import Config
from .resource_paths import bundled_resource_dir
from .user_paths import configured_box_agent_home, state_path

BUILTIN_SKILLS_ENV = "BOX_AGENT_BUILTIN_SKILLS_DIR"


def host_plugin_skills_dir() -> Path | None:
    """Opt-in host-owned plugin methods; never discover another profile's Skills."""
    raw = os.environ.get("BOX_AGENT_PLUGIN_SKILLS_DIR")
    if raw is None:
        return None
    path = Path(raw.strip()).expanduser()
    if not raw.strip() or not path.is_absolute():
        raise ValueError("BOX_AGENT_PLUGIN_SKILLS_DIR must be an absolute directory")
    expected = state_path("plugins/skills").resolve()
    if path.resolve() != expected:
        raise ValueError("Plugin Skills must be inside the active profile's plugins/skills directory")
    return expected


def resolve_builtin_skills_dir(config: Config) -> Path:
    """Prefer a host's process-local source; retain legacy config lookup otherwise.

    The desktop passes this value only to its Agent child. Shared config.yaml
    remains unchanged, so another client or a downgraded app keeps its source.
    """
    override = os.environ.get(BUILTIN_SKILLS_ENV)
    if override is not None:
        skills_path = Path(override.strip()).expanduser()
        if not override.strip() or not skills_path.is_absolute():
            raise ValueError(f"{BUILTIN_SKILLS_ENV} must be an absolute directory")
    else:
        skills_path = Path(config.tools.skills_dir).expanduser()
    package = Config.get_package_dir().resolve()
    bundled = bundled_resource_dir("skills", package_dir=package).resolve()
    if skills_path.is_absolute():
        builtin = skills_path
    elif (
        bundled != package / "skills"
        and skills_path == Path("skills")
        and "skills_dir" not in config.tools.model_fields_set
    ):
        # Only the implicit default bypasses the legacy relative lookup.
        builtin = bundled
    else:
        packaged_candidate = package / skills_path
        if bundled != package / "skills" and skills_path.parts and skills_path.parts[0] == "skills":
            packaged_candidate = bundled.joinpath(*skills_path.parts[1:])
        candidates = [skills_path, Path("box_agent") / skills_path, packaged_candidate]
        if configured_box_agent_home() is not None:
            candidates = [packaged_candidate]
        builtin = candidates[0]
        for candidate in candidates:
            if candidate.exists():
                builtin = candidate.resolve()
                break
    if configured_box_agent_home() is not None:
        resolved = builtin.resolve()
        if not resolved.is_relative_to(bundled) and not resolved.is_relative_to(package):
            builtin = state_path("skills", resolved)
    return builtin


def managed_builtin_skills_dir(config: Config) -> Path | None:
    """Only host-managed manifests opt into the additional read-only root.

    Legacy custom sources keep their behavior. Never make the user Skills
    directory or one of its ancestors read-only, even for a bad host config.
    """
    if not getattr(config.tools, "enable_skills", False):
        return None
    try:
        root = resolve_builtin_skills_dir(config).resolve()
        if state_path("skills").resolve().is_relative_to(root):
            return None
        manifest = root / "_manifest.json"
        if manifest.is_symlink():
            return None
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        # A legacy missing/unreadable/custom source must not disable other tools.
        return None
    return root if isinstance(data, dict) and data.get("desktopManaged") is True else None
