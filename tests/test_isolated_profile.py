"""Fresh-process profile probes with only synthetic config/data and fake LLMs."""

import os
from pathlib import Path
import subprocess
import sys


def run_profile_probe(tmp_path, script):
    root = tmp_path / "profile"
    root.mkdir()
    # Preserve OS identity, not credentials or runtime overrides. Never replace HOME.
    keep = {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update({"BOX_AGENT_HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1", "BOX_AGENT_SESSION_TRACE_ENABLED": "0"})
    prefix = '''
import os, sys
from pathlib import Path
root = Path(os.environ["BOX_AGENT_HOME"])
old_roots = [Path.home() / ".box-agent", Path.home() / ".openclaw"]
blocked_attempts = []
def audit(event, args):
    if event not in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename"}: return
    for raw in args[:2] if event == "os.rename" else args[:1]:
        if not isinstance(raw, (str, bytes, os.PathLike)): continue
        path = Path(os.fsdecode(raw)).absolute()
        if any(path == old or old in path.parents for old in old_roots):
            blocked_attempts.append(event)
            raise AssertionError("Probe touched legacy user state")
sys.addaudithook(audit)
'''
    verified_script = prefix + script + '\nassert not blocked_attempts, "Legacy access was attempted even if caught"\n'
    result = subprocess.run([sys.executable, "-c", verified_script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def test_all_engine_default_providers_and_constructor_writes_use_profile(tmp_path):
    run_profile_probe(tmp_path, '''
from box_agent.logger import AgentLogger
from box_agent.memory import MemoryManager
from box_agent.workspace_registry import WorkspaceRegistry, default_workspace_registry_path
from box_agent.session_trace import default_session_trace_dir
from box_agent.llm.model_profiles import default_model_profile_registry_path
from box_agent.tools.mcp_bootstrap import default_managed_mcp_config_path
from box_agent.tools.obsidian_tool import obsidian_config_path
from box_agent.tools.skill_loader import SKILL_SETTINGS_PATH
from box_agent.tools.safety import TRASH_DIR
from box_agent.tools.jupyter_tool import SANDBOX_BASE_DIR, RUNTIME_PACKAGES_DIR
from box_agent.tools.runtime import DEFAULT_NODE_RUNTIME_ROOT, SkillRuntimeContext
from box_agent.tools.skill_execution_env import build_skill_execution_env
from box_agent.hooks import USER_HOOKS_DIR
from box_agent.config import AgentConfig

paths = [AgentLogger().log_dir, MemoryManager().memory_dir, default_workspace_registry_path(),
    default_session_trace_dir(), default_model_profile_registry_path(), default_managed_mcp_config_path(),
    obsidian_config_path(), SKILL_SETTINGS_PATH, TRASH_DIR, SANDBOX_BASE_DIR, RUNTIME_PACKAGES_DIR,
    DEFAULT_NODE_RUNTIME_ROOT, USER_HOOKS_DIR, Path(AgentConfig().workspace_dir)]
assert all(path.is_relative_to(root) for path in paths), paths
WorkspaceRegistry().set(root / "workspace", "general")
assert (root / "config/workspaces.json").is_file()
env = build_skill_execution_env(SkillRuntimeContext(runtimes={}))
assert env["BOX_AGENT_HOME"] == str(root)
assert Path(env["BOX_AGENT_SKILL_TOOLS_ROOT"]).is_relative_to(root)
assert Path(env["PLAYWRIGHT_BROWSERS_PATH"]).is_relative_to(root)
''')


def test_fake_acp_turn_and_session_recovery_never_access_legacy_profile(tmp_path):
    root = run_profile_probe(tmp_path, '''
import asyncio
from types import SimpleNamespace
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from tests.test_acp import DummyConn, DoneLLM

async def probe():
    config = Config(llm=LLMConfig(api_key="fixture-key"),
        agent=AgentConfig(workspace_dir=str(root / "workspace"), enable_memory=False, max_steps=2),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False, enable_sub_agent=False))
    request = SimpleNamespace(cwd=str(root / "workspace"), field_meta={"session_id": "isolated-fixture"})
    first = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await first.newSession(request)
    response = await first.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "fixture"}], field_meta={}))
    assert response.field_meta["ok"] is True
    first._sessions[session.sessionId].agent.session_log.close()
    second = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    restored = await second.newSession(request)
    state = second._sessions[restored.sessionId]
    assert [message.content for message in state.agent.messages][-2:] == [
        "fixture\\n\\n<connector-status>\\nnone: selected\\n</connector-status>", "done",
    ]
    state.agent.session_log.close()
asyncio.run(probe())
''')
    assert list((root / "sessions").rglob("session.jsonl"))


def test_profile_example_reaches_acp_initialize_without_memory_mcp_skills_or_network(tmp_path):
    run_profile_probe(tmp_path, '''
import asyncio, shutil, socket
from types import SimpleNamespace
import box_agent.acp as acp
from box_agent.config import Config
from tests.test_acp import DummyConn

(root / "config").mkdir()
shutil.copyfile(Config.get_package_dir() / "config/isolated-profile-example.yaml", root / "config/config.yaml")
config = Config.load()
assert not config.agent.enable_memory and not config.agent.enable_memory_extraction
assert not config.tools.enable_mcp and not config.tools.enable_skills and not config.hooks.hooks
def forbidden(*args, **kwargs): raise AssertionError("Unexpected background or network work")
socket.socket.connect = forbidden
acp.MemoryManager = forbidden
calls = []
def fake_llm(**kwargs):
    assert kwargs["api_key"] == "isolated-fixture-key"
    assert Path(kwargs["auth_file"]).is_relative_to(root)
    calls.append("llm-constructor-only")
    return SimpleNamespace()
acp.LLMClient = fake_llm
real_tools = acp.initialize_base_tools
async def tools(*args, **kwargs):
    result = await real_tools(*args, **kwargs)
    assert result[1:] == (None, None, None)
    calls.append("no-background-tools")
    return result
acp.initialize_base_tools = tools
async def streams(): return object(), SimpleNamespace(transport=SimpleNamespace(_is_closing=False))
acp.stdio_streams_largebuf = streams

async def probe():
    loop = asyncio.get_running_loop()
    shutdown = []
    loop.add_signal_handler = lambda signal, callback: shutdown.append(callback)
    loop.remove_signal_handler = lambda signal: True
    initialized = []
    def connected(factory, writer, reader):
        agent = factory(DummyConn())
        async def handshake():
            response = await agent.initialize(SimpleNamespace(field_meta={}))
            initialized.append(response.protocolVersion)
            assert response.agentCapabilities.loadSession is False
            shutdown[0]()
        asyncio.create_task(handshake())
    acp.AgentSideConnection = connected
    await acp.run_acp_server(config)
    assert initialized == [1]
asyncio.run(probe())
assert calls == ["llm-constructor-only", "no-background-tools"]
''')
