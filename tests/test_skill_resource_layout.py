"""Standalone Skill resources retain discovery and protection outside Python modules."""

import json
import os
import runpy
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.skill_paths import BUILTIN_SKILLS_ENV, resolve_builtin_skills_dir
from box_agent.tools import safety
from box_agent.tools.file.read_tool import ReadTool
from box_agent.tools.file_tools import WriteTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine

_EXECUTION_PYTHON = sys.executable


@pytest.fixture
def release(tmp_path, monkeypatch):
    runtime = tmp_path / "release with spaces"
    package = runtime / "bin/_internal/box_agent"
    package.mkdir(parents=True)
    (runtime / "manifest.json").write_text('{"name":"box-agent"}', encoding="utf-8")
    executable = runtime / "bin/box-agent-acp"
    executable.write_text("fixture", encoding="utf-8")
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(Config, "get_package_dir", classmethod(lambda cls: package))
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    monkeypatch.delenv(BUILTIN_SKILLS_ENV, raising=False)
    return runtime, package, profile


def config_for(profile, skills_dir="./skills"):
    return Config(
        llm=LLMConfig(api_key="synthetic-only"),
        agent=AgentConfig(workspace_dir=str(profile / "workspace"), enable_memory=False),
        tools=ToolsConfig(
            enable_skills=True, skills_dir=skills_dir, enable_bash=False,
            enable_todo=False, enable_plan=False, enable_sub_agent=False, enable_mcp=False,
        ),
    )


def write_method(root, name="fixture", description="relocated method"):
    method = root / name
    method.mkdir(parents=True)
    (method / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n"
        "Read `references/example.txt`.\n", encoding="utf-8",
    )
    (method / "references").mkdir()
    (method / "references/example.txt").write_text("RESOURCE_READ_OK", encoding="utf-8")
    return method


def test_default_release_skills_are_authoritative_even_when_missing(release):
    runtime, package, profile = release
    legacy = package / "skills"
    legacy.mkdir()
    config = config_for(profile)
    assert resolve_builtin_skills_dir(config) == runtime / "skills"
    assert not (runtime / "skills").exists()
    assert config.tools.skills_dir == "./skills"


@pytest.mark.parametrize("host_override", [False, True])
def test_explicit_release_skill_source_keeps_host_precedence(release, monkeypatch, host_override):
    runtime, _, profile = release
    custom = profile / "downloaded-methods"
    custom.mkdir()
    config = config_for(profile, str(custom))
    expected = custom
    if host_override:
        expected = profile / "host-methods"
        expected.mkdir()
        monkeypatch.setenv(BUILTIN_SKILLS_ENV, str(expected))
    assert resolve_builtin_skills_dir(config) == expected
    assert config.tools.skills_dir == str(custom)
    # The real bundled directory is allowed outside the explicit profile.
    config.tools.skills_dir = str(runtime / "skills")
    if host_override:
        monkeypatch.delenv(BUILTIN_SKILLS_ENV)
    assert resolve_builtin_skills_dir(config) == runtime / "skills"


@pytest.mark.parametrize("source", ["config", "environment"])
def test_explicit_bundled_subtree_remains_available_with_profile(release, monkeypatch, source):
    runtime, _, profile = release
    subtree = runtime / "skills" / "focused-methods"
    subtree.mkdir(parents=True)
    config = config_for(profile, str(subtree) if source == "config" else "./skills")
    if source == "environment":
        monkeypatch.setenv(BUILTIN_SKILLS_ENV, str(subtree))
    assert resolve_builtin_skills_dir(config) == subtree


@pytest.mark.parametrize("source", ["config", "environment"])
def test_bundled_subtree_symlink_cannot_escape_profile(release, monkeypatch, source):
    runtime, _, profile = release
    outside = profile.parent / "foreign-methods"
    outside.mkdir()
    link = runtime / "skills" / "linked-methods"
    link.parent.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this platform")
    config = config_for(profile, str(link) if source == "config" else "./skills")
    if source == "environment":
        monkeypatch.setenv(BUILTIN_SKILLS_ENV, str(link))
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        resolve_builtin_skills_dir(config)


@pytest.mark.parametrize("profile_enabled", [False, True])
def test_relative_bundled_subtree_follows_new_release_root(release, monkeypatch, profile_enabled):
    runtime, _, profile = release
    subtree = runtime / "skills" / "document-skills"
    subtree.mkdir(parents=True)
    monkeypatch.chdir(profile)
    if not profile_enabled:
        monkeypatch.delenv("BOX_AGENT_HOME")
    config = config_for(profile, "skills/document-skills")
    assert resolve_builtin_skills_dir(config) == subtree
    if not profile_enabled:
        cwd_subtree = profile / "skills" / "document-skills"
        cwd_subtree.mkdir(parents=True)
        assert resolve_builtin_skills_dir(config) == cwd_subtree


def test_nondefault_relative_skill_source_keeps_package_lookup(release):
    runtime, package, profile = release
    (runtime / "skills").mkdir()
    (package / "alternate-methods").mkdir()
    assert resolve_builtin_skills_dir(config_for(profile, "alternate-methods")) == package / "alternate-methods"


@pytest.mark.parametrize("source", ["config", "environment"])
def test_unrelated_absolute_skill_source_still_cannot_escape_profile(release, monkeypatch, source):
    _, _, profile = release
    outside = profile.parent / "unrelated-methods"
    outside.mkdir()
    config = config_for(profile, str(outside) if source == "config" else "./skills")
    if source == "environment":
        monkeypatch.setenv(BUILTIN_SKILLS_ENV, str(outside))
    with pytest.raises(ValueError, match="outside BOX_AGENT_HOME"):
        resolve_builtin_skills_dir(config)


def test_source_skill_lookup_retains_cwd_and_profile_semantics(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(BUILTIN_SKILLS_ENV, raising=False)
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    package = tmp_path / "package"
    (package / "skills").mkdir(parents=True)
    cwd = tmp_path / "working"
    (cwd / "skills").mkdir(parents=True)
    monkeypatch.setattr(Config, "get_package_dir", classmethod(lambda cls: package))
    monkeypatch.chdir(cwd)
    config = SimpleNamespace(tools=SimpleNamespace(skills_dir="./skills"))
    assert resolve_builtin_skills_dir(config) == cwd / "skills"
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    assert resolve_builtin_skills_dir(config) == package / "skills"


@pytest.mark.asyncio
@pytest.mark.parametrize("defer", [False, True])
async def test_complete_skill_setup_reads_sibling_resources_with_all_sources(release, defer):
    from box_agent.tools.setup import await_skill_discovery, initialize_base_tools
    from box_agent.tools.skill_tool import GetSkillTool

    runtime, _, profile = release
    root = runtime / "skills"
    method = write_method(root)
    (root / "_manifest.json").write_text(json.dumps({
        "skills": [{"name": "fixture", "path": "fixture/SKILL.md"}],
    }), encoding="utf-8")
    write_method(profile / "skills", "user-method")
    write_method(profile / "connectors/skills", "connector-method")
    _, loader, mcp, task = await initialize_base_tools(
        config_for(profile), output=lambda *_: None, defer_skills=defer,
    )
    assert mcp is None
    await await_skill_discovery(task)
    assert loader is not None
    assert {"fixture", "user-method", "connector-method"} <= set(loader.loaded_skills)
    result = await GetSkillTool(loader).execute(skill_name="fixture")
    assert result.success, result.error
    assert result.raw_output["skill_reference"]["path"] == str(method / "SKILL.md")
    assert str(method / "references/example.txt") in result.content
    workspace = profile / "workspace"
    workspace.mkdir(exist_ok=True)
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace)
    read = await ReadTool(str(workspace), permission_engine=engine).execute(str(method / "references/example.txt"))
    assert read.success and "RESOURCE_READ_OK" in read.content


@pytest.mark.parametrize("shell_style", ["posix", "powershell"])
def test_pptx_sync_guard_trusts_relocated_builtin_and_rejects_stale_copies(release, shell_style):
    from box_agent.skill_paths import managed_builtin_skills_dir
    from box_agent.tools import pptx_safety
    from box_agent.tools.skill_loader import SkillLoader

    runtime, package, profile = release
    root = resolve_builtin_skills_dir(config_for(profile))
    method = write_method(root / "document-skills", "pptx")
    script = method / "scripts/sync_image_manifest_status.js"
    script.parent.mkdir()
    script.write_text("console.log('fixture');", encoding="utf-8")
    (root / "_manifest.json").write_text(json.dumps({
        "skills": [{"name": "pptx", "path": "document-skills/pptx/SKILL.md"}],
    }), encoding="utf-8")
    loader = SkillLoader(sources=[(root, "builtin")])
    loader.discover_skills()
    # Default bundled Skills have neither a managed manifest nor an expert binding.
    assert managed_builtin_skills_dir(config_for(profile)) is None
    assert loader.get_skill("pptx").source == "builtin"
    expert = loader.get_bound_expert_resource("pptx", "scripts/sync_image_manifest_status.js")
    assert expert is None
    guard = runpy.run_path(pptx_safety.__file__)["detect_pptx_image_status_command_bypass"]
    workspace = profile / "workspace"
    workspace.mkdir()
    old_script = package / "skills/document-skills/pptx/scripts/sync_image_manifest_status.js"
    old_script.parent.mkdir(parents=True)
    shutil.copyfile(script, old_script)
    copied_script = workspace / script.name
    shutil.copyfile(script, copied_script)

    def check(path):
        command = (
            '& "$env:BOX_AGENT_NODE" ' + "'" + str(path).replace("'", "''")
            + "' 'assets/generated/manifest.json'"
            if shell_style == "powershell"
            else '"$BOX_AGENT_NODE" ' + shlex.quote(str(path)) + " assets/generated/manifest.json"
        )
        return guard(
            command, workspace_dir=str(workspace), runtime_env=None,
            shell_style=shell_style, expert_sync_script=expert, builtin_skills_dir=None,
        )

    assert check(script) is None
    for untrusted in (old_script, copied_script):
        assert "PPTX_IMAGE_STATUS_SCRIPT_IDENTITY" in check(untrusted)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting and execution probe")
@pytest.mark.asyncio
async def test_bash_executes_relocated_skill_script_with_sibling_reference(release):
    from box_agent.tools.bash_tool import BashTool

    runtime, _, profile = release
    method = write_method(runtime / "skills")
    scripts = method / "scripts"
    scripts.mkdir()
    helper = scripts / "read reference.py"
    helper.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "content = (Path(__file__).resolve().parents[1] / 'references/example.txt').read_text()\n"
        "Path(sys.argv[1]).write_text(content)\n"
        "print(content)\n",
        encoding="utf-8",
    )
    workspace = profile / "workspace"
    workspace.mkdir()
    output = workspace / "result.txt"
    policy = CapabilityPolicy(
        session_workspace_root=str(workspace),
        allowed_directories=(str(Path(_EXECUTION_PYTHON).resolve().parent),),
    )
    engine = PermissionEngine(policy, workspace)
    tool = BashTool(
        workspace_dir=str(workspace), allow_full_access=False,
        non_interactive=True, permission_engine=engine,
    )
    result = await tool.execute(
        command=" ".join(shlex.quote(str(value)) for value in (_EXECUTION_PYTHON, helper, output)),
        timeout=15,
    )
    assert result.success, result.error
    assert not result.permission_request
    assert "RESOURCE_READ_OK" in result.content
    assert output.read_text() == "RESOURCE_READ_OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("scope", ["session_workspace", "user_home", "full_access"])
async def test_release_builtin_and_managed_roots_stay_read_only(release, monkeypatch, managed, scope):
    runtime, _, profile = release
    bundled = write_method(runtime / "skills")
    managed_root = profile / "host-methods" if managed else None
    if managed_root:
        write_method(managed_root)
    workspace = profile / "workspace"
    workspace.mkdir()
    # Importing safety in a real frozen process binds this same release root.
    isolated = runpy.run_path(safety.__file__, run_name="box_agent.tools._safety_resource_probe")
    assert isolated["BUILTIN_SKILLS_ROOT"] == runtime / "skills"
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", isolated["BUILTIN_SKILLS_ROOT"])
    policy = CapabilityPolicy(filesystem_scope=scope, session_workspace_root=str(workspace),
                              builtin_skills_dir=managed_root)
    engine = PermissionEngine(policy, workspace)
    roots = [bundled]
    if managed_root:
        roots.append(managed_root / "fixture")
    for method in roots:
        target = method / "references/example.txt"
        assert engine.check("filesystem.read", {"path": str(target)}, tool_name="bash").allowed
        denied = engine.check("filesystem.write", {"path": str(target)})
        assert not denied.allowed and "BUILTIN_SKILL_READ_ONLY" in denied.reason
        result = await WriteTool(str(workspace), allow_full_access=True,
                                 permission_engine=engine, builtin_skills_dir=managed_root).execute(
                                     str(target), "must not change")
        assert not result.success and "BUILTIN_SKILL_READ_ONLY" in result.error
        assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(
            f"printf changed > '{target}'", workspace, managed_root)
        assert target.read_text() == "RESOURCE_READ_OK"
    if scope == "session_workspace":
        outside = profile.parent / "unrelated.txt"
        outside.write_text("outside")
        assert not engine.check("filesystem.read", {"path": str(outside)}).allowed


def test_roadmap_schema_reads_new_resource_tree(release):
    from box_agent import roadmap_artifacts

    runtime, _, _ = release
    refs = runtime / "skills/roadmap/references"
    refs.mkdir(parents=True)
    (refs / "roadmap-draft.schema.json").write_text(json.dumps({
        "$id": "urn:fixture:draft", "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    }))
    (refs / "roadmap-spec.schema.json").write_text(json.dumps({
        "$id": "urn:fixture:spec", "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "required": ["fixture"], "properties": {"fixture": {"const": "relocated"}},
    }))
    roadmap_artifacts._roadmap_spec_validator.cache_clear()
    try:
        validator = roadmap_artifacts._roadmap_spec_validator()
        assert validator.is_valid({"fixture": "relocated"})
        assert not validator.is_valid({})
    finally:
        roadmap_artifacts._roadmap_spec_validator.cache_clear()


def test_cjk_watermark_font_is_loaded_from_release_skills(release):
    from box_agent.tools import watermark

    runtime, _, _ = release
    source = Path(watermark.__file__).resolve().parents[1] / "skills/canvas-design/canvas-fonts/NotoSansSC-Regular.otf"
    font = runtime / "skills/canvas-design/canvas-fonts/NotoSansSC-Regular.otf"
    font.parent.mkdir(parents=True)
    shutil.copyfile(source, font)
    isolated = runpy.run_path(watermark.__file__)
    assert isolated["_BUNDLED_FONT"] == font
    _, name = isolated["_resolve_font"](40)
    assert name == font.name
