"""Expert guidance stays complete through parsing, rendering and ACP assembly."""
import copy
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from box_agent import expert_packages
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.context_input import DefaultContextEngine
from box_agent.experts import ExpertProfile, ExpertSessionContext, ExpertTeamProfile
from box_agent.schema import Message
from box_agent.tools.engine.preparation import prepare_tools
from tests.expert_package_helpers import archive_bytes, reference


def body(marker):
    return "# Method\n\n" + ("Keep the complete instruction. " * 90) + (
        "\n\n```python\nif ready:\n    publish_result()  # indentation stays\n```\n\n" + marker
    )


@pytest.mark.parametrize("field,attr", [
    ("instructions", "instructions"), ("visibleRules", "visible_rules"),
    ("internalRules", "internal_rules"), ("constraints", "constraints"),
])
def test_expert_rule_lists_keep_complete_markdown_and_all_entries(field, attr):
    rules = [body(f"EXPERT_RULE_TAIL_{i}") for i in range(20)]
    meta = {"id": "writer", "name": "Writer", field: [None, " \n", *rules, rules[-1]]}
    original = copy.deepcopy(meta)
    profile = ExpertProfile.from_meta(meta)
    assert getattr(profile, attr) == rules
    rendered = profile.render_prompt()
    assert all(rule in rendered for rule in rules)
    assert meta == original


@pytest.mark.parametrize("field,attr", [
    ("workflow", "workflow"), ("visibleRules", "visible_rules"),
    ("internalRules", "internal_rules"), ("qualityGates", "quality_gates"),
    ("blockedConditions", "blocked_conditions"), ("reviewRules", "review_rules"),
])
def test_team_rule_lists_keep_complete_markdown_and_all_entries(field, attr):
    rules = [body(f"TEAM_RULE_TAIL_{i}") for i in range(20)]
    profile = ExpertTeamProfile.from_meta({"id": "team", "name": "Team", field: rules})
    assert getattr(profile, attr) == rules
    assert all(rule in profile.render_prompt() for rule in rules)


@pytest.mark.parametrize("kind", ["expert", "team"])
def test_string_rules_keep_the_whole_markdown_block(kind):
    rule = "    indented first line\r\n\r\n- parent\r\n    - child  \r\n\r\n```text\r\n  source\r\n```"
    field = "instructions" if kind == "expert" else "workflow"
    cls = ExpertProfile if kind == "expert" else ExpertTeamProfile
    profile = cls.from_meta({"id": "selected", "name": "Selected", field: rule})
    normalized = rule.replace("\r\n", "\n")
    assert getattr(profile, field) == [normalized]
    assert normalized in profile.render_prompt()


def test_freeform_roles_and_output_requirements_preserve_their_tail():
    text = body("FREEFORM_TAIL")
    expert = ExpertProfile.from_meta({"id": "one", "name": "One", "role": text,
        "description": text, "starterPrompt": text, "outputFormat": text})
    team = ExpertTeamProfile.from_meta({"id": "team", "name": "Team", "description": text,
        "starterPrompt": text, "teamPersona": text, "outputFormat": text})
    for attr in ("role", "description", "starter_prompt", "output_format"):
        assert getattr(expert, attr) == text
    for attr in ("description", "starter_prompt", "team_persona", "output_format"):
        assert getattr(team, attr) == text


def test_member_and_orchestration_guidance_preserves_all_rule_text():
    text = body("MEMBER_AND_ORCHESTRATION_TAIL")
    rules = [body(f"MEMBER_RULE_{i}") for i in range(20)]
    team = ExpertTeamProfile.from_meta({"id": "team", "name": "Team",
        "leader": {"id": "lead", "name": "Lead", "role": text, "instructions": rules},
        "members": [{"id": "reviewer", "name": "Reviewer", "instructions": rules}],
        "orchestration": {"trigger": text,
            "stages": [{"id": "review", "title": "Review", "goal": text, "deliverable": text}],
            "workstreams": [{"memberId": "reviewer", "title": "Review", "brief": text, "deliverable": text}],
            "reviewChecklist": rules}})
    assert team.leader.role == text
    assert team.leader.instructions == team.members[0].instructions == rules
    orchestration = team.orchestration
    assert orchestration.trigger == text
    assert orchestration.stages[0].goal == orchestration.stages[0].deliverable == text
    assert orchestration.workstreams[0].brief == orchestration.workstreams[0].deliverable == text
    assert orchestration.review_checklist == rules
    rendered = team.render_prompt()
    assert all(rule in rendered for rule in rules)


def test_deduplication_is_exact_and_does_not_merge_rules_with_equal_prefixes():
    prefix = "Shared prefix " * 60
    first, second = prefix + "FIRST_UNIQUE_TAIL", prefix + "SECOND_UNIQUE_TAIL"
    expert = ExpertProfile.from_meta({"id": "one", "name": "One",
        "instructions": [first, second], "visibleRules": [first]})
    rendered = expert.render_prompt()
    assert rendered.count(first) == rendered.count(second) == 1
    assert expert.instructions == [first, second] and expert.visible_rules == [first]


def test_team_keeps_workflow_order_and_dedupes_only_matching_visible_rules():
    first, second = body("FIRST_WORKFLOW_TAIL"), body("SECOND_WORKFLOW_TAIL")
    team = ExpertTeamProfile.from_meta({"id": "team", "name": "Team", "workflow": [first, second],
        "visibleRules": [first], "internalRules": ["INTERNAL_ONLY"],
        "members": [{"id": "a", "name": "A", "instructions": ["MEMBER_SHARED"]},
                    {"id": "b", "name": "B", "instructions": ["MEMBER_SHARED"]}]})
    rendered = team.render_prompt()
    assert f"  1. {first}" in rendered and f"  2. {second}" in rendered
    assert rendered.count(first) == rendered.count(second) == 1
    assert rendered.count("MEMBER_SHARED") == 2
    assert team.visible_rules == [first]
    assert "INTERNAL_ONLY" in rendered.split("- Internal rules", 1)[1]
    assert "INTERNAL_ONLY" not in rendered.split("- Internal rules", 1)[0]


def test_duplicate_visible_workflow_retains_permission_only_for_declared_steps():
    first, hidden, third = body("VISIBLE_FIRST"), body("HIDDEN_SECOND"), body("VISIBLE_THIRD")
    team = ExpertTeamProfile.from_meta({"id": "team", "name": "Team",
        "workflow": [first, hidden, third], "visibleRules": [first, third]})
    rendered = team.render_prompt()
    assert all(rendered.count(step) == 1 for step in (first, hidden, third))
    permission = next(line for line in rendered.splitlines()
                      if "Workflow steps explicitly marked visible" in line)
    assert "safe to reflect in the answer or process when useful" in permission
    assert permission.endswith(": 1, 3")
    assert team.workflow == [first, hidden, third] and team.visible_rules == [first, third]
    # Nothing implicitly grants visibility to a hidden-only workflow.
    hidden_team = ExpertTeamProfile.from_meta({"name": "Hidden team", "workflow": [hidden]})
    assert "Workflow steps explicitly marked visible" not in hidden_team.render_prompt()


def test_short_identity_fields_and_structural_limits_keep_existing_validation():
    meta = {"id": "i" * 100, "name": "n" * 150, "revision": "r" * 150,
            "defaultSkills": ["s" * 350] + [f"skill-{i}" for i in range(20)]}
    expert = ExpertProfile.from_meta(meta)
    assert len(expert.id) == 80 and len(expert.name) == len(expert.revision) == 120
    assert len(expert.default_skills) == 12 and len(expert.default_skills[0]) == 320
    team = ExpertTeamProfile.from_meta({"name": "Team", "members":
        [{"id": str(i), "name": f"Member {i}"} for i in range(20)]})
    assert len(team.members) == 12


def test_existing_context_budget_rejects_oversized_rules_without_cutting_them():
    text = body("BUDGET_TAIL") * 15
    profile = ExpertProfile.from_meta({"name": "Writer", "instructions": [text]})
    message = Message(role="system", content=profile.render_prompt())
    request = DefaultContextEngine().prepare_request([message], prepared_tools=prepare_tools([]), token_limit=1024)
    assert request.blocked_reason and "budget" in request.blocked_reason
    assert profile.instructions == [text]
    assert "BUDGET_TAIL" in message.content


def package_with_long_rules(kind, text):
    with zipfile.ZipFile(io.BytesIO(archive_bytes(kind))) as original:
        files = {name: original.read(name) for name in original.namelist()}
    files["agents/lead.md"] = text.encode()
    if kind == "team":
        files["agents/member.md"] = text.encode()
        files["workflows/team.md"] = text.encode()
    files["files.json"] = json.dumps([
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items() if name != "files.json"
    ]).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_downloaded_package_rules_survive_shared_profile_parsing(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    text = body("DOWNLOADED_RULE_TAIL")
    archive = package_with_long_rules(kind, text)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=archive))
    key = "expert" if kind == "expert" else "expert_team"
    prepared = await expert_packages.prepare_expert_meta({key: reference(archive)}, transport=transport)
    context = ExpertSessionContext.from_meta(prepared)
    if kind == "expert":
        assert context.expert.instructions == [text]
        assert context.render_prompt().count(text) == 1
    else:
        assert context.team.workflow == context.team.leader.instructions == [text]
        assert context.team.members[0].instructions == [text]
        assert context.render_prompt().count(text) == 3  # Workflow and two distinct members.


class QuietConnection:
    async def session_update(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_new_load_resume_keep_long_full_object_rules_and_base_system(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    text = body("RESTORED_RULE_TAIL")
    key = "expert" if kind == "expert" else "expert_team"
    profile = ({"id": "writer", "name": "Writer", "instructions": [text], "visibleRules": [text]}
        if kind == "expert" else {"id": "writers", "name": "Writers", "workflow": [text],
            "visibleRules": [text], "leader": {"id": "lead", "name": "Lead", "instructions": [text]}})
    original = copy.deepcopy(profile)
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir="workspace",
        enable_memory=False, enable_memory_extraction=False, goal_autopilot_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_sub_agent=False, enable_mcp=False))
    host = BoxACPAgent(QuietConnection(), config, object(), [], "BASE_SYSTEM_MARKER")
    try:
        first = await host.new_session(cwd=str(tmp_path / "work"), session_id=f"long-{kind}", **{key: profile})
        assert text in host._sessions[first.sessionId].agent.system_prompt
        for method in (host.load_session, host.resume_session):
            await method(cwd=str(tmp_path / "work"), session_id=first.sessionId, **{key: profile})
            prompt = host._sessions[first.sessionId].agent.system_prompt
            assert text in prompt and "BASE_SYSTEM_MARKER" in prompt
        assert profile == original
    finally:
        await host.aclose()
