"""Shared fixtures for the ACP host-like probe suite."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from .probe import AcpHostProbe, default_acp_command

REPO_ROOT = Path(__file__).resolve().parents[2]
MINIMAL_CONFIG = Path(__file__).resolve().parent / "minimal_config.yaml"

# Opt-in for live provider cases (T2-01/02, T3-02/03). Default suite stays
# deterministic and does not spend tokens or depend on personal credentials.
LIVE_ENV = "BOX_AGENT_ACP_HOST_LIVE"

def _config_candidates() -> list[Path]:
    home = Path.home() / ".box-agent" / "config" / "config.yaml"
    box_home = os.environ.get("BOX_AGENT_HOME")
    candidates = [
        REPO_ROOT / "box_agent" / "config" / "config.yaml",
        home,
    ]
    if box_home:
        candidates.insert(0, Path(box_home) / "config" / "config.yaml")
    return candidates


def llm_config_available() -> tuple[bool, str]:
    """Return (ok, reason). Used to skip LLM-dependent T2/T3 cases cleanly."""
    for path in _config_candidates():
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            return False, f"failed to parse {path}: {exc}"
        api_key = str(data.get("api_key") or "")
        if not api_key or api_key.startswith("YOUR_") or api_key.startswith("your-") or api_key == "sk-...":
            auth = path.parent / "auth.json"
            if auth.is_file():
                return True, f"config={path} (auth.json present)"
            return False, f"api_key placeholder in {path} and no auth.json"
        # Harness fixture keys are not live providers.
        if "fixture" in api_key or api_key.endswith("-fixture-key") or "example.invalid" in str(
            data.get("api_base") or ""
        ):
            continue
        return True, f"config={path}"
    return False, "no config.yaml found under box_agent/config or ~/.box-agent/config"


def live_llm_opted_in() -> bool:
    return os.environ.get(LIVE_ENV, "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def provision_isolated_box_agent_home(root: Path) -> Path:
    """Create an isolated BOX_AGENT_HOME with a minimal non-network config.

    Lets ``initialize`` / ``session/new`` / skill listing complete without a
    real API key and without mutating ``~/.box-agent``.
    """
    home = root / "box-agent-home"
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    target = config_dir / "config.yaml"
    shutil.copyfile(MINIMAL_CONFIG, target)
    return home


def isolated_probe_env(home: Path) -> dict[str, str]:
    return {
        "BOX_AGENT_HOME": str(home),
        "PLAYWRIGHT_BROWSERS_PATH": str(home / "browsers"),
        "BOX_AGENT_SESSION_TRACE_ENABLED": "0",
    }


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def acp_command() -> list[str]:
    return default_acp_command()


@pytest.fixture
def isolated_acp_home(tmp_path: Path) -> Path:
    return provision_isolated_box_agent_home(tmp_path)


@pytest.fixture
def acp_env(isolated_acp_home: Path) -> dict[str, str]:
    """Subprocess env for no-LLM handshake probes (isolated BOX_AGENT_HOME)."""
    return isolated_probe_env(isolated_acp_home)


@pytest.fixture
def make_acp_probe(acp_command: list[str], acp_env: dict[str, str], tmp_path: Path):
    """Factory: AcpHostProbe with isolated BOX_AGENT_HOME by default."""

    def _make(
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        timeout_s: float = 60.0,
        isolated: bool = True,
    ) -> AcpHostProbe:
        merged: dict[str, str] = {}
        if isolated:
            merged.update(acp_env)
        if env:
            merged.update(env)
        return AcpHostProbe(
            command=list(command) if command is not None else list(acp_command),
            cwd=cwd if cwd is not None else tmp_path / "ws",
            env=merged or None,
            timeout_s=timeout_s,
        )

    return _make


@pytest.fixture
def require_llm() -> str:
    if not live_llm_opted_in():
        pytest.skip(
            f"Live ACP host probes require {LIVE_ENV}=1 "
            "(default suite is deterministic / no provider calls)"
        )
    ok, reason = llm_config_available()
    if not ok:
        pytest.skip(f"LLM unavailable for ACP host probe: {reason}")
    return reason


@pytest.fixture
async def connected_probe(make_acp_probe, tmp_path):
    probe = make_acp_probe(cwd=tmp_path / 'ws')
    await probe.start()
    try:
        await probe.initialize()
        session_id = await probe.session_new(cwd=str(probe.cwd))
        probe.drain_notifications()
        yield probe, session_id
    finally:
        await probe.stop()


@pytest.fixture
async def live_probe(tmp_path, require_llm):
    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path / 'ws', timeout_s=180)
    await probe.start()
    try:
        await probe.initialize()
        session_id = await probe.session_new(cwd=str(probe.cwd))
        probe.drain_notifications()
        yield probe, session_id
    finally:
        await probe.stop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        from .issue_draft import to_issue_draft
        from .probe import CaseResult
        pair = item.funcargs.get('live_probe') or item.funcargs.get('connected_probe')
        logs = pair[0].stderr_text[-4000:] if pair else ''
        draft = to_issue_draft(CaseResult(
            case_id=item.name, ok=False,
            expected=item.function.__doc__ or item.name,
            actual=str(report.longrepr), logs=logs,
            repro_steps=[f'uv run pytest {item.nodeid} -q'],
        ))
        report.sections.append(('IssueDraft', draft['title'] + '\n' + draft['body']))
