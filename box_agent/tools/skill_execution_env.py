"""Cross-platform environment for third-party Skill subprocesses."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from box_agent.tools.runtime import SkillRuntimeContext


def build_skill_execution_env(
    ctx: SkillRuntimeContext,
    *,
    base_env: dict[str, str] | None = None,
    platform_name: str | None = None,
    home_dir: Path | None = None,
) -> dict[str, str]:
    """Build the environment shared by standalone CLI and hosted ACP Skills."""
    inherited = dict(os.environ if base_env is None else base_env)
    runtime_env = ctx.env()
    target_platform = platform_name or sys.platform
    is_windows = target_platform == "win32"
    separator = ";" if is_windows else ":"

    default_root = (home_dir or Path.home()) / ".box-agent" / "skill-tools"
    skill_tools_root = _safe_skill_tools_root(
        inherited.get("BOX_AGENT_SKILL_TOOLS_ROOT"),
        default=default_root,
    )
    npm_bin_dir = skill_tools_root if is_windows else skill_tools_root / "bin"
    python_user_base = skill_tools_root / "python"
    python_user_bin = python_user_base / ("Scripts" if is_windows else "bin")
    python_user_site = _python_user_site(
        python_user_base,
        is_windows=is_windows,
    )
    # Python drops a non-existent PYTHONPATH entry during interpreter startup.
    # Create the isolated user-site before a clean user's first Skill process so
    # a dependency installed with pip --user can be imported immediately by
    # that same long-running process.
    python_user_site.mkdir(parents=True, exist_ok=True)
    npm_global_modules = (
        skill_tools_root / "node_modules"
        if is_windows
        else skill_tools_root / "lib" / "node_modules"
    )

    managed_dirs: list[str] = [str(npm_bin_dir)]
    node_path_entries = _split_env_paths(runtime_env.get("NODE_PATH"), separator)
    managed_dirs.extend(str(Path(entry) / ".bin") for entry in node_path_entries)
    for name in ("BOX_AGENT_NODE", "BOX_AGENT_NPM", "BOX_AGENT_NPX"):
        executable = runtime_env.get(name)
        if executable:
            managed_dirs.append(str(Path(executable).parent))
    managed_dirs.append(str(python_user_bin))
    for name in ("BOX_AGENT_PYTHON", "BOX_AGENT_PYTHON3"):
        executable = runtime_env.get(name)
        if executable:
            managed_dirs.append(str(Path(executable).parent))

    path_prefix = _dedupe_paths(managed_dirs, is_windows=is_windows)
    inherited_path = _split_env_paths(inherited.get("PATH"), separator)
    final_path = _dedupe_paths([*path_prefix, *inherited_path], is_windows=is_windows)
    node_path = _dedupe_paths(
        [str(npm_global_modules), *node_path_entries],
        is_windows=is_windows,
    )
    python_path = _dedupe_paths(
        [
            str(python_user_site),
            *_split_env_paths(inherited.get("PYTHONPATH"), separator),
        ],
        is_windows=is_windows,
    )

    browser_root = Path(
        inherited.get("PLAYWRIGHT_BROWSERS_PATH")
        or (home_dir or Path.home()) / ".box-agent" / "browsers"
    )
    browser_executable = _resolve_skill_browser_executable(
        inherited,
        browser_root=browser_root,
        platform_name=target_platform,
    )

    result = {
        **runtime_env,
        "BOX_AGENT_SKILL_TOOLS_ROOT": str(skill_tools_root),
        "BOX_AGENT_SKILL_PATH_PREFIX": separator.join(path_prefix),
        "PATH": separator.join(final_path),
        "NPM_CONFIG_PREFIX": str(skill_tools_root),
        "NPM_CONFIG_CACHE": str(skill_tools_root / "npm-cache"),
        "PYTHONUSERBASE": str(python_user_base),
        "PYTHONPATH": separator.join(python_path),
        "PLAYWRIGHT_BROWSERS_PATH": str(browser_root),
    }
    if is_windows:
        # BashTool decodes subprocess streams as UTF-8. Force Python-based
        # Skill commands to emit UTF-8 even when the Windows locale is GBK,
        # otherwise non-ASCII paths are replaced before the model sees them.
        result["PYTHONUTF8"] = "1"
        result["PYTHONIOENCODING"] = "utf-8"
    if node_path:
        result["NODE_PATH"] = separator.join(node_path)
    if browser_executable:
        result["BOX_AGENT_BROWSER_EXECUTABLE_PATH"] = browser_executable
        result["AGENT_BROWSER_EXECUTABLE_PATH"] = browser_executable
    return result


def _safe_skill_tools_root(raw: str | None, *, default: Path) -> Path:
    if not raw or len(raw) > 1024:
        return default
    if "`" in raw or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        return default
    is_absolute = raw.startswith("/") or (
        len(raw) >= 3 and raw[1] == ":" and raw[2] in "\\/"
    )
    return Path(raw).expanduser() if is_absolute else default


def _python_user_site(root: Path, *, is_windows: bool) -> Path:
    major = sys.version_info.major
    minor = sys.version_info.minor
    if is_windows:
        return root / f"Python{major}{minor}" / "site-packages"
    return root / "lib" / f"python{major}.{minor}" / "site-packages"


def _split_env_paths(raw: str | None, separator: str) -> list[str]:
    return [entry for entry in (raw or "").split(separator) if entry]


def _dedupe_paths(entries: list[str], *, is_windows: bool) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries:
        if not entry:
            continue
        key = entry.casefold() if is_windows else entry
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def _resolve_skill_browser_executable(
    env: dict[str, str],
    *,
    browser_root: Path,
    platform_name: str,
) -> str | None:
    for name in ("BOX_AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_EXECUTABLE_PATH"):
        candidate = env.get(name)
        if candidate and Path(candidate).is_file():
            return candidate
    if not browser_root.is_dir():
        return None
    browser_dirs = sorted(
        browser_root.glob("chromium-*"),
        key=lambda path: _browser_revision(path.name),
        reverse=True,
    )
    for browser_dir in browser_dirs:
        for relative in _browser_executable_candidates(platform_name):
            candidate = browser_dir / relative
            if candidate.is_file():
                return str(candidate)
    return None


def _browser_revision(name: str) -> int:
    try:
        return int(name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _browser_executable_candidates(platform_name: str) -> tuple[Path, ...]:
    if platform_name == "darwin":
        app_root = Path("Google Chrome for Testing.app/Contents/MacOS")
        return (
            Path("chrome-mac-arm64") / app_root / "Google Chrome for Testing",
            Path("chrome-mac") / app_root / "Google Chrome for Testing",
            Path("chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
            Path("chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"),
        )
    if platform_name == "win32":
        return (Path("chrome-win64/chrome.exe"), Path("chrome-win/chrome.exe"))
    return (Path("chrome-linux/chrome"),)
