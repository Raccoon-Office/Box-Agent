import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.runtime_capabilities import _REQUIRED_ROADMAP_RESOURCES, runtime_capabilities
from box_agent.tools.runtime import SkillRuntime, SkillRuntimeContext


class _DummyConn:
    def __init__(self) -> None:
        self.ext_notifications = []

    async def sessionUpdate(self, _payload) -> None:
        return None

    async def extNotification(self, method, params) -> None:
        self.ext_notifications.append((method, params))


def _config(tmp_path, *, enable_skills: bool = True) -> Config:
    return Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            workspace_dir=str(tmp_path),
            enable_memory_extraction=False,
        ),
        tools=ToolsConfig(
            enable_todo=False,
            enable_sub_agent=False,
            enable_skills=enable_skills,
        ),
    )


def _copy_roadmap_skill_tree(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "box_agent" / "skills" / "roadmap"
    skills_root = tmp_path / "skills"
    copied_root = skills_root / "roadmap"
    shutil.copytree(source, copied_root)
    shutil.copy2(source.parent / "_manifest.json", skills_root / "_manifest.json")
    return copied_root


def test_runtime_capabilities_advertise_independent_html_renderer() -> None:
    capabilities = runtime_capabilities(
        node_available=True,
    )

    assert capabilities == {
        "contract": "box-agent.runtime-capabilities",
        "contractVersion": 1,
        "deckProtocolVersion": 1,
        "roadmap": {
            "schemaVersion": 1,
            "geometryVersion": 1,
            "rendererVersion": 1,
            "capabilities": [
                "roadmap.generate.html",
                "roadmap.preview",
                "roadmap.edit",
            ],
        },
    }


def test_runtime_capabilities_match_frozen_cross_repo_fixture() -> None:
    references = (
        Path(__file__).resolve().parents[1]
        / "box_agent"
        / "skills"
        / "roadmap"
        / "references"
    )
    fixture = references / "runtime-capabilities-v1.json"
    schema = json.loads(
        (references / "runtime-capabilities-v1.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    frozen = json.loads(fixture.read_text(encoding="utf-8"))
    validator.validate(frozen)
    assert runtime_capabilities(
        node_available=True,
    ) == frozen

    invalid = json.loads(fixture.read_text(encoding="utf-8"))
    invalid["roadmap"]["rendererVersion"] = None
    with pytest.raises(ValidationError, match="None is not of type 'integer'"):
        validator.validate(invalid)


def test_runtime_capabilities_returns_an_isolated_copy() -> None:
    capabilities = runtime_capabilities(
        node_available=True,
    )
    capabilities["roadmap"]["capabilities"].append("roadmap.future")

    fresh_capabilities = runtime_capabilities(
        node_available=True,
    )
    assert "roadmap.future" not in fresh_capabilities["roadmap"]["capabilities"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"skills_enabled": False, "node_available": True},
        {"skills_enabled": True, "node_available": False},
    ],
)
def test_runtime_capabilities_fail_closed_when_runtime_requirements_are_missing(kwargs) -> None:
    capabilities = runtime_capabilities(**kwargs)
    references = (
        Path(__file__).resolve().parents[1]
        / "box_agent"
        / "skills"
        / "roadmap"
        / "references"
    )
    schema = json.loads(
        (references / "runtime-capabilities-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator(schema).validate(capabilities)
    assert capabilities["roadmap"]["rendererVersion"] is None
    assert capabilities["roadmap"]["capabilities"] == []


def test_runtime_capabilities_fail_closed_when_resources_or_manifest_are_missing(
    tmp_path,
) -> None:
    missing_resources = runtime_capabilities(
        node_available=True,
        skill_root=tmp_path / "missing-skill",
    )
    missing_manifest = runtime_capabilities(
        node_available=True,
        capabilities_path=tmp_path / "missing-capabilities.json",
    )

    assert missing_resources["roadmap"]["capabilities"] == []
    assert missing_manifest["roadmap"]["capabilities"] == []


@pytest.mark.parametrize(
    "missing_resource",
    _REQUIRED_ROADMAP_RESOURCES,
)
def test_runtime_capabilities_require_every_declared_runtime_resource(
    tmp_path, missing_resource
) -> None:
    copied_root = _copy_roadmap_skill_tree(tmp_path)
    (copied_root / missing_resource).unlink()

    capabilities = runtime_capabilities(node_available=True, skill_root=copied_root)

    assert capabilities["roadmap"]["capabilities"] == []


def test_runtime_capabilities_require_roadmap_in_authoritative_builtin_manifest(
    tmp_path,
) -> None:
    copied_root = _copy_roadmap_skill_tree(tmp_path)
    manifest_path = copied_root.parent / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = [
        entry for entry in manifest["skills"] if entry.get("name") != "roadmap"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    capabilities = runtime_capabilities(node_available=True, skill_root=copied_root)

    assert capabilities["roadmap"]["rendererVersion"] is None
    assert capabilities["roadmap"]["capabilities"] == []


def test_runtime_capabilities_fail_closed_for_invalid_manifest_contract(tmp_path) -> None:
    manifest = tmp_path / "runtime-capabilities.json"
    manifest.write_text(
        json.dumps({"roadmap": {"capabilities": ["roadmap.evil"]}}),
        encoding="utf-8",
    )

    capabilities = runtime_capabilities(
        node_available=True,
        capabilities_path=manifest,
    )

    assert capabilities["roadmap"]["rendererVersion"] is None
    assert capabilities["roadmap"]["capabilities"] == []


@pytest.mark.asyncio
async def test_acp_initialize_exposes_runtime_capabilities(tmp_path, monkeypatch) -> None:
    expected = runtime_capabilities(
        node_available=True,
    )
    calls = []
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or expected,
    )
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda name: object() if name == "roadmap" else None,
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
    )

    response = await agent.initialize(SimpleNamespace(field_meta={}))

    assert response.field_meta["runtime_capabilities"] == expected
    assert calls == [
        {"skills_enabled": True, "roadmap_skill_available": True}
    ]


@pytest.mark.asyncio
async def test_acp_initialize_capabilities_fail_closed_for_empty_skill_catalog(
    tmp_path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or {"roadmap": {"capabilities": []}},
    )
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [],
        get_skill=lambda _name: None,
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
    )

    await agent.initialize(SimpleNamespace(field_meta={}))

    assert calls == [
        {"skills_enabled": True, "roadmap_skill_available": False}
    ]


@pytest.mark.asyncio
async def test_acp_initialize_capabilities_fail_closed_without_skill_loader(
    tmp_path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or {"roadmap": {"capabilities": []}},
    )
    agent = BoxACPAgent(_DummyConn(), _config(tmp_path), object(), [], "system")

    await agent.initialize(SimpleNamespace(field_meta={}))

    assert calls == [
        {"skills_enabled": True, "roadmap_skill_available": False}
    ]


@pytest.mark.asyncio
async def test_acp_initialize_does_not_wait_for_pending_skill_discovery(
    tmp_path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or {"roadmap": {"capabilities": []}},
    )
    discovery_gate = asyncio.Event()
    skill_task = asyncio.create_task(discovery_gate.wait())
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda _name: object(),
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
        skill_task=skill_task,
    )

    response = await asyncio.wait_for(
        agent.initialize(SimpleNamespace(field_meta={})),
        timeout=1.0,
    )

    assert response.field_meta["runtime_capabilities"] == {
        "roadmap": {"capabilities": []}
    }
    assert "skills" not in response.field_meta
    assert calls == [
        {"skills_enabled": True, "roadmap_skill_available": False}
    ]
    skill_task.cancel()
    await asyncio.gather(skill_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_acp_new_session_publishes_final_runtime_capabilities(
    tmp_path, monkeypatch
) -> None:
    expected = {"roadmap": {"capabilities": ["roadmap.preview"]}}
    calls = []
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or expected,
    )
    monkeypatch.setattr(
        "box_agent.acp.build_skill_runtime_context",
        lambda **_kwargs: SkillRuntimeContext(
            runtimes={
                "python": SkillRuntime("python", "available", "box_agent"),
                "node": SkillRuntime("node", "available", "box_agent"),
            }
        ),
    )
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda name: object() if name == "roadmap" else None,
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
    )

    response = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={})
    )

    assert response.field_meta["runtime_capabilities"] == expected
    assert calls == [
        {
            "skills_enabled": True,
            "node_available": True,
            "roadmap_skill_available": True,
        }
    ]


@pytest.mark.asyncio
async def test_acp_new_session_bounds_pending_skill_discovery_and_fails_closed(
    tmp_path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr("box_agent.acp._SKILL_DISCOVERY_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: calls.append(kwargs) or {"roadmap": {"capabilities": []}},
    )
    monkeypatch.setattr(
        "box_agent.acp.build_skill_runtime_context",
        lambda **_kwargs: SkillRuntimeContext(
            runtimes={
                "python": SkillRuntime("python", "available", "box_agent"),
                "node": SkillRuntime("node", "available", "box_agent"),
            }
        ),
    )
    discovery_gate = asyncio.Event()
    skill_task = asyncio.create_task(discovery_gate.wait())
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda name: object() if name == "roadmap" else None,
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
        skill_task=skill_task,
    )

    response = await asyncio.wait_for(
        agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={})),
        timeout=1.0,
    )

    assert response.sessionId
    assert "skills" not in response.field_meta
    assert calls[-1] == {
        "skills_enabled": True,
        "node_available": True,
        "roadmap_skill_available": False,
    }
    assert agent._skills_loaded is False
    assert skill_task.cancelled() is False
    skill_task.cancel()
    await asyncio.gather(skill_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_skill_wait_does_not_cancel_or_poison_discovery(
    tmp_path,
) -> None:
    discovery_gate = asyncio.Event()
    skill_task = asyncio.create_task(discovery_gate.wait())
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda name: object() if name == "roadmap" else None,
    )
    agent = BoxACPAgent(
        _DummyConn(),
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
        skill_task=skill_task,
    )

    waiting = asyncio.create_task(agent._ensure_skills_loaded())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert skill_task.cancelled() is False
    assert agent._skills_loaded is False
    discovery_gate.set()
    assert await agent._ensure_skills_loaded() is True
    assert agent._skills_loaded is True


@pytest.mark.asyncio
async def test_late_skill_discovery_publishes_session_capability_update(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("box_agent.acp._SKILL_DISCOVERY_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        "box_agent.acp.runtime_capabilities",
        lambda **kwargs: {
            "roadmap": {
                "capabilities": (
                    ["roadmap.generate.html"]
                    if kwargs["roadmap_skill_available"]
                    else []
                )
            }
        },
    )
    monkeypatch.setattr(
        "box_agent.acp.build_skill_runtime_context",
        lambda **_kwargs: SkillRuntimeContext(
            runtimes={
                "python": SkillRuntime("python", "available", "box_agent"),
                "node": SkillRuntime("node", "available", "box_agent"),
            }
        ),
    )
    discovery_gate = asyncio.Event()
    skill_task = asyncio.create_task(discovery_gate.wait())
    skill_loader = SimpleNamespace(
        maybe_reload=lambda: None,
        list_skills_metadata=lambda: [{"name": "roadmap"}],
        get_skill=lambda name: object() if name == "roadmap" else None,
    )
    conn = _DummyConn()
    agent = BoxACPAgent(
        conn,
        _config(tmp_path),
        object(),
        [],
        "system",
        skill_loader=skill_loader,
        skill_task=skill_task,
    )

    response = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={})
    )
    assert response.field_meta["runtime_capabilities"]["roadmap"]["capabilities"] == []

    discovery_gate.set()
    assert await agent._ensure_skills_loaded() is True
    state = agent._sessions[response.sessionId]
    await agent._publish_runtime_capabilities_if_changed(response.sessionId, state)

    assert conn.ext_notifications == [
        (
            "box-agent/runtime-capabilities-update",
            {
                "contract": "box-agent.runtime-capabilities-update",
                "contractVersion": 1,
                "sessionId": response.sessionId,
                "runtime_capabilities": {
                    "roadmap": {"capabilities": ["roadmap.generate.html"]}
                },
            },
        )
    ]
    assert state.runtime_capabilities == {
        "roadmap": {"capabilities": ["roadmap.generate.html"]}
    }
