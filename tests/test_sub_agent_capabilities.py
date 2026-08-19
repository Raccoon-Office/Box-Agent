from __future__ import annotations

from pathlib import Path

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


def _parse(**overrides) -> DelegationSpec | CapabilityFailure:
    values = {"task": "Inspect the repository"}
    values.update(overrides)
    return parse_delegation_spec(**values)


def _write_skill(
    root: Path,
    name: str,
    *,
    required: list[str] | None = None,
    related: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir()
    lines = ["---", f"name: {name}", f"description: {name} description"]
    if required is not None:
        lines.append(f"required_skills: [{', '.join(required)}]")
    if related is not None:
        lines.append(f"related_skills: [{', '.join(related)}]")
    if allowed_tools is not None:
        lines.append("allowed-tools:")
        lines.extend(f"  - {tool_name}" for tool_name in allowed_tools)
    lines.extend(["---", "", f"Instructions for {name}."])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def test_minimal_spec_defaults_to_all_parent_tools_and_no_skills() -> None:
    parsed = _parse(default_required_tools=("write_file", "read_file"))

    assert isinstance(parsed, DelegationSpec)
    assert parsed.title == ""
    assert parsed.skill_names == ()
    assert parsed.required_tool_names == ("read_file", "write_file")
    assert parsed.budget.to_dict() == {"max_steps": 60, "max_tool_calls": 100}
    assert parsed.defaults_applied == (
        "budget.max_steps",
        "budget.max_tool_calls",
        "required_tools",
        "skills",
        "title",
    )


def test_explicit_skills_and_required_tools_are_normalized() -> None:
    parsed = _parse(
        skills=["review", " tdd ", "review"],
        required_tools=["write_file", "read_file", "read_file"],
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.skill_names == ("review", "tdd")
    assert parsed.required_tool_names == ("read_file", "write_file")


def test_explicit_empty_required_tools_means_no_tools() -> None:
    spec = _parse(required_tools=[], default_required_tools=("read_file",))
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"read_file": NamedTool("read_file")},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.tools == {}


def test_budget_uses_configured_caps() -> None:
    parsed = _parse(
        budget={"max_steps": 99, "max_tool_calls": 99},
        general_max_steps=20,
        general_max_tool_calls=30,
    )

    assert isinstance(parsed, DelegationSpec)
    assert parsed.budget.to_dict() == {"max_steps": 20, "max_tool_calls": 30}


def test_recursive_sub_agent_tool_is_rejected() -> None:
    parsed = _parse(required_tools=["sub_agent"])

    assert isinstance(parsed, CapabilityFailure)
    assert parsed.invalid_fields == ("required_tools",)


def test_resolver_returns_exact_original_parent_tool_subset() -> None:
    read = NamedTool("read_file")
    write = NamedTool("write_file")
    spec = _parse(required_tools=["write_file"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={"read_file": read, "write_file": write},
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_tool_names == ("write_file",)
    assert result.tools["write_file"] is write
    assert result.diagnostic_payload()["requested_tools"] == ["write_file"]


def test_missing_required_tool_fails_before_child_start() -> None:
    spec = _parse(required_tools=["missing_tool"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(spec, parent_tools={})

    assert isinstance(result, CapabilityFailure)
    assert result.code == "REQUIRED_TOOL_NOT_FOUND"
    assert result.details == {"tool": "missing_tool"}


def test_skill_dependencies_add_guidance_without_changing_required_tools(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "base", allowed_tools=["read_file"])
    _write_skill(tmp_path, "related", allowed_tools=["missing_tool"])
    _write_skill(
        tmp_path,
        "selected",
        required=["base"],
        related=["related"],
        allowed_tools=["web_search"],
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    spec = _parse(skills=["selected"], required_tools=["write_file"])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={
            "read_file": NamedTool("read_file"),
            "write_file": NamedTool("write_file"),
        },
        skill_loader=loader,
    )

    assert isinstance(result, ResolvedCapabilityBundle)
    assert result.resolved_skill_names == ("base", "selected")
    assert result.resolved_tool_names == ("write_file",)


def test_selected_skill_requires_a_live_provider() -> None:
    spec = _parse(skills=["selected"], required_tools=[])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(spec, parent_tools={}, skill_loader=None)

    assert isinstance(result, CapabilityFailure)
    assert result.code == "SKILL_PROVIDER_UNAVAILABLE"


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
