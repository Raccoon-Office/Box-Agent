"""Checked platform adaptations of the pinned presentation suite."""

import ast


def _replace(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Presentation platform overlay needs review: {old[:80]!r}")
    return text.replace(old, new, 1)


def _function(text: str, name: str) -> str:
    matches = [node for node in ast.parse(text).body
               if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"Presentation platform overlay needs review: {name}")
    node = matches[0]
    return "".join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


STANDARD_BROWSER = '''\
def _browser_layouts():
    """Return native full/headless layouts, including older Playwright caches."""
    system = platform.system()
    if system == "Windows":
        return (
            ["chrome-headless-shell-win64/chrome-headless-shell.exe", "chrome-win/headless_shell.exe"],
            ["chrome-win64/chrome.exe", "chrome-win/chrome.exe"],
        )
    if system == "Darwin":
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        return (
            [f"chrome-headless-shell-mac-{arch}/chrome-headless-shell"],
            [f"chrome-mac-{arch}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
             "chrome-mac/Chromium.app/Contents/MacOS/Chromium"],
        )
    return (
        ["chrome-headless-shell-linux64/chrome-headless-shell", "chrome-linux/headless_shell"],
        ["chrome-linux64/chrome", "chrome-linux/chrome"],
    )


def _browser_cache_roots(p):
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if configured and configured != "0":
        return [Path(configured).expanduser()]
    roots = []
    expected = getattr(p.chromium, "executable_path", "")
    for parent in Path(expected).parents if expected else []:
        if re.fullmatch(r"chromium(?:_headless_shell)?-\\d+", parent.name):
            roots.append(parent.parent)
            break
    if configured == "0":
        return roots
    if platform.system() == "Windows":
        roots.append(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ms-playwright")
    elif platform.system() == "Darwin":
        roots.append(Path.home() / "Library/Caches/ms-playwright")
    roots.extend([Path.home() / ".cache/ms-playwright", Path.home() / ".box-agent/browsers"])
    return list(dict.fromkeys(roots))


def _browser_is_executable(candidate):
    return candidate.is_file() and (platform.system() == "Windows" or os.access(candidate, os.X_OK))


def _prefer_headless_shell(exe):
    revision = re.search(r"chromium-(\\d+)", str(exe))
    if revision:
        for parent in Path(exe).parents:
            if parent.name == f"chromium-{revision.group(1)}":
                root = parent.parent / f"chromium_headless_shell-{revision.group(1)}"
                for relative in _browser_layouts()[0]:
                    candidate = root / relative
                    if _browser_is_executable(candidate):
                        return str(candidate)
                break
    return exe


def _scan_local_chromium(p):
    expected = getattr(p.chromium, "executable_path", "") or ""
    revision = re.search(r"chromium(?:_headless_shell)?-(\\d+)", expected)
    desired = int(revision.group(1)) if revision else None
    candidates = []
    shells, full = _browser_layouts()
    for root in _browser_cache_roots(p):
        for priority, (pattern, layouts) in enumerate([
            ("chromium_headless_shell-*", shells), ("chromium-*", full)
        ]):
            for directory in root.glob(pattern):
                match = re.fullmatch(r"chromium(?:_headless_shell)?-(\\d+)", directory.name)
                if not match:
                    continue
                rev = int(match.group(1))
                for relative in layouts:
                    candidate = directory / relative
                    if _browser_is_executable(candidate):
                        distance = abs(desired - rev) if desired is not None else rev
                        candidates.append((distance, priority, str(candidate)))
    if not candidates:
        return None
    distance, _, selected = min(candidates)
    if desired is not None and distance:
        print(f"[render] chromium-{desired} unavailable; using cached {selected}", file=sys.stderr)
    return selected
'''


DAZZLE_BROWSER = '''\
def chromium_executable_path() -> str | None:
    """Honor host selection, then optional caches, then Playwright's default."""
    for key in ("BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "PPT_SKILL_BROWSER_EXE"):
        value = os.environ.get(key, "").strip()
        if value:
            candidate = Path(value).expanduser().resolve()
            if not candidate.is_file():
                raise RuntimeError(f"Configured Playwright browser is unavailable: {candidate}")
            return str(candidate)
    candidates: list[Path] = []
    for key in ("DYNAMIC_PPT_CHROMIUM_EXECUTABLE", "PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        value = os.environ.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    root_value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if root_value and root_value != "0":
        root = Path(root_value).expanduser()
        shells, full = _browser_layouts()
        for prefix, layouts in (("chromium-*", full), ("chromium_headless_shell-*", shells)):
            for relative in layouts:
                candidates.extend(sorted(root.glob(prefix + "/" + relative), reverse=True))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None
'''


DOCTOR_PROBE = '''\
    # Exercise the shipped renderer, including host selection and data locks.
    import tempfile

    renderer = standard_dir / "scripts" / "render.py"
    result = {
        "status": "failed", "python_package": True,
        "python_source": python_source, "python_runner": python_runner,
        "browser_present": False, "launchable": False,
        "renderer_script": str(renderer),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ppt-doctor-") as directory:
            root = Path(directory)
            source, output = root / "probe.html", root / "probe.png"
            source.write_text(
                '<!doctype html><html><head><meta charset="utf-8"><style>'
                'html,body{margin:0;width:1600px;height:900px;background:white}'
                '.slide{width:1600px;height:900px;display:grid;place-items:center;'
                'font:48px sans-serif;color:#111}</style></head>'
                '<body><div class="slide">Presentation renderer probe</div></body></html>',
                encoding="utf-8",
            )
            environment = dict(os.environ, RENDER_JOB_TIMEOUT="20", RENDER_SLOT_TIMEOUT="5")
            completed = subprocess.run(
                [*python_runner, str(renderer), str(source), str(output), "1600", "900"],
                capture_output=True, text=True, timeout=40, check=False, env=environment,
            )
            result["renderer_returncode"] = completed.returncode
            rendered = output.is_file() and output.read_bytes().startswith(b"\\x89PNG\\r\\n\\x1a\\n")
            if completed.returncode == 0 and rendered:
                result.update(status="available", browser_present=True, launchable=True)
                return result
            result["reason"] = "renderer_failed" if completed.returncode else "renderer_output_missing"
            result["detail"] = (completed.stderr.strip() or completed.stdout.strip())[-1000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["reason"] = type(exc).__name__
        result["detail"] = str(exc)[:1000]
    result["install_hint"] = install_hint
    return result
'''


def apply(relative: str, data: bytes) -> bytes:
    if relative not in {
        "skills/sn-ppt-standard/scripts/render.py",
        "skills/sn-ppt-standard/scripts/image_cutout.py",
        "skills/sn-ppt-standard/scripts/install.sh",
        "skills/sn-ppt-dazzle/scripts/render_deck.py",
        "skills/sn-ppt-doctor/ppt_doctor/check_environment.py",
    }:
        return data
    text = data.decode("utf-8")
    if relative.endswith("/render.py"):
        text = _replace(text, "import fcntl as _fcntl\n", "from pathlib import Path\nfrom file_lock import lock_file, unlock_file\n")
        text = _replace(text, '_fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)', 'lock_file(lock)')
        text = _replace(text, '_fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)', 'unlock_file(lock)')
        for variable in ("html", "present"):
            text = _replace(text, '"file://" + ' + variable, f"Path({variable}).resolve().as_uri()")
        text = _replace(text, 'os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))\n', '')
        text = _replace(text, _function(text, "_scan_local_chromium"), "")
        text = _replace(text, _function(text, "_prefer_headless_shell"), STANDARD_BROWSER.rstrip())
    elif relative.endswith("/image_cutout.py"):
        text = _replace(text, "import fcntl\n", "from file_lock import lock_file, unlock_file\n")
        text = _replace(text, "fcntl.flock(lock.fileno(), fcntl.LOCK_EX)", "lock_file(lock)")
        text = _replace(text, "fcntl.flock(lock.fileno(), fcntl.LOCK_UN)", "unlock_file(lock)")
    elif relative.endswith("/render_deck.py"):
        text = _replace(text, "\nimport os\n", "\nimport os\nimport platform\n")
        replacement = _function(STANDARD_BROWSER, "_browser_layouts") + "\n\n" + DAZZLE_BROWSER.rstrip()
        text = _replace(text, _function(text, "chromium_executable_path"), replacement)
    elif relative.endswith("/check_environment.py"):
        old = _function(text, "playwright_chromium_status")
        prefix, separator, _ = old.partition("    probe = r'''\n")
        if not separator:
            raise ValueError("Presentation platform overlay needs review: Doctor probe")
        text = _replace(text, old, prefix + DOCTOR_PROBE.rstrip())
    else:
        text = _replace(text, 'PYBIN="${PYBIN:-python3}"', 'PYBIN="${BOX_AGENT_PYTHON:-${PYBIN:-python3}}"')
        text = _replace(text, 'log(){ echo "[install] $*"; }\n', '''log(){ echo "[install] $*"; }

normalize_python(){
  local candidate
  for candidate in "$NORMALIZE_VENV/Scripts/python.exe" "$NORMALIZE_VENV/bin/python"; do
    if [ -f "$candidate" ]; then printf '%s\\n' "$candidate"; return 0; fi
  done
  log "normalize interpreter missing: $NORMALIZE_VENV" >&2
  return 1
}
''')
        text = _replace(text, '  log "1) normalize venv → $NORMALIZE_VENV"', '  local NORMALIZE_PY\n  log "1) normalize venv → $NORMALIZE_VENV"')
        text = _replace(text, '    uv venv "$NORMALIZE_VENV" >/dev/null 2>&1 || true', '    uv venv --python "$PYBIN" "$NORMALIZE_VENV" >/dev/null 2>&1 || true\n    NORMALIZE_PY="$(normalize_python)"')
        text = _replace(text, '    "$PYBIN" -m venv "$NORMALIZE_VENV"', '    "$PYBIN" -m venv "$NORMALIZE_VENV"\n    NORMALIZE_PY="$(normalize_python)"')
        # Restrict substitutions to call sites; keep the discovery paths literal.
        for old in (
            '    uv pip install --python "$NORMALIZE_VENV/bin/python"',
            '    "$NORMALIZE_VENV/bin/python" -m pip install -q --upgrade pip',
            '    "$NORMALIZE_VENV/bin/python" -m pip install -q "${NORMALIZE_PACKAGES[@]}"',
            '  "$NORMALIZE_VENV/bin/python" - <<\'PY\'',
        ):
            text = _replace(text, old, old.replace('$NORMALIZE_VENV/bin/python', '$NORMALIZE_PY'))
        text = _replace(text, '  local PW="$NORMALIZE_VENV/bin/python"', '  local PW\n  PW="$(normalize_python 2>/dev/null)" || PW="$PYBIN"')
        text = _replace(text, 'echo "  export NORMALIZE_PY=\\"$NORMALIZE_VENV/bin/python\\""', 'echo "  export NORMALIZE_PY=\\"$(normalize_python 2>/dev/null || printf \'%s\' "$PYBIN")\\""')
    return text.encode("utf-8")
