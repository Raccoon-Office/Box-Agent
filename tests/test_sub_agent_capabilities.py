from __future__ import annotations

from pathlib import Path

import pytest

from box_agent import skill_dependencies
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.sub_agent_capabilities import (
    CapabilityFailure,
    CapabilityResolver,
    DelegationSpec,
    ResolvedCapabilityBundle,
    parse_delegation_spec,
)


class NamedTool(Tool):
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content=self._name)


class NamedMcpTool(NamedTool):
    def __init__(self, name: str, server_name: str):
        super().__init__(name)
        self.server_name = server_name


def _parse(**overrides) -> DelegationSpec | CapabilityFailure:
    values = {"task": "Inspect the repository"}
    values.update(overrides)
    return parse_delegation_spec(**values)


def _write_skill(
    root: Path,
    name: str,
    *,
    required: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir()
    lines = ["---", f"name: {name}", f"description: {name} description"]
    if required is not None:
        lines.append(f"required_skills: [{', '.join(required)}]")
    if allowed_tools is not None:
        lines.append("allowed-tools:")
        lines.extend(f"  - {tool_name}" for tool_name in allowed_tools)
    lines.extend(["---", "", f"Instructions for {name}."])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def test_minimal_spec_defaults_to_trusted_local_read_tools_only() -> None:
    parsed = _parse(
        default_required_tools=(
            "write_file",
            "grep",
            "glob",
            "search_files",
            "read_file",
            "query_jsonl",
        )
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.required_tools == (
        "glob",
        "grep",
        "query_jsonl",
        "read_file",
        "search_files",
    )
    assert parsed.skill_names == ()
    assert parsed.files == ()
    assert parsed.strategy == "general_loop"
    assert parsed.constraints.to_dict() == {
        "read_only": True,
        "network": False,
        "write_scope": None,
        "external_side_effect": False,
    }
    assert parsed.budget.to_dict() == {"max_steps": 80, "max_tool_calls": 48}


def test_files_infer_bounded_batch_strategy_and_read_file() -> None:
    parsed = _parse(files=["b.md", "a.md", "a.md"])

    assert isinstance(parsed, DelegationSpec)
    assert parsed.strategy == "batch_files"
    assert parsed.files == ("a.md", "b.md")
    assert parsed.required_tools == ("read_file",)
    assert parsed.budget.to_dict() == {"max_steps": 1, "max_tool_calls": 2}


@pytest.mark.parametrize("files", [None, ["input.md"]])
@pytest.mark.parametrize("skills", [None, ["review"]])
def test_defaults_include_available_search_and_only_assigned_skill_tools(
    files, skills
) -> None:
    parsed = _parse(
        files=files,
        skills=skills,
        default_required_tools=(
            "read_file",
            "search_files",
            "grep",
            "glob",
            "query_jsonl",
            "web_search",
            "web_extract",
            "get_skill",
            "list_skills",
            "write_file",
            "bash",
            "execute_code",
            "inspect_images",
            "generate_image",
            "unknown_mcp",
        ),
    )

    assert isinstance(parsed, DelegationSpec)
    expected = {
        "read_file",
        "search_files",
        "grep",
        "glob",
        "query_jsonl",
        "web_search",
        "web_extract",
    }
    if skills:
        expected.update({"get_skill", "list_skills"})
    assert set(parsed.required_tools) == expected
    assert parsed.strategy == "general_loop"
    assert parsed.constraints.read_only is True
    assert parsed.constraints.network is True


@pytest.mark.parametrize(
    "budget,host_steps",
    [({"max_steps": 1}, 80), (None, 1), ({"max_steps": 5}, 1)],
)
def test_one_step_file_summary_keeps_batch_compatibility(budget, host_steps) -> None:
    parsed = _parse(
        files=["a.md", "b.md"],
        budget=budget,
        general_max_steps=host_steps,
        default_required_tools=("read_file", "search_files", "web_search"),
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.strategy == "batch_files"
    assert parsed.required_tools == ("read_file",)
    assert parsed.budget.to_dict() == {"max_steps": 1, "max_tool_calls": 2}


@pytest.mark.parametrize("steps", [True, 0, -1, "1"])
def test_invalid_one_step_budget_is_not_a_batch_shortcut(steps) -> None:
    parsed = _parse(
        files=["a.md"],
        budget={"max_steps": steps},
        default_required_tools=("read_file", "search_files"),
        general_max_steps=1,
    )
    assert isinstance(parsed, CapabilityFailure)
    assert "budget.max_steps" in parsed.invalid_fields


def test_one_step_file_defaults_keep_scope_and_explicit_tools_authoritative() -> None:
    common = dict(files=["a.md"], budget={"max_steps": 1},
                  default_required_tools=("read_file", "write_file", "web_search"))
    writable = _parse(**common, write_scope=["out.md"])
    explicit = _parse(**common, required_tools=["web_search"])
    empty = _parse(**common, required_tools=[])
    insufficient = _parse(files=["a.md", "b.md"],
                          budget={"max_steps": 1, "max_tool_calls": 1})

    assert isinstance(writable, DelegationSpec)
    assert writable.strategy == "general_loop"
    assert "write_file" in writable.required_tools
    assert isinstance(explicit, DelegationSpec)
    assert explicit.required_tools == ("web_search",)
    assert explicit.strategy == "general_loop"
    assert isinstance(empty, DelegationSpec)
    assert empty.required_tools == ()
    assert isinstance(insufficient, CapabilityFailure)
    assert "budget.max_tool_calls" in insufficient.invalid_fields


@pytest.mark.parametrize("files", [None, ["plan.md"]])
def test_output_scope_defaults_to_available_file_tools(files) -> None:
    parsed = _parse(
        files=files,
        write_scope=["slides/01.html"],
        default_required_tools=(
            "read_file", "search_files", "query_jsonl",
            "write_file", "edit_file", "append_file", "bash", "web_search",
        ),
        budget={"max_steps": 7, "max_tool_calls": 9},
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.required_tools == (
        "append_file", "edit_file", "query_jsonl", "read_file", "search_files", "web_search", "write_file",
    )
    assert parsed.strategy == "general_loop"
    assert parsed.constraints.read_only is False
    assert parsed.constraints.network is True
    assert parsed.constraints.write_scope == ("slides/01.html",)
    assert parsed.budget.to_dict() == {"max_steps": 7, "max_tool_calls": 9}


def test_output_scope_does_not_invent_unavailable_write_tools() -> None:
    parsed = _parse(
        write_scope=["result.md"],
        default_required_tools=("read_file", "edit_file"),
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.required_tools == ("edit_file", "read_file")


@pytest.mark.parametrize("tools", [[], ["read_file"]])
def test_explicit_readonly_scope_conflict_identifies_tool_selection(tools) -> None:
    parsed = _parse(
        required_tools=tools,
        write_scope=["out.md"],
        default_required_tools=("read_file", "write_file"),
    )

    assert isinstance(parsed, CapabilityFailure)
    assert parsed.code == "INVALID_DELEGATION_SPEC"
    correction = parsed.to_dict()["field_corrections"]["write_scope"]
    assert "required_tools" in correction["message"]
    assert "write_file" in parsed.to_dict()["minimal_valid_example"]["required_tools"]


@pytest.mark.parametrize("scope", [[], [" "], [123]])
def test_invalid_output_scope_is_rejected_with_default_write_tools(scope) -> None:
    parsed = _parse(
        write_scope=scope,
        default_required_tools=("read_file", "write_file"),
    )

    assert isinstance(parsed, CapabilityFailure)
    assert parsed.code == "INVALID_DELEGATION_SPEC"
    assert any(field.startswith("write_scope") for field in parsed.invalid_fields)


def test_explicit_lists_are_flat_normalized_and_can_be_empty() -> None:
    parsed = _parse(
        skills=["review", " tdd ", "review"],
        required_tools=["web_search", "read_file", "read_file"],
    )
    empty = _parse(required_tools=[])

    assert isinstance(parsed, DelegationSpec)
    assert parsed.skill_names == ("review", "tdd")
    assert parsed.required_tools == ("read_file", "web_search")
    assert parsed.constraints.network is True
    assert isinstance(empty, DelegationSpec)
    assert empty.required_tools == ()


def test_path_write_requires_exact_scope_and_scope_requires_path_write() -> None:
    missing_scope = _parse(required_tools=["write_file"])
    unused_scope = _parse(required_tools=["read_file"], write_scope=["out.md"])
    scoped = _parse(
        required_tools=["write_file"],
        write_scope=["research/result.md"],
    )

    assert isinstance(missing_scope, CapabilityFailure)
    assert missing_scope.invalid_fields == ("write_scope",)
    assert isinstance(unused_scope, CapabilityFailure)
    assert unused_scope.invalid_fields == ("write_scope",)
    assert isinstance(scoped, DelegationSpec)
    assert scoped.constraints.read_only is False
    assert scoped.constraints.write_scope == ("research/result.md",)


def test_files_keep_general_loop_when_additional_tools_are_requested() -> None:
    search = _parse(files=["a.md"], required_tools=["search_files"])
    writable = _parse(
        files=["a.md"],
        required_tools=[
            "bash",
            "read_file",
            "inspect_images",
            "web_search",
            "write_file",
        ],
        write_scope=["out.md"],
    )
    scoped = _parse(
        files=["a.md"],
        required_tools=["read_file"],
        write_scope=["out.md"],
    )

    assert isinstance(search, DelegationSpec)
    assert search.strategy == "general_loop"
    assert search.files == ("a.md",)
    assert isinstance(writable, DelegationSpec)
    assert writable.strategy == "general_loop"
    assert writable.constraints.write_scope == ("out.md",)
    assert writable.constraints.network is True
    assert isinstance(scoped, CapabilityFailure)
    assert "write_scope" in scoped.invalid_fields


def test_budget_uses_configured_caps_and_rejects_serialized_json() -> None:
    parsed = _parse(
        budget={"max_steps": 99, "max_tool_calls": 99},
        general_max_steps=20,
        general_max_tool_calls=30,
    )
    serialized = _parse(budget='{"max_steps": 12}')

    assert isinstance(parsed, DelegationSpec)
    assert parsed.budget.to_dict() == {"max_steps": 20, "max_tool_calls": 30}
    assert isinstance(serialized, CapabilityFailure)
    assert serialized.invalid_fields == ("budget",)


def test_recursive_sub_agent_tool_is_rejected() -> None:
    parsed = _parse(required_tools=["sub_agent"])

    assert isinstance(parsed, CapabilityFailure)
    assert parsed.invalid_fields == ("required_tools",)


def test_resolver_returns_exact_scoped_parent_tool_subset() -> None:
    read = NamedTool("read_file")
    write = NamedTool("write_file")
    spec = _parse(
        required_tools=["write_file"],
        write_scope=["out.md"],
    )
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"read_file": read, "write_file": write},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_tool_names == ("write_file",)
    assert result.tools["write_file"] is write
    assert result.diagnostic_payload()["requested_tools"] == ["write_file"]


def test_resolver_treats_ripgrep_tools_as_read_only_capabilities() -> None:
    spec = _parse(required_tools=["grep", "glob"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"grep": NamedTool("grep"), "glob": NamedTool("glob")},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_tool_names == ("glob", "grep")
    assert result.spec.constraints.read_only is True
    assert result.spec.constraints.network is False


def test_missing_required_tool_distinguishes_loading_from_not_found() -> None:
    spec = _parse(required_tools=["mcp_future_tool"])
    assert isinstance(spec, DelegationSpec)

    loading = CapabilityResolver().resolve(
        spec,
        parent_tools={},
        capability_state="loading",
    )
    ready = CapabilityResolver().resolve(
        spec,
        parent_tools={},
        capability_state="ready",
    )

    assert isinstance(loading, CapabilityFailure)
    assert loading.code == "REQUIRED_TOOL_NOT_READY"
    assert loading.pending_source == "mcp"
    assert isinstance(ready, CapabilityFailure)
    assert ready.code == "REQUIRED_TOOL_NOT_FOUND"


def test_derived_policy_allows_trusted_network_and_permission_gated_bash() -> None:
    web_spec = _parse(required_tools=["web_search"])
    bash_spec = _parse(required_tools=["bash"])
    assert isinstance(web_spec, DelegationSpec)
    assert isinstance(bash_spec, DelegationSpec)
    assert bash_spec.constraints.read_only is False
    assert bash_spec.constraints.network is True

    web_result = CapabilityResolver().resolve(
        web_spec,
        parent_tools={"web_search": NamedTool("web_search")},
    )
    bash_without_broker = CapabilityResolver().resolve(
        bash_spec,
        parent_tools={"bash": NamedTool("bash")},
    )
    bash_with_broker = CapabilityResolver().resolve(
        bash_spec,
        parent_tools={"bash": NamedTool("bash")},
        permission_negotiator_available=True,
    )

    assert isinstance(web_result, ResolvedCapabilityBundle)
    assert isinstance(bash_without_broker, CapabilityFailure)
    assert (
        bash_without_broker.details["denied_reason"]
        == "permission_negotiator_unavailable"
    )
    assert isinstance(bash_with_broker, ResolvedCapabilityBundle)


def test_explicit_web_extract_derives_network_capability() -> None:
    spec = _parse(required_tools=["web_extract"])
    assert isinstance(spec, DelegationSpec)
    assert spec.constraints.network is True

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"web_extract": NamedTool("web_extract")},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_tool_names == ("web_extract",)


def test_unknown_mcp_tools_fail_closed_even_when_explicitly_selected() -> None:
    spec = _parse(required_tools=["mcp_custom_write"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"mcp_custom_write": NamedMcpTool("mcp_custom_write", "custom")},
    )

    assert isinstance(result, CapabilityFailure)
    assert result.details["denied_reason"] == "unknown_capability_metadata"


_PLAYWRIGHT_DELEGABLE_MANAGED_TOOLS = (
    "managed_browser_click",
    "managed_browser_close",
    "managed_browser_drag",
    "managed_browser_drop",
    "managed_browser_evaluate",
    "managed_browser_fill_form",
    "managed_browser_handle_dialog",
    "managed_browser_hover",
    "managed_browser_navigate",
    "managed_browser_network_request",
    "managed_browser_press_key",
    "managed_browser_resize",
    "managed_browser_run_code",
    "managed_browser_select_option",
    "managed_browser_tabs",
    "managed_browser_take_screenshot",
    "managed_browser_type",
)
_PLAYWRIGHT_BLOCKED_MANAGED_TOOLS = (
    "managed_browser_file_upload",
)


def test_playwright_named_run_code_is_delegable_like_navigate() -> None:
    navigate = NamedMcpTool("managed_browser_navigate", "playwright")
    run_code = NamedMcpTool("managed_browser_run_code", "playwright")
    navigate_spec = _parse(required_tools=["managed_browser_navigate"])
    run_code_spec = _parse(required_tools=["managed_browser_run_code"])
    assert isinstance(navigate_spec, DelegationSpec)
    assert isinstance(run_code_spec, DelegationSpec)
    assert run_code_spec.constraints.network is True
    assert run_code_spec.constraints.external_side_effect is False

    navigate_result = CapabilityResolver().resolve(
        navigate_spec,
        parent_tools={"managed_browser_navigate": navigate},
    )
    run_code_result = CapabilityResolver().resolve(
        run_code_spec,
        parent_tools={"managed_browser_run_code": run_code},
    )

    assert isinstance(navigate_result, ResolvedCapabilityBundle)
    assert isinstance(run_code_result, ResolvedCapabilityBundle)
    assert run_code_result.resolved_tool_names == ("managed_browser_run_code",)


@pytest.mark.parametrize("tool_name", _PLAYWRIGHT_DELEGABLE_MANAGED_TOOLS)
def test_explicit_playwright_page_interact_tools_are_delegable(tool_name: str) -> None:
    spec = _parse(required_tools=[tool_name])
    assert isinstance(spec, DelegationSpec)
    assert spec.constraints.network is True
    assert spec.constraints.external_side_effect is False

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={tool_name: NamedMcpTool(tool_name, "playwright")},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_tool_names == (tool_name,)


@pytest.mark.parametrize("tool_name", _PLAYWRIGHT_BLOCKED_MANAGED_TOOLS)
def test_playwright_file_upload_remains_denied(tool_name: str) -> None:
    spec = _parse(required_tools=[tool_name])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={tool_name: NamedMcpTool(tool_name, "playwright")},
    )

    assert isinstance(result, CapabilityFailure)
    assert result.details["denied_reason"] in {
        "network_disabled",
        "external_side_effect_disabled",
    }


def test_playwright_interact_tools_are_not_default_child_tools() -> None:
    parsed = _parse(
        default_required_tools=(
            "read_file",
            *_PLAYWRIGHT_DELEGABLE_MANAGED_TOOLS,
            *_PLAYWRIGHT_BLOCKED_MANAGED_TOOLS,
        )
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.required_tools == ("read_file",)


def test_selected_skill_adds_guidance_without_adding_tools(tmp_path: Path) -> None:
    _write_skill(tmp_path, "base", allowed_tools=["read_file"])
    _write_skill(
        tmp_path,
        "selected",
        required=["base"],
        allowed_tools=["web_search"],
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    spec = _parse(skills=["selected"], required_tools=["read_file"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={
            "read_file": NamedTool("read_file"),
            "web_search": NamedTool("web_search"),
        },
        skill_loader=loader,
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_skill_names == ("base", "selected")
    assert result.resolved_tool_names == ("read_file",)


def test_selected_skill_requires_live_provider() -> None:
    spec = _parse(skills=["selected"], required_tools=[])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(spec, parent_tools={}, skill_loader=None)

    assert isinstance(result, CapabilityFailure)
    assert result.code == "SKILL_PROVIDER_UNAVAILABLE"


def test_shared_skill_dependency_failure_keeps_child_failure_contract(tmp_path, monkeypatch):
    _write_skill(tmp_path, "selected")
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    spec = _parse(skills=["selected"], required_tools=[])
    failure = skill_dependencies.SkillDependencyError(
        "SKILL_NOT_FOUND", "Required Skill 'dependency' was not found.", {"skill": "dependency"}
    )

    def reject_dependency(actual_loader, names):
        assert actual_loader is loader
        assert names == ("selected",)
        raise failure

    monkeypatch.setattr(skill_dependencies, "resolve_required_skills", reject_dependency)
    result = CapabilityResolver().resolve(spec, parent_tools={}, skill_loader=loader)

    assert isinstance(result, CapabilityFailure)
    assert result.code == failure.code
    assert result.message == failure.message
    assert result.details == failure.details
    assert result.retryable is False


def test_skill_dependency_cycle_fails_deterministically(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", required=["beta"])
    _write_skill(tmp_path, "beta", required=["alpha"])
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    spec = _parse(skills=["alpha"], required_tools=[])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(spec, parent_tools={}, skill_loader=loader)

    assert isinstance(result, CapabilityFailure)
    assert result.code == "SKILL_DEPENDENCY_CYCLE"
    assert result.details["cycle"] == ["alpha", "beta", "alpha"]


def test_disabled_and_broken_skills_fail_before_child_start(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "disabled")
    broken_dir = skills_dir / "broken"
    broken_dir.mkdir()
    (broken_dir / "SKILL.md").write_text("not valid frontmatter", encoding="utf-8")
    settings = tmp_path / "skill-settings.json"
    settings.write_text('{"disabledSkillNames":["disabled"]}', encoding="utf-8")
    loader = SkillLoader(skills_dir, skill_settings_path=settings)
    loader.discover_skills()

    for skill_name, code in (("disabled", "SKILL_DISABLED"), ("broken", "SKILL_BROKEN")):
        spec = _parse(skills=[skill_name], required_tools=[])
        assert isinstance(spec, DelegationSpec)
        result = CapabilityResolver().resolve(spec, parent_tools={}, skill_loader=loader)
        assert isinstance(result, CapabilityFailure)
        assert result.code == code
