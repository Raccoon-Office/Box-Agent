import json

from box_agent.tools.skill_loader import SkillLoader
from box_agent.workflows.presentation_provider import (
    parse_host_presentation_config,
    resolve_presentation_skill_provider,
    resolve_query_matched_presentation_skill_provider,
)


def _write_skill(
    root,
    name: str,
    description: str,
    *,
    capabilities: str | None = None,
    workflow: str | None = None,
    keywords: tuple[str, ...] = (),
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    lines = ["---", f"name: {name}", f'description: "{description}"']
    if capabilities:
        lines.append(f"capabilities: [{capabilities}]")
    if workflow:
        lines.append(f"workflow: {workflow}")
    if keywords:
        lines.append(f"keywords: [{', '.join(keywords)}]")
    lines.extend(["---", "", "Follow this workflow."])
    skill_dir.joinpath("SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def test_parse_host_presentation_config_requires_typed_create_contract() -> None:
    parsed = parse_host_presentation_config(
        {
            "presentation_config": {
                "schema_version": 1,
                "intent": "create",
                "confirmed_by": "user",
            }
        }
    )

    assert parsed is not None
    assert parsed.confirmed_by == "user"
    assert parse_host_presentation_config(
        {"presentation_config": {"schema_version": 1, "intent": "inspect"}}
    ) is None


def test_matched_declared_provider_replaces_builtin_default(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(
        user_root,
        "custom-decks",
        "Create polished presentations",
        capabilities="presentation.authoring",
        workflow="custom_cloud_presentation",
    )
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("custom-decks", "pptx"),
    )

    assert provider is not None
    assert provider.skill_name == "custom-decks"
    assert provider.workflow == "custom_cloud_presentation"


def test_unmatched_declared_integration_does_not_become_global_default(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(
        user_root,
        "custom-decks",
        "Create cloud presentations",
        capabilities="presentation.authoring",
        workflow="custom_cloud_presentation",
    )
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(loader)

    assert provider is not None
    assert provider.skill_name == "pptx"


def test_query_matched_provider_does_not_use_builtin_fallback(tmp_path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
        keywords=("ppt", "pptx", "slides"),
    )
    loader = SkillLoader([(builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_query_matched_presentation_skill_provider(
        loader,
        "Summarize this Python module",
    )

    assert provider is None


def test_query_matched_provider_ignores_informational_skill_mentions(tmp_path) -> None:
    builtin_root = tmp_path / "builtin"
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
        keywords=("ppt", "pptx", "slides"),
    )
    loader = SkillLoader([(builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_query_matched_presentation_skill_provider(
        loader,
        "解释一下 pptx 这个 Skill 的名字",
    )

    assert provider is None


def test_query_matched_provider_is_not_dropped_by_catalog_limit(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    for index in range(16):
        _write_skill(
            user_root,
            f"training-noise-{index}",
            "Create editable new employee training checklists",
            keywords=("新员工", "入职", "培训", "可编辑", "清单"),
        )
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
        keywords=("ppt", "pptx", "slides"),
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()
    prompt = "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"
    assert "pptx" not in [skill.name for skill in loader.filter_by_query(prompt)]

    provider = resolve_query_matched_presentation_skill_provider(loader, prompt)

    assert provider is not None
    assert provider.skill_name == "pptx"
    assert provider.uses_controlled_workflow is True


def test_matched_legacy_user_provider_needs_no_new_fields(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "legacy-slides", "创建和编辑幻灯片")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(loader, ("legacy-slides", "pptx"))

    assert provider is not None
    assert provider.skill_name == "legacy-slides"
    assert provider.declared_capability is False
    assert provider.workflow is None


def test_provider_specific_query_beats_generic_builtin_rank(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "legacy-slides", "创建和编辑飞书幻灯片")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("pptx", "legacy-slides"),
        query="用飞书创建幻灯片",
    )

    assert provider is not None
    assert provider.skill_name == "legacy-slides"


def test_explicit_provider_name_beats_builtin_description_overlap(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(
        user_root,
        "ppt-master",
        "Create editable presentations with rendering and visual QA",
    )
    _write_skill(
        builtin_root,
        "pptx",
        "Create editable presentation files with rendering and visual QA",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("pptx", "ppt-master"),
        query=(
            "Use PPT Master to create an editable PPTX with rendering and visual QA"
        ),
    )

    assert provider is not None
    assert provider.skill_name == "ppt-master"


def test_generic_ppt_query_prefers_builtin_over_matched_lark_provider(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "lark-slides", "创建和编辑飞书幻灯片")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("lark-slides", "pptx"),
        query="制作一份哈利波特主题介绍 PPT",
    )

    assert provider is not None
    assert provider.skill_name == "pptx"


def test_explicit_lark_query_selects_lark_provider_before_builtin(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "lark-slides", "Create and edit Lark presentations")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("pptx", "lark-slides"),
        query="请用 Lark Slides 制作季度汇报 PPT",
    )

    assert provider is not None
    assert provider.skill_name == "lark-slides"


def test_lark_only_provider_is_not_global_presentation_fallback(tmp_path) -> None:
    user_root = tmp_path / "user"
    _write_skill(
        user_root,
        "lark-slides",
        "创建和编辑飞书幻灯片",
        capabilities="presentation.authoring",
    )
    loader = SkillLoader([(user_root, "user")])
    loader.discover_skills()

    assert (
        resolve_presentation_skill_provider(
            loader,
            ("lark-slides",),
            query="制作一份产品介绍 PPT",
        )
        is None
    )


def test_negative_presentation_reference_is_not_a_legacy_provider(tmp_path) -> None:
    user_root = tmp_path / "user"
    _write_skill(
        user_root,
        "lark-apps",
        "创建应用和 HTML 站点。不负责普通文档或原生幻灯片创建。",
    )
    _write_skill(user_root, "lark-slides", "创建和编辑飞书幻灯片")
    loader = SkillLoader([(user_root, "user")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(
        loader,
        ("lark-apps", "lark-slides"),
        query="用飞书创建幻灯片",
    )

    assert provider is not None
    assert provider.skill_name == "lark-slides"


def test_same_name_user_pptx_override_needs_no_new_fields(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "pptx", "PowerPoint toolkit")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(loader)

    assert provider is not None
    assert provider.skill_name == "pptx"
    assert provider.source == "user"
    assert provider.workflow is None


def test_unmatched_legacy_skill_does_not_implicitly_replace_builtin(tmp_path) -> None:
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    _write_skill(user_root, "legacy-slides", "创建和编辑幻灯片")
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    loader = SkillLoader([(user_root, "user"), (builtin_root, "builtin")])
    loader.discover_skills()

    provider = resolve_presentation_skill_provider(loader)

    assert provider is not None
    assert provider.skill_name == "pptx"


def test_disabled_builtin_is_not_revived(tmp_path) -> None:
    builtin_root = tmp_path / "builtin"
    settings_path = tmp_path / "skill-settings.json"
    _write_skill(
        builtin_root,
        "pptx",
        "Create presentation files",
        capabilities="presentation.authoring",
        workflow="controlled_presentation",
    )
    settings_path.write_text(
        json.dumps({"disabledSkillNames": ["pptx"]}),
        encoding="utf-8",
    )
    loader = SkillLoader(
        [(builtin_root, "builtin")],
        skill_settings_path=settings_path,
    )
    loader.discover_skills()

    assert resolve_presentation_skill_provider(loader) is None
