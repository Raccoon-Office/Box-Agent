"""Shared fixtures for the ACP host-like probe suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from .probe import default_acp_command

REPO_ROOT = Path(__file__).resolve().parents[2]

# Substrings that mean the hosted LLM auth/env is broken (not an ACP product bug).
_LLM_ENV_FAILURE_MARKERS = (
    "登录态已过期",
    "请重新登录",
    "401 Unauthorized",
    "invalid_api_key",
    "Incorrect API key",
    "authentication_error",
)


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
        return True, f"config={path}"
    return False, "no config.yaml found under box_agent/config or ~/.box-agent/config"


def skip_if_llm_env_failure(stderr_text: str, *, case_id: str) -> None:
    """Skip when logs show expired/invalid LLM credentials (env, not product)."""
    for marker in _LLM_ENV_FAILURE_MARKERS:
        if marker in stderr_text:
            pytest.skip(
                f"{case_id}: LLM environment failure ({marker!r}); "
                "not an ACP product bug — refresh ~/.box-agent/config/auth.json or api_key"
            )


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def acp_command() -> list[str]:
    return default_acp_command()


@pytest.fixture
def require_llm() -> str:
    ok, reason = llm_config_available()
    if not ok:
        pytest.skip(f"LLM unavailable for ACP host probe: {reason}")
    return reason
