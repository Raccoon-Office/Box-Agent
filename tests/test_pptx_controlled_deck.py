"""Regression coverage for the controlled HTML deck compiler."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from box_agent.tools.skill_loader import SkillLoader


SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "document-skills"
    / "pptx"
)
SCRIPTS_DIR = SKILL_DIR / "scripts"
EXAMPLE = SKILL_DIR / "examples" / "controlled-deck" / "deck.json"
VISUAL_DNA = (
    SKILL_DIR.parents[1]
    / "html-templates"
    / "references"
    / "visual_dna.json"
)
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")
COMPOSITION_TEMPLATES = {
    "institutional-grid": "ledger",
    "editorial-spread": "spread",
    "poster-asymmetric": "stage",
    "playful-collage": "collage",
    "brutalist-frame": "frame",
    "retro-interface": "window",
    "literary-minimal": "article",
    "product-showcase": "device",
    "cinematic-canvas": "cinema",
    "analytical-exhibit": "exhibit",
    "technical-schematic": "schematic",
}
DEFAULT_COMPOSITION_FAMILIES = {
    "institutional-grid",
    "analytical-exhibit",
    "technical-schematic",
    "editorial-spread",
    "poster-asymmetric",
    "playful-collage",
    "brutalist-frame",
    "retro-interface",
    "literary-minimal",
    "product-showcase",
}
COMPOSITION_DIRECTIONS = {
    "structured-systems": [
        "institutional-grid",
        "analytical-exhibit",
        "technical-schematic",
    ],
    "narrative-pages": ["editorial-spread", "literary-minimal"],
    "visual-impact": ["poster-asymmetric", "cinematic-canvas"],
    "interface-modules": ["product-showcase", "retro-interface"],
    "expressive-objects": ["playful-collage", "brutalist-frame"],
}
SPECIALIZED_COMPOSITION_ANCHORS = {
    "product-showcase": "composition-device-screen",
    "cinematic-canvas": "composition-cinema-timecode",
    "analytical-exhibit": "composition-exhibit-board",
    "technical-schematic": "composition-schematic-canvas",
}
STRUCTURAL_VARIANT_ANCHORS = {
    "product-showcase": {
        "device-stage": "composition-device-bezel",
        "browser-story": "composition-device-browserbar",
        "annotated-flow": "composition-device-callouts",
    },
    "analytical-exhibit": {
        "exhibit-grid": "composition-exhibit-key",
        "evidence-rail": "composition-exhibit-scale",
        "decision-board": "composition-exhibit-decisions",
    },
    "technical-schematic": {
        "blueprint-canvas": "composition-schematic-registration",
        "annotated-system": "composition-schematic-bus",
        "spec-sheet": "composition-schematic-spec-rail",
    },
}


def _run(
    script: str,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if NODE is None:
        pytest.skip("Node.js is required to test the controlled deck compiler")
    result = subprocess.run(
        [str(NODE), str(SCRIPTS_DIR / script), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )
    # ── playwright probe ──────────────────────────────────────────
    # Several scripts need playwright but CI runners don't have it.
    # Skip the whole test instead of failing with an opaque exit code.
    if result.returncode != 0 and (
        "Cannot find module 'playwright'" in result.stderr
        or "Executable doesn't exist" in result.stderr
        or "Missing dependency: playwright" in result.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    return result


def _write_outline(
    path: Path,
    *,
    page_count: int = 3,
    source_mode: str = "public_authoritative_research",
) -> dict:
    payload = {
        "deck_goal": "用可靠证据解释一个主题",
        "audience": "普通观众",
        "source_mode": source_mode,
        "storyline": "从背景进入关键阶段，再以未来行动收束完整叙事。",
        "slides": [
            {
                "page": index,
                "title": f"主题页 {index}",
                "message": f"第 {index} 页保留自己的核心信息",
                "bullets": [
                    f"第 {index} 页支持点甲",
                    f"第 {index} 页支持点乙",
                ],
                "layout": "cards",
                "visual": "结构化信息卡",
                "evidence": [
                    f"公开资料证据 {chr(64 + index)} | Example | "
                    f"https://example.com/source-{chr(96 + index)}"
                ],
            }
            for index in range(1, page_count + 1)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92
        if value <= 0.03928
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(left: str, right: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(left), _relative_luminance(right)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _mix_hex(foreground: str, background: str, foreground_weight: float) -> str:
    foreground_channels = [
        int(foreground[index : index + 2], 16) for index in (1, 3, 5)
    ]
    background_channels = [
        int(background[index : index + 2], 16) for index in (1, 3, 5)
    ]
    channels = [
        round(front * foreground_weight + back * (1 - foreground_weight))
        for front, back in zip(foreground_channels, background_channels, strict=True)
    ]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def test_layout_manifest_is_generated_from_registry() -> None:
    result = _run("build_layout_manifest.js", "--check")

    assert result.returncode == 0, result.stderr
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    assert manifest["generated_from"] == "layouts/registry.js + themes/*.json"


def test_every_collection_layout_publishes_one_typed_count_contract() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    collection_layouts = 0
    for layout in manifest["layouts"]:
        array_fields = {
            name
            for name, contract in layout["fields"].items()
            if contract.get("type") == "array"
        }
        if not array_fields:
            continue
        collection_layouts += 1
        published_paths = set((layout.get("counts") or {}).values())
        if len(array_fields) > 1 or published_paths:
            assert array_fields <= published_paths, layout["id"]
        else:
            assert len(array_fields) == 1
    assert collection_layouts == 27
    assert manifest["default_theme_id"] == "blue-professional"
    visual_dna_ids = {
        item["template_id"]
        for item in json.loads(VISUAL_DNA.read_text(encoding="utf-8"))["templates"]
    }
    theme_ids = {theme["id"] for theme in manifest["themes"]}
    covered_dna_ids = {
        dna_id
        for theme in manifest["themes"]
        for dna_id in theme["selection"]["visual_dna_ids"]
    }
    assert len(visual_dna_ids) == 32
    assert len(theme_ids) == 47
    assert visual_dna_ids - theme_ids == {"playful"}
    assert covered_dna_ids == (visual_dna_ids - {"playful"}) | {
        "comic-panel",
        "technical-blueprint",
        "product-console",
        "data-intelligence",
        "people-handbook",
        "capital-ledger",
        "clinical-atlas",
        "civic-brief",
        "research-notebook",
        "factory-floor",
        "legal-docket",
        "property-atlas",
        "commerce-pulse",
        "logistics-control-tower",
    }
    assert {
        direction["id"]: direction["family_ids"]
        for direction in manifest["composition_directions"]
    } == COMPOSITION_DIRECTIONS
    assert {
        family["family"]
        for direction in manifest["composition_directions"]
        for family in direction["families"]
    } == set(COMPOSITION_TEMPLATES)
    assert all(
        family["direction"] == direction["id"]
        and family["selection_signals"]
        and len(family["variants"]) == 3
        for direction in manifest["composition_directions"]
        for family in direction["families"]
    )
    assert all(
        set(theme["style"]) == {
            "canvas",
            "surface",
            "shadow",
            "heading",
            "label",
            "accent",
            "alternation",
        }
        for theme in manifest["themes"]
    )
    composition_families = {
        theme["composition"]["family"] for theme in manifest["themes"]
    }
    assert composition_families == DEFAULT_COMPOSITION_FAMILIES
    assert all(
        len(theme["composition"]["variants"]) == 3
        and len(set(theme["composition"]["variants"])) == 3
        for theme in manifest["themes"]
    )
    selectable_families = {
        family["family"]
        for theme in manifest["themes"]
        for family in theme["composition"]["families"]
    }
    assert selectable_families == set(COMPOSITION_TEMPLATES)
    assert all(
        theme["composition"]["family"]
        == theme["composition"]["default_family"]
        and theme["composition"]["direction"]
        == theme["composition"]["default_direction"]
        and theme["composition"]["default_family"]
        in theme["composition"]["allowed_families"]
        and theme["composition"]["default_direction"]
        in theme["composition"]["allowed_directions"]
        and {
            family
            for direction in theme["composition"]["directions"]
            for family in direction["families"]
        }
        == set(theme["composition"]["allowed_families"])
        and all(len(family["variants"]) == 3 for family in theme["composition"]["families"])
        for theme in manifest["themes"]
    )
    block_frame = next(
        theme for theme in manifest["themes"] if theme["id"] == "block-frame"
    )
    assert block_frame["selection"]["visual_dna_ids"] == ["block-frame"]
    assert block_frame["shape"]["radius_large"] == 0
    mono_blue = next(
        theme
        for theme in manifest["themes"]
        if theme["id"] == "block-frame-mono-blue"
    )
    assert mono_blue["selection"]["visual_dna_ids"] == ["block-frame"]
    assert mono_blue["palette"]["primary"] == "#1E2BFA"
    comic_panel = next(
        theme for theme in manifest["themes"] if theme["id"] == "comic-panel"
    )
    assert comic_panel["selection"]["visual_dna_ids"] == ["comic-panel"]
    assert comic_panel["style"]["canvas"] == "dots"
    assert comic_panel["composition"]["family"] == "brutalist-frame"
    assert "technical-schematic" in comic_panel["composition"]["allowed_families"]
    pixel_orbit = next(
        theme for theme in manifest["themes"] if theme["id"] == "8-bit-orbit"
    )
    assert pixel_orbit["selection"]["visual_dna_ids"] == ["8-bit-orbit"]
    assert pixel_orbit["style"]["canvas"] == "pixel"
    assert pixel_orbit["composition"]["family"] == "retro-interface"
    consulting_navy = next(
        theme for theme in manifest["themes"] if theme["id"] == "consulting-navy"
    )
    assert consulting_navy["selection"]["scheme"] == "cool-light"
    assert consulting_navy["selection"]["formality"] == "high"
    assert consulting_navy["palette"]["background"] == "#F4F7FA"
    assert consulting_navy["palette"]["primary"] == "#173B63"
    assert consulting_navy["composition"]["family"] == "institutional-grid"
    technical_blueprint = next(
        theme for theme in manifest["themes"] if theme["id"] == "technical-blueprint"
    )
    assert technical_blueprint["selection"]["visual_dna_ids"] == [
        "technical-blueprint"
    ]
    assert technical_blueprint["composition"]["family"] == "technical-schematic"
    product_console = next(
        theme for theme in manifest["themes"] if theme["id"] == "product-console"
    )
    assert product_console["selection"]["visual_dna_ids"] == ["product-console"]
    assert product_console["composition"]["family"] == "product-showcase"
    data_intelligence = next(
        theme for theme in manifest["themes"] if theme["id"] == "data-intelligence"
    )
    assert data_intelligence["selection"]["visual_dna_ids"] == [
        "data-intelligence"
    ]
    assert data_intelligence["composition"]["family"] == "analytical-exhibit"
    assert len(manifest["layouts"]) == 33
    assert {layout["id"] for layout in manifest["layouts"]} >= {
        "cover-hero-v1",
        "cover-editorial-v1",
        "comparison-two-column-v1",
        "text-columns-v1",
        "architecture-layered-v1",
        "system-integration-v1",
        "technical-diagram-v1",
        "dashboard-overview-v1",
        "chart-bar-v1",
        "chart-data-v1",
        "heatmap-matrix-v1",
        "table-data-v1",
        "timeline-horizontal-v1",
        "swimlane-process-v1",
        "customer-journey-map-v1",
        "maturity-model-v1",
        "cause-tree-v1",
        "project-case-study-v1",
        "image-feature-v1",
        "image-full-bleed-v1",
        "closing-next-steps-v1",
    }
    assert all(layout["editor"]["defaultProps"] for layout in manifest["layouts"])
    cover = next(layout for layout in manifest["layouts"] if layout["id"] == "cover-hero-v1")
    assert cover["editor"]["defaultProps"]["eyebrow"] == "年度作品集"
    assert cover["mediaSlots"]["decision"]["mode"] == "auto"
    assert cover["mediaSlots"]["slots"][0] == {
        "id": "hero",
        "propPath": "hero",
        "role": "primary-visual",
        "required": False,
        "strategies": ["generate", "use_existing", "skip"],
        "preferredRatio": "4:3",
        "placementControlledBy": "media_side",
    }
    assert all(
        layout["mediaSlots"]["background"]["supported"]
        and layout["mediaSlots"]["background"]["requiresLayoutContract"]
        and layout["mediaSlots"]["background"]["textRegionNames"]
        for layout in manifest["layouts"]
    )
    project = next(
        layout for layout in manifest["layouts"] if layout["id"] == "project-case-study-v1"
    )
    assert project["fields"]["image"]["required"] is False
    assert project["fields"]["metrics"]["minItems"] == 2
    assert project["fields"]["metrics"]["maxItems"] == 3
    assert project["fields"]["composition"]["values"] == ["split", "poster"]
    assert project["mediaSlots"]["slots"][0]["strategies"] == [
        "generate",
        "use_existing",
        "skip",
    ]
    architecture = next(
        layout for layout in manifest["layouts"] if layout["id"] == "architecture-layered-v1"
    )
    assert architecture["fields"]["layers"]["maxItems"] == 6
    table = next(
        layout for layout in manifest["layouts"] if layout["id"] == "table-data-v1"
    )
    assert table["fields"]["columns"]["maxItems"] == 6
    assert table["fields"]["rows"]["maxItems"] == 12
    assert table["fields"]["rows"]["itemShape"]["maxItems"] == 6
    assert "gantt" in table["variants"]
    assert "gantt" in table["fields"]["variant"]["values"]
    heatmap = next(
        layout for layout in manifest["layouts"] if layout["id"] == "heatmap-matrix-v1"
    )
    assert heatmap["fields"]["columns"]["maxItems"] == 6
    assert heatmap["fields"]["rows"]["maxItems"] == 8
    assert "heatmap" in heatmap["capabilities"]


def test_high_frequency_layouts_publish_three_structural_variants() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    variants = {layout["id"]: layout["variants"] for layout in manifest["layouts"]}

    assert variants["cards-grid-v1"] == ["balanced", "numbered", "featured"]
    assert variants["quadrant-matrix-v1"] == [
        "impact-urgency",
        "equal-cross",
        "focus-high-high",
    ]
    assert variants["comparison-two-column-v1"] == [
        "contrast",
        "symmetric",
        "stacked",
    ]
    assert variants["kpi-grid-v1"] == ["cards", "ledger", "hero"]
    assert variants["timeline-horizontal-v1"] == [
        "horizontal",
        "staggered",
        "phase-band",
    ]


def test_outline_recipe_registry_matches_soft_business_scenarios() -> None:
    listed = _run("query_outline_recipes.js", "--list")
    matched = _run("query_outline_recipes.js", "--text", "季度经营分析")
    unmatched = _run("query_outline_recipes.js", "--text", "通用主题")

    assert listed.returncode == 0, listed.stderr
    assert [item["id"] for item in json.loads(listed.stdout)["recipes"]] == [
        "project-review",
        "sales-proposal",
        "operating-analysis",
    ]
    assert matched.returncode == 0, matched.stderr
    assert json.loads(matched.stdout)["recipe"]["id"] == "operating-analysis"
    assert json.loads(matched.stdout)["source"] == "signal_match"
    assert unmatched.returncode == 0, unmatched.stderr
    assert json.loads(unmatched.stdout) == {"matched": False, "recipe": None}


def test_p0_structural_variants_render_distinct_layout_classes(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "quadrant-matrix-v1",
        "comparison-two-column-v1",
        "kpi-grid-v1",
        "timeline-horizontal-v1",
        "--theme",
        "blue-professional",
        "--family",
        "analytical-exhibit",
        "--design-seed",
        "p0-variant-1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    selected = ["featured", "focus-high-high", "stacked", "hero", "phase-band"]
    for slide, variant in zip(deck["slides"], selected, strict=True):
        slide["props"]["variant"] = variant
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    validation = _run("validate_deck_spec.js", str(deck_path))
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    for class_name in (
        "cards-featured",
        "quadrant-focus-high-high",
        "comparison-stacked",
        "kpis-hero",
        "timeline-phase-band",
    ):
        assert class_name in html
    assert "cards-featured.cards-count-3 .cards-grid" in html
    assert '<div class="comparison-arrow" aria-hidden="true">↓</div>' in html
    assert "body[data-deck-composition] .comparison-stacked .comparison-grid" in html
    assert "quadrant-focus-high-high .quadrant-grid::before { left: 41%; }" in html
    assert "quadrant-focus-high-high .quadrant-grid::after { top: 59%; }" in html
    self_check = _run("html_self_check.js", str(html_path))
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr


def test_business_semantic_layouts_render_editable_contracts(tmp_path: Path) -> None:
    layout_ids = [
        "swimlane-process-v1",
        "customer-journey-map-v1",
        "maturity-model-v1",
        "cause-tree-v1",
    ]
    deck_path = tmp_path / "deck.json"
    scaffold = _run("inspect_deck_contract.js", *layout_ids, "--out", str(deck_path))
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    validation = _run("validate_deck_spec.js", str(deck_path))
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    for layout_id in layout_ids:
        assert f'data-layout-id="{layout_id}"' in html
    assert "layout-maturity-model maturity-variant-ladder" in html
    assert "layout-maturity-model maturity-ladder" not in html
    for prop_path in (
        "lanes.0.activities.0",
        "stages.0.opportunity",
        "levels.0.criteria",
        "causes.0.factors.0",
    ):
        assert f'data-prop-path="{prop_path}"' in html


def test_swimlane_requires_one_activity_per_phase(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run("inspect_deck_contract.js", "swimlane-process-v1", "--out", str(deck_path))
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["lanes"][0]["activities"].append("多余活动")
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    validation = _run("validate_deck_spec.js", str(deck_path))

    assert validation.returncode != 0
    assert "activity cell(s) to match columns" in validation.stdout


@pytest.mark.parametrize(
    ("visual", "expected_layout"),
    [
        ("角色 × 阶段泳道流程，标出跨部门交接", "swimlane-process-v1"),
        ("客户旅程图：行为、触点、感受、痛点与机会", "customer-journey-map-v1"),
        ("五级能力成熟度模型，标记当前与目标状态", "maturity-model-v1"),
        ("根因树：核心问题、原因类别与影响因素", "cause-tree-v1"),
        ("横向大图叙事，下面保留简短说明", "image-feature-v1"),
        ("整页生图，左侧文字安全区、右侧视觉焦点", "image-full-bleed-v1"),
    ],
)
def test_scaffold_normalizes_business_semantics_to_dedicated_layouts(
    tmp_path: Path,
    visual: str,
    expected_layout: str,
) -> None:
    outline_path = tmp_path / expected_layout / "outline.json"
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "业务结构",
            "message": "使用专用可编辑结构表达页面关系。",
            "layout": "business-visual",
            "visual": visual,
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = outline_path.parent / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == expected_layout
    contract = json.loads(scaffold.stdout)
    assert contract["layout_normalizations"][0]["to"] == expected_layout


def test_skill_avoids_public_research_permission_and_micro_todo_loops() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "do not call\n`plan_write` or `todo_write`" in skill
    assert "already authorizes the normal use\n   of public, authoritative sources" in skill
    assert "Do not ask the user\n   for a second \"permission to use public sources\"" in skill
    assert "must not replace it with a separate four-query cap" in skill
    assert "Inspect the full\n   useful result returned by each search" in skill


def test_every_visual_dna_theme_has_complete_contrast_safe_runtime_tokens() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    required_palette = {
        "background",
        "surface",
        "surface_strong",
        "primary",
        "primary_soft",
        "text",
        "muted",
        "border",
        "inverse",
        "chart",
    }
    for theme in manifest["themes"]:
        palette = theme["palette"]
        assert required_palette <= set(palette), theme["id"]
        assert len(palette["chart"]) == 4, theme["id"]
        assert all(
            isinstance(color, str)
            and len(color) == 7
            and color.startswith("#")
            for color in [
                palette["background"],
                palette["surface"],
                palette["surface_strong"],
                palette["primary"],
                palette["primary_soft"],
                palette["text"],
                palette["muted"],
                palette["border"],
                palette["inverse"],
                *palette["chart"],
            ]
        ), theme["id"]
        primary_text = palette.get("primary_text", palette["primary"])
        assert _contrast_ratio(palette["text"], palette["background"]) >= 4.5, theme["id"]
        assert _contrast_ratio(palette["muted"], palette["background"]) >= 3, theme["id"]
        assert _contrast_ratio(palette["text"], palette["surface"]) >= 4.5, theme["id"]
        assert _contrast_ratio(palette["muted"], palette["surface"]) >= 3, theme["id"]
        assert _contrast_ratio(primary_text, palette["background"]) >= 3, theme["id"]
        assert _contrast_ratio(palette["inverse"], palette["primary"]) >= 4.5, theme["id"]


def test_spec_sheet_measurement_rail_uses_one_consistent_left_gutter() -> None:
    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text()

    assert (
        'body[data-deck-composition="technical-schematic"]'
        '[data-deck-composition-variant="spec-sheet"] '
        ".composition-schematic-spec-rail {\n"
        "  left: 48px;"
    ) in composition_css
    assert (
        'body[data-deck-composition="technical-schematic"]'
        '[data-deck-composition-variant="spec-sheet"] .data-table-wrap {\n'
        "  margin-left: 150px;"
    ) in composition_css


def test_factory_floor_uses_role_based_page_rhythm() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    factory_floor = next(
        theme for theme in manifest["themes"] if theme["id"] == "factory-floor"
    )
    palette = factory_floor["palette"]

    assert palette["alt_background"] == "#173F5F"
    assert palette["alt_primary"] == "#F2C94C"
    assert _contrast_ratio(palette["alt_text"], palette["alt_background"]) >= 4.5
    assert _contrast_ratio(palette["alt_muted"], palette["alt_background"]) >= 3

    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text()
    assert (
        'body[data-deck-theme="factory-floor"] .layout-kpis .kpi-card:first-child'
        " { background: var(--deck-primary-soft); }"
    ) in deck_css
    assert 'body[data-deck-theme="factory-floor"] .layout-statement {' in deck_css
    assert 'body[data-deck-theme="factory-floor"] .layout-closing {' in deck_css
    assert "  --deck-bg: #F2C94C;" in deck_css

    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text()
    assert (
        'body[data-deck-theme-id="factory-floor"]'
        '[data-deck-composition="technical-schematic"]\n'
        "  .slide\n"
        "  :is(.slide-header, .cover-copy) {\n"
        "  border-left: 0;"
    ) in composition_css
    assert (
        'body[data-deck-theme-id="factory-floor"]'
        '[data-deck-composition="technical-schematic"]\n'
        "  .slide\n"
        "  .eyebrow {"
    ) in composition_css
    assert "  background: var(--deck-primary);" in composition_css
    assert "  color: var(--deck-bg);" in composition_css
    assert (
        'body[data-deck-theme-id="factory-floor"]'
        '[data-deck-composition="technical-schematic"]\n'
        "  .layout-data-table\n"
        "  .data-table-wrap {\n"
        "  margin-left: 0;"
    ) in composition_css


def test_comparison_table_emphasizes_body_column_without_tinting_header() -> None:
    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text()

    assert (
        ".table-comparison .data-table td:last-child {\n"
        "  background: var(--deck-primary-soft);"
    ) in deck_css
    assert (
        ".table-comparison .data-table th:last-child,\n"
        ".table-comparison .data-table td:last-child"
    ) not in deck_css


def test_all_themes_receive_contrast_safe_role_surface_rhythm() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    for theme in manifest["themes"]:
        palette = theme["palette"]
        role_surface = _mix_hex(palette["primary"], palette["surface"], 0.1)
        assert _contrast_ratio(palette["text"], role_surface) >= 4.5, theme["id"]


def test_theme_guide_lines_follow_composition_content_gutter() -> None:
    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text(encoding="utf-8")
    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text(
        encoding="utf-8"
    )

    assert "--deck-content-left: 92px" in deck_css
    assert "--deck-guide-gap: 28px" in deck_css
    assert deck_css.count(
        "calc(var(--deck-content-left) - var(--deck-guide-gap))"
    ) >= 2
    assert "transparent 0 6.8%" not in deck_css
    assert "transparent 0 7.5%" not in deck_css
    assert composition_css.count("--deck-content-left:") >= 11
    assert (
        'body[data-deck-composition="editorial-spread"] .timeline-count-3 .timeline-track'
        in composition_css
    )
    assert (
        '.layout-cards.cards-count-5:not(.cards-featured) .cards-grid'
        in composition_css
    )
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in composition_css
    assert (
        '.layout-cards.cards-count-5:not(.cards-featured) .content-card:nth-child(4)'
        in composition_css
    )
    assert "grid-column: 2 / span 2;" in composition_css

    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text()
    assert ".slide.layout-kpis .kpi-card:first-child," in deck_css
    assert ".slide.layout-comparison .comparison-right," in deck_css
    assert ".slide.layout-timeline .timeline-step:nth-child(even)" in deck_css
    assert "var(--deck-primary) 10%, var(--deck-surface)" in deck_css
    assert ".layout-statement," in deck_css


def test_flagship_themes_define_distinct_contrast_safe_role_pages() -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    themes = {theme["id"]: theme for theme in manifest["themes"]}

    orbit = themes["8-bit-orbit"]["palette"]
    for background in (orbit["chart"][0], orbit["chart"][3], orbit["chart"][1]):
        assert _contrast_ratio(orbit["inverse"], background) >= 4.5

    daisy_text = themes["daisy-days"]["palette"]["text"]
    for background in ("#A8E6CF", "#F7C8D4", "#FDE68A"):
        assert _contrast_ratio(daisy_text, background) >= 4.5

    editorial_text = themes["soft-editorial"]["palette"]["text"]
    for background in ("#B7C7A8", "#E8C9B6", "#D6DD63"):
        assert _contrast_ratio(editorial_text, background) >= 4.5

    studio = themes["studio"]["palette"]
    assert _contrast_ratio(studio["alt_text"], studio["alt_background"]) >= 4.5
    intelligence = themes["data-intelligence"]["palette"]
    assert (
        _contrast_ratio(
            intelligence["alt_text"], intelligence["alt_background"]
        )
        >= 4.5
    )

    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text()
    for theme_id in ("8-bit-orbit", "daisy-days", "soft-editorial", "factory-floor"):
        assert f'body[data-deck-theme="{theme_id}"] .layout-statement' in deck_css
        assert f'body[data-deck-theme="{theme_id}"] .layout-closing' in deck_css
    assert 'body[data-deck-theme="studio"] .layout-statement' in deck_css
    assert themes["studio"]["style"]["alternation"] == "section"
    assert (
        'body[data-deck-theme="data-intelligence"] :is('
        ".layout-comparison, .layout-data-table)"
    ) in deck_css
    assert themes["data-intelligence"]["style"]["alternation"] == "section"


def test_outline_role_pages_choose_role_layouts_and_preserve_overflow(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=5, source_mode="user_provided")
    outline["slides"] = [
        {
            "page": 1,
            "title": "第一章",
            "message": "从问题背景进入主题。",
            "bullets": [],
            "layout": "章节页",
            "visual": "章节分隔页",
            "evidence": [],
        },
        {
            "page": 2,
            "title": "核心结论",
            "message": "一句话说明最需要被记住的判断。",
            "bullets": ["证明点甲", "证明点乙", "证明点丙"],
            "layout": "观点页",
            "visual": "单一核心结论与三个证明点",
            "evidence": [],
        },
        {
            "page": 3,
            "title": "四项总结",
            "message": "四项内容必须全部保留。",
            "bullets": ["总结甲", "总结乙", "总结丙", "总结丁"],
            "layout": "观点页",
            "visual": "核心结论要点",
            "evidence": [],
        },
        {
            "page": 4,
            "title": "行动式收尾",
            "message": "把结论落实为三个后续动作。",
            "bullets": ["行动甲", "行动乙", "行动丙"],
            "layout": "closing-next-steps-v1",
            "visual": "行动式收尾",
            "evidence": [],
        },
        {
            "page": 5,
            "title": "五项后续行动",
            "message": "五项行动必须全部保留。",
            "bullets": ["行动一", "行动二", "行动三", "行动四", "行动五"],
            "layout": "closing-next-steps-v1",
            "visual": "行动式收尾",
            "evidence": [],
        },
    ]
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "--theme",
        "blue-professional",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "section-marker-v1",
        "statement-focus-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "cards-grid-v1",
    ]


def test_every_registered_theme_renders_all_controlled_layouts(tmp_path: Path) -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    layout_ids = [layout["id"] for layout in manifest["layouts"]]
    rendered_templates: set[str] = set()
    deck_path = tmp_path / "all-layouts.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        *layout_ids,
        "--title",
        "Theme compatibility gallery",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert len(deck["slides"]) == len(layout_ids)

    for theme in manifest["themes"]:
        theme_id = theme["id"]
        deck["theme_id"] = theme_id
        deck["design"]["family"] = theme["composition"]["family"]
        deck["design"]["variant"] = theme["composition"]["variants"][0]
        deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
        validation = _run("validate_deck_spec.js", str(deck_path))
        assert validation.returncode == 0, f"{theme_id}: {validation.stdout}"
        html_path = tmp_path / f"{theme_id}.html"
        rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
        assert rendered.returncode == 0, f"{theme_id}: {rendered.stderr}"
        html = html_path.read_text(encoding="utf-8")
        rendered_deck = html.split('<section class="deck-layout-picker"', 1)[0]
        assert rendered_deck.count('<section class="slide ') == len(layout_ids), theme_id
        assert f'data-deck-theme-id="{theme_id}"' in html
        assert (
            f'data-deck-composition="{theme["composition"]["family"]}"' in html
        )
        template = COMPOSITION_TEMPLATES[theme["composition"]["family"]]
        rendered_templates.add(template)
        assert f'data-composition-template="{template}"' in rendered_deck
        assert f'class="composition-root composition-{template}"' in rendered_deck
        assert any(
            f'data-deck-composition-variant="{variant}"' in html
            for variant in theme["composition"]["variants"]
        )
        for axis in (
            "canvas",
            "surface",
            "shadow",
            "heading",
            "label",
            "accent",
            "alternation",
        ):
            assert f'data-deck-{axis}="{theme["style"][axis]}"' in html
    assert rendered_templates == {
        COMPOSITION_TEMPLATES[family] for family in DEFAULT_COMPOSITION_FAMILIES
    }


@pytest.mark.parametrize(
    ("theme_id", "family"),
    [
        ("blue-professional", "product-showcase"),
        ("studio", "cinematic-canvas"),
        ("blue-professional", "analytical-exhibit"),
        ("blue-professional", "technical-schematic"),
    ],
)
def test_new_composition_families_render_every_layout(
    tmp_path: Path,
    theme_id: str,
    family: str,
) -> None:
    manifest = json.loads((SKILL_DIR / "layouts" / "manifest.json").read_text())
    layout_ids = [layout["id"] for layout in manifest["layouts"]]
    deck_path = tmp_path / family / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        *layout_ids,
        "--theme",
        theme_id,
        "--family",
        family,
        "--design-seed",
        f"{family.replace('-', '_')}_seed",
        "--title",
        f"{family} compatibility gallery",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    contract = json.loads(scaffold.stdout)
    expected_direction = next(
        direction
        for direction, families in COMPOSITION_DIRECTIONS.items()
        if family in families
    )
    assert contract["authoring_rules"]["design_policy"]["selected_direction"] == (
        expected_direction
    )
    assert contract["authoring_rules"]["design_policy"]["user_choice_path"] == (
        "selected_theme.composition.directions"
    )
    assert contract["authoring_rules"]["design_policy"]["family_selection_path"] == (
        "selected_theme.composition.families[].selection_signals"
    )
    assert expected_direction in contract["selected_theme"]["composition"][
        "allowed_directions"
    ]
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["design"]["family"] == family

    validation = _run("validate_deck_spec.js", str(deck_path))
    assert validation.returncode == 0, validation.stdout + validation.stderr
    html_path = deck_path.parent / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    rendered_deck = html.split('<section class="deck-layout-picker"', 1)[0]
    rendered_dom = rendered_deck.split('<main id="deck-root">', 1)[1]
    template = COMPOSITION_TEMPLATES[family]
    assert rendered_deck.count('<section class="slide ') == len(layout_ids)
    assert f'data-deck-composition="{family}"' in html
    assert f'data-composition-template="{template}"' in rendered_deck
    assert f'class="composition-root composition-{template}"' in rendered_deck
    assert SPECIALIZED_COMPOSITION_ANCHORS[family] in rendered_dom
    variant = deck["design"]["variant"]
    if family in STRUCTURAL_VARIANT_ANCHORS:
        assert STRUCTURAL_VARIANT_ANCHORS[family][variant] in rendered_dom

    self_check = _run("html_self_check.js", str(html_path))
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr


def test_theme_gallery_renders_real_opt_in_theme_previews(tmp_path: Path) -> None:
    gallery_path = tmp_path / "theme-previews" / "index.html"

    result = _run(
        "render_theme_gallery.js",
        "--out",
        str(gallery_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["theme_count"] == 13
    assert payload["themes"] == [
        "technical-blueprint",
        "product-console",
        "data-intelligence",
        "blue-professional",
        "signal",
        "biennale-yellow",
        "studio",
        "daisy-days",
        "comic-panel",
        "8-bit-orbit",
        "block-frame-mono-blue",
        "retro-windows",
        "soft-editorial",
    ]
    gallery = gallery_path.read_text(encoding="utf-8")
    assert "先看主题，再开始做 PPT" in gallery
    assert "回复卡片上的 theme_id" in gallery
    assert gallery.count("?mode=gallery") == 13
    assert gallery.count("打开 3 页完整预览") == 13
    for theme_id in payload["themes"]:
        preview_path = gallery_path.parent / f"{theme_id}.html"
        preview = preview_path.read_text(encoding="utf-8")
        rendered_preview = preview.split('<section class="deck-layout-picker"', 1)[0]
        assert f'data-deck-theme-id="{theme_id}"' in preview
        assert rendered_preview.count('<section class="slide ') == 3
        assert "data-composition-template=" in rendered_preview
        assert 'id="deck-document"' in preview
    assert "layout-technical-diagram" in (
        gallery_path.parent / "technical-blueprint.html"
    ).read_text(encoding="utf-8")
    assert "layout-project-case" in (
        gallery_path.parent / "product-console.html"
    ).read_text(encoding="utf-8")
    assert "layout-kpis" in (
        gallery_path.parent / "data-intelligence.html"
    ).read_text(encoding="utf-8")


def test_composition_gallery_renders_every_family_and_variant(
    tmp_path: Path,
) -> None:
    gallery_path = tmp_path / "composition-previews" / "index.html"
    variants = {
        "institutional-grid": ["balanced-grid", "rail-grid", "ledger-grid"],
        "editorial-spread": ["split-spread", "feature-spread", "banded-spread"],
        "poster-asymmetric": ["offset-hero", "stacked-poster", "split-poster"],
        "playful-collage": ["mosaic", "staggered", "capsule"],
        "brutalist-frame": ["block-grid", "offset-frame", "ledger-frame"],
        "retro-interface": ["window-grid", "terminal-stack", "pixel-panels"],
        "literary-minimal": ["margin-note", "quiet-center", "asymmetric-column"],
        "product-showcase": ["device-stage", "browser-story", "annotated-flow"],
        "cinematic-canvas": ["full-bleed", "split-film", "chapter-cut"],
        "analytical-exhibit": ["exhibit-grid", "evidence-rail", "decision-board"],
        "technical-schematic": ["blueprint-canvas", "annotated-system", "spec-sheet"],
    }

    result = _run(
        "render_composition_gallery.js",
        "--out",
        str(gallery_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["family_count"] == 11
    assert payload["variant_count"] == 33
    assert {
        group["id"]: group["families"] for group in payload["groups"]
    } == COMPOSITION_DIRECTIONS
    assert {
        family
        for group in payload["groups"]
        for family in group["families"]
    } == set(variants)
    gallery = gallery_path.read_text(encoding="utf-8")
    assert "看骨架，不看换色" in gallery
    assert "用户只需要理解 5 个方向" in gallery
    assert "内容信号" in gallery
    assert gallery.count("?mode=gallery") == 33

    for family, family_variants in variants.items():
        assert family in gallery
        for variant in family_variants:
            preview_path = gallery_path.parent / f"{family}--{variant}.html"
            preview = preview_path.read_text(encoding="utf-8")
            rendered_preview = preview.split('<section class="deck-layout-picker"', 1)[0]
            rendered_dom = rendered_preview.split('<main id="deck-root">', 1)[1]
            assert f'data-deck-composition="{family}"' in preview
            assert f'data-deck-composition-variant="{variant}"' in preview
            assert rendered_preview.count('<section class="slide ') == 1
            structural_anchors = STRUCTURAL_VARIANT_ANCHORS.get(family, {})
            if structural_anchors:
                assert structural_anchors[variant] in rendered_dom
                assert all(
                    anchor not in rendered_dom
                    for other_variant, anchor in structural_anchors.items()
                    if other_variant != variant
                )

    for family in SPECIALIZED_COMPOSITION_ANCHORS:
        for variant in variants[family]:
            self_check = _run(
                "html_self_check.js",
                str(gallery_path.parent / f"{family}--{variant}.html"),
            )
            assert self_check.returncode == 0, self_check.stdout + self_check.stderr


def test_restrained_information_families_do_not_reuse_pill_label_chrome() -> None:
    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text(
        encoding="utf-8"
    )
    label_rules = composition_css.split(
        "/* Restrained information families", 1
    )[1].split("/* --------------------------------------------------------------------------", 1)[0]

    for family in (
        "institutional-grid",
        "literary-minimal",
        "product-showcase",
        "analytical-exhibit",
        "technical-schematic",
    ):
        assert f'body[data-deck-composition="{family}"]' in label_rules
    assert "border-radius: 0;" in label_rules
    assert "background: transparent;" in label_rules
    assert "transform: none;" in label_rules


def test_soft_editorial_replaces_repeated_rules_with_pastel_panels() -> None:
    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text(
        encoding="utf-8"
    )
    soft_rules = composition_css.split(
        "/* Soft Editorial uses quiet pastel surfaces", 1
    )[1].split("/* The layout picker renders miniature slides", 1)[0]

    theme_selector = (
        'body[data-deck-theme-id="soft-editorial"]'
        '[data-deck-composition="editorial-spread"]'
    )
    assert f"{theme_selector} .slide-header" in soft_rules
    assert f"{theme_selector} .content-card" in soft_rules
    assert f"{theme_selector} .comparison-column" in soft_rules
    assert f"{theme_selector} .timeline-step" in soft_rules
    assert "border-bottom: 0;" in soft_rules
    assert "border-top: 0;" in soft_rules
    assert "border-radius: var(--deck-radius-large);" in soft_rules
    assert "background: var(--deck-primary-soft);" in soft_rules


def test_theme_gallery_is_opt_in_and_precedes_deck_checkpoint() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    theme_factory = (
        SKILL_DIR.parents[1] / "theme-factory" / "SKILL.md"
    ).read_text(encoding="utf-8")
    editor = (SKILL_DIR / "runtime" / "deck-editor.js").read_text(encoding="utf-8")

    assert "Theme preview intent (before deck authoring)" in skill
    assert "theme discovery" in skill
    assert "must not slow the default path" in skill
    assert "Before writing `outline.json`, scaffolding" in skill
    assert "scripts/render_theme_gallery.js" in skill
    assert "Composition comparison intent" in skill
    assert "scripts/render_composition_gallery.js" in skill
    assert "layout.render(modelSlide, index, documentModel.design)" in editor
    assert "PPT ownership boundary (mandatory)" in theme_factory
    assert 'get_skill(skill_name="pptx")' in theme_factory
    assert "Do not list the ten themes below" in theme_factory


def test_design_seed_is_reproducible_and_selects_distinct_variants(
    tmp_path: Path,
) -> None:
    designs = []
    for index, seed in enumerate(("seed_alpha", "seed_bravo", "seed_foxtrot"), 1):
        deck_path = tmp_path / f"variant-{index}" / "deck.json"
        scaffold = _run(
            "inspect_deck_contract.js",
            "cover-hero-v1",
            "cards-grid-v1",
            "--theme",
            "blue-professional",
            "--design-seed",
            seed,
            "--out",
            str(deck_path),
        )
        assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
        design = json.loads(deck_path.read_text(encoding="utf-8"))["design"]
        assert design["seed"] == seed
        assert design["family"] == "institutional-grid"
        designs.append(design)

        html_path = deck_path.parent / "index.html"
        first = _run(
            "render_deck_html.js", str(deck_path), "--out", str(html_path)
        )
        assert first.returncode == 0, first.stdout + first.stderr
        first_bytes = html_path.read_bytes()
        second = _run(
            "render_deck_html.js", str(deck_path), "--out", str(html_path)
        )
        assert second.returncode == 0, second.stdout + second.stderr
        assert html_path.read_bytes() == first_bytes

    assert {design["variant"] for design in designs} == {
        "balanced-grid",
        "rail-grid",
        "ledger-grid",
    }


def test_friendly_onboarding_auto_corrects_fallback_theme_and_avoids_schematic(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "为新员工提供一场清爽亲和、会议室可讲的入职培训。",
            "audience": "刚入职的新员工",
            "storyline": "从欢迎开始，用模块卡片和时间线讲清公司、文化与制度。",
        }
    )
    outline["slides"][0].update(
        {
            "title": "欢迎加入：一起把 AI 办公变得更好用",
            "layout": "浅色欢迎封面",
            "visual": "柔和品牌色小色块与欢迎插画",
        }
    )
    outline["slides"][1].update(
        {
            "title": "今天我们会讲什么",
            "layout": "模块卡片",
            "visual": "五个浅色模块卡片，每个模块配一个线性图标",
        }
    )
    outline["slides"][2].update(
        {
            "title": "入职节奏",
            "layout": "横向时间线",
            "visual": "从账号开通到团队融入的横向流程",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "auto" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "timeline-horizontal-v1",
        "--theme",
        "blue-professional",
        "--outline",
        str(outline_path),
        "--title",
        "新员工入职培训",
        "--fact",
        "清爽亲和，浅色背景为主，多用图标和小色块",
        "--fact",
        "不要深色高冷、不要复古手绘、不要拼贴",
        "--fact",
        "浅底加一两个柔和的品牌色点缀",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "soft-editorial"
    assert deck["design"]["family"] == "editorial-spread"
    assert report["theme_selection"]["source"] == "auto_corrected_default"
    assert report["theme_selection"]["confidence"] == "high"
    assert report["design_selection"]["family"] == "editorial-spread"
    assert report["design_selection"]["scores"].get("technical-schematic", 0) == 0


def test_theme_inference_does_not_treat_negated_playful_style_as_positive(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "为连锁零售集团生成稳健可落地的智能客服评标方案。",
            "audience": "客户采购负责人、IT 负责人和业务负责人",
            "storyline": "从客户需求、解决方案到业务价值与实施计划。",
        }
    )
    outline["slides"][0].update(
        {
            "title": "某连锁零售集团智能客服升级",
            "layout": "浅底咨询风封面",
            "visual": "深蓝、钢灰和浅灰的规整封面",
        }
    )
    outline["slides"][1].update(
        {
            "title": "需求与方案",
            "layout": "规整的咨询公司式卡片",
            "visual": "极简装饰与高信息密度",
        }
    )
    outline["slides"][2].update(
        {
            "title": "实施计划",
            "layout": "规整时间线",
            "visual": "可落地的阶段式计划",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "selection" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "timeline-horizontal-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--title",
        "智能客服评标方案",
        "--fact",
        "整体风格要稳健、专业、可信、可落地",
        "--fact",
        "浅底，深蓝 / 钢灰 / 浅灰冷色调，版式规整，装饰极简",
        "--fact",
        "不要做成投资人路演风、文艺杂志风、活泼亲和风或花哨拼贴风",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "consulting-navy"
    assert report["theme_selection"]["theme_id"] == "consulting-navy"
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "cool consulting review signature" in signals
    assert "friendly mood" not in signals
    assert "lively supporting mood" not in signals
    assert "friendly lively signature" not in signals


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "为西班牙精品酒庄制作葡萄酒大师班，"
            "讲清葡萄园风土、酿造逻辑与代表酒款。"
        ),
        (
            "Create a winery portfolio deck about vineyard terroir, "
            "winemaking, and a curated wine tasting."
        ),
    ],
)
def test_winery_brief_selects_mat_theme_without_fallback(
    tmp_path: Path,
    prompt: str,
) -> None:
    deck_path = tmp_path / "winery" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )

    assert deck["theme_id"] == "mat"
    assert report["theme_selection"]["theme_id"] == "mat"
    assert report["theme_selection"]["source"] == "content_inference"
    assert report["theme_selection"]["confidence"] == "high"
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "keyword rule: wine and vineyard" in signals


@pytest.mark.parametrize(
    "prompt",
    [
        "为精品酒店制作品牌战略汇报，介绍客房、餐饮和宾客体验。",
        "复盘门店酒水销售表现，包含销量、渠道和下一步动作。",
    ],
)
def test_wine_keyword_rule_does_not_match_broad_alcohol_terms(
    tmp_path: Path,
    prompt: str,
) -> None:
    deck_path = tmp_path / "not-wine" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "keyword rule: wine and vineyard" not in signals


@pytest.mark.parametrize(
    ("theme_id", "prompt", "expected_signal"),
    [
        (
            "block-frame-mono-blue",
            "黑白新野兽主义结构，只用克制的电光蓝点缀，硬阴影和零圆角。",
            "user intent rule: monochrome electric-blue neo-brutalism",
        ),
        (
            "creative-mode",
            "创意机构作品汇报，使用绿色、粉色、橙色和黄色的高饱和色块海报。",
            "user intent rule: multicolor creative-agency poster",
        ),
        (
            "retro-windows",
            "用 Windows 95 复古电脑窗口界面介绍软件发展历史。",
            "user intent rule: classic desktop operating-system interface",
        ),
        (
            "studio",
            "创意工作室品牌提案，黑底配电光黄，高冲击大字与克制图形。",
            "user intent rule: warm-black and electric-yellow studio",
        ),
    ],
)
def test_distinctive_visual_briefs_reach_registered_theme(
    tmp_path: Path,
    theme_id: str,
    prompt: str,
    expected_signal: str,
) -> None:
    deck_path = tmp_path / theme_id / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert report["theme_selection"]["theme_id"] == theme_id
    assert report["theme_selection"]["confidence"] == "high"
    assert expected_signal in {
        item["signal"]
        for item in report["theme_selection"]["matched_signals"]
    }


def test_auto_theme_prompts_cover_every_registered_theme(tmp_path: Path) -> None:
    manifest = json.loads(
        (SKILL_DIR / "layouts" / "manifest.json").read_text(encoding="utf-8")
    )
    distinctive_prompts = {
        "block-frame-mono-blue": (
            "黑白新野兽主义结构，只用克制的电光蓝点缀，"
            "硬阴影和零圆角。"
        ),
        "capsule": "小红书春季年轻美妆与护肤新品发布，清新活泼。",
        "creative-mode": (
            "创意机构作品汇报，使用绿色、粉色、橙色和黄色的"
            "高饱和色块海报。"
        ),
        "daisy-days": (
            "给小学生做一份儿童科普课堂课件，气质亲切活泼，"
            "使用奶油底和彩虹粉彩。"
        ),
        "legal-docket": (
            "法律意见书与案件分析，整理证据链、诉讼策略和合规审查。"
        ),
        "retro-windows": "用 Windows 95 复古电脑窗口界面介绍软件发展历史。",
        "retro-zine": (
            "为独立乐队新专辑和巡演制作发布演示，"
            "地下音乐杂志与 riso 油墨。"
        ),
        "studio": (
            "创意工作室品牌提案，黑底配电光黄，"
            "高冲击大字与克制图形。"
        ),
        "vellum": "介绍哈利波特与霍格沃茨魔法世界的奇幻文学演示。",
    }
    missed = []

    for theme in manifest["themes"]:
        theme_id = theme["id"]
        selection = theme["selection"]
        prompt = distinctive_prompts.get(theme_id)
        if prompt is None:
            industries = "、".join(selection["industry_fit"][:3])
            moods = "、".join(selection["mood_keywords"][:5])
            prompt = (
                f"请制作一份关于 {industries} 的演示。"
                f"视觉希望 {moods}，{selection['scheme']} 配色，"
                f"{selection.get('formality', 'medium')} 正式度。"
            )

        deck_path = tmp_path / theme_id / "deck.json"
        scaffold = _run(
            "inspect_deck_contract.js",
            "cover-editorial-v1",
            "--theme",
            "auto",
            "--title",
            f"Auto selection coverage: {theme_id}",
            "--fact",
            prompt,
            "--out",
            str(deck_path),
        )
        assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
        report = json.loads(
            (deck_path.parent / "qa" / "deck_contract.json").read_text()
        )
        selected = report["theme_selection"]["theme_id"]
        if selected != theme_id:
            missed.append((theme_id, selected))

    assert missed == []


def test_theme_catalog_preflight_returns_every_registered_theme() -> None:
    prompt = "创意机构年度品牌活动复盘，现代大胆，不要复古或像素风。"
    ranked = _run(
        "inspect_deck_contract.js",
        "--design-catalog",
        "--title",
        "品牌活动复盘",
        "--fact",
        prompt,
    )

    assert ranked.returncode == 0, ranked.stdout + ranked.stderr
    payload = json.loads(ranked.stdout)
    manifest = json.loads(
        (SKILL_DIR / "layouts" / "manifest.json").read_text(encoding="utf-8")
    )
    assert payload["mode"] == "theme_catalog"
    assert payload["candidate_count"] == len(manifest["themes"])
    assert payload["candidate_count"] == len(payload["candidates"])
    assert len({item["id"] for item in payload["candidates"]}) == len(
        payload["candidates"]
    )
    assert "creative-mode" in {item["id"] for item in payload["candidates"]}
    assert all(item["composition"]["allowed_families"] for item in payload["candidates"])
    assert payload["model_choice_contract"]["choose_from_registered_catalog_only"] is True
    assert payload["model_choice_contract"]["aesthetic_choice_owned_by_model"] is True


def test_model_theme_choice_selects_registered_theme_without_aesthetic_veto(
    tmp_path: Path,
) -> None:
    prompt = "创意机构年度品牌活动复盘，现代大胆，不要复古或像素风。"
    deck_path = tmp_path / "accepted" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "cards-grid-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "creative-mode",
        "--theme-model-reason",
        "创意机构场景与多彩海报语法更匹配。",
        "--title",
        "品牌活动复盘",
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    selection = report["theme_selection"]
    assert selection["theme_id"] == "creative-mode"
    assert selection["source"] == "model_selected"
    assert selection["model_choice"] == {
        "theme_id": "creative-mode",
        "reason": "创意机构场景与多彩海报语法更匹配。",
        "identity_basis": None,
        "confidence": None,
        "accepted": True,
    }


def test_registered_model_theme_choice_is_not_rejected_for_semantic_mismatch(
    tmp_path: Path,
) -> None:
    prompt = "创意机构年度品牌活动复盘，现代大胆，不要复古或像素风。"
    deck_path = tmp_path / "rejected" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "8-bit-orbit",
        "--theme-model-reason",
        "尝试使用像素主题。",
        "--title",
        "品牌活动复盘",
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    selection = report["theme_selection"]
    assert selection["theme_id"] == "8-bit-orbit"
    assert selection["source"] == "model_selected"
    assert selection["model_choice"]["accepted"] is True


def test_unregistered_model_theme_choice_falls_back_without_aesthetic_retry(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "invented-sports-theme",
        "--theme-model-reason",
        "模型返回了未注册主题。",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    selection = report["theme_selection"]
    assert selection["theme_id"] == "blue-professional"
    assert selection["source"] == "model_choice_rejected"
    assert selection["model_choice"]["rejection_reason"] == "unregistered_theme_id"


def test_low_contrast_identity_palette_is_kept_while_invalid_family_falls_back(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "bold-poster",
        "--theme-model-reason",
        "体育人物海报。",
        "--family",
        "technical-schematic",
        "--palette-background",
        "#FFFFFF",
        "--palette-text",
        "#111111",
        "--palette-primary",
        "#FFDF00",
        "--palette-accent",
        "#0B5D36",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "bold-poster"
    assert deck["design"]["family"] in {
        "poster-asymmetric",
        "cinematic-canvas",
        "product-showcase",
        "editorial-spread",
    }
    assert report["theme_selection"]["model_palette"]["accepted"] is True
    assert deck["design_contract"]["palette"]["primary"]["value"] == "#FFDF00"
    assert deck["design_contract"]["palette"]["accent"]["value"] == "#0B5D36"
    assert not any("Model palette rejected" in item for item in report["warnings"])
    assert any("not allowed by theme" in item for item in report["warnings"])


def test_low_text_contrast_still_rejects_model_palette(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "bold-poster",
        "--theme-model-reason",
        "体育人物海报。",
        "--palette-background",
        "#FFFFFF",
        "--palette-text",
        "#FFF59D",
        "--palette-primary",
        "#009C3B",
        "--palette-accent",
        "#002776",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert report["theme_selection"]["model_palette"]["accepted"] is False
    assert any("background and text contrast" in item for item in report["warnings"])


def test_missing_final_design_review_is_advisory(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "bold-poster",
        "--theme-model-reason",
        "体育人物海报。",
        "--palette-background",
        "#FFF8E7",
        "--palette-text",
        "#1A1A1A",
        "--palette-primary",
        "#0B5D36",
        "--palette-accent",
        "#FFDF00",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report_path = tmp_path / "qa" / "design_review_check.json"
    checked = _run(
        "design_review_receipt.js",
        str(deck_path),
        "validate",
        "--report",
        str(report_path),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["count"] == 0
    assert "receipt is missing" in report["warnings"][0]

    recorded = _run(
        "design_review_receipt.js",
        str(deck_path),
        "record",
        "--verdict",
        "accepted",
        "--reason",
        "最终设计语义一致。",
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["chart_style"] = "cool-ordinal"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    stale_report_path = tmp_path / "qa" / "design_review_stale.json"
    stale = _run(
        "design_review_receipt.js",
        str(deck_path),
        "validate",
        "--report",
        str(stale_report_path),
    )
    assert stale.returncode == 0, stale.stdout + stale.stderr
    stale_report = json.loads(stale_report_path.read_text(encoding="utf-8"))
    assert any("changed after semantic review" in item for item in stale_report["warnings"])


def test_registered_model_theme_choice_is_not_overridden_by_subject_rule(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "protected" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "data-intelligence",
        "--theme-model-reason",
        "复盘包含经营数据。",
        "--title",
        "智能制造工厂运营复盘",
        "--fact",
        "生产线 OEE、良率、精益生产与质量管理。",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    selection = report["theme_selection"]
    assert selection["theme_id"] == "data-intelligence"
    assert selection["source"] == "model_selected"
    assert selection["model_choice"]["accepted"] is True


def test_sports_biography_kpi_page_does_not_override_global_poster_theme(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline.update(
        {
            "title": "内马尔高光时刻",
            "deck_goal": "回顾内马尔职业生涯高光时刻与传奇纪录",
            "audience": "足球爱好者与大众球迷",
            "purpose": "人物传奇回顾演讲",
            "design_requirements": {"mood": "热血、传奇、桑巴激情"},
            "storyline": "桑托斯少年、巴萨巅峰、世界杯与奥运国家队高光",
        }
    )
    outline["slides"] = [
        {
            "page": 1,
            "title": "内马尔高光时刻",
            "message": "从桑托斯少年到巴西国家队传奇",
            "bullets": ["足球天才的热血旅程", "世界杯与奥运高光"],
            "layout": "cover-hero-v1",
            "visual": "football hero portrait",
            "evidence": [],
        },
        {
            "page": 2,
            "title": "桑托斯：天才少年横空出世",
            "message": "17岁完成职业首秀",
            "bullets": ["解放者杯冠军", "南美足球先生"],
            "layout": "timeline-horizontal-v1",
            "visual": "career timeline",
            "evidence": [],
        },
        {
            "page": 3,
            "title": "巴黎岁月：世界纪录",
            "message": "用一页数字呈现转会和进球纪录",
            "bullets": ["2.22亿欧元转会费", "巴黎生涯进球纪录"],
            "layout": "kpi-grid-v1",
            "visual": "kpi grid",
            "evidence": [],
        },
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "timeline-horizontal-v1",
        "kpi-grid-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "bold-poster",
        "--theme-model-reason",
        "体育人物传奇与热血海报表达更匹配。",
        "--theme-model-confidence",
        "high",
        "--theme-model-identity",
        "巴西国家队与桑巴足球",
        "--family",
        "poster-asymmetric",
        "--palette-background",
        "#FFF8E7",
        "--palette-text",
        "#1A1A1A",
        "--palette-primary",
        "#0B5D36",
        "--palette-accent",
        "#FFDF00",
        "--palette-secondary",
        "#1F5AA6",
        "--palette-accent-usage",
        "balanced",
        "--outline",
        str(outline_path),
        "--title",
        "内马尔高光时刻",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    selection = report["theme_selection"]
    assert selection["theme_id"] == "bold-poster"
    assert selection["model_choice"]["accepted"] is True
    assert selection["model_choice"]["identity_basis"] == "巴西国家队与桑巴足球"
    assert selection["model_choice"]["confidence"] == "high"
    palette = json.loads(deck_path.read_text(encoding="utf-8"))["design_contract"]["palette"]
    assert palette["background"]["value"] == "#FFF8E7"
    assert palette["text"]["value"] == "#1A1A1A"
    assert palette["primary"]["value"] == "#0B5D36"
    assert palette["accent"]["value"] == "#FFDF00"
    assert palette["secondary"]["value"] == "#1F5AA6"
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "--deck-bg: #FFF8E7" in html
    assert "--deck-primary: #0B5D36" in html
    assert "--deck-accent-color: #FFDF00" in html
    assert "--deck-chart-3: #1F5AA6" in html
    receipt = _run(
        "design_review_receipt.js",
        str(deck_path),
        "record",
        "--verdict",
        "revised",
        "--reason",
        "复核将身份修正为巴西国家队，并补充黄绿蓝配色。",
    )
    assert receipt.returncode == 0, receipt.stdout + receipt.stderr
    checked = _run(
        "design_review_receipt.js",
        str(deck_path),
        "validate",
        "--report",
        str(tmp_path / "qa" / "design_review_check.json"),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    review = json.loads(
        (tmp_path / "qa" / "design_review_check.json").read_text(encoding="utf-8")
    )
    assert review["verdict"] == "revised"
    assert review["count"] == 1
    assert review["warnings"] == []


def test_model_theme_choice_cannot_impersonate_explicit_user_lock(
    tmp_path: Path,
) -> None:
    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--theme",
        "studio",
        "--lock-theme",
        "--theme-model-choice",
        "creative-mode",
        "--theme-model-reason",
        "模型偏好。",
        "--out",
        str(tmp_path / "deck.json"),
    )

    assert result.returncode != 0
    assert "--theme-model-choice requires --theme auto" in result.stderr


def test_comic_brief_auto_selects_comic_panel_theme(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=4,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "用漫画分镜讲清 AI 客服从提问到响应的一天。",
            "audience": "产品与技术团队",
            "storyline": "漫画封面进入三格分镜，再用专业技术图解释系统，最后用漫画式收束。",
        }
    )
    outline["slides"][0].update(
        {
            "title": "AI 智能客服的一天",
            "layout": "漫画封面",
            "visual": "粗黑描边、网点纸和动作标签",
        }
    )
    outline["slides"][1].update(
        {
            "title": "一次咨询如何被接住",
            "layout": "三格漫画分镜",
            "visual": "对话气泡、拟声词和三个连续面板",
        }
    )
    outline["slides"][2].update(
        {
            "title": "专业系统架构",
            "layout": "专业技术图",
            "visual": "漫画面板外框中的清晰架构图",
        }
    )
    outline["slides"][3].update(
        {
            "title": "价值收束",
            "layout": "漫画总结页",
            "visual": "FIN 动作字和价值卡片",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "comic" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "technical-diagram-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--title",
        "AI 智能客服的一天",
        "--fact",
        "使用漫画分镜、对话气泡、拟声词、粗黑描边和波普漫画色彩",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "comic-panel"
    assert deck["design"]["family"] == "brutalist-frame"
    assert report["theme_selection"]["source"] == "content_inference"
    assert report["theme_selection"]["confidence"] == "high"
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "comic-panel visual language" in signals
    assert "comic-panel signature" in signals


def test_pixel_brief_auto_selects_8_bit_orbit_theme(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=4,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "用像素街机风介绍开发者工具平台。",
            "audience": "开发者与黑客松评委",
            "storyline": "从 8-bit 封面进入能力关卡，再展示系统地图和通关总结。",
        }
    )
    outline["slides"][0].update(
        {
            "title": "代码冒险：开发者平台",
            "layout": "像素街机封面",
            "visual": "深蓝 CRT 网格、8-bit 字体和霓虹状态条",
        }
    )
    outline["slides"][1].update(
        {
            "title": "三个能力关卡",
            "layout": "像素卡片",
            "visual": "街机 HUD、点阵方框和青粉黄霓虹",
        }
    )
    outline["slides"][2].update(
        {
            "title": "专业系统地图",
            "layout": "专业技术图",
            "visual": "像素显示器外框中的清晰架构图",
        }
    )
    outline["slides"][3].update(
        {
            "title": "继续下一关",
            "layout": "通关总结",
            "visual": "CONTINUE 状态字与行动卡片",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "pixel" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "technical-diagram-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--title",
        "代码冒险：开发者平台",
        "--fact",
        "使用 8-bit 像素艺术、CRT 扫描线、复古街机 HUD 和霓虹色",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "8-bit-orbit"
    assert deck["design"]["family"] == "retro-interface"
    assert report["theme_selection"]["source"] == "content_inference"
    assert report["theme_selection"]["confidence"] == "high"
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "pixel-arcade visual language" in signals
    assert "8-bit-orbit signature" in signals


@pytest.mark.parametrize(
    ("prompt", "expected_theme", "expected_family", "expected_signals"),
    [
        (
            "做一个介绍《哈利·波特》魔法世界的PPT",
            "vellum",
            "literary-minimal",
            {"subject rule: fantasy and wizarding worlds"},
        ),
        (
            "做一个介绍《我的世界》创造与冒险的PPT",
            "8-bit-orbit",
            "retro-interface",
            {"subject rule: sandbox and voxel games"},
        ),
        (
            "做一个介绍故宫与紫禁城文化遗产的PPT",
            "biennale-yellow",
            "editorial-spread",
            {"subject rule: museums and cultural heritage"},
        ),
        (
            "做一个介绍特斯拉电动汽车与未来出行的PPT",
            "neo-grid-bold",
            "brutalist-frame",
            {"subject rule: electric mobility and future vehicles"},
        ),
        (
            "介绍多云 AI 平台的技术架构和数据流",
            "technical-blueprint",
            "technical-schematic",
            {"keyword rule: architecture and infrastructure"},
        ),
        (
            "做一个 SaaS 产品介绍，面向客户，突出三个核心功能",
            "product-console",
            "product-showcase",
            {"keyword rule: SaaS and product interface"},
        ),
        (
            "做一份 Q2 经营数据复盘，包含核心 KPI、趋势和下一步动作",
            "data-intelligence",
            "analytical-exhibit",
            {"keyword rule: KPI and business intelligence"},
        ),
        (
            "说明 CRM、订单、客服和数据平台如何完成系统集成与数据流转",
            "technical-blueprint",
            "technical-schematic",
            {"keyword rule: architecture and infrastructure"},
        ),
        (
            "介绍 AI 文档助手，讲清核心功能、使用流程和产品价值",
            "product-console",
            "product-showcase",
            {"keyword rule: SaaS and product interface"},
        ),
        (
            "给董事会做年度风险分析，包含风险治理、预警信号和战略建议",
            "signal",
            "institutional-grid",
            {"keyword rule: board risk and advisory"},
        ),
        (
            "总结用户访谈结果，说明研究方法、研究发现和产品改进方向",
            "soft-editorial",
            "literary-minimal",
            {"keyword rule: qualitative user research"},
        ),
        (
            "做一个3页小学生认识太阳系的PPT，不要搜索",
            "daisy-days",
            "playful-collage",
            {"audience rule: children and primary education"},
        ),
        (
            "做一个3页介绍恐龙时代的PPT，不要搜索",
            "stencil-tablet",
            "brutalist-frame",
            {"subject rule: archaeology and ancient civilizations"},
        ),
        (
            "做一个3页垃圾分类课堂教学PPT，不要搜索",
            "pin-and-paper",
            "literary-minimal",
            {"user intent rule: classroom teaching and school workshop"},
        ),
        (
            "做一个3页季度经营复盘PPT，不要搜索",
            "data-intelligence",
            "analytical-exhibit",
            {"user intent rule: operating and performance review"},
        ),
        (
            "做一个3页企业数据中台投标方案PPT，不要搜索",
            "consulting-navy",
            "institutional-grid",
            {"user intent rule: formal proposal and procurement review"},
        ),
        (
            "做一个3页咖啡店融资路演PPT，不要搜索",
            "long-table",
            "editorial-spread",
            {"subject rule: food hospitality and social dining"},
        ),
        (
            "做一个3页小红书春季美妆新品发布PPT，不要搜索",
            "capsule",
            "playful-collage",
            {"subject rule: youthful beauty and lifestyle launch"},
        ),
        (
            "做一个3页敦煌壁画艺术介绍PPT，不要搜索",
            "stencil-tablet",
            "brutalist-frame",
            {"subject rule: archaeology and ancient civilizations"},
        ),
        (
            "做一个3页宫崎骏动画世界PPT，不要搜索",
            "soft-editorial",
            "literary-minimal",
            {"subject rule: gentle animation travel and wedding stories"},
        ),
        (
            "做一个3页介绍NBA篮球文化PPT，不要搜索",
            "bold-poster",
            "poster-asymmetric",
            {"subject rule: sports culture and high-energy competition"},
        ),
        (
            "做一个3页个人作品集PPT，不要搜索",
            "block-frame",
            "brutalist-frame",
            {"user intent rule: personal and creative portfolio"},
        ),
        (
            "做一个3页流浪动物领养公益活动PPT，不要搜索",
            "peoples-platform",
            "poster-asymmetric",
            {"user intent rule: public-interest and community campaign"},
        ),
        (
            "做一个3页气候变化研究摘要PPT，不要搜索",
            "grove",
            "literary-minimal",
            {"subject rule: climate nature and sustainability research"},
        ),
        (
            "做一个3页独立乐队新专辑发布PPT，不要搜索",
            "retro-zine",
            "retro-interface",
            {"subject rule: independent music and DIY culture"},
        ),
        (
            "做一个3页新员工入职与企业文化培训PPT，不要搜索",
            "people-handbook",
            "editorial-spread",
            {"user intent rule: employee onboarding and people programs"},
        ),
        (
            "做一个3页上市公司年度财报解读与资本配置PPT，不要搜索",
            "capital-ledger",
            "analytical-exhibit",
            {"subject rule: finance investment and capital strategy"},
        ),
        (
            "做一个3页临床试验结果与患者诊疗路径PPT，不要搜索",
            "clinical-atlas",
            "technical-schematic",
            {"subject rule: clinical medicine and patient care"},
        ),
        (
            "做一个3页城市公共服务与社会治理工作汇报PPT，不要搜索",
            "civic-brief",
            "institutional-grid",
            {"subject rule: government policy and public governance"},
        ),
        (
            "做一个3页硕士论文开题答辩PPT，包含文献综述和研究方法，不要搜索",
            "research-notebook",
            "literary-minimal",
            {"user intent rule: thesis and academic research"},
        ),
        (
            "做一个3页智能制造生产线与质量改善PPT，不要搜索",
            "factory-floor",
            "technical-schematic",
            {"subject rule: manufacturing operations and production quality"},
        ),
        (
            "做一个3页企业合规审查与法律风险PPT，不要搜索",
            "legal-docket",
            "institutional-grid",
            {"subject rule: legal matters evidence and compliance"},
        ),
        (
            "做一个3页房地产项目投拓与土地研判PPT，不要搜索",
            "property-atlas",
            "institutional-grid",
            {"subject rule: real estate development and asset facts"},
        ),
        (
            "做一个3页零售电商经营复盘与转化漏斗PPT，不要搜索",
            "commerce-pulse",
            "product-showcase",
            {"subject rule: retail ecommerce and merchandising performance"},
        ),
        (
            "做一个3页供应链物流网络与订单履约PPT，不要搜索",
            "logistics-control-tower",
            "product-showcase",
            {"subject rule: supply chain logistics and fulfillment"},
        ),
        (
            "做一份系统化、精密、严谨的说明材料",
            "technical-blueprint",
            "institutional-grid",
            {"mood match: technical precision"},
        ),
    ],
)
def test_natural_briefs_use_keyword_industry_and_mood_theme_selection(
    tmp_path: Path,
    prompt: str,
    expected_theme: str,
    expected_family: str,
    expected_signals: set[str],
) -> None:
    deck_path = tmp_path / expected_theme / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == expected_theme
    assert deck["design"]["family"] == expected_family
    assert report["theme_selection"]["confidence"] in {"medium", "high"}
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert expected_signals <= signals


@pytest.mark.parametrize(
    (
        "prompt",
        "theme_id",
        "identity_basis",
        "expected_background",
        "expected_primary",
        "expected_accent",
    ),
    [
        (
            "做一个介绍《我的世界》创造与冒险的PPT",
            "8-bit-orbit",
            "Minecraft forest, stone, and grass green",
            "#101A11",
            "#EEF5E9",
            "#65B741",
        ),
        (
            "做一个介绍故宫与紫禁城文化遗产的PPT",
            "biennale-yellow",
            "Forbidden City vermilion, parchment, and gold",
            "#F4E8D1",
            "#7A1D16",
            "#C9A227",
        ),
        (
            "做一个介绍特斯拉电动汽车与未来出行的PPT",
            "neo-grid-bold",
            "Tesla black, white, and red",
            "#FFFFFF",
            "#111111",
            "#E82127",
        ),
    ],
)
def test_model_structured_subject_palette_overrides_theme_colors(
    tmp_path: Path,
    prompt: str,
    theme_id: str,
    identity_basis: str,
    expected_background: str,
    expected_primary: str,
    expected_accent: str,
) -> None:
    deck_path = tmp_path / expected_accent.removeprefix("#") / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        theme_id,
        "--theme-model-reason",
        "主体视觉身份与该主题匹配。",
        "--theme-model-confidence",
        "high",
        "--theme-model-identity",
        identity_basis,
        "--palette-background",
        expected_background,
        "--palette-text",
        expected_primary,
        "--palette-primary",
        expected_primary,
        "--palette-accent",
        expected_accent,
        "--palette-accent-usage",
        "sparse",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    palette = deck["design_contract"]["palette"]
    assert palette["source"] == "inferred"
    assert palette["background"]["value"] == expected_background
    assert palette["primary"]["value"] == expected_primary
    assert palette["accent"]["value"] == expected_accent
    assert palette["accent_usage"] == "sparse"


def test_explicit_palette_outranks_named_subject_palette(tmp_path: Path) -> None:
    prompt = "做一个特斯拉PPT，配色使用深蓝、米白和少量橙色点缀"
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "neo-grid-bold",
        "--theme-model-reason",
        "电动汽车发布使用高对比网格。",
        "--theme-model-identity",
        "特斯拉品牌",
        "--palette-background",
        "#FFFFFF",
        "--palette-text",
        "#111111",
        "--palette-primary",
        "#111111",
        "--palette-accent",
        "#E82127",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    palette = json.loads(deck_path.read_text(encoding="utf-8"))["design_contract"]["palette"]
    assert palette["source"] == "explicit"
    assert palette["background"]["value"] == "#F4EFE4"
    assert palette["primary"]["value"] == "#173B63"
    assert palette["accent"]["value"] == "#D97706"


def test_explicit_cream_background_and_black_text_stay_distinct(
    tmp_path: Path,
) -> None:
    prompt = "作品集采用米白底、纯黑字，并用高饱和色少量点缀。"
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    palette = json.loads(deck_path.read_text(encoding="utf-8"))["design_contract"]["palette"]
    assert palette["background"]["value"] == "#F4EFE4"
    assert "#111111" in palette["requested"]
    assert "primary" not in palette


def test_user_collage_opt_out_refines_family_without_replacing_it(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "做一份友好圆润的新员工培训，保留胶囊色块，但不要拼贴。".encode()
    ).decode()
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "timeline-horizontal-v1",
        "--theme",
        "capsule",
        "--family",
        "playful-collage",
        "--style-override",
        "shadow=off",
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["theme_id"] == "capsule"
    assert deck["design"]["family"] == "playful-collage"
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cards-grid-v1",
        "timeline-horizontal-v1",
    ]
    assert deck["design_contract"]["style_overrides"] == {
        "collage": "off",
        "shadow": "off",
    }

    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-composition="playful-collage"' in html
    assert 'data-deck-style-collage="off"' in html
    assert 'data-deck-style-shadow="off"' in html
    assert 'body[data-deck-style-collage="off"] .composition-collage-pin' in html


def test_controlled_redesign_merges_style_overrides_without_changing_layouts(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--theme",
        "capsule",
        "--family",
        "playful-collage",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    before = json.loads(deck_path.read_text(encoding="utf-8"))
    redesign_path = tmp_path / "deck.redesign.json"
    redesign_path.write_text(
        json.dumps(
            {
                "style_overrides": {
                    "collage": "off",
                    "decorations": "off",
                    "stagger": "off",
                    "irregular_grid": "off",
                    "shadow": "off",
                    "texture": "off",
                    "gradient": "off",
                    "radius": "rounded",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    redesigned = _run(
        "apply_deck_redesign.js",
        str(deck_path),
        str(redesign_path),
    )

    assert redesigned.returncode == 0, redesigned.stdout + redesigned.stderr
    after = json.loads(deck_path.read_text(encoding="utf-8"))
    assert after["theme_id"] == before["theme_id"]
    assert after["design"] == before["design"]
    assert [slide["layout_id"] for slide in after["slides"]] == [
        slide["layout_id"] for slide in before["slides"]
    ]
    assert after["design_contract"]["style_overrides"] == {
        "collage": "off",
        "decorations": "off",
        "stagger": "off",
        "irregular_grid": "off",
        "shadow": "off",
        "texture": "off",
        "gradient": "off",
        "radius": "rounded",
    }


def test_software_product_does_not_match_soft_style_by_substring(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--title",
        "software product overview",
        "--fact",
        "software product overview",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert report["theme_selection"]["theme_id"] == "product-console"
    signals = {
        item["signal"] for item in report["theme_selection"]["matched_signals"]
    }
    assert "soft palette mood" not in signals


def test_lock_theme_preserves_explicit_user_choice_for_onboarding(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=2,
        source_mode="user_provided",
    )
    outline["deck_goal"] = "清爽亲和的新员工入职培训"
    outline["audience"] = "刚入职的新员工"
    outline["storyline"] = "从欢迎进入内部培训模块。"
    outline["slides"][0]["layout"] = "浅色欢迎封面"
    outline["slides"][0]["visual"] = "柔和品牌色封面"
    outline["slides"][1]["layout"] = "模块卡片"
    outline["slides"][1]["visual"] = "浅色模块卡片"
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "locked" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "--theme",
        "blue-professional",
        "--lock-theme",
        "--outline",
        str(outline_path),
        "--title",
        "新员工入职培训",
        "--fact",
        "清爽亲和，浅色背景为主",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["theme_id"] == "blue-professional"
    assert deck["design"]["family"] == "institutional-grid"
    assert report["theme_selection"]["source"] == "explicit_locked"


def test_theme_family_allowlist_preserves_compatible_choice_and_falls_back_on_mismatch(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "compatible" / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "chart-data-v1",
        "--theme",
        "blue-professional",
        "--family",
        "analytical-exhibit",
        "--design-seed",
        "compatible_seed",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["design"]["family"] == "analytical-exhibit"

    validation = _run("validate_deck_spec.js", str(deck_path))
    assert validation.returncode == 0, validation.stdout + validation.stderr
    html_path = deck_path.parent / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-composition="analytical-exhibit"' in html
    assert 'data-composition-template="exhibit"' in html

    rejected = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--theme",
        "retro-windows",
        "--family",
        "technical-schematic",
        "--design-seed",
        "mismatch_seed",
    )
    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    payload = json.loads(rejected.stdout)
    assert payload["authoring_rules"]["design_policy"]["selection_source"] == (
        "model_family_rejected"
    )
    assert payload["authoring_rules"]["design_policy"]["selected_family"] != (
        "technical-schematic"
    )


def test_scaffold_infers_product_family_and_cover_image_from_outline(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "product-outline.json"
    outline = _write_outline(outline_path)
    outline["deck_goal"] = "展示桌面客户端的产品能力与用户流程"
    outline["storyline"] = "从产品主界面进入功能工作流，最后给出下一步。"
    outline["slides"][0].update(
        {
            "title": "把本地工作流放进一个客户端",
            "message": "首页需要让观众一眼理解产品形态。",
            "layout": "cover",
            "visual": "客户端主界面 hero，包含文档、表格、PPT 与本地图标",
        }
    )
    outline["slides"][1].update(
        {
            "message": "用功能卡片解释产品流程。",
            "visual": "产品功能卡片与用户流程",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "product" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "blue-professional",
        "--title",
        "Box Agent 客户端",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    manifest = json.loads(
        (deck_path.parent / "assets" / "generated" / "manifest.json").read_text()
    )
    assert deck["design"]["family"] == "product-showcase"
    assert report["design_selection"]["source"] == "content_inference"
    assert report["design_selection"]["family"] == "product-showcase"
    assert report["design_selection"]["matched_signals"]
    cover = manifest["image_plan"][0]
    assert cover["decision"] == "generate"
    assert cover["required"] is True
    assert "product or interface cover visual" in cover["decision_reason"]
    assert "conceptual product-interface illustration" in cover["prompt"]
    assert "No embedded text, no logos, no watermark" in cover["prompt"]


def test_scaffold_promotes_person_profile_cover_from_slide_visual(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "profile-outline.json"
    outline = _write_outline(outline_path)
    outline["deck_goal"] = "用权威资料介绍拉明·亚马尔的履历与成长路径。"
    outline["storyline"] = "从基础身份、成长路径到关键纪录和未来看点。"
    outline["slides"][0].update(
        {
            "title": "拉明·亚马尔：打破年龄边界的新星",
            "message": "先建立主角识别度与整套演示的叙事基调。",
            "layout": "cover",
            "visual": "人物海报式封面，强调年轻、速度与聚光灯感",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "profile" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    manifest = json.loads(
        (deck_path.parent / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["slot"] == "hero"
    assert cover["decision"] == "generate"
    assert cover["required"] is True
    assert "visual story" in cover["decision_reason"]
    assert "人物海报式封面" in cover["prompt"]


def test_scaffold_infers_technical_family_from_code_and_system_outline(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "technical-outline.json"
    outline = _write_outline(outline_path)
    outline["deck_goal"] = "解释 Agent 运行时的系统架构与协作方式"
    outline["storyline"] = "从代码执行入口进入协作节点和运行时数据流。"
    outline["slides"][0].update(
        {
            "title": "一个可观察的 Agent 运行时",
            "message": "封面先建立代码与系统连接关系。",
            "layout": "cover",
            "visual": "代码窗口连接多个协作节点的技术架构图",
        }
    )
    outline["slides"][1].update(
        {
            "message": "沿着运行时数据流解释模块协作。",
            "visual": "系统架构、模块接口与数据流节点",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "technical" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "timeline-horizontal-v1",
        "closing-next-steps-v1",
        "--theme",
        "blue-professional",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    manifest = json.loads(
        (deck_path.parent / "assets" / "generated" / "manifest.json").read_text()
    )
    assert deck["design"]["family"] == "technical-schematic"
    assert report["design_selection"]["source"] == "content_inference"
    assert manifest["image_plan"][0]["decision"] == "generate"
    assert "code or technical-system cover visual" in (
        manifest["image_plan"][0]["decision_reason"]
    )


def test_explicit_family_overrides_outline_inference(tmp_path: Path) -> None:
    outline_path = tmp_path / "product-outline.json"
    outline = _write_outline(outline_path)
    outline["deck_goal"] = "展示产品主界面与功能流程"
    outline["slides"][0]["visual"] = "客户端主界面 UI 截图"
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "explicit" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "blue-professional",
        "--family",
        "analytical-exhibit",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert deck["design"]["family"] == "analytical-exhibit"
    assert report["design_selection"]["source"] == "model_selected_family"


def test_single_theme_record_can_define_composition_policy_without_legacy_mapping() -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test composition policy")
    core = SCRIPTS_DIR / "composition_core.js"
    probe = """
const composition = require(process.argv[1]);
const theme = {
  id: "single-file-theme",
  composition: {
    default_family: "literary-minimal",
    allowed_families: ["literary-minimal", "cinematic-canvas"],
  },
};
const manifest = composition.compositionManifestRecord(theme);
const design = composition.createDeckDesign(theme, "single_file_seed", "cinematic-canvas");
process.stdout.write(JSON.stringify({ manifest, design }));
"""
    result = subprocess.run(
        [str(NODE), "-e", probe, str(core)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["manifest"]["default_family"] == "literary-minimal"
    assert payload["manifest"]["allowed_families"] == [
        "literary-minimal",
        "cinematic-canvas",
    ]
    assert payload["manifest"]["default_direction"] == "narrative-pages"
    assert payload["manifest"]["allowed_directions"] == [
        "narrative-pages",
        "visual-impact",
    ]
    assert payload["design"]["family"] == "cinematic-canvas"
    assert payload["design"]["variant"] in {
        "full-bleed",
        "split-film",
        "chapter-cut",
    }


def test_editor_defaults_and_every_layout_migration_validate() -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test the controlled deck compiler")
    registry = SKILL_DIR / "layouts" / "registry.js"
    core = SCRIPTS_DIR / "deck_spec_core.js"
    probe = """
const registry = require(process.argv[1]);
const core = require(process.argv[2]);
const slides = registry.layouts.map((layout, index) => ({
  id: `slide-${index + 1}`,
  layout_id: layout.id,
  props: registry.createEditorProps(layout.id),
}));
const deck = {
  schema_version: 1,
  title: "Editor contracts",
  theme_id: "blue-professional",
  slides,
};
const defaults = core.validateAndNormalizeDeck(deck);
if (!defaults.ok) throw new Error(defaults.issues.join("\\n"));
const pathParts = value => String(value).split(".").filter(Boolean);
const getAtPath = (target, value) => pathParts(value).reduce(
  (cursor, part) => cursor && cursor[part],
  target
);
const contractAtPath = (layout, value) => {
  const parts = pathParts(value);
  let fields = layout.fields;
  let contract = null;
  for (let index = 0; index < parts.length; index += 1) {
    contract = fields && fields[parts[index]];
    if (!contract) return null;
    if (index < parts.length - 1) fields = contract.shape;
  }
  return contract;
};
let enumControls = 0;
let collectionControls = 0;
for (const layout of registry.layouts) {
  const controls = layout.editor.controls || {};
  for (const [pathValue, config] of Object.entries(controls.enums || {})) {
    const contract = contractAtPath(layout, pathValue);
    if (!contract || contract.type !== "enum") throw new Error(`${layout.id}.${pathValue} is not enum`);
    const declared = [...contract.values].sort();
    const labeled = Object.keys(config.options || {}).sort();
    if (JSON.stringify(declared) !== JSON.stringify(labeled)) {
      throw new Error(`${layout.id}.${pathValue} enum labels do not match contract`);
    }
    enumControls += 1;
  }
  for (const [pathValue, config] of Object.entries(controls.collections || {})) {
    const contract = contractAtPath(layout, pathValue);
    const props = JSON.parse(JSON.stringify(layout.editor.defaultProps));
    const items = getAtPath(props, pathValue);
    if (!contract || contract.type !== "array" || !Array.isArray(items)) {
      throw new Error(`${layout.id}.${pathValue} is not an editable array`);
    }
    if (items.length >= contract.maxItems) throw new Error(`${layout.id}.${pathValue} has no add capacity`);
    items.push(JSON.parse(JSON.stringify(config.itemDefault)));
    const candidate = core.validateAndNormalizeDeck({
      ...deck,
      slides: [{ id: "control-probe", layout_id: layout.id, props }],
    });
    if (!candidate.ok) throw new Error(candidate.issues.join("\\n"));
    collectionControls += 1;
  }
}
let migrations = 0;
for (const source of slides) {
  for (const target of slides) {
    const candidate = {
      ...deck,
      slides: [{
        id: "probe",
        layout_id: target.layout_id,
        props: registry.createEditorProps(target.layout_id, source),
        layout_drafts: { [source.layout_id]: source.props },
      }],
    };
    const result = core.validateAndNormalizeDeck(candidate);
    if (!result.ok) {
      throw new Error(`${source.layout_id} -> ${target.layout_id}\\n${result.issues.join("\\n")}`);
    }
    migrations += 1;
  }
}
console.log(JSON.stringify({ layouts: slides.length, migrations, enumControls, collectionControls }));
"""

    result = subprocess.run(
        [str(NODE), "-e", probe, str(registry), str(core)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "layouts": 33,
        "migrations": 1089,
        "enumControls": 33,
        "collectionControls": 31,
    }


def test_layout_query_filters_role_density_and_media_capacity() -> None:
    result = _run(
        "query_layouts.js",
        "--role",
        "comparison",
        "--density",
        "medium-high",
        "--media-count",
        "0",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [layout["id"] for layout in payload["layouts"]] == [
        "comparison-two-column-v1"
    ]
    assert payload["layouts"][0]["score"] == 135


def test_compact_theme_and_layout_list_aliases_are_supported() -> None:
    themes = _run("inspect_deck_contract.js", "--list-themes")
    layouts = _run("query_layouts.js", "--list")

    assert themes.returncode == 0, themes.stderr
    assert layouts.returncode == 0, layouts.stderr
    theme_payload = json.loads(themes.stdout)
    layout_payload = json.loads(layouts.stdout)
    theme_ids = [item["id"] for item in theme_payload["themes"]]
    assert theme_payload["composition_directions"] == list(COMPOSITION_DIRECTIONS)
    assert len(theme_ids) == 47
    assert theme_ids == sorted(theme_ids)
    assert "playful" not in theme_ids
    assert {
        "signal",
        "studio",
        "vellum",
        "8-bit-orbit",
        "technical-blueprint",
        "product-console",
        "data-intelligence",
        "people-handbook",
        "capital-ledger",
        "clinical-atlas",
        "civic-brief",
        "research-notebook",
        "factory-floor",
        "legal-docket",
        "property-atlas",
        "commerce-pulse",
        "logistics-control-tower",
    } <= set(theme_ids)
    assert layout_payload["count"] == 33
    assert {item["id"] for item in layout_payload["layouts"]} >= {
        "architecture-layered-v1",
        "system-integration-v1",
        "technical-diagram-v1",
        "dashboard-overview-v1",
        "project-case-study-v1",
        "image-hero-split-v1",
        "image-feature-v1",
        "image-full-bleed-v1",
        "chart-bar-v1",
        "chart-data-v1",
        "table-data-v1",
    }
    assert len(themes.stdout) + len(layouts.stdout) < 45_000


def test_removed_playful_theme_is_rejected_by_validation(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["theme_id"] = "playful"
    deck_path = tmp_path / "removed-playful-theme.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    payload = json.loads(result.stdout.split("\nDeck spec validation", 1)[0])
    assert payload["issues"][0].startswith('Unknown theme_id: "playful"')


def test_scaffold_normalizes_known_semantic_theme_alias(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--theme",
        "carnival",
        "--title",
        "巴西足球历史",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selected_theme"]["id"] == "bold-poster"
    assert payload["theme_id_normalization"] == {
        "from": "carnival",
        "to": "bold-poster",
    }
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["theme_id_normalization"] == payload["theme_id_normalization"]
    assert json.loads(deck_path.read_text())["theme_id"] == "bold-poster"


def test_scaffold_normalizes_comic_theme_alias(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "--theme",
        "comic",
        "--lock-theme",
        "--title",
        "漫画分镜测试",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selected_theme"]["id"] == "comic-panel"
    assert payload["theme_id_normalization"] == {
        "from": "comic",
        "to": "comic-panel",
    }
    assert json.loads(deck_path.read_text())["theme_id"] == "comic-panel"


def test_scaffold_normalizes_pixel_theme_alias(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "--theme",
        "pixel",
        "--lock-theme",
        "--title",
        "像素街机测试",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selected_theme"]["id"] == "8-bit-orbit"
    assert payload["theme_id_normalization"] == {
        "from": "pixel",
        "to": "8-bit-orbit",
    }
    assert json.loads(deck_path.read_text())["theme_id"] == "8-bit-orbit"


def test_deck_contract_scaffolds_ordered_repeated_layouts_once(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "image-hero-split-v1",
        "image-hero-split-v1",
        "--theme",
        "block-frame",
        "--title",
        "NOON Studio",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["default_theme_id"] == "blue-professional"
    assert payload["selected_theme"]["id"] == "block-frame"
    assert [layout["id"] for layout in payload["layouts"]] == [
        "cover-hero-v1",
        "image-hero-split-v1",
    ]
    assert payload["layouts"][0]["fields"]["hero"]["type"] == "media"
    assert payload["layouts"][1]["fields"]["image"]["required"] is True
    assert payload["deck_skeleton"]["theme_id"] == "block-frame"
    assert payload["deck_skeleton"]["design"]["version"] == 1
    assert len(payload["deck_skeleton"]["design"]["seed"]) == 16
    assert payload["deck_skeleton"]["design"]["family"] == "brutalist-frame"
    assert payload["deck_skeleton"]["design"]["variant"] in {
        "block-grid",
        "offset-frame",
        "ledger-frame",
    }
    assert payload["deck_skeleton"]["truth_contract"] == {
        "mode": "source_bound",
        "source_facts": [],
        "assumptions": [],
    }
    assert payload["deck_skeleton"]["slides"][0]["props"]["hero"] is None
    assert [slide["layout_id"] for slide in payload["deck_skeleton"]["slides"]] == [
        "cover-hero-v1",
        "image-hero-split-v1",
        "image-hero-split-v1",
    ]
    assert json.loads(deck_path.read_text()) == payload["deck_skeleton"]
    assert payload["deck_file"] == str(deck_path.resolve())
    image_manifest = deck_path.parent / "assets" / "generated" / "manifest.json"
    assert payload["image_manifest"] == str(image_manifest.resolve())
    image_payload = json.loads(image_manifest.read_text())
    assert image_payload["mode"] == "auto"
    assert image_payload["deck"]["design"] == payload["deck_skeleton"]["design"]
    assert len(image_payload["image_plan"]) == 3
    assert image_payload["image_plan"][0]["slot"] == "hero"
    assert image_payload["image_plan"][0]["decision"] == "generate"
    assert image_payload["image_plan"][0]["status"] == "pending"
    assert image_payload["image_plan"][1]["slot"] == "image"
    assert image_payload["image_plan"][1]["decision"] == "generate"
    assert image_payload["image_plan"][1]["status"] == "pending"
    contract_report = deck_path.parent / "qa" / "deck_contract.json"
    assert payload["contract_report"] == str(contract_report.resolve())
    contract_payload = json.loads(contract_report.read_text())
    assert contract_payload["ok"] is True
    assert contract_payload["slide_count"] == 3
    assert contract_payload["image_mode"] == "auto"
    assert contract_payload["design"] == payload["deck_skeleton"]["design"]
    assert contract_payload["layout_plan"] == [
        "cover-hero-v1",
        "image-hero-split-v1",
        "image-hero-split-v1",
    ]
    assert len(contract_payload["contract_hash"]) == 64
    assert payload["authoring_rules"]["write_policy"]["initial_full_deck_writes"] == 0
    assert payload["authoring_rules"]["write_policy"]["initial_scaffold_writes"] == 1
    assert payload["authoring_rules"]["write_policy"]["batch_patch_command"].startswith(
        "Write deck.patch.json once, then run ${BOX_AGENT_NODE:-node} "
    )

    repeated = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--out",
        str(deck_path),
    )
    assert repeated.returncode == 1
    assert "Refusing to overwrite existing deck skeleton" in repeated.stderr

    original_deck = deck_path.read_text(encoding="utf-8")
    (deck_path.parent / "deck.patch.json").write_text("{}", encoding="utf-8")
    forced_reset = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--out",
        str(deck_path),
        "--force",
    )
    assert forced_reset.returncode == 1
    assert "Refusing --force reset because downstream deck artifacts" in forced_reset.stderr
    assert deck_path.read_text(encoding="utf-8") == original_deck


def test_scaffold_binds_outline_pages_and_imports_public_research_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path)
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [slide["source_outline_page"] for slide in deck["slides"]] == [1, 2, 3]
    assert deck["truth_contract"]["research_facts"] == [
        slide["evidence"][0] for slide in outline["slides"]
    ]
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["outline_binding"]["outline_file"] == str(outline_path.resolve())
    assert report["outline_binding"]["source_mode"] == (
        "public_authoritative_research"
    )
    assert report["outline_binding"]["page_count"] == 3
    assert report["outline_binding"]["evidence_import_count"] == 3
    assert len(report["outline_binding"]["outline_hash"]) == 64
    payload = json.loads(result.stdout)
    assert payload["authoring_rules"]["outline_policy"]["pages"][1]["title"] == (
        "主题页 2"
    )


def test_scaffold_derives_ordered_layout_plan_from_outline_when_ids_are_omitted(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path, page_count=3, source_mode="user_provided")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert len(deck["slides"]) == 3
    assert all(slide["layout_id"] for slide in deck["slides"])
    assert [slide["source_outline_page"] for slide in deck["slides"]] == [1, 2, 3]


def test_auto_mode_promotes_one_generic_inner_page_to_image_feature(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=5,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "季度业务复盘",
            "layout": "cover",
            "visual": "文字封面",
        }
    )
    outline["slides"][-1].update(
        {
            "title": "下一步",
            "layout": "closing",
            "visual": "行动式收尾",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cover-editorial-v1",
        "image-feature-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
    ]
    payload = json.loads(result.stdout)
    assert {
        "slide": 2,
        "from": "cards-grid-v1",
        "to": "image-feature-v1",
        "reason": (
            "ordinary multi-page deck promotes one eligible qualitative card "
            "page to an image-led narrative"
        ),
    } in payload["layout_normalizations"]
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    assert manifest["image_plan"][1]["decision"] == "generate"
    assert manifest["image_plan"][1]["required"] is True


def test_auto_inner_image_promotion_respects_no_images(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=5,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {"title": "季度业务复盘", "layout": "cover", "visual": "文字封面"}
    )
    outline["slides"][-1].update(
        {"title": "下一步", "layout": "closing", "visual": "行动式收尾"}
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--no-images",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert "image-feature-v1" not in {
        slide["layout_id"] for slide in deck["slides"]
    }
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    assert all(entry["decision"] == "skip" for entry in manifest["image_plan"])


def test_auto_inner_image_promotion_skips_quantitative_page(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=5,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {"title": "季度业务复盘", "layout": "cover", "visual": "文字封面"}
    )
    outline["slides"][1].update(
        {
            "title": "业务增长",
            "message": "ARR 从 500 万元增长到 800 万元。",
            "bullets": ["上期 ARR 500 万元", "本期 ARR 800 万元"],
            "layout": "cards",
            "visual": "摘要信息卡",
        }
    )
    outline["slides"][-1].update(
        {"title": "下一步", "layout": "closing", "visual": "行动式收尾"}
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][1]["layout_id"] == "cards-grid-v1"
    assert deck["slides"][2]["layout_id"] == "image-feature-v1"


def test_scaffold_rejects_outline_count_and_normalizes_qualitative_quantitative_layout(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path)

    count_mismatch = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(tmp_path / "count-mismatch.json"),
    )
    assert count_mismatch.returncode == 1
    assert "contains 3 page(s)" in count_mismatch.stderr
    assert "ordered layout plan contains 2" in count_mismatch.stderr

    qualitative_chart = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "kpi-grid-v1",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(tmp_path / "qualitative-chart.json"),
    )
    assert qualitative_chart.returncode == 0, qualitative_chart.stderr
    contract = json.loads(qualitative_chart.stdout)
    assert contract["layout_normalizations"] == [
        {
            "slide": 2,
            "from": "kpi-grid-v1",
            "to": "cards-grid-v1",
            "reason": (
                "qualitative outline pages use a safe editable cards layout "
                "instead of invented chart or KPI values"
            ),
        }
    ]
    deck = json.loads((tmp_path / "qualitative-chart.json").read_text())
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cards-grid-v1",
        "cards-grid-v1",
        "cards-grid-v1",
    ]


def test_market_size_chart_scaffold_respects_illustrative_authorization(
    tmp_path: Path,
) -> None:
    outline = {
        "deck_goal": "完成 AI 质检与智能排产平台融资沟通",
        "audience": "一线 VC 投资人",
        "source_mode": "user_provided",
        "storyline": "从产品切入点进入市场扩展空间。",
        "slides": [
            {
                "page": 1,
                "title": "市场规模：以中小制造工厂软件化改造为切入口",
                "message": (
                    "市场规模页用示意假设展示 TAM/SAM/SOM 逻辑，帮助投资人"
                    "理解从质检与排产切入到工厂级运营平台的扩展空间。"
                ),
                "bullets": [
                    (
                        "市场规模图使用示意假设：目标可服务市场按中小制造工厂"
                        "软件与 AI 改造支出分层估算。"
                    ),
                    "切入市场：优先服务有高频质检与排产需求的离散制造工厂。",
                    "正式融资材料建议补充第三方市场报告或客户池测算。",
                ],
                "layout": "market-sizing-chart",
                "visual": (
                    "市场规模图：TAM/SAM/SOM 漏斗或分层柱状图；金额使用示意"
                    "假设，并在备注标明“示意 / 假设”。"
                ),
                "evidence": [
                    (
                        "用户要求：具体市场金额未提供，使用合理假设并标明"
                        "“示意 / 假设”。"
                    )
                ],
            }
        ],
    }

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    safe_outline = safe_dir / "outline.json"
    safe_outline.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    safe_scaffold = _run(
        "inspect_deck_contract.js",
        "chart-bar-v1",
        "--outline",
        str(safe_outline),
        "--out",
        str(safe_dir / "deck.json"),
    )

    assert safe_scaffold.returncode == 0, safe_scaffold.stdout + safe_scaffold.stderr
    safe_payload = json.loads(safe_scaffold.stdout)
    assert safe_payload["deck_skeleton"]["slides"][0]["layout_id"] == "cards-grid-v1"
    assert safe_payload["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "chart-bar-v1",
            "to": "cards-grid-v1",
            "reason": (
                "outline names a quantitative visual without quantitative evidence "
                "or an authorized illustrative assumption, so preserve the qualitative "
                "argument in editable cards instead of inventing values"
            ),
        }
    ]

    authorized_dir = tmp_path / "authorized"
    authorized_dir.mkdir()
    authorized_outline = authorized_dir / "outline.json"
    authorized_outline.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    source_text = (
        "制作市场规模页。未提供的具体数据请使用合理假设数据，"
        "并在备注中标明为示意 / 假设。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    authorized_deck = authorized_dir / "deck.json"
    authorized_scaffold = _run(
        "inspect_deck_contract.js",
        "chart-bar-v1",
        "--outline",
        str(authorized_outline),
        "--assumption",
        "市场规模示意假设：TAM 100 亿元、SAM 30 亿元、SOM 5 亿元",
        "--out",
        str(authorized_deck),
        env=env,
    )

    assert (
        authorized_scaffold.returncode == 0
    ), authorized_scaffold.stdout + authorized_scaffold.stderr
    authorized_payload = json.loads(authorized_scaffold.stdout)
    assert (
        authorized_payload["deck_skeleton"]["slides"][0]["layout_id"]
        == "chart-bar-v1"
    )
    assert authorized_payload.get("layout_normalizations", []) == []

    deck = json.loads(authorized_deck.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "市场空间",
            "title": outline["slides"][0]["title"],
            "subtitle": outline["slides"][0]["message"],
            "series_label": "示意规模",
            "items": [
                {"label": "TAM", "value": "100 亿元", "note": "总目标市场"},
                {"label": "SAM", "value": "30 亿元", "note": "可服务市场"},
                {"label": "SOM", "value": "5 亿元", "note": "近期可获得市场"},
            ],
            "insight": outline["slides"][0]["bullets"][0],
            "source": "示意 / 假设，正式材料需替换为可验证市场数据",
        }
    )
    authorized_deck.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    spec_check = _run("validate_deck_spec.js", str(authorized_deck))

    assert spec_check.returncode == 0, spec_check.stdout + spec_check.stderr

    illustrative_dir = tmp_path / "illustrative"
    illustrative_dir.mkdir()
    illustrative_outline = illustrative_dir / "outline.json"
    illustrative_outline.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    illustrative_scaffold = _run(
        "inspect_deck_contract.js",
        "chart-bar-v1",
        "--truth-mode",
        "illustrative",
        "--outline",
        str(illustrative_outline),
        "--out",
        str(illustrative_dir / "deck.json"),
    )

    assert (
        illustrative_scaffold.returncode == 0
    ), illustrative_scaffold.stdout + illustrative_scaffold.stderr
    assert (
        json.loads(illustrative_scaffold.stdout)["deck_skeleton"]["slides"][0][
            "layout_id"
        ]
        == "chart-bar-v1"
    )


def test_market_size_chart_warns_for_unauthorized_assumption(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "市场规模",
            "message": "用 TAM/SAM/SOM 说明市场空间。",
            "layout": "market-sizing-chart",
            "visual": "TAM/SAM/SOM 分层柱状图",
            "evidence": [],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作市场规模页，不可以使用假设数据。".encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "chart-bar-v1",
        "--outline",
        str(outline_path),
        "--assumption",
        "TAM 100 亿元、SAM 30 亿元、SOM 5 亿元",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert deck_path.is_file()
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert any(
        "requires explicit user permission" in warning
        for warning in report["warnings"]
    )


def test_scaffold_recovers_architecture_integration_and_qualitative_dashboard(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "说明智能客服解决方案如何落地",
                "audience": "采购、业务与 IT 评审人",
                "source_mode": "user_provided",
                "storyline": "从技术架构进入系统集成，再以管理闭环收束。",
                "slides": [
                    {
                        "page": 1,
                        "title": "真实技术分层架构",
                        "message": "按职责边界组织各层能力。",
                        "bullets": ["触点、AI 服务、业务集成与运营治理分层"],
                        "layout": "architecture-layered-v1",
                        "visual": "分层架构图，包含触点、AI 服务、业务系统与安全运维模块",
                        "evidence": [],
                    },
                    {
                        "page": 2,
                        "title": "系统集成与数据流设计",
                        "message": "中心平台与现有系统双向连接。",
                        "bullets": ["连接订单、会员、CRM、工单和统一认证"],
                        "layout": "system-integration-v1",
                        "visual": "系统集成图，平台居中并标注与外围系统的数据流",
                        "evidence": [],
                    },
                    {
                        "page": 3,
                        "title": "数据看板与管理闭环",
                        "message": "先定义管理指标域，接入后再呈现真实值。",
                        "bullets": ["关注效率、体验、分流、知识与稳定性"],
                        "layout": "dashboard-overview-v1",
                        "visual": "管理驾驶舱示意，不展示未经提供的数值",
                        "evidence": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "kpi-grid-v1",
        "kpi-grid-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    contract = json.loads(result.stdout)
    assert [item["to"] for item in contract["layout_normalizations"]] == [
        "technical-diagram-v1",
        "technical-diagram-v1",
        "dashboard-overview-v1",
    ]
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "technical-diagram-v1",
        "technical-diagram-v1",
        "dashboard-overview-v1",
    ]
    assert deck["slides"][0]["props"]["diagram_kind"] == "architecture"
    assert deck["slides"][1]["props"]["diagram_kind"] == "integration"

    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    rendered_markup = html.split('<script type="application/json" id="deck-document">', 1)[0]
    assert rendered_markup.count(" data-pptx-diagram") == 2
    assert 'data-diagram-kind="architecture"' in html
    assert 'data-diagram-kind="integration"' in html
    assert 'data-deck-runtime="elkjs"' in html
    assert 'data-deck-runtime="diagram-runtime"' in html
    assert "layout-dashboard-overview" in html

    report_path = tmp_path / "html-self-check.json"
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    self_check_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert self_check_report["warnings"] == []
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["diagramCount"] == 2
    assert not any(
        "short background text uses vertical padding" in warning
        for warning in report["warnings"]
    )


def test_scaffold_routes_risk_heatmap_to_editable_heatmap_layout(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "给董事会呈现年度风险分析与治理优先级。",
            "audience": "董事会与风险委员会",
            "storyline": "先看风险热力分布，再决定治理优先级。",
        }
    )
    outline["slides"][0].update(
        {
            "title": "年度风险热力图",
            "message": "用概率、影响和应对优先级识别重点风险。",
            "layout": "风险热力图",
            "visual": "风险热力图，用五级颜色强度呈现风险矩阵。",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["theme_id"] == "signal"
    assert deck["slides"][0]["layout_id"] == "heatmap-matrix-v1"
    contract = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert contract["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "table-data-v1",
            "to": "heatmap-matrix-v1",
            "reason": (
                "outline asks for an editable heatmap matrix with semantic "
                "intensity cells"
            ),
        }
    ]

    deck["slides"][0]["props"].update(
        {
            "eyebrow": "董事会风险审阅",
            "title": "年度风险热力图",
            "subtitle": "颜色越深，治理优先级越高",
            "columns": ["风险域", "发生概率", "影响程度", "应对优先级"],
            "rows": [
                ["供应链", "高", "严重", "关键"],
                ["合规", "中", "高", "高"],
                ["人才", "低", "中", "中"],
            ],
            "low_label": "低",
            "high_label": "高",
            "insight": "优先处理深色区域。",
            "source": "董事会风险清单",
        }
    )
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-layout-id="heatmap-matrix-v1"' in html
    assert "layout-heatmap-matrix" in html
    assert "heat-level-5" in html
    assert 'data-prop-path="rows.0.3"' in html

    report_path = tmp_path / "html-self-check.json"
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["warnings"] == []


def test_truth_validator_ignores_diagram_structural_ids_but_checks_labels(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "technical-diagram-v1",
        "--fact",
        "客户请求进入 AI 服务并查询 CRM",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "系统集成",
            "title": "客户请求进入 AI 服务并查询 CRM",
            "subtitle": "",
            "diagram_kind": "integration",
            "direction": "RIGHT",
            "nodes": [
                {"id": "n1", "label": "客户", "detail": "请求", "kind": "client"},
                {"id": "n2", "label": "AI 服务", "detail": "查询 CRM", "kind": "hub"},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "label": "请求"},
            ],
            "note": "",
        }
    )
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )

    sanitizer = subprocess.run(
        [
            str(NODE),
            "-e",
            (
                "const fs=require('fs');"
                "const truth=require(process.argv[1]);"
                "const deck=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
                "console.log(JSON.stringify(truth.sanitizeStrictSourceDeck(deck).deck));"
            ),
            str(SCRIPTS_DIR / "validate_deck_truth.js"),
            str(deck_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sanitizer.returncode == 0, sanitizer.stderr
    sanitized = json.loads(sanitizer.stdout)
    assert [node["id"] for node in sanitized["slides"][0]["props"]["nodes"]] == [
        "n1",
        "n2",
    ]
    assert sanitized["slides"][0]["props"]["edges"][0] == {
        "id": "e1",
        "source": "n1",
        "target": "n2",
        "label": "请求",
    }

    accepted = _run("validate_deck_truth.js", str(deck_path))
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    deck["slides"][0]["props"]["edges"][0]["label"] = "99% 可用性"
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    warned = _run("validate_deck_truth.js", str(deck_path))
    assert warned.returncode == 0, warned.stdout + warned.stderr
    payload = json.loads(warned.stdout.split("\nDeck truth validation:", 1)[0])
    assert any("numeric claim \"99%\"" in warning for warning in payload["warnings"])


def test_truth_validator_does_not_bind_one_entitys_number_to_another(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    example_fact = (
        "Example Corp | Example Corp revenue reached 10 million in 2026. | "
        "first_party | https://example.com/results"
    )
    another_fact = (
        "Another Corp | Another Corp revenue reached 99 million in 2026. | "
        "first_party | https://another.example/results"
    )
    scaffold = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "--research-fact",
        example_fact,
        "--research-fact",
        another_fact,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "2026 RESULTS",
            "title": "Example Corp performance",
            "subtitle": "Entity-bound evidence",
                "items": [
                    {
                        "label": "REVENUE",
                        "value": "99",
                        "detail": "million",
                        "delta": "",
                    },
                    {
                        "label": "CUSTOMERS",
                        "value": "待补充",
                        "detail": "not researched",
                        "delta": "",
                    },
                    {
                        "label": "GROWTH",
                        "value": "待补充",
                        "detail": "not researched",
                        "delta": "",
                    },
                ],
        }
    )
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["entityBoundResearchFactCount"] == 2
    assert any(
        'numeric claim "99"' in warning for warning in payload["warnings"]
    )


def test_batch_patch_keeps_ranking_source_warning_non_blocking(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "请制作一页系统集成说明。".encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "technical-diagram-v1",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "信息反馈回路",
                            "diagram_kind": "integration",
                            "nodes": [
                                {
                                    "id": "input",
                                    "label": "信息输入",
                                    "detail": "通知、排名、评价不断刷新",
                                    "kind": "client",
                                },
                                {
                                    "id": "review",
                                    "label": "反馈处理",
                                    "detail": "形成行动建议",
                                    "kind": "hub",
                                },
                            ],
                            "edges": [
                                {
                                    "id": "e1",
                                    "source": "input",
                                    "target": "review",
                                    "label": "进入",
                                }
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["truth_warning_count"] > 0
    assert any(
        "performance/award/publication claim is not source-backed" in warning
        for warning in payload["truth_guard_warnings"]
    )
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["nodes"][0]["detail"] == (
        "通知、排名、评价不断刷新"
    )


def test_dense_integration_diagram_keeps_edge_labels_clear(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "technical-diagram-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "diagram_kind": "integration",
            "nodes": [
                {"id": "hub", "label": "客户服务平台", "detail": "统一集成", "kind": "hub"},
                {"id": "crm", "label": "CRM", "detail": "客户资料", "kind": "external"},
                {"id": "order", "label": "订单系统", "detail": "订单状态", "kind": "external"},
                {"id": "support", "label": "客服系统", "detail": "工单", "kind": "external"},
                {"id": "data", "label": "数据平台", "detail": "分析", "kind": "data"},
                {"id": "auth", "label": "统一认证", "detail": "身份", "kind": "service"},
                {"id": "channel", "label": "用户渠道", "detail": "Web 与 App", "kind": "client"},
                {"id": "event", "label": "事件总线", "detail": "异步事件", "kind": "service"},
            ],
            "edges": [
                {"id": "e1", "source": "crm", "target": "hub", "label": "客户查询"},
                {"id": "e2", "source": "hub", "target": "crm", "label": "资料回写"},
                {"id": "e3", "source": "order", "target": "hub", "label": "订单同步"},
                {"id": "e4", "source": "hub", "target": "support", "label": "创建工单"},
                {"id": "e5", "source": "hub", "target": "data", "label": "指标事件"},
                {"id": "e6", "source": "auth", "target": "hub", "label": "身份校验"},
                {"id": "e7", "source": "channel", "target": "hub", "label": "客户请求"},
                {"id": "e8", "source": "hub", "target": "event", "label": "发布事件"},
            ],
        }
    )
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        "1600x1000",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    assert probe.returncode == 0, probe.stdout + probe.stderr
    runtime = json.loads(probe.stdout)
    diagram = runtime["editor"]["diagrams"][0]
    assert diagram["edgeLabels"] == 8
    assert diagram["labelNodeOverlapCount"] == 0
    assert diagram["labelLabelOverlapCount"] == 0


def test_pipeline_diagram_rejects_duplicate_stage_labels(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    report_path = tmp_path / "qa" / "deck_spec.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "technical-diagram-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "diagram_kind": "pipeline",
            "nodes": [
                {"id": "source", "label": "知识数据流", "detail": "接入", "kind": "client"},
                {"id": "service", "label": "向量检索", "detail": "召回", "kind": "hub"},
                {"id": "sink", "label": "知识数据流", "detail": "回写", "kind": "data"},
            ],
            "edges": [
                {"id": "e1", "source": "source", "target": "service", "label": "处理"},
                {"id": "e2", "source": "service", "target": "sink", "label": "回写"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    validation = _run(
        "validate_deck_spec.js",
        str(deck_path),
        "--report",
        str(report_path),
    )

    assert validation.returncode == 1
    issues = json.loads(report_path.read_text(encoding="utf-8"))["issues"]
    assert any("duplicate pipeline stage label" in issue for issue in issues)


def test_quantitative_dashboard_keeps_kpi_layout(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "复盘客服运营指标",
                "audience": "管理层",
                "source_mode": "user_provided",
                "storyline": "用真实指标说明运营表现。",
                "slides": [
                    {
                        "page": 1,
                        "title": "客服数据看板",
                        "message": "机器人解决率为 68%。",
                        "bullets": ["转人工率为 21%"],
                        "layout": "dashboard-overview-v1",
                        "visual": "数据看板，展示机器人解决率与转人工率",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "dashboard-overview-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "kpi-grid-v1"


def test_scaffold_accepts_user_provided_quantitative_outline_without_links(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "复盘客服指标变化",
                "audience": "管理层",
                "source_mode": "user_provided",
                "storyline": "用前后对比说明客服效率变化。",
                "slides": [
                    {
                        "page": 1,
                        "title": "改进结果",
                        "message": "首次响应时间从 18 分钟降到 7 分钟。",
                        "bullets": [
                            "一次解决率从 68% 提升到 81%",
                            "满意度从 4.2/5 提升到 4.6/5",
                        ],
                        "layout": "可编辑图表页",
                        "visual": "可编辑前后对比柱状图",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "chart-data-v1"
    outline_check = _run("validate_outline.js", str(outline_path), "--min-slides", "1")
    assert outline_check.returncode == 0, outline_check.stdout + outline_check.stderr
    assert json.loads(outline_check.stdout)["warnings"] == []


def test_scaffold_normalizes_chart_with_only_one_real_value_to_cards(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "说明 APS 市场数据现状",
                "audience": "业务决策者",
                "source_mode": "user_provided",
                "storyline": "仅呈现已有事实，不构造虚假趋势。",
                "slides": [
                    {
                        "page": 1,
                        "title": "APS 市场规模",
                        "message": "目前只有 2024 年市场规模数据。",
                        "bullets": ["其他年份待补充，不制作虚假趋势。"],
                        "layout": "可编辑图表页",
                        "visual": "可编辑折线图",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "chart-data-v1",
            "to": "cards-grid-v1",
            "reason": (
                "outline names a quantitative visual without quantitative evidence "
                "or an authorized illustrative assumption, so preserve the qualitative "
                "argument in editable cards instead of inventing values"
            ),
        }
    ]
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "cards-grid-v1"


def test_scaffold_keeps_project_media_and_metrics_as_project_case_study(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "制作设计工作室年终作品集",
                "audience": "同行设计师与潜在客户",
                "source_mode": "user_provided",
                "storyline": "用精选项目展示工作室能力。",
                "slides": [
                    {
                        "page": 1,
                        "title": "精选项目三：空间",
                        "message": "把品牌语言延伸到三维场景与展陈体验。",
                        "bullets": [
                            "项目名称：待补充",
                            "一句话定位：待补充",
                            "关键数字：待补充",
                        ],
                        "layout": "cards",
                        "visual": "项目缩略图 + 一句话定位 + 2-3 个数字指标卡",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "--outline",
        str(outline_path),
        "--require-field",
        "1:metrics",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    contract = json.loads(result.stdout)
    assert contract["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "kpi-grid-v1",
            "to": "project-case-study-v1",
            "reason": (
                "outline asks for a project case study with both media and "
                "project metrics"
            ),
        }
    ]
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "project-case-study-v1"
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["required_fields"] == [{"slide": 1, "field": "metrics"}]


def test_scaffold_normalizes_metrics_field_when_project_page_falls_back_to_kpi(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "制作设计工作室年终作品集",
                "audience": "潜在客户",
                "source_mode": "user_provided",
                "storyline": "用年度成果展示工作室能力。",
                "slides": [
                    {
                        "page": 1,
                        "title": "年度成果",
                        "message": "用一组结果指标概括今年的交付。",
                        "bullets": ["28 个交付项目"],
                        "layout": "project-case-study-v1",
                        "visual": "KPI 指标卡",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "project-case-study-v1",
        "--outline",
        str(outline_path),
        "--require-field",
        "1:metrics",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "kpi-grid-v1"
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["required_fields"] == [{"slide": 1, "field": "items"}]
    assert report["required_field_normalizations"] == [
        {"slide": 1, "from": "metrics", "to": "items"}
    ]


def test_scaffold_persists_visual_intent_and_normalizes_strong_layout_mismatches(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=4,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "把业务规划整理成内部沟通用的汇报",
            "audience": "内部产品、研发与管理团队",
            "storyline": "从四条主线进入能力路径、风险矩阵和月底目标。",
        }
    )
    outline["slides"][0].update(
        {"layout": "cover", "visual": "标题封面 + 四条主线标签"}
    )
    outline["slides"][1].update(
        {"layout": "cards", "visual": "三段式能力路径"}
    )
    outline["slides"][2].update(
        {
            "layout": "matrix",
            "visual": "风险/依赖矩阵：事项、依据、影响、收口动作",
        }
    )
    outline["slides"][3].update(
        {"layout": "closing", "visual": "四象限目标状态卡片 + 下一步观察项"}
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "table-data-v1",
        "--theme",
        "blue-professional",
        "--title",
        "8月业务规划",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert payload["contract_version"] == 2
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cover-editorial-v1",
        "cards-grid-v1",
        "table-data-v1",
        "cards-grid-v1",
    ]
    assert [item["slide"] for item in payload["layout_normalizations"]] == [1, 3, 4]
    assert len(deck["slides"][3]["props"]["items"]) == 4
    assert payload["authoring_rules"]["outline_policy"]["pages"][3][
        "expected_visual_item_count"
    ] == 4
    assert deck["slides"][0]["outline_intent"] == {
        key: outline["slides"][0][key]
        for key in ("title", "message", "layout", "visual")
    }
    assert deck["design"]["family"] == "institutional-grid"


def test_scaffold_splits_ten_item_agenda_across_readable_card_pages(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "今天我们一起走一遍入职地图",
            "message": "这是一份10站旅程：从认识公司到找到你的下一站。",
            "bullets": [
                f"{index:02d} 第 {index} 站：议程内容 {index}"
                for index in range(1, 11)
            ],
            "layout": "agenda-grid-v1",
            "visual": "编号流程：10个图标节点横向或双列排布，清爽图标风格",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    outline_validation = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )
    assert outline_validation.returncode == 0, (
        outline_validation.stdout + outline_validation.stderr
    )
    assert "trim to 5 or fewer" not in outline_validation.stdout

    scaffold = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    contract = json.loads(scaffold.stdout)
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cards-grid-v1",
        "cards-grid-v1",
    ]
    assert [len(slide["props"]["items"]) for slide in deck["slides"]] == [5, 5]
    assert [slide["source_outline_page"] for slide in deck["slides"]] == [1, 1]
    assert [slide["source_outline_item_range"] for slide in deck["slides"]] == [
        {"start": 1, "end": 5, "total": 10, "part": 1, "parts": 2},
        {"start": 6, "end": 10, "total": 10, "part": 2, "parts": 2},
    ]
    assert report["layout_plan"] == ["cards-grid-v1", "cards-grid-v1"]
    assert [
        page["expected_visual_item_count"]
        for page in contract["authoring_rules"]["outline_policy"]["pages"]
    ] == [5, 5]

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    slide["id"]: {
                        "props": {
                            "eyebrow": "今日行程",
                            "title": outline["slides"][0]["title"],
                            "subtitle": outline["slides"][0]["message"],
                            "variant": "numbered",
                            "items": [
                                {
                                    "kicker": f"{item_index:02d}",
                                    "title": f"第 {item_index} 站",
                                    "body": f"议程内容 {item_index}",
                                }
                                for item_index in range(
                                    slide["source_outline_item_range"]["start"],
                                    slide["source_outline_item_range"]["end"] + 1,
                                )
                            ],
                        }
                    }
                    for slide in deck["slides"]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    applied = _run("apply_deck_patch.js", str(deck_path), str(patch_path))
    assert applied.returncode == 0, applied.stdout + applied.stderr
    validated = _run("validate_deck_spec.js", str(deck_path))
    assert validated.returncode == 0, validated.stdout + validated.stderr


def test_outline_rejects_agenda_count_without_one_bullet_per_item(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "入职议程",
            "message": "完整介绍六项入职内容。",
            "bullets": ["公司与文化", "组织与制度", "工具与安全", "成长与结语"],
            "layout": "agenda-grid-v1",
            "visual": "6张议程卡片",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    validated = _run("validate_outline.js", str(outline_path))

    assert validated.returncode == 1
    assert "agenda requests 6 visual items but bullets contains 4" in validated.stdout


def test_outline_rejects_conflicting_agenda_counts_across_fields(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "分享提纲",
            "message": "八个视角，快速读懂行业现状",
            "bullets": [f"议程项 {index}" for index in range(1, 8)],
            "layout": "agenda-list-v1",
            "visual": "目录式七项条目列表",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    validated = _run("validate_outline.js", str(outline_path))

    assert validated.returncode == 1
    assert "conflicting agenda item counts 7, 8" in validated.stdout


@pytest.mark.parametrize(
    ("title", "visual", "bullets", "layout_id", "field", "count"),
    [
        (
            "5项KPI总览",
            "5个KPI指标卡",
            ["指标一为10%", "指标二为20%"],
            "kpi-grid-v1",
            "items",
            5,
        ),
        (
            "六类趋势",
            "6个类别的折线图",
            ["类别一为10", "类别二为20"],
            "chart-data-v1",
            "categories",
            6,
        ),
        (
            "七项风险",
            "7项风险热力图",
            ["风险一", "风险二"],
            "heatmap-matrix-v1",
            "rows",
            7,
        ),
        (
            "十节点架构",
            "10个节点的技术架构图",
            ["入口连接服务", "服务连接数据"],
            "technical-diagram-v1",
            "nodes",
            10,
        ),
    ],
)
def test_scaffold_uses_typed_count_contract_for_collection_layouts(
    tmp_path: Path,
    title: str,
    visual: str,
    bullets: list[str],
    layout_id: str,
    field: str,
    count: int,
) -> None:
    case_dir = tmp_path / layout_id
    case_dir.mkdir()
    outline_path = case_dir / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": title,
            "message": "按明确的集合维度完整呈现内容。",
            "bullets": bullets,
            "layout": layout_id,
            "visual": visual,
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = case_dir / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    slide = json.loads(deck_path.read_text(encoding="utf-8"))["slides"][0]
    assert slide["layout_id"] == layout_id
    assert len(slide["props"][field]) == count
    if layout_id == "technical-diagram-v1":
        assert len({item["id"] for item in slide["props"][field]}) == count
    if layout_id == "chart-data-v1":
        assert all(
            len(series["values"]) == count
            for series in slide["props"]["series"]
        )


@pytest.mark.parametrize(
    ("title", "visual", "item_count", "layout_id", "field", "chunk_sizes"),
    [
        ("8项KPI总览", "8个KPI指标卡", 8, "kpi-grid-v1", "items", [4, 4]),
        ("10项风险", "10项风险热力图", 10, "heatmap-matrix-v1", "rows", [5, 5]),
        ("20节点架构", "20个节点的技术架构图", 20, "technical-diagram-v1", "nodes", [10, 10]),
        ("14类趋势", "14个类别的折线图", 14, "chart-data-v1", "categories", [7, 7]),
    ],
)
def test_scaffold_splits_overflow_for_typed_collection_layouts(
    tmp_path: Path,
    title: str,
    visual: str,
    item_count: int,
    layout_id: str,
    field: str,
    chunk_sizes: list[int],
) -> None:
    case_dir = tmp_path / layout_id
    case_dir.mkdir()
    outline_path = case_dir / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": title,
            "message": "完整呈现所有集合项，不截断内容。",
            "bullets": [
                f"{index:02d} 集合项 {index}，示例值 {index * 10}"
                for index in range(1, item_count + 1)
            ],
            "layout": layout_id,
            "visual": visual,
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = case_dir / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    slides = json.loads(deck_path.read_text(encoding="utf-8"))["slides"]
    assert [slide["layout_id"] for slide in slides] == [layout_id, layout_id]
    assert [len(slide["props"][field]) for slide in slides] == chunk_sizes
    assert [slide["source_outline_page"] for slide in slides] == [1, 1]
    assert slides[0]["source_outline_item_range"]["start"] == 1
    assert slides[-1]["source_outline_item_range"]["end"] == item_count


def test_scaffold_keeps_single_series_eight_month_trend_on_one_slide(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    categories = [f"{month} 月" for month in range(1, 9)]
    values = ["120", "145", "168", "205", "248", "292", "326", "351"]
    outline["slides"][0].update(
        {
            "title": "连续八个月收入趋势",
            "message": "月度经常性收入保持连续增长。",
            "bullets": [
                f"{category} {value} 万元"
                for category, value in zip(categories, values, strict=True)
            ],
            "layout": "chart",
            "visual": "单系列折线图，展示八个月的趋势",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    contract = json.loads(scaffold.stdout)
    slides = json.loads(deck_path.read_text(encoding="utf-8"))["slides"]
    assert len(slides) == 1
    assert "source_outline_item_range" not in slides[0]
    assert not any(
        item.get("strategy") == "split"
        for item in contract.get("layout_normalizations", [])
    )

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "categories": categories,
                            "series": [{"name": "月度收入", "values": values}],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert applied.returncode == 0, applied.stdout + applied.stderr
    slide = json.loads(deck_path.read_text(encoding="utf-8"))["slides"][0]
    assert slide["props"]["categories"] == categories
    assert slide["props"]["series"] == [{"name": "月度收入", "values": values}]


def test_scaffold_and_patch_support_six_column_nine_row_gantt(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "实施计划与里程碑甘特图",
            "message": "项目按启动、建设、集成、上线和优化阶段推进。",
            "bullets": [
                "包含启动、调研、知识库建设、AI配置训练、系统集成、联调测试、试点上线、全量推广、运营优化。"
            ],
            "layout": "gantt-plan",
            "visual": "规整甘特图，横轴为项目阶段，纵轴为九项工作包。",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    contract = json.loads(scaffold.stdout)
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slide = deck["slides"][0]
    assert slide["layout_id"] == "table-data-v1"
    assert slide["props"]["variant"] == "gantt"
    assert len(slide["props"]["rows"]) == 9
    assert contract["authoring_rules"]["outline_policy"]["pages"][0][
        "expected_visual_item_count"
    ] == 9

    patch_path = tmp_path / "deck.patch.json"
    rows = [
        ["启动", "■", "", "", "", ""],
        ["调研", "■", "", "", "", ""],
        ["知识库建设", "", "■", "", "", ""],
        ["AI配置训练", "", "■", "", "", ""],
        ["系统集成", "", "", "■", "", ""],
        ["联调测试", "", "", "■", "", ""],
        ["试点上线", "", "", "", "■", ""],
        ["全量推广", "", "", "", "■", ""],
        ["运营优化", "", "", "", "", "■"],
    ]
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "实施计划与里程碑甘特图",
                            "columns": [
                                "工作包",
                                "启动调研",
                                "建设配置",
                                "集成测试",
                                "上线推广",
                                "运营优化",
                            ],
                            "rows": rows,
                            "variant": "ledger",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert applied.returncode == 0, applied.stdout + applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert any(
        "replaced empty schedule/table cell with an em dash" in item
        for item in applied_payload["normalization_changes"]
    )
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    props = deck["slides"][0]["props"]
    assert props["variant"] == "gantt"
    assert len(props["columns"]) == 6
    assert len(props["rows"]) == 9
    assert props["rows"][0] == ["启动", "■", "—", "—", "—", "—"]

    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "table-gantt" in html
    assert "table-columns-6" in html
    assert "gantt-active" in html
    assert "gantt-idle" in html


def test_bound_deck_rejects_visual_cardinality_and_missing_persisted_intent(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "三段能力路径",
            "message": "三个阶段共同形成能力闭环。",
            "layout": "cards",
            "visual": "三段式能力路径",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "title": outline["slides"][0]["title"],
            "subtitle": outline["slides"][0]["message"],
            "items": [
                {"kicker": str(index), "title": f"阶段 {index}", "body": "说明"}
                for index in range(1, 5)
            ],
        }
    )
    deck["slides"][0].pop("outline_intent")
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert "outline_intent: required by deck contract v2" in result.stdout
    assert "outline visual explicitly requests 3 visual item(s), got 4" in result.stdout


def test_bound_deck_rejects_unchanged_scaffold_content(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path, page_count=1)
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert "props.items: still contains scaffold placeholder content" in result.stdout


def test_controlled_redesign_preserves_outline_intent_and_previous_layout_draft(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    before = json.loads(deck_path.read_text(encoding="utf-8"))
    redesign_path = tmp_path / "deck.redesign.json"
    redesign_path.write_text(
        json.dumps(
            {
                "theme_id": "soft-editorial",
                "design": {"family": "editorial-spread"},
                "palette": {
                    "background": "#FFF8E7",
                    "text": "#1A1A1A",
                    "primary": "#0B5D36",
                    "accent": "#FFDF00",
                    "secondary": "#1F5AA6",
                    "accent_usage": "balanced",
                    "identity_basis": "巴西国家队与桑巴足球",
                },
                "slides": {
                    "slide-01": {
                        "layout_id": "timeline-horizontal-v1",
                        "props": {
                            "eyebrow": "路径",
                            "title": outline["slides"][0]["title"],
                            "subtitle": outline["slides"][0]["message"],
                            "steps": [
                                {"phase": "01", "title": "第一步", "body": "支持点甲"},
                                {"phase": "02", "title": "第二步", "body": "支持点乙"},
                                {"phase": "03", "title": "第三步", "body": "形成闭环"},
                            ],
                            "variant": "staggered",
                        },
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "apply_deck_redesign.js",
        str(deck_path),
        str(redesign_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(deck_path.read_text(encoding="utf-8"))
    assert after["theme_id"] == "soft-editorial"
    assert after["design"]["family"] == "editorial-spread"
    assert after["design_contract"]["palette"]["source"] == "explicit"
    assert after["design_contract"]["palette"]["primary"]["value"] == "#0B5D36"
    assert after["design_contract"]["palette"]["secondary"]["value"] == "#1F5AA6"
    slide = after["slides"][0]
    assert slide["layout_id"] == "timeline-horizontal-v1"
    assert slide["outline_intent"] == before["slides"][0]["outline_intent"]
    assert slide["layout_drafts"]["cards-grid-v1"] == before["slides"][0]["props"]
    contract = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert contract["contract_version"] == 2
    assert contract["theme_id"] == "soft-editorial"
    assert contract["layout_plan"] == ["timeline-horizontal-v1"]
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    assert manifest["deck"]["theme_id"] == "soft-editorial"
    assert manifest["deck"]["design_contract"] == after["design_contract"]
    validation = _run("validate_deck_spec.js", str(deck_path))
    assert validation.returncode == 0, validation.stdout + validation.stderr


def test_bound_deck_spec_requires_outline_page_title_and_support_copy(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path)
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    for slide, outline_slide in zip(deck["slides"], outline["slides"], strict=True):
        slide["props"]["title"] = outline_slide["title"]
        slide["props"]["subtitle"] = outline_slide["message"]
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    valid = _run("validate_deck_spec.js", str(deck_path))
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert json.loads(valid.stdout.split("\nDeck spec validation", 1)[0])[
        "outlineBinding"
    ]["required"] is True

    deck["slides"][1].pop("source_outline_page")
    deck["slides"][1]["props"]["title"] = "被替换的页面"
    deck["slides"][1]["props"]["subtitle"] = "正文也偏离了原大纲"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    invalid = _run("validate_deck_spec.js", str(deck_path))

    assert invalid.returncode == 1
    assert "slides.slide-02.source_outline_page" in invalid.stdout
    assert "must include outline page 2 title" in invalid.stdout
    assert "must preserve at least one exact message/bullet" in invalid.stdout


def test_bound_deck_spec_accepts_labeled_outline_bullet_fragment(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "Q2 客服效率改进复盘",
            "message": "本次复盘覆盖问题、行动、结果和下一步。",
            "bullets": ["副标题：从响应提速到一次解决率提升"],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "title": "Q2 客服效率改进复盘",
            "subtitle": "从响应提速到一次解决率提升",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_bound_deck_spec_accepts_labeled_actions_as_timeline_titles(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "采取的行动",
            "message": "三项行动形成客服改进闭环。",
            "bullets": [
                "统一知识库：沉淀标准答案与处理口径",
                "工单自动分流：提升分配效率",
                "每周质检复盘：持续推动流程修正",
            ],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "title": "采取的行动",
            "subtitle": "",
            "steps": [
                {"phase": "知识库", "title": "统一知识库", "body": ""},
                {"phase": "工单", "title": "工单自动分流", "body": ""},
                {"phase": "质检", "title": "每周质检复盘", "body": ""},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_bound_deck_spec_accepts_quantitative_copy_split_across_kpi_fields(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "改进前问题",
            "message": "改进前客服链路包含三项指标。",
            "bullets": [
                "首次响应时间为 18 分钟。",
                "一次解决率为 68%。",
                "满意度为 4.2/5。",
            ],
            "evidence": [],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "title": "改进前问题",
            "items": [
                {"label": "首次响应时间", "value": "18 分钟", "detail": "", "delta": ""},
                {"label": "一次解决率", "value": "68%", "detail": "", "delta": ""},
                {"label": "满意度", "value": "4.2/5", "detail": "", "delta": ""},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_strict_source_patch_keeps_percent_chart_values_from_rate_category(
    tmp_path: Path,
) -> None:
    source_fact = (
        "首次响应时间从 18 分钟降到 7 分钟；"
        "一次解决率从 68% 提升到 81%；"
        "满意度从 4.2/5 提升到 4.6/5。"
    )
    source_text = f"改进结果：{source_fact}不补充任何我未提供的事实。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        source_fact,
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "改进结果",
                            "categories": ["首次响应时间", "一次解决率", "满意度"],
                            "series": [
                                {"name": "改进前", "values": ["18", "68", "4.2"]},
                                {"name": "改进后", "values": ["7", "81", "4.6"]},
                            ],
                            "insight": source_fact,
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["series"][0]["values"] == ["18", "68", "4.2"]
    assert deck["slides"][0]["props"]["series"][1]["values"] == ["7", "81", "4.6"]
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_strict_source_sanitizer_removes_production_directives_from_slide_copy(
    tmp_path: Path,
) -> None:
    source_fact = (
        "首次响应时间从 18 分钟降到 7 分钟；"
        "一次解决率从 68% 提升到 81%。"
    )
    source_text = (
        f"制作一份 5 页复盘。改进结果：{source_fact}"
        "必须使用可编辑图表呈现前后对比；全部为可编辑文字、图表和形状。"
        "不补充任何我未提供的事实。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "chart-data-v1",
        "--fact",
        "5 页复盘",
        "--fact",
        source_fact,
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "内部项目复盘",
                            "subtitle": "待补充",
                            "meta": "5 页复盘｜全部为可编辑文字、图表和形状",
                        }
                    },
                    "slide-02": {
                        "props": {
                            "title": "改进结果",
                            "subtitle": "结果展示必须使用可编辑图表呈现前后对比",
                            "categories": ["首次响应时间", "一次解决率"],
                            "series": [
                                {"name": "改进前", "values": ["18", "68%"]},
                                {"name": "改进后", "values": ["7", "81%"]},
                            ],
                            "insight": source_fact,
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    slides = json.loads(deck_path.read_text(encoding="utf-8"))["slides"]
    assert slides[0]["props"]["meta"] == "5 页复盘"
    assert slides[1]["props"]["subtitle"] == ""
    assert slides[1]["props"]["insight"] == source_fact


def test_deck_spec_rejects_chart_placeholder_as_data(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "一次解决率从 68% 提升到 81%",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["series"][0]["values"][1] = "待补充"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert "placeholders are not valid data" in result.stdout
    assert "Keep a complete factual category/series subset" in result.stdout
    assert "never invent a missing baseline or forecast" in result.stdout


def test_chart_patch_keeps_complete_series_and_moves_isolated_metric_to_highlight(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "AI 工业质检市场从 2017 年 9 亿元增长到 2024 年 454 亿元",
        "--fact",
        "APS 2024 年市场规模为 25.11 亿元",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "categories": ["2017", "2024"],
                            "series": [
                                {
                                    "name": "AI 工业质检（亿元）",
                                    "values": ["9", "454"],
                                }
                            ],
                            "highlights": [
                                {
                                    "value": "25.11 亿元",
                                    "label": "APS 市场规模",
                                    "note": "2024 年单点数据",
                                }
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    props = deck["slides"][0]["props"]
    assert props["categories"] == ["2017", "2024"]
    assert props["series"] == [
        {"name": "AI 工业质检（亿元）", "values": ["9", "454"]}
    ]
    assert props["highlights"][0]["value"] == "25.11 亿元"


def test_strict_source_kpi_sanitizer_uses_local_fact_fragments(
    tmp_path: Path,
) -> None:
    source_fact = "第2页 改进前问题：首次响应时间 18 分钟；一次解决率 68%；满意度 4.2/5。"
    source_text = f"改进前问题：{source_fact}不补充任何我未提供的事实。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "--fact",
        source_fact,
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "改进前问题",
                            "subtitle": "问题链路表达为响应慢、解决不充分、体验受影响。",
                            "items": [
                                {"label": "首次响应时间", "value": "18 分钟", "detail": "体现等待偏长", "delta": "响应慢"},
                                {"label": "一次解决率", "value": "68%", "detail": "说明解决不充分", "delta": "解决不充分"},
                                {"label": "满意度", "value": "4.2/5", "detail": "反映体验受影响", "delta": "体验受影响"},
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    props = deck["slides"][0]["props"]
    assert props["subtitle"] == "首次响应时间 → 一次解决率 → 满意度"
    assert [item["detail"] for item in props["items"]] == [
        "首次响应时间 18 分钟",
        "一次解决率 68%",
        "满意度 4.2/5",
    ]
    assert [item["delta"] for item in props["items"]] == ["", "", ""]
    assert "待补充" not in json.dumps(props, ensure_ascii=False)


def test_strict_source_table_sanitizer_drops_unsupported_optional_column(
    tmp_path: Path,
) -> None:
    source_facts = [
        "补齐高频问题知识库",
        "优化分流规则",
        "建立月度复盘机制",
        "执行角色为产品、客服运营、数据分析",
        "成员姓名未提供",
    ]
    source_text = "；".join(source_facts) + "。不补充任何我未提供的事实。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold_args = ["inspect_deck_contract.js", "table-data-v1"]
    for fact in source_facts:
        scaffold_args.extend(["--fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run(*scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "下一步",
                            "subtitle": "下一阶段继续推动三项工作形成闭环。",
                            "columns": ["推进事项", "执行角色", "角色职责", "成员姓名"],
                            "rows": [
                                ["补齐高频问题知识库", "产品", "补齐相关产品信息和口径", "姓名未提供"],
                                ["优化分流规则", "客服运营", "推动执行落地", "姓名未提供"],
                                ["建立月度复盘机制", "数据分析", "完善指标跟踪", "姓名未提供"],
                            ],
                            "source": "成员姓名未提供",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    props = deck["slides"][0]["props"]
    assert props["subtitle"] == ""
    assert props["columns"] == ["推进事项", "执行角色", "成员姓名"]
    assert props["rows"] == [
        ["补齐高频问题知识库", "产品", "姓名未提供"],
        ["优化分流规则", "客服运营", "姓名未提供"],
        ["建立月度复盘机制", "数据分析", "姓名未提供"],
    ]
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_strict_source_table_sanitizer_drops_duplicate_column(
    tmp_path: Path,
) -> None:
    source_facts = [
        "补齐高频问题知识库",
        "优化分流规则",
        "建立月度复盘机制",
        "执行角色为产品、客服运营、数据分析",
        "成员姓名未提供",
    ]
    source_text = "；".join(source_facts) + "。不补充任何我未提供的事实。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold_args = ["inspect_deck_contract.js", "table-data-v1"]
    for fact in source_facts:
        scaffold_args.extend(["--fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run(*scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "下一步",
                            "columns": ["下一步事项", "执行角色", "角色职责", "成员姓名"],
                            "rows": [
                                ["补齐高频问题知识库", "产品", "补齐高频问题知识库", "姓名未提供"],
                                ["优化分流规则", "客服运营", "优化分流规则", "姓名未提供"],
                                ["建立月度复盘机制", "数据分析", "建立月度复盘机制", "姓名未提供"],
                            ],
                            "source": "成员姓名未提供",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    props = deck["slides"][0]["props"]
    assert props["columns"] == ["下一步事项", "执行角色", "成员姓名"]
    assert props["rows"] == [
        ["补齐高频问题知识库", "产品", "姓名未提供"],
        ["优化分流规则", "客服运营", "姓名未提供"],
        ["建立月度复盘机制", "数据分析", "姓名未提供"],
    ]


def test_scaffold_normalizes_structured_next_steps_closing_to_table(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "下一步",
            "message": "展示下一步、执行角色和成员姓名状态。",
            "bullets": [
                "补齐高频问题知识库",
                "执行角色为产品、客服运营、数据分析",
                "成员姓名未提供",
            ],
            "layout": "下一步行动与角色职责",
            "visual": "用职责矩阵展示任务、角色和姓名状态。",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "closing-next-steps-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "table-data-v1"
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert report["layout_plan_requested"] == ["closing-next-steps-v1"]
    assert report["layout_plan"] == ["table-data-v1"]
    assert report["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "closing-next-steps-v1",
            "to": "table-data-v1",
            "reason": "outline requires parallel next-step, role/owner, and identity fields",
        }
    ]


def test_scaffold_normalizes_five_item_closing_summary_to_cards(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "合作价值与评标结论",
            "message": "用评标价值收束整套方案。",
            "bullets": [
                "理解业务",
                "技术可信",
                "集成清晰",
                "实施可控",
                "投入产出清晰",
            ],
            "layout": "closing-next-steps-v1",
            "visual": (
                "结论页，用五项评标价值条目收束：理解业务、技术可信、"
                "集成清晰、实施可控、投入产出清晰。"
            ),
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "closing-next-steps-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "cards-grid-v1"
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert report["layout_plan_requested"] == ["closing-next-steps-v1"]
    assert report["layout_plan"] == ["cards-grid-v1"]
    assert report["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "closing-next-steps-v1",
            "to": "cards-grid-v1",
            "reason": (
                "outline requests 5 closing value items, "
                "which exceeds the four-action closing layout"
            ),
        }
    ]


def test_scaffold_normalizes_six_step_timeline_to_numbered_cards(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "结构化思维的完整路径",
            "message": "从明确问题到组织表达，形成完整的结构化思考闭环。",
            "bullets": [
                "明确问题",
                "收集信息",
                "拆分问题",
                "分类归纳",
                "提炼结论",
                "组织表达",
            ],
            "layout": "横向流程图",
            "visual": (
                "六节点横向流程图，依次呈现明确问题、收集信息、拆分问题、"
                "分类归纳、提炼结论、组织表达。"
            ),
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slide = deck["slides"][0]
    assert slide["layout_id"] == "cards-grid-v1"
    assert slide["props"]["variant"] == "numbered"
    assert len(slide["props"]["items"]) == 6
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert report["layout_normalizations"] == [
        {
            "slide": 1,
            "from": "timeline-horizontal-v1",
            "to": "cards-grid-v1",
            "reason": (
                "outline requests 6 ordered visual items, "
                "which exceeds the timeline layout capacity of 5"
            ),
        }
    ]

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": outline["slides"][0]["title"],
                            "subtitle": outline["slides"][0]["message"],
                            "items": [
                                {
                                    "kicker": f"{index:02d}",
                                    "title": title,
                                    "body": "结构化思维步骤",
                                }
                                for index, title in enumerate(
                                    outline["slides"][0]["bullets"],
                                    start=1,
                                )
                            ],
                            "variant": "numbered",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    authored = json.loads(deck_path.read_text(encoding="utf-8"))
    assert len(authored["slides"][0]["props"]["items"]) == 6

    validation = _run("validate_deck_spec.js", str(deck_path))
    assert validation.returncode == 0, validation.stdout + validation.stderr


def test_scaffold_keeps_five_step_timeline(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "五步工作路径",
            "message": "用五个连续阶段说明工作路径。",
            "layout": "横向流程图",
            "visual": "五节点横向流程图，依次呈现定义、分析、归纳、结论、表达。",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slide = deck["slides"][0]
    assert slide["layout_id"] == "timeline-horizontal-v1"
    assert len(slide["props"]["steps"]) == 5
    report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert report["layout_normalizations"] == []


def test_onboarding_semantic_guards_normalize_parallel_layouts(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=4,
        source_mode="user_provided",
    )
    outline.update(
        {
            "deck_goal": "为新员工介绍公司文化、制度与第一周安排",
            "audience": "新员工",
            "design_requirements": {"style": "浅色友好员工手册"},
        }
    )
    outline["slides"] = [
        {
            "page": 1,
            "title": "我们的文化",
            "message": "四个并列文化关键词",
            "bullets": ["客户第一", "快速迭代", "坦诚协作", "结果导向"],
            "layout": "quadrant-matrix-v1",
            "visual": "四象限文化关键词",
            "evidence": [],
        },
        {
            "page": 2,
            "title": "客户第一",
            "message": "所有工作从客户价值出发",
            "bullets": ["理解场景", "每周迭代", "对客户坦诚"],
            "layout": "statement-focus-v1",
            "visual": "文化详解页",
            "evidence": [],
        },
        {
            "page": 3,
            "title": "考勤与办公时间",
            "message": "弹性但守约",
            "bullets": [
                "核心协作时间 10:00–16:00",
                "请假提前申请",
                "远程办公按规则报备",
            ],
            "layout": "timeline-horizontal-v1",
            "visual": "一天时间线+规则卡",
            "evidence": [],
        },
        {
            "page": 4,
            "title": "入职第一周清单",
            "message": "五天完成上手",
            "bullets": [
                "Day 1 领设备、开账号",
                "Day 2–3 熟悉产品与团队",
                "Day 4 完成安全培训",
                "Day 5 复盘并设定目标",
            ],
            "layout": "timeline-horizontal-v1",
            "visual": "横向时间线",
            "evidence": [],
        },
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "quadrant-matrix-v1",
        "statement-focus-v1",
        "timeline-horizontal-v1",
        "timeline-horizontal-v1",
        "--theme",
        "people-handbook",
        "--family",
        "editorial-spread",
        "--no-images",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads(
        (deck_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert report["layout_plan"] == [
        "cards-grid-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "timeline-horizontal-v1",
    ]
    reasons = "\n".join(item["reason"] for item in report["layout_normalizations"])
    assert "do not define x/y axes" in reasons
    assert "editorial-spread statement pages require a short thesis" in reasons
    assert "do not provide at least two ordered time/phase signals" in reasons


def test_patch_rejects_bound_visual_count_above_layout_capacity(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["outline_intent"] = {
        "title": "结构化思维的完整路径",
        "message": "从明确问题到组织表达。",
        "layout": "横向流程图",
        "visual": "六节点横向流程图",
    }
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "steps": [
                                {
                                    "phase": f"阶段 {index}",
                                    "title": f"步骤 {index}",
                                    "body": "说明",
                                }
                                for index in range(1, 7)
                            ]
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
    )

    assert applied.returncode == 1
    assert "layout capacity mismatch" in applied.stderr
    assert "bound outline requires 6 visual items" in applied.stderr
    assert "timeline-horizontal-v1 supports at most 5" in applied.stderr


def test_strict_source_closing_sanitizer_removes_unsupported_action_expansion(
    tmp_path: Path,
) -> None:
    source_facts = [
        "下一步",
        "优化分流规则",
        "执行角色为产品",
        "成员姓名未提供",
    ]
    source_text = "；".join(source_facts) + "。不补充任何我未提供的事实。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold_args = ["inspect_deck_contract.js", "closing-next-steps-v1"]
    for fact in source_facts:
        scaffold_args.extend(["--fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run(*scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "eyebrow": "下一步",
                            "title": "下一步",
                            "subtitle": "推动所有工作形成闭环。",
                            "actions": [
                                {
                                    "label": "产品｜姓名未提供",
                                    "detail": "优化分流规则，明确规则调整与落地职责。",
                                }
                            ],
                            "contact": "成员姓名未提供",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    serialized = json.dumps(deck["slides"][0]["props"], ensure_ascii=False)
    assert "明确规则调整与落地职责" not in serialized
    assert "推动所有工作形成闭环" not in serialized
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_batch_patch_restores_bound_outline_title(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "被模型改写的标题",
                            "subtitle": outline["slides"][0]["message"],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert (
        "slides.slide-01.props.title: restored bound outline page title"
        in payload["normalization_changes"]
    )
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["title"] == outline["slides"][0]["title"]


def test_source_bound_patch_guard_recognizes_no_unprovided_facts_wording(
    tmp_path: Path,
) -> None:
    source_text = (
        "制作一页客服效率复盘。首次响应时间 18 分钟。"
        "不补充任何我未提供的事实。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "首次响应时间 18 分钟",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "2024 年成立",
                            "subtitle": "首次响应时间 18 分钟",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["truth_guard_changes"]
    assert "2024" not in deck_path.read_text(encoding="utf-8")
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr
    truth_payload = json.loads(truth.stdout.split("\nDeck truth validation:", 1)[0])
    assert truth_payload["sourceBinding"]["strict"] is True


def test_strict_source_patch_preserves_user_provided_action_titles(
    tmp_path: Path,
) -> None:
    source_text = (
        "采取的行动：统一知识库、工单自动分流、每周质检复盘。"
        "不补充任何我未提供的事实。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--fact",
        "采取的行动：统一知识库、工单自动分流、每周质检复盘。",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "采取的行动",
                            "subtitle": "不补充未提供事实",
                            "steps": [
                                {"phase": "01", "title": "统一知识库", "body": "待补充"},
                                {"phase": "02", "title": "工单自动分流", "body": "待补充"},
                                {"phase": "03", "title": "每周质检复盘", "body": "待补充"},
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [step["title"] for step in deck["slides"][0]["props"]["steps"]] == [
        "统一知识库",
        "工单自动分流",
        "每周质检复盘",
    ]


def test_truth_validator_accepts_compound_copy_of_exact_source_facts(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "首次响应时间从 18 分钟降到 7 分钟",
        "--fact",
        "一次解决率从 68% 提升到 81%",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "前后对比",
            "title": "改进结果",
            "subtitle": "两项指标前后对比",
            "chart_type": "column",
            "categories": ["首次响应时间", "一次解决率"],
            "series": [
                {"name": "改进前", "values": ["18", "68%"]},
                {"name": "改进后", "values": ["7", "81%"]},
            ],
            "insight": (
                "首次响应时间从 18 分钟降到 7 分钟；"
                "一次解决率从 68% 提升到 81%。"
            ),
            "source": "",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("layout_id", "expected_slot"),
    [
        ("cover-hero-v1", "hero"),
        ("cover-editorial-v1", "background"),
    ],
)
def test_creative_image_mode_scaffolds_a_required_cover_generation(
    tmp_path: Path,
    layout_id: str,
    expected_slot: str,
) -> None:
    deck_path = tmp_path / f"{layout_id}.json"

    result = _run(
        "inspect_deck_contract.js",
        layout_id,
        "--image-mode",
        "creative_image_mode",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    assert manifest["mode"] == "creative_image_mode"
    assert manifest["image_plan"][0]["slot"] == expected_slot
    assert manifest["image_plan"][0]["required"] is True
    assert manifest["image_plan"][0]["decision"] == "generate"
    assert manifest["image_plan"][0]["status"] == "pending"
    assert manifest["image_plan"][0]["output_path"].endswith(
        f"slide-01-{expected_slot}.png"
    )
    if expected_slot == "background":
        assert manifest["image_plan"][0]["layout_contract"]["text_regions"] == [
            {
                "name": "editorial-cover-safe",
                "x": 92,
                "y": 78,
                "width": 1736,
                "height": 930,
            }
        ]


def test_creative_image_mode_generates_only_explicit_inner_page_visuals(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["slides"][0]["visual"] = "高饱和抽象封面主视觉"
    outline["slides"][1]["visual"] = "数字产品界面截图与设备样机"
    outline["slides"][2]["visual"] = "结构化信息卡"
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "project-case-study-v1",
        "project-case-study-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "creative_image_mode",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover, explicit_visual, text_led = manifest["image_plan"]
    assert cover["decision"] == "generate"
    assert explicit_visual["slot"] == "image"
    assert explicit_visual["required"] is True
    assert explicit_visual["decision"] == "generate"
    assert explicit_visual["status"] == "pending"
    assert explicit_visual["output_path"].endswith("slide-02-image.png")
    assert "explicitly requests" in explicit_visual["decision_reason"]
    assert text_led["required"] is False
    assert text_led["decision"] == "skip"
    assert text_led["status"] == "skipped"


def test_auto_image_mode_promotes_investor_pitch_cover_to_generation(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--title",
        "AI 质检平台融资路演",
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["slot"] == "hero"
    assert cover["required"] is True
    assert cover["decision"] == "generate"
    assert cover["status"] == "pending"
    assert "investor/pitch/launch" in cover["decision_reason"]


def test_auto_image_mode_generates_a_generic_standard_cover_by_default(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--title",
        "季度经营复盘",
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["required"] is True
    assert cover["decision"] == "generate"
    assert cover["status"] == "pending"
    assert "defaults an eligible standard cover" in cover["decision_reason"]


def test_auto_image_mode_uses_an_optional_inner_media_slot_by_default(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=2,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "年度作品集",
            "layout": "cover",
            "visual": "纯文字编辑式封面",
        }
    )
    outline["slides"][1].update(
        {
            "title": "重点案例",
            "layout": "project case",
            "visual": "案例成果与关键指标",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "project-case-study-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover, case_study = manifest["image_plan"]
    assert cover["decision"] == "skip"
    assert case_study["slot"] == "image"
    assert case_study["required"] is True
    assert case_study["decision"] == "generate"
    assert "eligible media slot" in case_study["decision_reason"]


def test_full_bleed_layout_scaffolds_a_required_background_contract(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "未来工作方式",
            "message": "用一个沉浸场景建立未来愿景。",
            "layout": "整页主视觉",
            "visual": "整页生图，左侧文字安全区、右侧视觉焦点",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    entry = manifest["image_plan"][0]
    assert deck["slides"][0]["layout_id"] == "image-full-bleed-v1"
    assert entry["slot"] == "background"
    assert entry["prop_path"] == "background"
    assert entry["required"] is True
    assert entry["decision"] == "generate"
    assert entry["kind"] == "background"
    assert entry["placement"] == "full-slide"
    assert entry["treatment"] == "wash-dark"
    assert entry["layout_contract"] == {
        "slide_size": {"width": 1920, "height": 1080},
        "text_regions": [
            {
                "name": "full-bleed-copy",
                "x": 120,
                "y": 170,
                "width": 760,
                "height": 650,
            }
        ],
        "visual_focus_regions": [
            {
                "name": "primary-visual-focus",
                "x": 1040,
                "y": 80,
                "width": 800,
                "height": 920,
            }
        ],
    }
    assert "text-safe region" in entry["prompt"]
    assert "primary-visual-focus" in entry["prompt"]


def test_full_bleed_generated_background_binds_and_matches_dom_contract(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "未来工作方式",
            "message": "用一个沉浸场景建立未来愿景。",
            "layout": "整页主视觉",
            "visual": "整页生图，左侧文字安全区、右侧视觉焦点",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    generated = tmp_path / "assets" / "generated" / "slide-01-background.png"
    generated.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    synced = _run(
        "sync_image_manifest_status.js",
        str(tmp_path / "assets" / "generated" / "manifest.json"),
    )
    assert synced.returncode == 0, synced.stdout + synced.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text('{"slides":{}}', encoding="utf-8")
    patched = _run("apply_deck_patch.js", str(deck_path), str(patch_path))
    assert patched.returncode == 0, patched.stdout + patched.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["background"]["treatment"] == "wash-dark"
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "layout-image-full-bleed" in html
    assert "background-wash-dark" in html

    validated = _run(
        "validate_image_layout_contract.js",
        str(html_path),
        str(tmp_path / "assets" / "generated" / "manifest.json"),
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr


def test_no_images_uses_registered_fallback_for_full_bleed_layout(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "image-full-bleed-v1",
        "--no-images",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    assert deck["slides"][0]["layout_id"] == "statement-focus-v1"
    assert manifest["image_plan"][0]["decision"] == "skip"


def test_auto_image_mode_promotes_visual_story_cover_to_generation(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--title",
        "内马尔巴萨传奇故事",
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["required"] is True
    assert cover["decision"] == "generate"
    assert "visual story" in cover["decision_reason"]


@pytest.mark.parametrize(
    "visual",
    [
        "西班牙地图感背景与四个产区锚点",
        "精酿啤酒瓶与酒厂场景",
        "咖啡产区地图与海拔层次",
        "卡通太阳与环绕轨道",
    ],
)
def test_auto_image_mode_uses_visual_medium_not_domain_keywords(
    tmp_path: Path,
    visual: str,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "对象概览",
            "layout": "cover",
            "visual": visual,
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["required"] is True
    assert cover["decision"] == "generate"
    assert "generative visual medium" in cover["decision_reason"]


def test_auto_image_mode_keeps_structured_visuals_editable(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "决策框架",
            "layout": "cover",
            "visual": "可编辑四象限矩阵与流程箭头",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["required"] is False
    assert cover["decision"] == "skip"


def test_auto_image_mode_respects_explicit_image_opt_out(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--title",
        "内马尔巴萨传奇故事，不要生成图片",
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text()
    )
    cover = manifest["image_plan"][0]
    assert cover["required"] is False
    assert cover["decision"] == "skip"


def test_scaffold_copies_and_validates_user_supplied_image_asset(
    tmp_path: Path,
) -> None:
    source = tmp_path / "client-ui.png"
    source.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAusB9Wl2YvQAAAAASUVORK5CYII="
        )
    )
    deck_path = tmp_path / "deck" / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--image-mode",
        "auto",
        "--image-asset",
        f"1:hero={source}",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    manifest_path = deck_path.parent / "assets" / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cover = manifest["image_plan"][0]
    assert cover["decision"] == "use_existing"
    assert cover["status"] == "ready"
    assert cover["origin"] == "uploaded"
    assert cover["output_path"] == "assets/source/slide-01-hero.png"
    copied = deck_path.parent / cover["output_path"]
    assert copied.read_bytes() == source.read_bytes()

    unbound = _run(
        "validate_image_manifest.js",
        str(manifest_path),
        "--deck",
        str(deck_path),
    )
    assert unbound.returncode == 1
    assert "planned image asset is not referenced by deck.json" in unbound.stdout

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["hero"] = {
        "src": cover["output_path"],
        "alt": "用户提供的客户端界面",
        "origin": "uploaded",
    }
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False),
        encoding="utf-8",
    )
    report_path = deck_path.parent / "qa" / "image_manifest.json"
    validated = _run(
        "validate_image_manifest.js",
        str(manifest_path),
        "--deck",
        str(deck_path),
        "--report",
        str(report_path),
    )

    assert validated.returncode == 0, validated.stdout + validated.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["successfulGeneratedCount"] == 0
    assert report["successfulExistingCount"] == 1


def test_deck_contract_normalizes_observed_model_layout_aliases(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "manifesto-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "clients-logo-grid-v1",
        "awards-press-v1",
        "team-showcase-v1",
        "timeline-horizontal-v1",
        "closing-v2",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cover-hero-v1",
        "statement-focus-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "cards-grid-v1",
        "timeline-horizontal-v1",
        "closing-next-steps-v1",
    ]

    inspected = _run("inspect_layout.js", "team-showcase-v1")
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["id"] == "cards-grid-v1"


def test_deck_contract_normalizes_pitch_layout_and_required_field_aliases(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "problem-solution-v1",
        "process-flow-v1",
        "business-model-v1",
        "comparison-matrix-v1",
        "funding-use-v1",
        "--require-field",
        "3:cards",
        "--require-field",
        "4:matrix",
        "--require-field",
        "5:chart",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stderr
    scaffold_payload = json.loads(scaffold.stdout)
    for path_field in ("deck_file", "image_manifest", "contract_report"):
        scaffold_payload[path_field] = "<artifact-path>"
    scaffold_payload["authoring_rules"]["write_policy"]["artifact_root"] = (
        "<artifact-path>"
    )
    normalized_stdout = (
        json.dumps(scaffold_payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    assert len(normalized_stdout) < 23_750
    deck = json.loads(deck_path.read_text())
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "comparison-two-column-v1",
        "timeline-horizontal-v1",
        "cards-grid-v1",
        "table-data-v1",
        "chart-data-v1",
    ]
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["required_fields"] == [
        {"slide": 3, "field": "items"},
        {"slide": 4, "field": "rows"},
        {"slide": 5, "field": "series"},
    ]
    assert report["required_field_normalizations"] == [
        {"slide": 3, "from": "cards", "to": "items"},
        {"slide": 4, "from": "matrix", "to": "rows"},
        {"slide": 5, "from": "chart", "to": "series"},
    ]


@pytest.mark.parametrize(
    ("requested", "canonical"),
    [
        ("statement-large-v1", "statement-focus-v1"),
        ("creative-team-v2", "cards-grid-v1"),
        ("client-logo-wall-v1", "cards-grid-v1"),
        ("portfolio-showcase-v2", "project-case-study-v1"),
        ("process-roadmap-v2", "timeline-horizontal-v1"),
        ("metrics-dashboard-v2", "kpi-grid-v1"),
        ("versus-split-v2", "comparison-two-column-v1"),
        ("chapter-divider-v2", "section-marker-v1"),
        ("opening-title-v2", "cover-hero-v1"),
        ("visual-split-v2", "image-hero-split-v1"),
        ("ranking-bar-chart-v2", "chart-bar-v1"),
        ("revenue-line-chart-v2", "chart-data-v1"),
        ("market-donut-chart-v2", "chart-data-v1"),
        ("feature-matrix-v2", "table-data-v1"),
        ("risk-heatmap-v1", "heatmap-matrix-v1"),
        ("heatmap-v1", "heatmap-matrix-v1"),
        ("research-deep-dive-v2", "text-columns-v1"),
        ("architecture-diagram-v1", "technical-diagram-v1"),
        ("integration-map-v1", "technical-diagram-v1"),
        ("data-pipeline-v1", "technical-diagram-v1"),
        ("qualitative-dashboard-v1", "dashboard-overview-v1"),
        ("thank-you-v2", "closing-next-steps-v1"),
    ],
)
def test_inspect_layout_normalizes_semantic_aliases(
    requested: str,
    canonical: str,
) -> None:
    inspected = _run("inspect_layout.js", requested)

    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["id"] == canonical


def test_deck_contract_rejects_layout_missing_required_page_field(tmp_path: Path) -> None:
    rejected = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "image-hero-split-v1",
        "--require-field",
        "2:metrics",
        "--out",
        str(tmp_path / "rejected.json"),
    )
    accepted = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "project-case-study-v1",
        "--require-field",
        "2:metrics",
        "--out",
        str(tmp_path / "accepted.json"),
    )

    assert rejected.returncode == 1
    assert "does not provide required field metrics" in rejected.stderr
    assert accepted.returncode == 0, accepted.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["required_fields"] == [{"slide": 2, "field": "metrics"}]


def test_deck_contract_relaxes_decorative_tags_after_visual_cover_normalization(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "制作 NOON Studio 年终作品集",
                "audience": "同行设计师与潜在客户",
                "source_mode": "user_provided",
                "storyline": "用强视觉封面建立作品集语气。",
                "slides": [
                    {
                        "page": 1,
                        "title": "NOON STUDIO — 2026 年终作品集",
                        "message": "第三年，继续把设计做得更直接。",
                        "bullets": ["品牌视觉 + 数字产品设计", "今年交付 28 个项目"],
                        "layout": "cover",
                        "visual": (
                            "米白底、粗黑描边、高饱和色块、旋转角标；"
                            "配一张生成的封面主视觉图"
                        ),
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--outline",
        str(outline_path),
        "--require-field",
        "1:tags",
        "--out",
        str(deck_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "cover-hero-v1"
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["required_fields"] == []
    assert report["required_field_relaxations"] == [
        {
            "slide": 1,
            "field": "tags",
            "requested_layout_id": "cover-editorial-v1",
            "effective_layout_id": "cover-hero-v1",
            "reason": (
                "decorative field is not an explicit semantic outline requirement "
                "and is unsupported by the effective layout"
            ),
        }
    ]
    assert report["warnings"] == [
        (
            "Slide 1 ignored decorative --require-field tags because "
            "cover-hero-v1 does not expose it and the outline does not require "
            "semantic tag content"
        )
    ]


def test_deck_contract_keeps_explicit_tag_content_as_a_hard_requirement(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "制作带内容标签的作品集封面",
                "audience": "潜在客户",
                "source_mode": "user_provided",
                "storyline": "用封面概括四条业务主线。",
                "slides": [
                    {
                        "page": 1,
                        "title": "NOON STUDIO",
                        "message": "四条业务主线必须在封面可编辑。",
                        "bullets": ["封面标签：品牌、产品、空间、文化"],
                        "layout": "cover",
                        "visual": "四个关键词标签与一张生成的封面主视觉图",
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--outline",
        str(outline_path),
        "--require-field",
        "1:tags",
        "--out",
        str(tmp_path / "deck.json"),
    )

    assert result.returncode == 1
    assert (
        "layout cover-hero-v1 does not provide required field tags"
        in result.stderr
    )


def test_strict_source_binding_warns_for_derived_or_paraphrased_facts(
    tmp_path: Path,
) -> None:
    source_text = (
        "工作室：NOON Studio，2026 年是第三年。"
        "业务是品牌视觉 + 数字产品设计；今年交付 28 个项目。"
        "只使用我提供的事实，禁止虚构。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    advisory_path = tmp_path / "advisory" / "deck.json"

    advisory = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "NOON Studio 成立于 2024 年，2026 年是第三年",
        "--fact",
        "业务方向：品牌视觉 + 数字产品设计",
        "--out",
        str(advisory_path),
        env=env,
    )

    assert advisory.returncode == 0, advisory.stdout + advisory.stderr
    assert advisory_path.is_file()
    advisory_report = json.loads(
        (advisory_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert any(
        "成立于 2024 年" in warning and "contiguous phrase" in warning
        for warning in advisory_report["warnings"]
    )

    accepted_path = tmp_path / "accepted" / "deck.json"
    accepted = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--fact",
        "品牌视觉 + 数字产品设计",
        "--fact",
        "今年交付 28 个项目",
        "--out",
        str(accepted_path),
        env=env,
    )

    assert accepted.returncode == 0, accepted.stderr
    report = json.loads((accepted_path.parent / "qa" / "deck_contract.json").read_text())
    assert report["source_binding"]["strict"] is True
    assert report["source_binding"]["verified_fact_count"] == 4


def test_strict_source_binding_restores_exact_source_after_safe_copy_drift(
    tmp_path: Path,
) -> None:
    source_text = (
        "首次响应时间从 18 分钟降至 7 分钟，一次解决率从 68% 提升到 81%。"
        "不补充任何我未提供的事实。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    copied_with_drift = "一次解决率从 68% 提升到 7 分钟"

    result = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        copied_with_drift,
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["truth_contract"]["source_facts"] == [source_text]
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_binding"]["verified_fact_count"] == 1
    assert report["source_fact_normalizations"] == [
        {
            "from": copied_with_drift,
            "to": source_text,
            "reason": "restored exact runtime source text after a non-numeric copy drift",
        }
    ]

    unrelated_path = tmp_path / "unrelated" / "deck.json"
    unrelated = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "这是业内最领先的客服体系",
        "--out",
        str(unrelated_path),
        env=env,
    )
    assert unrelated.returncode == 0, unrelated.stdout + unrelated.stderr
    assert unrelated_path.is_file()
    unrelated_report = json.loads(
        (unrelated_path.parent / "qa" / "deck_contract.json").read_text()
    )
    assert any(
        "这是业内最领先的客服体系" in warning
        for warning in unrelated_report["warnings"]
    )


def test_researched_facts_are_scaffolded_separately_from_user_source(
    tmp_path: Path,
) -> None:
    source_text = "制作一个关于内马尔巴萨传奇故事的 ppt"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--research-fact",
        "Neymar joined FC Barcelona in 2013.",
        "--research-fact",
        "FC Barcelona won the treble in the 2014/15 season.",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text())
    assert deck["truth_contract"] == {
        "mode": "source_bound",
        "source_facts": [],
        "research_facts": [
            "Neymar joined FC Barcelona in 2013.",
            "FC Barcelona won the treble in the 2014/15 season.",
        ],
        "assumptions": [],
    }
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_fact_count"] == 0
    assert report["research_fact_count"] == 2
    assert report["source_binding"]["verified_fact_count"] == 0

    validated = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    truth_report = json.loads(validated.stdout.split("\nDeck truth validation:", 1)[0])
    assert truth_report["researchFactCount"] == 2


def test_short_source_bound_brief_defaults_runtime_request_into_truth_contract(
    tmp_path: Path,
) -> None:
    source_text = "做一个3页介绍《哈利·波特》魔法世界的PPT，不要搜索"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "technical-diagram-v1",
        "statement-focus-v1",
        "--theme",
        "auto",
        "--title",
        "哈利·波特魔法世界",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["truth_contract"]["source_facts"] == [source_text]
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_fact_defaulted_from_runtime"] is True
    assert report["source_binding"]["verified_fact_count"] == 1


def test_long_source_bound_brief_defaults_to_bounded_contiguous_facts(
    tmp_path: Path,
) -> None:
    source_text = (
        "当前场景为市场调研，目标受众为公司内部管理层，页数应根据内容自然决定。"
        "这些配置只用于影响内容结构和视觉表达，不应被误当作外部研究事实。"
        "请制作一份新能源汽车市场分析演示文稿，用于管理层战略汇报。"
        "需要覆盖市场份额、销量变化、品牌竞争、热门车型、区域表现和行业趋势；"
        "主动搜索中国公开数据，优先使用政府部门、行业协会和上市公司披露。"
        "所有数据注明来源、统计时间、发布日期和报告名称，并保持统计口径一致。"
        "如果同一指标存在多个来源，优先采用最新、权威且可验证的数据。"
        "对于无法查证的数据不得虚构，可以明确标注暂无公开数据，"
        "但仍需继续完成可交付的 HTML 演示文稿。"
        "整体风格现代、专业、简洁，适合企业管理层决策参考。"
    )
    assert len(source_text) > 280
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--title",
        "新能源汽车市场分析",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    source_facts = deck["truth_contract"]["source_facts"]
    normalized_source = "".join(source_text.split())
    assert len(source_facts) > 1
    assert all(len(fact) <= 280 for fact in source_facts)
    assert all("".join(fact.split()) in normalized_source for fact in source_facts)
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_fact_defaulted_from_runtime"] is True
    assert report["source_binding"]["verified_fact_count"] == len(source_facts)


def test_number_backing_keeps_cjk_comma_separated_date_and_year_tokens() -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test the controlled deck compiler")
    fact = "出生于 2007-07-13，2014 年以 7 岁加入 FC Barcelona"
    script = (
        f"const truth=require({json.dumps(str(SCRIPTS_DIR / 'validate_deck_truth.js'))});"
        f"const facts=[{json.dumps(fact, ensure_ascii=False)}];"
        "console.log(JSON.stringify(["
        "truth.isNumberSourceBacked('13', facts),"
        "truth.isNumberSourceBacked('2014', facts)"
        "]));"
    )

    result = subprocess.run(
        [str(NODE), "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [True, True]


def test_public_research_deck_accepts_source_preserving_award_paraphrases(
    tmp_path: Path,
) -> None:
    source_text = "制作一套介绍拉明·亚马尔的 PPT"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    research_facts = [
        (
            "UEFA 报道 Lamine Yamal 随西班牙赢得 EURO 2024，并获 "
            "Young Player of the Tournament"
        ),
        "EURO 2024 决赛在其 17 岁生日后一天",
    ]
    deck_path = tmp_path / "deck.json"
    scaffold_args = ["inspect_deck_contract.js", "cards-grid-v1"]
    for fact in research_facts:
        scaffold_args.extend(["--research-fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run(*scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    items = [
        {
            "kicker": "01",
            "title": "西班牙夺冠成员",
            "body": "UEFA 报道称，亚马尔随西班牙赢得 EURO 2024。",
        },
        {
            "kicker": "02",
            "title": "最佳年轻球员",
            "body": "UEFA 报道称，他被评为 Young Player of the Tournament。",
        },
        {
            "kicker": "03",
            "title": "决赛节点",
            "body": "决赛发生在其 17 岁生日后一天。",
        },
    ]
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "国家队与欧洲杯突破",
                            "subtitle": "纪录进入冠军叙事",
                            "items": items,
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["items"] == items
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_public_research_deck_accepts_supported_chinese_result_paraphrase(
    tmp_path: Path,
) -> None:
    source_text = "制作一套4页介绍西班牙夺得2026世界杯的ppt"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    research_fact = (
        "西班牙1:0击败阿根廷，时隔16年再次捧杯；"
        "队史第二座世界杯冠军 | 中国新闻网"
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "--research-fact",
        research_fact,
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    proof = {
        "value": "第二座",
        "label": "西班牙再次赢得世界杯，队史冠军数来到第二座。",
    }
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "statement": "历史意义：16年后再捧世界杯",
                            "support": "西班牙时隔16年再次夺冠。",
                            "proofs": [proof],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["proofs"] == [proof]
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_public_research_patch_omits_only_unsupported_optional_proof(
    tmp_path: Path,
) -> None:
    source_text = "制作一套4页介绍西班牙夺得2026世界杯的ppt"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "--research-fact",
        "西班牙1:0击败阿根廷，时隔16年再次捧杯；队史第二座世界杯冠军",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    supported = {
        "value": "第二座",
        "label": "西班牙再次赢得世界杯，队史冠军数来到第二座。",
    }
    unsupported = [
        {"value": "错误对手", "label": "阿根廷赢得世界杯。"},
        {"value": "错误赛事", "label": "西班牙赢得欧洲杯冠军。"},
    ]
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "statement": "历史意义：16年后再捧世界杯",
                            "support": "西班牙时隔16年再次夺冠。",
                            "proofs": [supported, *unsupported],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    payload = json.loads(applied.stdout)
    assert (
        "slides.slide-01.props.proofs.1: omitted unsupported optional research proof"
        in payload["truth_guard_changes"]
    )
    assert len(payload["truth_guard_changes"]) == 2
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["proofs"] == [supported]


def test_public_research_deck_accepts_generic_award_heading_and_synthesis(
    tmp_path: Path,
) -> None:
    source_text = "制作一套介绍拉明·亚马尔的 PPT"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    research_facts = [
        "2024 Kopa Trophy; 21岁以下 | FC Barcelona",
        "2024 Golden Boy | FC Barcelona",
        "2023 Golden Boy The Youngest | FC Barcelona",
    ]
    deck_path = tmp_path / "deck.json"
    scaffold_args = ["inspect_deck_contract.js", "cards-grid-v1"]
    for fact in research_facts:
        scaffold_args.extend(["--research-fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run(*scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "已获奖项与可信总结",
                            "subtitle": "公开记录构成外部认可。",
                            "items": [
                                {
                                    "kicker": "2024",
                                    "title": "Kopa Trophy",
                                    "body": "2024 Kopa Trophy; 21岁以下",
                                },
                                {
                                    "kicker": "2024",
                                    "title": "Golden Boy",
                                    "body": "2024 Golden Boy",
                                },
                                {
                                    "kicker": "2023",
                                    "title": "Golden Boy The Youngest",
                                    "body": "2023 Golden Boy The Youngest",
                                },
                                {
                                    "kicker": "总结",
                                    "title": "可信定位",
                                    "body": (
                                        "以官方纪录、冠军经历和已获奖项支撑其青年代表"
                                        "人物形象。"
                                    ),
                                },
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_apply_patch_flattens_nested_background_image_object(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "cover.png").write_bytes(b"image")
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "background": {
                            "image": {
                                "src": "assets/generated/cover.png",
                                "alt": "球场氛围概念图",
                            },
                            "treatment": "wash-light",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    result = json.loads(applied.stdout)
    assert (
        "slides.slide-01.background.image: flattened nested media object"
        in result["normalization_changes"]
    )
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["background"] == {
        "src": "assets/generated/cover.png",
        "alt": "球场氛围概念图",
        "origin": "generated",
        "fit": "cover",
        "position": "center",
        "treatment": "wash-light",
    }


def test_strict_source_request_warns_for_researched_facts(tmp_path: Path) -> None:
    source_text = "只使用我提供的事实，禁止虚构：内马尔。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "内马尔",
        "--research-fact",
        "Neymar joined FC Barcelona in 2013.",
        "--out",
        str(tmp_path / "deck.json"),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert any("strict source-only request" in warning for warning in report["warnings"])


def test_source_fact_binding_ignores_editorial_whitespace(tmp_path: Path) -> None:
    source_text = (
        "产品为面向 中小制造工厂 的 AI 质检 + 智能排产平台。"
        "已有 30 家试点客户。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "产品为面向中小制造工厂的 AI 质检 + 智能排产平台。",
        "--fact",
        "已有30家试点客户",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_binding"]["verified_fact_count"] == 2


def test_strict_source_binding_strips_model_added_fact_labels(tmp_path: Path) -> None:
    source_text = (
        "工作室叫 NOON Studio。业务是品牌视觉 + 数字产品设计。"
        "只使用我提供的事实，禁止虚构。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"

    result = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "工作室名称：NOON Studio",
        "--fact",
        "业务：品牌视觉 + 数字产品设计",
        "--out",
        str(deck_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text())
    assert deck["truth_contract"]["source_facts"] == [
        "NOON Studio",
        "品牌视觉 + 数字产品设计",
    ]
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["source_binding"]["verified_fact_count"] == 2
    assert report["source_fact_normalizations"] == [
        {"from": "工作室名称：NOON Studio", "to": "NOON Studio"},
        {"from": "业务：品牌视觉 + 数字产品设计", "to": "品牌视觉 + 数字产品设计"},
    ]


def test_authorized_assumptions_support_disclosed_percent_chart_data(
    tmp_path: Path,
) -> None:
    source_text = (
        "公司名为 ACME。"
        "如涉及未提供的具体数据，请使用合理假设数据，"
        "并在备注中标明为示意 / 假设。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "ACME",
        "--assumption",
        "服务收入占比假设：咨询 45%、产品 35%、培训 20%",
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert report["assumption_count"] == 1
    assert report["source_binding"]["allows_assumptions"] is True

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "ACME",
            "title": "服务收入结构",
            "subtitle": "三类业务占比",
            "chart_type": "donut",
            "categories": ["咨询", "产品", "培训"],
            "series": [{"name": "占比", "values": ["45", "35", "20"]}],
            "value_suffix": "%",
            "insight": "咨询业务占比最高",
            "source": "假设数据，仅作示意",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    accepted = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    payload = json.loads(accepted.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["assumptionCount"] == 1

    deck["slides"][0]["props"]["source"] = ""
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    rejected = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    rejected_payload = json.loads(
        rejected.stdout.split("\nDeck truth validation:", 1)[0]
    )
    assert any(
        "visible 假设/示意 disclosure" in warning
        for warning in rejected_payload["warnings"]
    )


def test_truth_validator_accepts_only_matching_zero_padded_structure_ordinals(
    tmp_path: Path,
) -> None:
    source_text = "公司名为 ACME。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "kpi-grid-v1",
        "--fact",
        "ACME",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "ACME",
            "title": "ACME",
            "subtitle": "产品介绍",
            "meta": "",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "01 痛点",
            "title": "产品痛点",
            "subtitle": "",
            "items": [
                {"kicker": "质量", "title": "检测", "body": "质量流程"},
                {"kicker": "计划", "title": "排产", "body": "计划流程"},
                {"kicker": "交付", "title": "履约", "body": "交付流程"},
            ],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "02 产品",
            "title": "产品模块",
            "subtitle": "",
            "items": [
                {"label": "检测", "value": "01", "detail": "", "delta": ""},
                {"label": "排产", "value": "02", "detail": "", "delta": ""},
                {"label": "预警", "value": "03", "detail": "", "delta": ""},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    accepted = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    deck["slides"][1]["props"]["eyebrow"] = "02 痛点"
    deck["slides"][2]["props"]["items"][0]["value"] = "02"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    rejected = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    warnings = "\n".join(
        json.loads(rejected.stdout.split("\nDeck truth validation:", 1)[0])["warnings"]
    )
    assert "slides.slide-02.props.eyebrow" in warnings
    assert "slides.slide-03.props.items.0.value" in warnings


def test_truth_validator_accepts_disclosed_unenumerated_chart_assumptions(
    tmp_path: Path,
) -> None:
    source_text = (
        "公司名为 ACME。未提供的具体数据可以使用合理假设数据，"
        "并标明为示意 / 假设。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "chart-bar-v1",
        "--fact",
        "ACME",
        "--assumption",
        "市场规模、预测趋势与资金用途拆分均为示意 / 假设数据。",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "市场空间",
            "title": "ACME 市场空间",
            "subtitle": "预测数字仅作路演表达。",
            "chart_type": "column",
            "categories": ["2025Q1", "2030E"],
            "series": [{"name": "2030E 预测", "values": ["90", "660"]}],
            "highlights": [
                {
                    "value": "3个入口",
                    "label": "2030E 合计空间",
                    "note": "示意 / 假设",
                }
            ],
            "source": "示意 / 假设数据",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "资金用途",
            "title": "ACME 资金用途",
            "subtitle": "拆分比例仅作路演表达。",
            "series_label": "2030E 计划",
            "items": [
                {"label": "研发 2026E", "value": "45", "note": "规划"},
                {"label": "销售", "value": "35", "note": "规划"},
                {"label": "交付", "value": "20", "note": "规划"},
            ],
            "source": "示意 / 假设数据",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr


def test_truth_validator_keeps_unenumerated_assumption_guardrails(
    tmp_path: Path,
) -> None:
    source_text = (
        "公司名为 ACME。未提供的具体数据可以使用合理假设数据，"
        "并标明为示意 / 假设。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "statement-focus-v1",
        "--fact",
        "ACME",
        "--assumption",
        "市场规模与预测趋势均为示意 / 假设数据。",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "市场空间",
            "title": "ACME 市场空间",
            "subtitle": "规划数据",
            "chart_type": "column",
            "categories": ["2025Q1", "2030E"],
            "series": [{"name": "2030E 预测", "values": ["90", "660"]}],
            "highlights": [],
            "source": "内部规划",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "长期目标",
            "statement": "2030 年目标收入 660 亿元（示意 / 假设）",
            "support": "示意 / 假设数据",
            "proofs": [],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    without_disclosure = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert without_disclosure.returncode == 0, (
        without_disclosure.stdout + without_disclosure.stderr
    )
    warnings = "\n".join(
        json.loads(
            without_disclosure.stdout.split("\nDeck truth validation:", 1)[0]
        )["warnings"]
    )
    assert "slides.slide-01.props.categories.0" in warnings
    assert "slides.slide-02.props.statement" in warnings

    deck["slides"][0]["props"]["source"] = "示意 / 假设数据"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    authorized = _run("validate_deck_truth.js", str(deck_path), env=env)
    authorized_warnings = "\n".join(
        json.loads(authorized.stdout.split("\nDeck truth validation:", 1)[0])["warnings"]
    )

    assert authorized.returncode == 0, authorized.stdout + authorized.stderr
    assert "slides.slide-01.props.categories.0" not in authorized_warnings
    assert "slides.slide-02.props.statement" in authorized_warnings

    unauthorized_env = os.environ.copy()
    unauthorized_env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "公司名为 ACME。请制作一份介绍。".encode("utf-8")
    ).decode("ascii")
    unauthorized = _run(
        "validate_deck_truth.js",
        str(deck_path),
        env=unauthorized_env,
    )
    unauthorized_warnings = "\n".join(
        json.loads(unauthorized.stdout.split("\nDeck truth validation:", 1)[0])["warnings"]
    )

    assert unauthorized.returncode == 0, unauthorized.stdout + unauthorized.stderr
    assert (
        "truth_contract.assumptions requires explicit user permission"
        in unauthorized_warnings
    )
    assert "slides.slide-01.props.categories.0" in unauthorized_warnings


@pytest.mark.parametrize(
    "source_text",
    [
        "公司名为 ACME。请制作一份简介。",
        "公司名为 ACME。不可以使用假设数据。",
        "公司名为 ACME。只使用我提供的事实，禁止虚构。可以使用假设数据。",
    ],
)
def test_assumptions_without_unambiguous_user_permission_are_advisory(
    tmp_path: Path,
    source_text: str,
) -> None:
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")

    result = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "ACME",
        "--assumption",
        "业务占比假设为 45%",
        "--out",
        str(tmp_path / "deck.json"),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert any(
        "requires explicit user permission" in warning
        for warning in report["warnings"]
    )


def test_derived_assumption_in_source_facts_is_advisory(
    tmp_path: Path,
) -> None:
    source_text = "公司名为 ACME。请使用合理假设数据并标明为示意。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")

    result = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "ACME 收入增长 45%",
        "--out",
        str(tmp_path / "deck.json"),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "qa" / "deck_contract.json").read_text())
    assert any("contiguous phrase" in warning for warning in report["warnings"])


def test_batch_patch_can_add_only_authorized_assumptions(
    tmp_path: Path,
) -> None:
    source_text = "公司名为 ACME。请使用合理假设数据并标明为示意。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--fact",
        "ACME",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "truth_contract": {
                    "mode": "illustrative",
                    "source_facts": ["伪造事实"],
                    "assumptions": ["业务占比假设为 45%"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    patched = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert patched.returncode == 0, patched.stdout + patched.stderr
    deck = json.loads(deck_path.read_text())
    assert deck["truth_contract"] == {
        "mode": "source_bound",
        "source_facts": ["ACME"],
        "assumptions": ["业务占比假设为 45%"],
    }
    changes = json.loads(patched.stdout)["normalization_changes"]
    assert "truth_contract.mode: ignored patch mutation and preserved scaffold mode" in changes
    assert (
        "truth_contract.source_facts: ignored patch mutation and preserved scaffold facts"
        in changes
    )


def test_truth_validator_rechecks_strict_source_fact_provenance(tmp_path: Path) -> None:
    source_text = "NOON Studio，2026 年是第三年。只使用我提供的事实，禁止虚构。"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["truth_contract"]["source_facts"].append("成立于 2024 年")
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["sourceBinding"]["strict"] is True
    assert any("成立于 2024 年" in warning for warning in payload["warnings"])


def test_strict_truth_validator_warns_for_invented_narrative_and_unlabeled_concept_media(
    tmp_path: Path,
) -> None:
    source_text = (
        "NOON Studio。2026 年是第三年。品牌视觉 + 数字产品设计。"
        "今年交付 28 个项目。覆盖 SaaS、消费品、文化机构三个领域。"
        "页面包括合作客户、获奖与刊载、团队、流程、明年。"
        "只使用我提供的事实，禁止虚构。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "timeline-horizontal-v1",
        "cards-grid-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--fact",
        "品牌视觉 + 数字产品设计",
        "--fact",
        "今年交付 28 个项目",
        "--fact",
        "SaaS、消费品、文化机构三个领域",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "statement": "成为更有影响力的设计工作室",
            "support": "持续拓展国际客户与长期品牌价值",
            "proofs": [{"value": "获奖数量翻倍", "label": "明年目标"}],
        }
    )
    deck["slides"][1]["props"]["items"] = [
        {"label": "PROJECTS", "value": "28", "detail": "全年高质量交付", "delta": ""},
        {"label": "CLIENTS", "value": "待补充", "detail": "待补充", "delta": ""},
        {"label": "AWARDS", "value": "待补充", "detail": "待补充", "delta": ""},
    ]
    deck["slides"][2]["props"].update(
        {
            "title": "品牌项目 A（待补充）",
            "positioning": "从 0 到 1 构建 SaaS 品牌识别系统",
            "image": {
                "src": "assets/generated/project-brand.png",
                "alt": "SaaS 品牌项目实景",
            },
            "metrics": [
                {"value": "待补充", "label": "项目指标"},
                {"value": "待补充", "label": "项目结果"},
            ],
            "caption": "项目上线后获得客户一致认可",
        }
    )
    deck["slides"][3]["props"]["steps"] = [
        {"phase": "阶段 1", "title": "深度理解", "body": "与客户共创业务洞察"},
        {"phase": "阶段 2", "title": "大胆提案", "body": "用视觉建立差异化"},
        {"phase": "阶段 3", "title": "快速迭代", "body": "持续验证并完善方案"},
    ]
    deck["slides"][4]["props"].update(
        {
            "title": "团队",
            "items": [
                {"kicker": "01", "title": "设计", "body": "负责品牌与产品体验"},
                {"kicker": "02", "title": "策略", "body": "连接商业与创意"},
                {"kicker": "03", "title": "技术", "body": "推动数字产品落地"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    warnings = "\n".join(payload["warnings"])
    assert "strict source-only field is not source-backed" in warnings
    assert "generated project media must declare origin" in warnings
    assert "generated project media must be labeled as AI concept/placeholder" in warnings
    assert "slides.slide-04.props.steps.0.title" in warnings
    assert "team-member name is not source-backed" in warnings


def test_strict_truth_validator_accepts_exact_copy_placeholders_and_labeled_concept_media(
    tmp_path: Path,
) -> None:
    source_text = (
        "NOON Studio。2026 年是第三年。品牌视觉 + 数字产品设计。"
        "今年交付 28 个项目。流程是理解、提案、迭代。"
        "只使用我提供的事实，禁止虚构。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "timeline-horizontal-v1",
        "cards-grid-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--fact",
        "品牌视觉 + 数字产品设计",
        "--fact",
        "今年交付 28 个项目",
        "--fact",
        "流程是理解、提案、迭代",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "statement": "2026 年是第三年",
            "support": "品牌视觉 + 数字产品设计",
            "proofs": [{"value": "今年交付 28 个项目", "label": "项目"}],
        }
    )
    deck["slides"][1]["props"]["items"] = [
        {"label": "PROJECTS", "value": "28", "detail": "今年交付 28 个项目", "delta": ""},
        {"label": "CLIENTS", "value": "待补充", "detail": "待补充", "delta": ""},
        {"label": "AWARDS", "value": "待补充", "detail": "待补充", "delta": ""},
    ]
    deck["slides"][2]["props"].update(
        {
            "title": "品牌项目 A（待补充）",
            "positioning": "品牌视觉 + 数字产品设计",
            "image": {
                "src": "assets/generated/project-brand.png",
                "alt": "AI 概念视觉，实际项目图待补充",
                "origin": "generated",
            },
            "metrics": [
                {"value": "待补充", "label": "项目指标"},
                {"value": "待补充", "label": "项目结果"},
            ],
            "caption": "AI 概念视觉，实际项目图待补充",
        }
    )
    deck["slides"][3]["props"]["steps"] = [
        {"phase": "阶段 1", "title": "理解", "body": "待补充"},
        {"phase": "阶段 2", "title": "提案", "body": "待补充"},
        {"phase": "阶段 3", "title": "迭代", "body": "待补充"},
    ]
    deck["slides"][4]["props"].update(
        {
            "title": "团队",
            "items": [
                {"kicker": "01", "title": "团队成员待补充", "body": "待补充"},
                {"kicker": "02", "title": "团队成员待补充", "body": "待补充"},
                {"kicker": "03", "title": "团队成员待补充", "body": "待补充"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr


def test_controlled_batch_patch_preserves_layout_contract_and_scaffolded_facts(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "truth_contract": {
                    "mode": "source_bound",
                    "source_facts": ["NOON Studio", "2026 年是第三年", "今年交付 28 个项目"],
                },
                "slides": {
                    "slide-01": {
                        "props": {
                            "headline": "NOON Studio",
                            "subhead": "2026 · 第三年",
                            "kicker": "品牌视觉 + 数字产品设计",
                            "caption": "今年交付 28 个项目",
                            "image": {
                                "path": "assets/generated/cover-hero.png",
                                "alt_text": "NOON Studio 封面视觉",
                            },
                        }
                    },
                    "slide-02": {
                        "props": {
                            "statement": "第三年，继续把设计做深。",
                            "support": "品牌视觉 × 数字产品设计",
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stderr
    deck = json.loads(deck_path.read_text())
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "cover-hero-v1",
        "statement-focus-v1",
    ]
    assert deck["slides"][0]["props"]["title"] == "NOON Studio"
    assert deck["slides"][0]["props"]["subtitle"] == "2026 · 第三年"
    assert deck["slides"][0]["props"]["eyebrow"] == "品牌视觉 + 数字产品设计"
    assert deck["slides"][0]["props"]["meta"] == "今年交付 28 个项目"
    assert deck["slides"][0]["props"]["hero"] == {
        "src": "assets/generated/cover-hero.png",
        "alt": "NOON Studio 封面视觉",
        "origin": "generated",
    }
    assert deck["truth_contract"]["source_facts"] == [
        "NOON Studio",
        "2026 年是第三年",
    ]
    payload = json.loads(result.stdout)
    assert (
        "truth_contract.source_facts: ignored patch mutation and preserved scaffold facts"
        in payload["normalization_changes"]
    )


def test_batch_patch_reconciles_ready_manifest_background_at_declared_path(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--image-mode",
        "creative_image_mode",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cover = manifest["image_plan"][0]
    assert cover["prop_path"] == "background"
    asset_path = tmp_path / cover["output_path"]
    asset_path.write_bytes(b"generated-cover")
    cover["status"] = "generated"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "media": {
                                "src": cover["output_path"],
                                "origin": "generated",
                            }
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slide = deck["slides"][0]
    assert "media" not in slide["props"]
    assert slide["background"]["src"] == cover["output_path"]
    assert slide["background"]["origin"] == "generated"
    payload = json.loads(result.stdout)
    assert any(
        "dropped unknown field for cover-editorial-v1" in change
        for change in payload["normalization_changes"]
    )
    assert any(
        "bound ready media to slide background" in change
        for change in payload["normalization_changes"]
    )


def test_batch_patch_normalizes_nested_architecture_module_capacity(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "architecture-layered-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "layers": [
                                {
                                    "label": "TOUCHPOINT",
                                    "title": "用户触点层",
                                    "modules": ["官网", "APP"],
                                },
                                {
                                    "label": "AI SERVICE",
                                    "title": "智能服务层",
                                    "modules": [
                                        "意图识别",
                                        "多轮对话",
                                        "知识检索",
                                        "会话路由",
                                        "人工转接",
                                        "额外模块",
                                    ],
                                },
                                {
                                    "label": "INTEGRATION",
                                    "title": "业务集成层",
                                    "modules": ["订单系统", "会员系统"],
                                },
                            ]
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    modules = deck["slides"][0]["props"]["layers"][1]["modules"]
    assert modules == ["意图识别", "多轮对话", "知识检索", "会话路由", "人工转接"]
    payload = json.loads(result.stdout)
    assert (
        "slides.slide-01.props.layers.1.modules: truncated to 5 items"
        in payload["normalization_changes"]
    )


def test_batch_patch_moves_need_solution_value_source_to_table_insight(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    source = (
        "需求：客户需要清楚评估实施阶段、责任分工、关键里程碑、验收边界和上线风险。"
        "方案：按启动、调研、建设、集成、测试、试点、推广和优化分阶段推进。"
        "价值：通过试点验证和分阶段推广控制风险，让业务与技术团队逐步进入稳定运营。"
    )
    assert len(source) > 100
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {"slides": {"slide-01": {"props": {"source": source}}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["source"] == ""
    assert deck["slides"][0]["props"]["insight"] == (
        "客户收益：通过试点验证和分阶段推广控制风险，让业务与技术团队逐步进入稳定运营。"
    )
    payload = json.loads(result.stdout)
    assert (
        "slides.slide-01.props.source: moved overlong need-solution-value copy to insight"
        in payload["normalization_changes"]
    )
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'class="table-insight" data-prop-path="insight"' in html
    assert "客户收益：通过试点验证和分阶段推广控制风险" in html


def test_batch_patch_compacts_overlong_optional_source_caption(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    source = "来源：客户提供的项目资料与内部访谈纪要；" + "补充来源说明" * 20
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {"slides": {"slide-01": {"props": {"source": source}}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    compacted = deck["slides"][0]["props"]["source"]
    assert 0 < len(compacted) <= 100
    assert compacted.startswith("来源：客户提供的项目资料与内部访谈纪要")
    payload = json.loads(result.stdout)
    assert (
        "slides.slide-01.props.source: compacted optional source caption to 100 characters"
        in payload["normalization_changes"]
    )


def test_batch_patch_wraps_unambiguous_top_level_slide_ids(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slide-01": {"props": {"title": "巴西足球历史"}},
                "slide-02": {"props": {"statement": "五冠之外，风格仍在延续"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["patched_slides"] == ["slide-01", "slide-02"]
    assert (
        'patch: nested direct slide-id keys under the top-level "slides" object'
        in payload["normalization_changes"]
    )
    deck = json.loads(deck_path.read_text())
    assert deck["slides"][0]["props"]["title"] == "巴西足球历史"
    assert deck["slides"][1]["props"]["statement"] == "五冠之外，风格仍在延续"


def test_batch_patch_removes_only_redundant_trailing_json_closers(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    valid_patch = {
        "slides": {"slide-01": {"props": {"title": "安全恢复后的标题"}}}
    }
    patch_path.write_text(
        json.dumps(valid_patch, ensure_ascii=False) + "}\n",
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "patch: removed 1 redundant trailing JSON closer(s)" in payload[
        "normalization_changes"
    ]
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["props"]["title"] == "安全恢复后的标题"

    invalid_patch = tmp_path / "invalid.patch.json"
    invalid_patch.write_text(
        '{"slides":{"slide-01":{"props":{"title":}}}}',
        encoding="utf-8",
    )
    rejected = _run("apply_deck_patch.js", str(deck_path), str(invalid_patch))
    assert rejected.returncode == 1
    assert "Invalid JSON" in rejected.stderr


def test_batch_patch_normalizes_background_type_and_missing_kpi_detail(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "kpi-grid-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "background": {
                            "type": "image",
                            "src": "assets/generated/slide-01-hero.png",
                        }
                    },
                    "slide-02": {
                        "props": {
                            "items": [
                                {"label": "成立年数", "value": "3", "unit": "年"},
                                {"label": "交付项目", "value": "28", "unit": "个"},
                                {"label": "覆盖领域", "value": "3", "unit": "个"},
                                {
                                    "label": "业务方向",
                                    "value": "品牌视觉 + 数字产品设计",
                                },
                            ]
                        }
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "slides.slide-01.background.type: dropped unknown media field" in payload[
        "normalization_changes"
    ]
    deck = json.loads(deck_path.read_text())
    assert deck["slides"][0]["background"] == {
        "src": "assets/generated/slide-01-hero.png",
        "alt": "AI 生成的背景概念视觉",
        "origin": "generated",
        "fit": "cover",
        "position": "center",
        "treatment": "wash-light",
    }
    items = deck["slides"][1]["props"]["items"]
    assert [item["value"] for item in items] == [
        "3年",
        "28个",
        "3个",
        "品牌视觉 + 数字产品设计",
    ]
    assert all(item.get("detail", "") == "" for item in items)


def test_batch_patch_normalizes_background_image_and_string_proofs(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "background": {
                            "image": "assets/generated/slide-01-cover.png",
                            "origin": "generated",
                            "alt": "NOON Studio 2026 封面",
                        }
                    },
                    "slide-02": {
                        "props": {
                            "proofs": [
                                "28 个项目",
                                "SaaS / 消费品 / 文化机构",
                                "第三年",
                            ],
                            "proof_style": "block",
                            "emphasis": "statement",
                        }
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("apply_deck_patch.js", str(deck_path), str(patch_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    changes = payload["normalization_changes"]
    assert "slides.slide-01.background.image: mapped to src" in changes
    assert "slides.slide-02.props.proofs.0: converted proof text to an object" in changes
    deck = json.loads(deck_path.read_text())
    assert deck["slides"][0]["background"]["src"] == (
        "assets/generated/slide-01-cover.png"
    )
    assert deck["slides"][0]["background"]["alt"] == "NOON Studio 2026 封面"
    assert deck["slides"][1]["props"]["proofs"] == [
        {"value": "28 个项目", "label": ""},
        {"value": "SaaS / 消费品 / 文化机构", "label": ""},
        {"value": "第三年", "label": ""},
    ]
    assert deck["slides"][1]["props"]["proof_style"] == "auto"
    assert deck["slides"][1]["props"]["emphasis"] == "balanced"

    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'class="proof-label"' not in html
    assert ">待补充<" not in html


def test_strict_batch_patch_normalizes_observed_model_drift_in_one_pass(
    tmp_path: Path,
) -> None:
    source_text = (
        "做一份 NOON Studio 作品集。2026 年是第三年，"
        "业务是品牌视觉 + 数字产品设计，今年交付 28 个项目，"
        "覆盖 SaaS、消费品、文化机构三个领域。"
        "页面包括合作客户、获奖与刊载、团队、流程、明年。"
        "只使用我提供的事实，禁止虚构。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    original_facts = [
        "NOON Studio",
        "2026 年是第三年",
        "业务是品牌视觉 + 数字产品设计",
        "今年交付 28 个项目",
        "覆盖 SaaS、消费品、文化机构三个领域",
    ]
    scaffold_args: list[str] = [
        "cover-hero-v1",
        "statement-focus-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "cards-grid-v1",
        "timeline-horizontal-v1",
    ]
    for fact in original_facts:
        scaffold_args.extend(["--fact", fact])
    scaffold_args.extend(["--out", str(deck_path)])
    scaffold = _run("inspect_deck_contract.js", *scaffold_args, env=env)
    assert scaffold.returncode == 0, scaffold.stderr

    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "truth_contract": {
                    "mode": "source_bound",
                    "source_facts": [
                        "NOON Studio 成立于 2024 年，2026 年是第三年",
                        "2026 年交付 28 个项目",
                    ],
                },
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "NOON Studio",
                            "subtitle": "品牌视觉 × 数字产品设计",
                            "caption": "2024-2026",
                            "hero": "assets/generated/slide-01-cover.png",
                        }
                    },
                    "slide-02": {
                        "props": {
                            "statement": "第三年，28 个项目，三个领域，一条主线",
                            "subtitle": "成立于 2024 年，持续拓展国际客户",
                            "proofs": [
                                {"label": "成立年份", "value": "2024"},
                            ],
                        }
                    },
                    "slide-03": {
                        "props": {
                            "items": [
                                {
                                    "label": "交付项目",
                                    "value": "28",
                                    "unit": "个",
                                    "trend": "2026 全年",
                                },
                                {
                                    "label": "覆盖领域",
                                    "value": "3",
                                    "trend": "SaaS · 消费品 · 文化机构",
                                },
                                {
                                    "label": "运营年限",
                                    "value": "3rd",
                                    "trend": "2026 年是工作室第三年",
                                },
                                {
                                    "label": "业务方向",
                                    "value": "2",
                                    "trend": "品牌视觉 · 数字产品设计",
                                },
                            ]
                        }
                    },
                    "slide-04": {
                        "props": {
                            "title": "品牌项目 A",
                            "client": "某知名客户",
                            "year": "2026",
                            "tags": ["品牌视觉"],
                            "summary": "从 0 到 1 构建品牌识别系统",
                            "image": {
                                "path": "assets/generated/slide-04-brand.png",
                                "alt_text": "品牌项目实景",
                            },
                            "metrics": [
                                {"label": "项目指标", "value": "待补充"},
                                {"label": "结果指标", "value": "待补充"},
                            ],
                            "composition": "image-right",
                        }
                    },
                    "slide-05": {
                        "props": {
                            "eyebrow": "合作客户",
                            "title": "他们选择了 NOON",
                            "subtitle": "被不同领域持续信任",
                            "items": [
                                {"title": "客户 A", "description": "长期合作"},
                                {"title": "SaaS 领域", "description": "待补充"},
                                {"title": "客户 C", "description": "待补充"},
                            ],
                        }
                    },
                    "slide-06": {
                        "props": {
                            "title": "我们的流程",
                            "subtitle": "每个项目都经历三个阶段，确保从理解到交付的一致性",
                            "steps": [
                                {"phase": "01", "title": "发现", "description": "理解客户"},
                                {"phase": "02", "title": "设计", "description": "大胆提案"},
                                {"phase": "03", "title": "交付", "description": "确保落地"},
                            ],
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["normalization_changes"]
    assert payload["truth_guard_changes"]
    deck = json.loads(deck_path.read_text())
    assert deck["truth_contract"]["source_facts"] == original_facts
    serialized = json.dumps(deck, ensure_ascii=False)
    assert "2024" not in serialized
    assert "某知名客户" not in serialized
    assert "client" not in deck["slides"][3]["props"]
    assert "summary" not in deck["slides"][3]["props"]
    assert deck["slides"][3]["props"]["positioning"] == "待补充"
    assert deck["slides"][3]["props"]["image"]["origin"] == "generated"
    assert "AI 概念" in deck["slides"][3]["props"]["image"]["alt"]
    assert "path" not in deck["slides"][3]["props"]["image"]
    assert "alt_text" not in deck["slides"][3]["props"]["image"]
    assert deck["slides"][4]["props"]["title"] == "合作客户"
    assert deck["slides"][4]["props"]["subtitle"] == "待补充"
    assert deck["slides"][4]["props"]["items"][0]["body"] == "待补充"
    assert deck["slides"][5]["props"]["title"] == "我们的流程"
    assert deck["slides"][5]["props"]["subtitle"] == ""
    assert deck["slides"][5]["props"]["steps"][0]["title"] == "待补充"
    assert deck["slides"][2]["props"]["items"][1]["value"] == "3"
    assert deck["slides"][2]["props"]["items"][3]["value"] == "待补充"

    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr

    deck["slides"][4]["props"]["title"] = "他们选择了 NOON"
    deck["slides"][4]["props"]["subtitle"] = "被不同领域持续信任"
    deck["slides"][5]["props"]["title"] = "这套流程确保每个项目成功"
    deck["slides"][5]["props"]["subtitle"] = "每个项目都经历三个阶段"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    rejected_truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert rejected_truth.returncode == 0, (
        rejected_truth.stdout + rejected_truth.stderr
    )
    rejected_payload = json.loads(
        rejected_truth.stdout.split("\nDeck truth validation:", 1)[0]
    )
    rejected_warnings = "\n".join(rejected_payload["warnings"])
    assert "slides.slide-05.props.title" in rejected_warnings
    assert "slides.slide-05.props.subtitle" in rejected_warnings
    assert "slides.slide-06.props.title" in rejected_warnings
    assert "slides.slide-06.props.subtitle" in rejected_warnings


def test_truth_validator_warns_for_observed_unsourced_claims(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--fact",
        "今年交付 28 个项目",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "title": "NOON Studio",
            "meta": "EST. 2024",
        }
    )
    deck["slides"][1]["props"]["proofs"] = [
        {"label": "客户关系", "value": "客户复购率持续提升"},
        {"label": "团队发展", "value": "团队从 3 人走到今天"},
    ]
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    warnings = "\n".join(payload["warnings"])
    assert 'numeric claim "2024"' in warnings
    assert "performance/award/publication claim is not source-backed" in warnings
    assert "team-size claim is not source-backed" in warnings


def test_truth_validator_allows_qualitative_problem_and_expected_value_copy(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--fact",
        "AA 科技能力包括 AI 客服机器人、知识库管理、人工转接和数据看板",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "需求 — 方案 — 价值",
            "title": "客户价值",
            "subtitle": (
                "需求：客户希望降低人工客服压力并提升响应速度。"
                "方案：以 AI 客服机器人连接知识库、人工转接和数据看板。"
                "价值：让采购、IT、业务同时看到成本可控、技术可信、运营可持续。"
            ),
            "items": [
                {
                    "kicker": "01",
                    "title": "客户痛点",
                    "body": (
                        "复杂咨询转人工时若上下文丢失，客户需要重复描述问题，"
                        "人工处理效率也会下降。"
                    ),
                },
                {
                    "kicker": "02",
                    "title": "预期收益",
                    "body": (
                        "客户在控制人工资源投入的同时，"
                        "提升复杂问题处理效率和服务连续性。"
                    ),
                },
                {
                    "kicker": "03",
                    "title": "风险控制",
                    "body": (
                        "客户可在稳妥前提下推进智能客服升级，"
                        "降低技术、业务和运营风险。"
                    ),
                },
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_truth_validator_accepts_user_backed_case_coverage_paraphrase(
    tmp_path: Path,
) -> None:
    source_fact = "已有案例覆盖：电商、零售、教育。"
    source_text = (
        "请生成客户评标 PPT。"
        f"{source_fact}"
        "案例页的真实客户名称和关键数字未提供时使用待补充占位。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    patch_path = tmp_path / "deck.patch.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--fact",
        source_fact,
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "eyebrow": "行业案例",
                            "title": "电商、零售、教育行业案例",
                            "subtitle": (
                                "AA 科技已有电商、零售和教育行业覆盖场景材料，"
                                "可为本项目沉淀可复用的实施方法；"
                                "具体客户名称与量化成果待授权补充。"
                            ),
                            "columns": ["行业案例", "成果说明", "关键数字"],
                            "rows": [
                                ["电商", "场景说明待补充", "待补充"],
                                ["零售", "场景说明待补充", "待补充"],
                                ["教育", "场景说明待补充", "待补充"],
                            ],
                            "source": source_fact,
                            "variant": "ledger",
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    applied = _run(
        "apply_deck_patch.js",
        str(deck_path),
        str(patch_path),
        env=env,
    )

    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert "覆盖场景材料" in deck["slides"][0]["props"]["subtitle"]
    truth = _run("validate_deck_truth.js", str(deck_path), env=env)
    assert truth.returncode == 0, truth.stdout + truth.stderr


def test_truth_validator_warns_for_extra_observed_result_in_coverage_paraphrase(
    tmp_path: Path,
) -> None:
    source_fact = "已有案例覆盖：电商、零售、教育。"
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--fact",
        source_fact,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["subtitle"] = (
        "AA 科技已有电商、零售和教育行业覆盖，并已实现客户成本降低。"
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    warnings = json.loads(
        result.stdout.split("\nDeck truth validation:", 1)[0]
    )["warnings"]
    assert any("performance/award/publication claim" in warning for warning in warnings)


def test_truth_validator_warns_but_allows_generic_ranking_wording(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "--research-fact",
        "信息噪音持续增加，用户需要更稳定的节奏感。",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "statement": "通知、排名、评价不断刷新",
            "support": "信息流持续叠加，注意力被频繁打断。",
            "proofs": [],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["ok"] is True


def test_truth_validator_accepts_dotted_date_and_transition_wording(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "kpi-grid-v1",
        "--research-fact",
        "拉明·亚马尔出生于2007年7月13日。",
        "--research-fact",
        "2024/25赛季55次出场、18球、21助攻。",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "title": "拉明·亚马尔",
            "subtitle": "出生于2007年7月13日",
            "marker": "2007.7.13",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "title": "成长与俱乐部表现",
            "subtitle": "早熟纪录已经转化为巴萨可见产出。",
            "items": [
                {
                    "label": "赛季出场",
                    "value": "55",
                    "detail": "2024/25赛季55次出场、18球、21助攻。",
                    "delta": "",
                },
                {
                    "label": "赛季进球",
                    "value": "18",
                    "detail": "2024/25赛季55次出场、18球、21助攻。",
                    "delta": "",
                },
                {
                    "label": "赛季助攻",
                    "value": "21",
                    "detail": "2024/25赛季55次出场、18球、21助攻。",
                    "delta": "",
                },
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_truth_validator_accepts_pitch_assumptions_and_capability_sections(
    tmp_path: Path,
) -> None:
    source_text = (
        "产品为面向中小制造工厂的 AI 质检 + 智能排产平台。"
        "已有 30 家试点客户。当前年化收入 800 万元。"
        "未提供的增长曲线和竞争评分可以使用合理假设数据，"
        "但必须标明示意 / 假设。团队姓名未提供，不要虚构个人信息。"
    )
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "table-data-v1",
        "chart-data-v1",
        "cards-grid-v1",
        "--fact",
        "产品为面向中小制造工厂的 AI 质检 + 智能排产平台",
        "--fact",
        "已有 30 家试点客户",
        "--fact",
        "当前年化收入 800 万元",
        "--assumption",
        "示意 / 假设：增长曲线使用 100、400、800 万元和 10、20、30 家；竞争评分仅用于表达产品定位。",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "商业模式｜先切刚需，再扩平台",
            "title": "订阅、模型与交付服务形成组合收入",
            "subtitle": "客户按产线与模块付费，具体合同数据待补充。",
            "items": [
                {"kicker": "订阅", "title": "软件订阅", "body": "按年使用平台。"},
                {"kicker": "模型", "title": "模型包", "body": "按场景配置。"},
                {"kicker": "服务", "title": "交付服务", "body": "按项目实施。"},
            ],
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "竞争格局｜示意 / 假设",
            "title": "竞争维度对比",
            "subtitle": "示意 / 假设：评分只表达产品定位，不代表公开排名。",
            "columns": ["维度", "本项目", "传统软件"],
            "rows": [["AI 质检", "强", "弱"], ["智能排产", "强", "中"]],
            "source": "示意 / 假设：待真实竞品调研校准。",
        }
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "业务进展",
            "title": "试点客户与年化收入",
            "subtitle": "增长曲线为示意 / 假设；当前锚点来自用户事实。",
            "chart_type": "line",
            "categories": ["早期", "中期", "当前"],
            "series": [
                {"name": "年化收入（万元，假设）", "values": ["100", "400", "800"]},
                {"name": "试点客户数（家，假设）", "values": ["10", "20", "30"]},
            ],
            "insight": "示意 / 假设：增长路径用于路演表达。",
            "source": "事实：已有 30 家试点客户；当前年化收入 800 万元。",
        }
    )
    deck["slides"][3]["props"].update(
        {
            "eyebrow": "团队｜复合能力结构",
            "title": "团队",
            "subtitle": "团队姓名与履历未提供，以下只表达能力结构，不虚构个人信息。",
            "items": [
                {"kicker": "AI", "title": "算法与工程化", "body": "能力方向：视觉识别与稳定部署。"},
                {"kicker": "制造", "title": "工艺与现场理解", "body": "能力方向：产线与排产工作流。"},
                {"kicker": "SaaS", "title": "企业服务产品化", "body": "能力方向：平台模块化。"},
                {"kicker": "GTM", "title": "销售与交付体系", "body": "能力方向：试点与交付。"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr


def test_truth_validator_accepts_source_facts_and_honest_placeholders(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "kpi-grid-v1",
        "project-case-study-v1",
        "--fact",
        "NOON Studio",
        "--fact",
        "2026 年是第三年",
        "--fact",
        "今年交付 28 个项目",
        "--fact",
        "业务是品牌视觉 + 数字产品设计",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "NOON Studio",
            "title": "2026 YEAR IN REVIEW",
            "subtitle": "第三年",
            "meta": "28 PROJECTS",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "statement": "第三年，继续把设计做深。",
            "support": "品牌视觉 × 数字产品设计",
            "proofs": [],
        }
    )
    deck["slides"][2]["props"]["items"] = [
        {"label": "PROJECTS", "value": "28", "detail": "全年交付", "delta": ""},
        {"label": "CLIENTS", "value": "待补充", "detail": "客户数", "delta": ""},
        {"label": "AWARDS", "value": "待补充", "detail": "获奖数", "delta": ""},
        {"label": "TEAM", "value": "待补充", "detail": "团队人数", "delta": ""},
    ]
    deck["slides"][3]["props"].update(
        {
            "title": "品牌项目 A（待补充）",
            "positioning": "品牌视觉与数字产品的协同设计。",
            "metrics": [
                {"value": "待补充", "label": "项目指标"},
                {"value": "待补充", "label": "项目结果"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "qa" / "truth_check.json"

    result = _run(
        "validate_deck_truth.js",
        str(deck_path),
        "--report",
        str(report),
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(report.read_text())["ok"] is True


def test_truth_validator_allows_non_project_story_in_case_study_visual_layout(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "project-case-study-v1",
        "--research-fact",
        "FIFA 公开资料记录巴西在 1958、1962、1970 年获得世界杯冠军。",
        "--research-fact",
        "贝利是巴西黄金时代的代表人物。",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "黄金时代",
            "title": "黄金时代：贝利与三次世界杯冠军",
            "positioning": "1958、1962、1970 三次夺冠将巴西足球推向黄金时代。",
            "metrics": [
                {"value": "1958", "label": "首次登顶"},
                {"value": "1962", "label": "成功卫冕"},
                {"value": "1970", "label": "黄金高峰"},
            ],
            "caption": "贝利是巴西黄金时代的代表人物。",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr


def test_truth_validator_warns_for_unbacked_real_project_name(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "project-case-study-v1",
        "--fact",
        "NOON Studio",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "项目案例",
            "title": "品牌项目 Alpha",
            "positioning": "NOON Studio",
            "metrics": [
                {"value": "待补充", "label": "业务结果"},
                {"value": "待补充", "label": "设计影响"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert any(
        "project name is not source-backed" in warning
        for warning in payload["warnings"]
    )


def test_truth_validator_accepts_chinese_quantity_and_section_marker_number(
    tmp_path: Path,
) -> None:
    source_text = "制作一个关于巴西足球历史的 PPT"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        source_text.encode("utf-8")
    ).decode("ascii")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "kpi-grid-v1",
        "section-marker-v1",
        "--research-fact",
        "巴西男足国家队五次获得 FIFA World Cup 冠军。",
        "--out",
        str(deck_path),
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "title": "世界杯冠军",
            "items": [
                {
                    "label": "冠军次数",
                    "value": "5",
                    "detail": "巴西男足国家队五次获得 FIFA World Cup 冠军。",
                    "delta": "",
                },
                {
                    "label": "待补充",
                    "value": "待补充",
                    "detail": "待补充",
                    "delta": "",
                },
                {
                    "label": "待补充",
                    "value": "待补充",
                    "detail": "待补充",
                    "delta": "",
                },
            ],
        }
    )
    deck["slides"][1]["props"].update(
        {
            "number": "06",
            "eyebrow": "SECTION",
            "title": "巴西足球历史",
            "subtitle": "",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["ok"] is True


def test_truth_validator_accepts_cover_slide_count_metadata(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--fact",
        "历史梳理",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"]["meta"] = "2 页历史梳理 · HTML 交付"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_truth.js", str(deck_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert payload["ok"] is True


def test_controlled_deck_scripts_resolve_relative_paths_from_canonical_output_root(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "session" / "output"
    wrong_cwd = tmp_path / "session"
    canonical.mkdir(parents=True)
    env = os.environ.copy()
    env["BOX_AGENT_OUTPUT_DIR"] = str(canonical)

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "statement-focus-v1",
        "--theme",
        "block-frame",
        "--title",
        "Canonical root",
        "--out",
        "deck.json",
        cwd=wrong_cwd,
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stderr
    assert (canonical / "deck.json").is_file()
    assert (canonical / "assets/generated/manifest.json").is_file()
    assert (canonical / "qa/deck_contract.json").is_file()
    assert not (wrong_cwd / "deck.json").exists()

    validation = _run(
        "validate_deck_spec.js",
        "deck.json",
        "--report",
        "qa/deck_spec.json",
        cwd=wrong_cwd,
        env=env,
    )
    rendered = _run(
        "render_deck_html.js",
        "deck.json",
        "--out",
        "index.html",
        cwd=wrong_cwd,
        env=env,
    )

    assert validation.returncode == 0, validation.stderr
    assert rendered.returncode == 0, rendered.stderr
    assert json.loads((canonical / "qa/deck_spec.json").read_text())["ok"] is True
    assert (canonical / "index.html").is_file()
    assert not (wrong_cwd / "index.html").exists()


def test_pptx_theme_selection_has_no_hard_html_templates_dependency() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert "html-templates" not in (frontmatter.get("required_skills") or [])
    assert frontmatter["related_skills"] == ["html-templates"]
    assert "Source-bound decks never invent named clients" in text
    assert "复购率持续提升" in text
    assert "Never create a fake bitmap with Pillow" in text
    assert "--out deck.json" in text
    assert "--require-field" in text
    assert "Do not convert visual styling language" in text
    assert "not `--require-field 1:tags`" in text
    assert "BOX_AGENT_OUTPUT_DIR" in text
    assert 'write_file(path="deck.json", ...)' in text
    assert "required `image_plan` key" in text
    assert "apply_deck_patch.js" in text
    assert "${BOX_AGENT_NODE:-node} scripts/apply_deck_patch.js" in text
    assert "validate_deck_truth.js" in text
    assert "validate_outline.js --research-handoff" in text
    assert "verified_facts[].canonical" in text
    assert "Conflicting, unverified, cross-entity" in text
    assert "`comic-panel`" in text
    assert "`8-bit-orbit`" in text
    assert "DiagramSpec SVG clean and professional" in text


def test_pptx_skill_exposes_platform_specific_standalone_image_sync() -> None:
    skill = SkillLoader(sources=[(SKILL_DIR, "builtin")]).load_skill(
        SKILL_DIR / "SKILL.md"
    )

    assert skill is not None
    sync_script = str(SCRIPTS_DIR / "sync_image_manifest_status.js")
    assert sync_script in skill.content
    assert '"$BOX_AGENT_NODE" "<LOADER_EXPANDED_SYNC_SCRIPT>"' in skill.content
    assert '& "$env:BOX_AGENT_NODE" "<LOADER_EXPANDED_SYNC_SCRIPT>"' in skill.content
    assert "already runs with `BOX_AGENT_OUTPUT_DIR` as its cwd" in skill.content
    assert "synchronization invocation must be" in skill.content
    assert "the only command" in skill.content


def test_pptx_solution_briefs_do_not_default_to_deep_research() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    outline = (SKILL_DIR / "references" / "outline.md").read_text(encoding="utf-8")

    assert "Solution/design brief?" in text
    assert "do not load `research-synthesis`" in text
    assert "use at most two targeted" in text
    assert "official-source lookups" in text
    assert "The request is a solution/design brief" in outline
    assert "a concise proposal brief stays in branch 2" in outline


def test_pptx_missing_facts_use_placeholders_without_pausing() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "Do not use `request_user_input` for a missing fact" in text
    assert "Missing case metrics, quote amounts, contact names" in text
    assert "are never pre-delivery blockers" in text
    assert "`暂无可验证公开数据`" in text
    assert "The user's next reply resumes this same deck" in text
    assert "Source/URL/private-fact findings never trigger an automatic repair loop" in text


def test_pptx_framing_choices_use_recommended_decision_with_countdown() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    outline = (SKILL_DIR / "references" / "outline.md").read_text(encoding="utf-8")

    assert "never request them with `request_user_input`" in text
    assert "request `requested_auto_submit_seconds: 30`" in text
    assert 'declare `risk_level: "low"`' in text
    assert "never use `request_user_input` for\n   them" in outline
    assert "default with a 30-second timeout" in outline


def test_outline_validator_writes_report_and_flags_reused_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    report_path = tmp_path / "qa" / "outline_check.json"
    shared_evidence = (
        "巴西男足国家队五次获得 FIFA World Cup 冠军。 | FIFA | "
        "https://www.fifa.com/example"
    )
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "解释巴西足球历史的关键阶段",
                "audience": "普通体育观众",
                "source_mode": "public_authoritative_research",
                "storyline": "从组织起点到冠军时代，再总结其全球影响。",
                "slides": [
                    {
                        "page": index,
                        "title": f"阶段 {label}",
                        "message": f"第{label}页承担不同的历史叙事任务",
                        "bullets": ["事实线索一", "事实线索二"],
                        "layout": "timeline",
                        "visual": "时间线",
                        "evidence": [shared_evidence],
                        "notes": "",
                    }
                    for index, label in enumerate(("一", "二", "三"), start=1)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--report",
        str(report_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert any("evidence is reused across 3 slides" in item for item in report["warnings"])


def test_outline_research_handoff_accepts_only_verified_canonical_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    canonical = (
        "Example Corp | Example Corp launched Product One in 2026. | "
        "first_party | https://example.com/news/product-one"
    )
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0].update(
        {
            "title": "Example Corp 产品进展",
            "message": "Example Corp 在 2026 年推出 Product One。",
            "bullets": ["产品发布得到官方页面支持", "结论绑定第一方来源"],
            "evidence": [canonical],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "ok": True,
                "validator": "research-synthesis",
                "evidence_schema_version": 1,
                "verified_evidence": [
                    {
                        "entity": "Example Corp",
                        "claim": "Example Corp launched Product One in 2026.",
                        "source_url": "https://example.com/news/product-one",
                        "source_type": "first_party",
                        "status": "verified",
                        "canonical": canonical,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    accepted = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-report",
        str(research_report),
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    outline["slides"][0]["evidence"] = [
        "Example Corp | Unverified claim | secondary | https://other.example/item"
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    rejected = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-report",
        str(research_report),
    )

    assert rejected.returncode == 1
    payload = json.loads(rejected.stdout)
    assert any(
        "is not an exact canonical item" in issue for issue in payload["issues"]
    )


def test_outline_accepts_partial_research_handoff_verified_subset(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    canonical = (
        "Example Corp | Example Corp launched Product One in 2026. | "
        "first_party | https://example.com/news/product-one"
    )
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0].update(
        {
            "title": "Example Corp 产品进展",
            "message": "Example Corp 在 2026 年推出 Product One。",
            "bullets": ["产品发布得到官方页面支持", "其余市场数据暂无可验证公开数据"],
            "evidence": [canonical],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "presentation_handoff": {
                    "schema_version": 1,
                    "delivery_mode": "partial",
                    "verified_facts": [
                        {
                            "entity": "Example Corp",
                            "claim": "Example Corp launched Product One in 2026.",
                            "source_url": "https://example.com/news/product-one",
                            "source_type": "first_party",
                            "canonical": canonical,
                        }
                    ],
                    "gaps": ["Market share remains unverified"],
                    "quality_summary": {"quality_ok": False},
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-handoff",
        str(research_report),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_outline_accepts_framework_research_handoff_with_explicit_gap(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0].update(
        {
            "title": "市场数据框架",
            "message": "当前暂无可验证公开数据，保留后续补充位置。",
            "bullets": ["市场规模待补充", "竞争格局待补充"],
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "presentation_handoff": {
                    "schema_version": 1,
                    "delivery_mode": "framework",
                    "verified_facts": [],
                    "gaps": ["No verified public facts"],
                    "quality_summary": {"quality_ok": False},
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-handoff",
        str(research_report),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_outline_rejects_relabeling_framework_research_as_user_provided(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "市场数据框架",
            "message": "当前暂无可验证公开数据，保留后续补充位置。",
            "bullets": ["市场规模待补充", "竞争格局待补充"],
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    research_status = tmp_path / "research_status.json"
    research_status.write_text(
        json.dumps(
            {
                "presentation_handoff": {
                    "schema_version": 1,
                    "delivery_mode": "framework",
                    "verified_facts": [],
                    "gaps": ["No verified public facts"],
                    "quality_summary": {"quality_ok": False},
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-handoff",
        str(research_status),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "source_mode must remain public_authoritative_research" in issue
        for issue in payload["issues"]
    )


def test_outline_accepts_framework_structural_pages_without_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=3)
    outline["slides"][0].update(
        {
            "title": "新能源汽车市场动态分析",
            "message": "建立面向管理层的市场观察框架。",
            "layout": "cover",
            "visual": "咨询风封面",
            "evidence": [],
        }
    )
    outline["slides"][1].update(
        {
            "title": "分析框架与数据口径",
            "message": "从市场、品牌和区域三个维度展开。",
            "layout": "agenda",
            "visual": "目录列表",
            "evidence": [],
        }
    )
    outline["slides"][2].update(
        {
            "title": "市场规模",
            "message": "当前暂无可验证公开数据。",
            "bullets": ["销量规模待核验", "市场渗透率待核验"],
            "layout": "kpi-grid",
            "visual": "KPI 指标卡",
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "presentation_handoff": {
                    "schema_version": 1,
                    "delivery_mode": "framework",
                    "verified_facts": [],
                    "gaps": ["No verified public facts"],
                    "quality_summary": {"quality_ok": False},
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "3",
        "--max-slides",
        "3",
        "--research-handoff",
        str(research_report),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert sum(
        "structural public-research page has no factual evidence" in warning
        for warning in payload["warnings"]
    ) == 2


@pytest.mark.parametrize(
    ("title", "message", "bullets", "notes"),
    [
        (
            "市场规模",
            "展示新能源汽车销量与渗透率。",
            ["梳理市场趋势", "比较品牌表现"],
            "",
        ),
        (
            "市场规模",
            "市场规模待补充。",
            ["销量待补充", "渗透率待补充"],
            "",
        ),
        (
            "暂无可验证公开数据",
            "展示新能源汽车销量与渗透率。",
            ["梳理市场趋势", "比较品牌表现"],
            "",
        ),
        (
            "市场规模",
            "展示新能源汽车销量与渗透率。",
            ["梳理市场趋势", "比较品牌表现"],
            "暂无可验证公开数据",
        ),
    ],
)
def test_outline_framework_data_page_still_requires_exact_gap_or_evidence(
    tmp_path: Path,
    title: str,
    message: str,
    bullets: list[str],
    notes: str,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=2)
    outline["slides"][0].update(
        {"layout": "cover", "visual": "封面", "evidence": []}
    )
    outline["slides"][1].update(
        {
            "title": title,
            "message": message,
            "bullets": bullets,
            "notes": notes,
            "layout": "kpi-grid",
            "visual": "KPI 指标卡",
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "presentation_handoff": {
                    "schema_version": 1,
                    "delivery_mode": "framework",
                    "verified_facts": [],
                    "gaps": ["No verified public facts"],
                    "quality_summary": {"quality_ok": False},
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "2",
        "--max-slides",
        "2",
        "--research-handoff",
        str(research_report),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        issue.startswith("slide-02: framework research page")
        for issue in payload["issues"]
    )


def test_outline_research_handoff_rejects_cross_entity_binding(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    canonical = (
        "Example Corp | Example Corp launched Product One in 2026. | "
        "first_party | https://example.com/news/product-one"
    )
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0].update(
        {
            "title": "Another Company 产品进展",
            "message": "Another Company 在 2026 年推出 Product One。",
            "bullets": ["产品发布得到页面支持", "结论绑定来源"],
            "evidence": [canonical],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    research_report = tmp_path / "research_check.json"
    research_report.write_text(
        json.dumps(
            {
                "ok": True,
                "validator": "research-synthesis",
                "evidence_schema_version": 1,
                "verified_evidence": [
                    {
                        "entity": "Example Corp",
                        "claim": "Example Corp launched Product One in 2026.",
                        "source_url": "https://example.com/news/product-one",
                        "source_type": "first_party",
                        "status": "verified",
                        "canonical": canonical,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
        "--research-report",
        str(research_report),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "slide narrative does not name that entity" in issue
        for issue in payload["issues"]
    )


def test_outline_array_audience_and_storyline_pass_validation_and_scaffold(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["audience"] = ["采购负责人", "IT 负责人", "业务负责人"]
    outline["storyline"] = [
        "先说明客户需求与评审重点。",
        "再展开解决方案、系统集成与实施计划。",
        "最后以客户价值和下一步收束。",
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    validation = _run(
        "validate_outline.js",
        str(outline_path),
        "--report",
        str(tmp_path / "qa" / "outline_check.json"),
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert json.loads(validation.stdout)["ok"] is True

    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "cards-grid-v1",
        "closing-next-steps-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--out",
        str(tmp_path / "deck.json"),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    assert (tmp_path / "deck.json").is_file()
    assert (tmp_path / "assets" / "generated" / "manifest.json").is_file()


def test_public_research_outline_warns_for_numeric_claims_missing_from_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "解释巴西足球历史的关键阶段",
                "audience": "普通体育观众",
                "source_mode": "public_authoritative_research",
                "storyline": "从早期参赛到首次夺冠。",
                "slides": [
                    {
                        "page": 1,
                        "title": "1930 年开启国际参赛史",
                        "message": "巴西持续积累国际大赛经验。",
                        "bullets": ["逐步形成技术风格", "最终进入冠军行列"],
                        "layout": "timeline",
                        "visual": "时间线",
                        "evidence": [
                            "公开资料确认 1958 年首次夺冠。 | FIFA | "
                            "https://www.fifa.com/example"
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert any(
        'title numeric literal "1930" is not present in this page\'s evidence'
        in warning
        for warning in payload["warnings"]
    )


def test_public_research_outline_ignores_campaign_name_digits(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["source_mode"] = "public_authoritative_research"
    outline["slides"][0].update(
        {
            "title": "双11全渠道营销策划",
            "message": "围绕双11建立跨渠道协同机制。",
            "bullets": ["双11主会场与会员运营联动", "保持统一活动节奏"],
            "evidence": [],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert not any(
        'numeric literal "11"' in warning for warning in payload["warnings"]
    )


def test_public_research_outline_warns_when_actual_source_url_is_missing(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0]["evidence"] = [
        "FIFA/权威检索线索确认：巴西曾获得世界杯冠军。"
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert any(
        "must include the actual http(s) source URL" in warning
        for warning in payload["warnings"]
    )


def test_outline_rejects_evidence_too_long_for_deck_truth_contract(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0]["evidence"] = [
        f"{'已核实的公开事实。' * 32} | Example | https://example.com/source-a"
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "evidence.0 exceeds 280 characters" in issue
        and "split it into separate evidence items" in issue
        for issue in payload["issues"]
    )


def test_public_research_outline_warns_when_slide_has_no_evidence(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0]["evidence"] = []
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert any(
        "requires at least one claim | source | http(s) URL evidence item"
        in warning
        for warning in payload["warnings"]
    )


def test_public_research_outline_allows_required_unavailable_fact_placeholder(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1)
    outline["slides"][0].update(
        {
            "title": "市场规模口径",
            "message": "暂无可验证公开数据",
            "bullets": ["公开来源未披露统一口径", "交付后可替换为客户确认数据"],
            "evidence": [],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run(
        "validate_outline.js",
        str(outline_path),
        "--min-slides",
        "1",
        "--max-slides",
        "1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert any(
        "marks a required fact as unavailable" in warning
        for warning in payload["warnings"]
    )


def test_outline_warns_for_assumed_private_financing_stage(tmp_path: Path) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["slides"][0]["evidence"] = [
        "用户提供：计划融资 3000 万元。",
        "假设：融资阶段按早期成长轮 / Pre-A 轮示意表达。",
    ]
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert any(
        "assumes a private identity fact" in warning
        and "financing round" in warning
        for warning in payload["warnings"]
    )


def test_outline_numbered_action_directive_is_not_counted_as_a_sixth_item(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=3, source_mode="user_provided")
    outline["slides"][2].update(
        {
            "title": "五步行动清单",
            "message": "用五步完成结构化表达。",
            "layout": "horizontal process",
            "visual": "可编辑横向行动清单，5 个等权步骤从左到右排列。",
            "bullets": [
                "从左到右完整展示 5 项，不得删减或压缩。",
                "1.先写结论。",
                "2.拆分问题。",
                "3.检查MECE。",
                "4.组织汇报。",
                "5.每周复盘。",
            ],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert not any("trim to 5 or fewer" in warning for warning in payload["warnings"])


def test_outline_data_visual_detection_accepts_cover_and_named_charts(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    slides = [
        {
            "page": 1,
            "title": "AI 制造平台融资路演",
            "message": "本轮计划融资 3000 万元。",
            "bullets": ["产品定位", "融资诉求"],
            "layout": "封面页",
            "visual": "科技制造风背景与融资金额视觉焦点",
            "evidence": ["用户提供：融资 3000 万元。"],
        },
        {
            "page": 2,
            "title": "市场规模",
            "message": "TAM、SAM、SOM 展示分层市场空间。",
            "bullets": ["TAM", "SAM", "SOM"],
            "layout": "市场规模页",
            "visual": "市场规模图，展示 TAM、SAM、SOM",
            "evidence": ["示意：TAM、SAM、SOM。"],
        },
        {
            "page": 3,
            "title": "商业模式",
            "message": "软件订阅收入与交付服务构成收入组合。",
            "bullets": ["订阅", "部署", "服务"],
            "layout": "商业模式页",
            "visual": "三段式收入结构图与客户扩展阶梯图",
            "evidence": ["用户要求：说明收入来源。"],
        },
        {
            "page": 4,
            "title": "业务增长",
            "message": "当前 ARR 达到 800 万元。",
            "bullets": ["试点", "商业化", "扩张"],
            "layout": "业务进展页",
            "visual": "ARR 增长曲线",
            "evidence": ["用户提供：ARR 800 万元。"],
        },
        {
            "page": 5,
            "title": "融资计划",
            "message": "融资 3000 万元用于三类投入。",
            "bullets": ["研发", "销售", "交付"],
            "layout": "融资计划页",
            "visual": "资金用途图，使用环形图展示比例",
            "evidence": ["用户提供：融资 3000 万元。"],
        },
    ]
    outline_path.write_text(
        json.dumps(
            {
                "deck_goal": "完成融资沟通",
                "audience": "投资人",
                "source_mode": "user_provided",
                "storyline": "从项目定位进入市场、商业模式与进展，最后提出融资计划。",
                "slides": slides,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert not any(
        "appears data-heavy but visual does not name" in warning
        for warning in payload["warnings"]
    )


def test_outline_requires_a_data_visual_for_multiple_real_values(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["slides"][1].update(
        {
            "title": "业务增长",
            "message": "ARR 从 500 万元增长到 800 万元。",
            "bullets": ["上期 ARR 500 万元", "本期 ARR 800 万元"],
            "layout": "业务进展页",
            "visual": "三张摘要信息卡",
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "appears data-heavy but visual does not name" in issue
        for issue in payload["issues"]
    )


def test_outline_repeated_single_metric_remains_a_warning(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["slides"][1].update(
        {
            "title": "业务进展",
            "message": "当前 ARR 达到 800 万元。",
            "bullets": ["本期 ARR 800 万元", "保持增长节奏"],
            "layout": "业务进展页",
            "visual": "三张摘要信息卡",
            "evidence": ["用户提供：ARR 800 万元。"],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert not any(
        "appears data-heavy but visual does not name" in issue
        for issue in payload["issues"]
    )
    assert any(
        "appears data-heavy but visual does not name" in warning
        for warning in payload["warnings"]
    )


def test_outline_detects_two_values_when_data_terms_are_only_in_bullets(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=3,
        source_mode="user_provided",
    )
    outline["slides"][1].update(
        {
            "title": "最新进展",
            "message": "本期表现较上期明显改善。",
            "bullets": ["ARR 从 500 万元提升", "本期收入达到 800 万元"],
            "layout": "业务回顾",
            "visual": "三张摘要信息卡",
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_outline.js", str(outline_path))

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any(
        "appears data-heavy but visual does not name" in issue
        for issue in payload["issues"]
    )


def test_controlled_finalizer_stops_at_first_failed_dependency(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    deck_path.write_text('{"slides": []}', encoding="utf-8")
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"mode":"auto","image_plan":[]}', encoding="utf-8")

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(tmp_path / "index.html"),
    )

    assert result.returncode == 1
    assert "FINALIZE_STOP stage=deck_spec" in result.stderr
    assert json.loads((tmp_path / "qa" / "deck_spec.json").read_text())["ok"] is False
    assert not (tmp_path / "qa" / "truth_check.json").exists()
    assert not (tmp_path / "index.html").exists()


def test_truth_validator_keeps_invalid_deck_structure_blocking(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    deck_path.write_text('{"slides": []}', encoding="utf-8")
    report_path = tmp_path / "qa" / "truth_check.json"

    result = _run(
        "validate_deck_truth.js",
        str(deck_path),
        "--report",
        str(report_path),
    )

    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert any(issue.startswith("deck-spec:") for issue in report["issues"])


@pytest.mark.parametrize("custom_manifest", [False, True])
@pytest.mark.parametrize("browser_check", [False, True])
def test_finalizer_binds_images_generated_after_content_patch(
    tmp_path: Path, custom_manifest: bool, browser_check: bool,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js", "cover-editorial-v1", "project-case-study-v1",
        "--theme", "block-frame", "--out", str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text('{"slides":{}}', encoding="utf-8")
    patched = _run("apply_deck_patch.js", str(deck_path), str(patch_path))
    assert patched.returncode == 0, patched.stdout + patched.stderr

    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    entries = []
    for number, prop in [(1, "background"), (2, "image")]:
        asset = f"assets/generated/slide-{number:02d}-{prop}.png"
        (tmp_path / asset).write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        entries.append({
            "slide": number, "slide_id": f"slide-{number:02d}",
            "prop_path": prop, "required": True, "decision": "generate",
            "acquire_via": "ai", "status": "pending", "output_path": asset,
        })
    # Binding must use slide_id/prop_path, independent of manifest row order.
    manifest_path.write_text(json.dumps({
        "schema_version": 1, "mode": "auto", "image_plan": entries[::-1],
    }), encoding="utf-8")
    synced = _run("sync_image_manifest_status.js", str(manifest_path))
    assert synced.returncode == 0, synced.stdout + synced.stderr
    options = []
    if custom_manifest:
        alternate = tmp_path / "alternate-images.json"
        manifest_path.rename(alternate)
        options = ["--manifest", str(alternate)]
    html_path = tmp_path / "index.html"
    result = _run("finalize_controlled_deck.js", str(deck_path), "--out", str(html_path), *options)
    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["background"]["src"] == entries[0]["output_path"]
    assert "background" not in deck["slides"][0]["props"]
    assert deck["slides"][1]["props"]["image"]["src"] == entries[1]["output_path"]
    assert deck["slides"][1]["props"]["image"]["origin"] == "generated"
    report = json.loads((tmp_path / "qa" / "image_manifest.json").read_text())
    assert not any("not referenced" in w for w in report["warnings"])
    html_report = json.loads((tmp_path / "qa" / "html_self_check.json").read_text())
    assert len(json.loads(result.stdout.splitlines()[-1])["media_bindings"]) == 2
    saved_deck = deck_path.read_bytes()
    repeated = _run("finalize_controlled_deck.js", str(deck_path), "--out", str(html_path), *options)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout.splitlines()[-1])["media_bindings"] == []
    assert deck_path.read_bytes() == saved_deck

    if browser_check:
        # Finalization can succeed with an advisory report when the managed
        # browser is absent. Probe directly so _run skips only browser cases.
        report_path = tmp_path / "qa" / "browser_image_check.json"
        checked = _run(
            "html_self_check.js", str(html_path), "--dom-to-pptx",
            "--allow-local-images", "--report", str(report_path),
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert html_report["projectCaseStyles"][0]["imageLoaded"] is True
        assert html_report["projectCaseStyles"][0]["imagePlaceholder"] is False


@pytest.mark.parametrize("asset_state", ["selected", "failed", "missing", "malformed"])
def test_finalizer_preserves_chosen_media_and_delivers_with_unavailable_assets(
    tmp_path: Path, asset_state: str,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run("inspect_deck_contract.js", "project-case-study-v1", "--out", str(deck_path))
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text())
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    asset = "assets/generated/project.png"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    if asset_state != "missing":
        (tmp_path / asset).write_bytes(png)
    if asset_state == "selected":
        (tmp_path / "chosen.png").write_bytes(png)
        deck["slides"][0]["props"]["image"] = {
            "src": "chosen.png", "alt": "My chosen visual", "fit": "contain",
            "position": "left", "origin": "asset",
        }
    deck_path.write_text(json.dumps(deck), encoding="utf-8")
    before = deck_path.read_bytes()
    manifest_path.write_text(json.dumps({
        "mode": "auto", "image_plan": [{
            "slide": 1, "slide_id": "slide-01", "prop_path": "image",
            "required": True, "decision": "generate", "acquire_via": "ai",
            "status": "failed" if asset_state == "failed" else "generated",
            "output_path": asset,
        }],
    }) if asset_state != "malformed" else "{", encoding="utf-8")
    result = _run("finalize_controlled_deck.js", str(deck_path), "--out", str(tmp_path / "index.html"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "index.html").is_file()
    assert deck_path.read_bytes() == before
    assert json.loads(result.stdout.splitlines()[-1])["media_bindings"] == []


def test_controlled_finalizer_runs_compact_complete_chain(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["truth_contract"] = {
        "mode": "source_bound",
        "source_facts": [],
        "research_facts": [],
        "assumptions": [],
    }
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "auto",
                "image_plan": [
                    {
                        "slide": index,
                        "slide_id": slide["id"],
                        "layout_id": slide["layout_id"],
                        "required": False,
                        "decision": "skip",
                        "status": "skipped",
                        "output_path": None,
                    }
                    for index, slide in enumerate(deck["slides"], start=1)
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(tmp_path / "index.html"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FINALIZE_ADVISORY stage=image_manifest warnings=0" in result.stdout
    assert "FINALIZE_ADVISORY stage=truth" in result.stdout
    stage_markers = [
        next(
            marker
            for marker in (
                f"FINALIZE_PASS stage={stage}",
                f"FINALIZE_ADVISORY stage={stage}",
            )
            if marker in result.stdout
        )
        for stage in (
            "deck_spec",
            "deck_contract",
            "image_manifest",
            "render",
            "html_self_check",
            "truth",
            "runtime_probe",
        )
    ]
    assert [result.stdout.index(marker) for marker in stage_markers] == sorted(
        result.stdout.index(marker) for marker in stage_markers
    )
    assert '"ok":true' in result.stdout
    assert (tmp_path / "index.html").is_file()
    for report_name in (
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    ):
        report = json.loads((tmp_path / "qa" / report_name).read_text())
        assert report["ok"] is True
    contract_report = json.loads(
        (tmp_path / "qa" / "deck_contract.json").read_text(encoding="utf-8")
    )
    assert contract_report["refreshed_by"] == "finalize_controlled_deck"
    assert len(contract_report["deck_hash"]) == 64
    truth_report = json.loads((tmp_path / "qa" / "truth_check.json").read_text())
    assert truth_report["advisory"] is True
    assert truth_report["warnings"]


def test_controlled_finalizer_delivers_degraded_html_for_image_manifest_failure(
    tmp_path: Path,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "creative_image_mode",
                "image_plan": [
                    {
                        "slide": 1,
                        "slide_id": deck["slides"][0]["id"],
                        "layout_id": deck["slides"][0]["layout_id"],
                        "prop_path": "background",
                        "required": True,
                        "decision": "generate",
                        "status": "blocked",
                        "output_path": "assets/generated/missing-cover.png",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    html_path = tmp_path / "index.html"

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(html_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert html_path.is_file()
    assert "FINALIZE_ADVISORY stage=image_manifest" in result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert payload["delivery_status"] == "degraded"
    image_report = json.loads(
        (tmp_path / "qa" / "image_manifest.json").read_text(encoding="utf-8")
    )
    assert image_report["ok"] is True
    assert image_report["advisory"] is True
    assert image_report["issues"] == []
    assert image_report["warnings"]


@pytest.mark.parametrize("local_warning_only", [False, True])
def test_controlled_finalizer_delivers_degraded_html_for_runtime_probe_failure(
    tmp_path: Path, local_warning_only: bool,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "auto",
                "image_plan": [
                    {
                        "slide": index,
                        "slide_id": slide["id"],
                        "layout_id": slide["layout_id"],
                        "required": False,
                        "decision": "skip",
                        "status": "skipped",
                        "output_path": None,
                    }
                    for index, slide in enumerate(deck["slides"], start=1)
                ],
            }
        ),
        encoding="utf-8",
    )
    preload_path = tmp_path / "force-runtime-probe-failure.js"
    preload_path.write_text(
        """
const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
const originalSpawnSync = childProcess.spawnSync;
childProcess.spawnSync = function(command, args, options) {
  const script = Array.isArray(args) && args.length ? String(args[0]) : "";
  if (script.endsWith("probe_deck_runtime.js")) {
    const reportIndex = args.indexOf("--report");
    const reportPath = args[reportIndex + 1];
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    if (process.env.PPTX_TEST_LOCAL_WARNING === "1") {
      fs.writeFileSync(reportPath, JSON.stringify({
        ok: true, issues: [], warnings: ["Slide 1 local text contrast is 1.25:1"],
        editor: {componentContrast: {failures: [{slide: 1, ratio: 1.25}]}},
      }));
      return {status: 0, stdout: "", stderr: ""};
    }
    fs.writeFileSync(reportPath, JSON.stringify({
      ok: false,
      issues: [
        "Core deck colors collapse to one value",
        "Deck text/background contrast is too low: 1.00",
      ],
      warnings: [],
    }));
    return { status: 1, stdout: "", stderr: "forced runtime probe failure" };
  }
  return originalSpawnSync.call(this, command, args, options);
};
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["NODE_OPTIONS"] = f"--require={preload_path}"
    env["PPTX_TEST_LOCAL_WARNING"] = "1" if local_warning_only else "0"
    html_path = tmp_path / "index.html"

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(html_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert html_path.is_file()
    marker = ("FINALIZE_PASS stage=runtime_probe warnings=1" if local_warning_only
              else "FINALIZE_ADVISORY stage=runtime_probe warnings=2")
    assert marker in result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert payload["delivery_status"] == "degraded"
    assert payload["degraded_stages"][-1] == "runtime_probe"
    assert set(payload["degraded_stages"]).issubset(
        {"html_self_check", "runtime_probe"}
    )
    runtime_report = json.loads(
        (tmp_path / "qa" / "runtime_probe.json").read_text(encoding="utf-8")
    )
    assert runtime_report["ok"] is True
    if local_warning_only:
        assert runtime_report["issues"] == []
        assert runtime_report["warnings"] == ["Slide 1 local text contrast is 1.25:1"]
        return
    assert runtime_report["advisory"] is True
    assert runtime_report["degraded_reason"] == "runtime_probe"
    assert runtime_report["issues"] == []
    assert runtime_report["warnings"] == [
        "Core deck colors collapse to one value",
        "Deck text/background contrast is too low: 1.00",
    ]


def test_controlled_finalizer_delivers_degraded_html_for_outline_binding_drift(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "主题页",
            "message": "必须保留的原始核心信息",
            "bullets": ["必须保留的原始支持点"],
        }
    )
    outline_path.write_text(
        json.dumps(outline, ensure_ascii=False),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    patch_path = tmp_path / "deck.patch.json"
    patch_path.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "props": {
                            "title": "主题页",
                            "items": [
                                {
                                    "kicker": "A",
                                    "title": "重新表述",
                                    "body": "没有逐字复用大纲消息",
                                }
                            ],
                        }
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    applied = _run("apply_deck_patch.js", str(deck_path), str(patch_path))
    assert applied.returncode == 0, applied.stdout + applied.stderr
    contract_path = tmp_path / "qa" / "deck_contract.json"
    contract_mtime_before = contract_path.stat().st_mtime_ns
    html_path = tmp_path / "index.html"
    env = os.environ.copy()
    env["BOX_AGENT_ALLOW_DEGRADED_OUTLINE_BINDING"] = "1"

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(html_path),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert html_path.is_file()
    assert "FINALIZE_ADVISORY stage=deck_spec" in result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["delivery_status"] == "degraded"
    spec_report = json.loads(
        (tmp_path / "qa" / "deck_spec.json").read_text(encoding="utf-8")
    )
    assert spec_report["ok"] is True
    assert spec_report["advisory"] is True
    assert spec_report["degraded_reason"] == "outline_binding"
    assert spec_report["warnings"]
    contract_report = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract_path.stat().st_mtime_ns > contract_mtime_before
    assert contract_path.stat().st_mtime_ns >= deck_path.stat().st_mtime_ns
    assert contract_report["ok"] is True
    assert contract_report["refreshed_by"] == "finalize_controlled_deck"
    assert contract_report["outline_binding"]["ok"] is False
    assert contract_report["outline_binding"]["issues"]
    assert len(contract_report["deck_hash"]) == 64


def test_controlled_finalizer_blocks_outline_binding_drift_by_default(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0]["message"] = "必须保留的原始核心信息"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["items"][0]["body"] = "不匹配的新内容"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "finalize_controlled_deck.js",
        str(deck_path),
        "--out",
        str(tmp_path / "index.html"),
    )

    assert result.returncode != 0
    assert "FINALIZE_STOP stage=deck_spec" in result.stderr


def test_truth_validator_keeps_unapproved_illustrative_mode_blocking(
    tmp_path: Path,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["truth_contract"] = {
        "mode": "illustrative",
        "source_facts": [],
        "research_facts": [],
        "assumptions": [],
    }
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "只使用我提供的事实，禁止虚构或使用示意内容。".encode("utf-8")
    ).decode("ascii")

    result = _run("validate_deck_truth.js", str(deck_path), env=env)

    assert result.returncode == 1
    report = json.loads(result.stdout.split("\nDeck truth validation:", 1)[0])
    assert report["ok"] is False
    assert any(
        "has not explicitly permitted invented or illustrative content" in issue
        for issue in report["issues"]
    )


def test_example_validates_and_renders_deterministically(tmp_path: Path) -> None:
    report = tmp_path / "qa" / "deck_spec.json"
    validation = _run(
        "validate_deck_spec.js",
        str(EXAMPLE),
        "--report",
        str(report),
    )

    assert validation.returncode == 0, validation.stderr
    assert json.loads(report.read_text())["ok"] is True

    first = tmp_path / "first.html"
    second = tmp_path / "second.html"
    first_render = _run("render_deck_html.js", str(EXAMPLE), "--out", str(first))
    second_render = _run("render_deck_html.js", str(EXAMPLE), "--out", str(second))

    assert first_render.returncode == 0, first_render.stderr
    assert second_render.returncode == 0, second_render.stderr
    assert first.read_bytes() == second.read_bytes()

    self_check = _run(
        "html_self_check.js",
        str(first),
        "--dom-to-pptx",
        "--allow-local-images",
    )
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    self_check_payload = json.loads(
        self_check.stdout.split("\nHTML self-check:", 1)[0]
    )
    slack_warnings = [
        warning
        for warning in self_check_payload["warnings"]
        if "PowerPoint wrap slack" in warning
    ]
    assert slack_warnings == []

    html = first.read_text(encoding="utf-8")
    rendered_deck = html.split('<section class="deck-layout-picker"', 1)[0]
    assert rendered_deck.count('<section class="slide ') == 7
    assert 'data-layout-id="comparison-two-column-v1"' in html
    assert 'data-deck-composition="institutional-grid"' in html
    assert 'data-deck-composition-variant="ledger-grid"' in html
    assert '"design": {' in html
    assert 'id="deck-document"' in html
    assert 'data-action="save"' in html
    assert 'data-action="add-slide"' in html
    assert 'data-action="layout"' in html
    assert 'data-action="adjust"' in html
    assert 'data-toolbar-menu="design"' in html
    assert 'data-toolbar-menu="page"' in html
    assert 'data-toolbar-menu-trigger' in html
    assert 'role="menu" aria-label="设计操作"' in html
    assert 'role="menu" aria-label="页面操作"' in html
    assert 'data-action="present"' in html
    assert 'aria-label="播放"' in html
    assert ">▶</button>" in html
    assert "▶ 播放" not in html
    assert 'data-action="export-pptx"' in html
    assert 'class="toolbar-export"' in html
    assert 'class="toolbar-nav"' not in html
    assert 'data-action="previous"' not in html
    assert 'data-action="next"' not in html
    assert 'data-role="thumbnail-list"' in html
    assert 'data-save-state="download"' in html
    assert 'id="deck-layout-picker"' in html
    assert 'id="deck-layout-controls"' in html
    assert 'data-role="present-controls"' in html
    assert 'data-role="present-progress"' in html
    assert 'data-deck-runtime="layout-registry"' in html
    assert 'data-role="current-page"' in html
    assert 'data-role="current-title"' in html
    assert "前移此页" not in html
    assert "后移此页" not in html
    assert "前移</button>" in html
    assert "后移</button>" in html
    assert "已前移" in html
    assert 'class="statement-main has-proofs proofs-metrics"' in html
    assert 'class="proof-index"' in html
    assert "border-radius: 0;" in html
    assert "0 0 0 4px rgba(30, 43, 250" not in html
    assert "navigator.webdriver" in html
    assert "box-agent:deck-change" in html
    assert "box-agent-controlled-deck" in html
    assert "officev3-controlled-deck-host" in html
    assert 'postToHost("save-request"' in html
    assert 'postToHost("export-pptx-request"' in html
    assert 'message.type === "export-pptx-result"' in html
    assert "文件已在外部改变" in html
    assert 'cloneSaveButton.dataset.saveState = "download"' in html
    assert 'emitChange("add-slide")' in html
    assert 'emitChange("change-layout")' in html
    assert '"change-layout-option"' in html
    assert '"add-layout-item"' in html
    assert '"delete-layout-item"' in html
    assert '"move-layout-item"' in html
    assert "event.stopPropagation();" in html
    assert "clickPath.includes(layoutControls)" in html
    assert "function enterPresentation()" in html
    assert "function exitPresentation(" in html
    assert "function updateEditorScale()" in html
    assert "function renderThumbnails()" in html
    assert "deck-thumbnails-visible" in html
    assert '(min-width: 1080px) and (min-height: 560px)' in html
    assert 'menu.addEventListener("pointerenter"' in html
    assert 'menu.addEventListener("focusin"' in html
    assert "function scheduleToolbarMenuClose(menu)" in html
    assert 'class="toolbar-popover-bridge"' in html
    assert 'currentIndex = nextIndex;\n      scrollToCurrent("auto");' in html
    assert 'setProperty("--deck-editor-slide-gap"' in html
    assert 'removeProperty("--deck-editor-scale")' in html
    assert "body.deck-presenting" in html
    assert "body:not(.deck-presenting) #deck-root > .slide" in html
    assert 'data-media-slot="hero"' in html
    assert 'data-layout-region="cover-copy"' in html
    assert "layout_drafts" in html
    assert "切换回来即可恢复" in html
    assert "box-shadow: inset 3px 0 #222222" not in html
    assert "outline: 1px solid #A8A8A2" in html


@pytest.mark.parametrize(
    ("viewport_width", "expect_thumbnails"),
    [(520, False), (1100, True)],
)
def test_toolbar_groups_fit_embedded_editor_viewport(
    tmp_path: Path,
    viewport_width: int,
    expect_thumbnails: bool,
) -> None:
    html_path = tmp_path / "toolbar.html"
    render = _run("render_deck_html.js", str(EXAMPLE), "--out", str(html_path))
    assert render.returncode == 0, render.stderr

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        f"{viewport_width}x800",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")

    assert probe.returncode == 0, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is True
    assert runtime["editor"]["thumbnailsVisible"] is expect_thumbnails
    assert runtime["editor"]["toolbar"]["hasOverflow"] is False
    assert runtime["editor"]["toolbar"]["left"] >= 0
    assert runtime["editor"]["toolbar"]["right"] <= viewport_width
    assert runtime["editor"]["toolbarMenus"] == {
        "design": {"available": True, "open": True, "expanded": True},
        "page": {"available": True, "open": True, "expanded": True},
    }


def test_text_contrast_self_heals_while_identity_primary_is_preserved(
    tmp_path: Path,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cream = {
        "value": "#F4EFE4",
        "requested": "cream",
        "source": "explicit",
    }
    deck["design_contract"] = {
        "version": 1,
        "palette": {
            "source": "explicit",
            "background": cream,
            "primary": cream,
            "requested": ["#F4EFE4"],
        },
    }
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert render.returncode == 0, render.stderr

    repaired_probe = _run("probe_deck_runtime.js", str(html_path))
    if repaired_probe.returncode != 0 and (
        "Cannot find module 'playwright'" in repaired_probe.stderr
        or "Executable doesn't exist" in repaired_probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")

    assert repaired_probe.returncode == 0, repaired_probe.stderr or repaired_probe.stdout
    repaired_runtime = json.loads(repaired_probe.stdout)
    assert repaired_runtime["editor"]["palette"]["distinctCoreColors"] > 1
    assert repaired_runtime["editor"]["palette"]["textOnBackgroundContrast"] >= 4.5
    assert repaired_runtime["editor"]["palette"]["primary"] == "#F4EFE4"

    html = html_path.read_text(encoding="utf-8")
    html = re.sub(
        r"(--deck-(?:bg|text|primary|inverse):\s*)#[0-9A-Fa-f]{6};",
        r"\1#F4EFE4;",
        html,
    )
    html_path.write_text(html, encoding="utf-8")
    probe = _run("probe_deck_runtime.js", str(html_path))
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")

    assert probe.returncode == 1, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is False
    assert runtime["editor"]["palette"]["distinctCoreColors"] == 1
    assert runtime["editor"]["palette"]["textOnBackgroundContrast"] == 1
    assert "Core deck colors collapse to one value" in runtime["issues"]
    assert "Deck text/background contrast is too low: 1.00" in runtime["issues"]


@pytest.mark.parametrize("colors", [
    {"background": "#F7F3E8", "text": "#151515", "primary": "#2147C7", "accent": "#E6462E"},
    {"background": "#EAF6FF", "text": "#17324D", "primary": "#4C9FE6", "accent": "#FF8A65"},
    {"background": "#FFF8DB", "text": "#173D24", "primary": "#F9D400", "accent": "#15803D"},
    {"background": "#101820", "text": "#F4F6F8", "primary": "#67D9EF", "accent": "#FFBC66"},
], ids=["deep-blue-on-light", "pastel-on-light", "yellow-green", "light-on-dark"])
def test_palette_text_roles_preserve_brand_ink_and_separate_solid_fill_foreground(colors: dict) -> None:
    result = subprocess.run([
        NODE, "-e", """
const fs = require('fs');
const {paletteWithOverrides} = require(process.argv[1]);
const themes = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).themes;
const colors = JSON.parse(process.argv[3]);
const contract = {palette:Object.fromEntries(Object.entries(colors).map(([k,value])=>[k,{value}]))};
console.log(JSON.stringify(themes.map(theme=>({id:theme.id,palette:paletteWithOverrides(theme.palette,contract)}))));
""", str(SCRIPTS_DIR / "design_contract_core.js"),
        str(SKILL_DIR / "layouts" / "manifest.json"), json.dumps(colors),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    for item in json.loads(result.stdout):
        palette = item["palette"]
        assert palette["primary"] == colors["primary"], item["id"]
        assert palette["accent"] == colors["accent"], item["id"]
        assert _contrast_ratio(palette["inverse"], palette["primary"]) >= 4.5, item["id"]
        for key in ("background", "surface", "surface_strong", "primary_soft"):
            assert _contrast_ratio(palette["primary_text"], palette[key]) >= 4.5, item["id"]
        if colors["primary"] == "#2147C7":
            assert palette["primary_text"] == colors["primary"], item["id"]
            assert palette["inverse"] == "#FFFFFF", item["id"]


@pytest.mark.parametrize(("theme", "family"), [
    ("commerce-pulse", "product-showcase"),
    ("people-handbook", "editorial-spread"),
    ("block-frame", "brutalist-frame"),
    ("blue-professional", "institutional-grid"),
    ("data-intelligence", "analytical-exhibit"),
])
@pytest.mark.parametrize(("background", "text_color", "primary", "accent"), [
    ("#F7F3E8", "#151515", "#2147C7", "#E6462E"),
    ("#EAF6FF", "#17324D", "#4C9FE6", "#FF8A65"),
    ("#FFF8DB", "#173D24", "#F9D400", "#15803D"),
    ("#101820", "#F4F6F8", "#67D9EF", "#FFBC66"),
], ids=["deep-blue-on-light", "pastel-on-light", "yellow-green", "light-on-dark"])
def test_rendered_palette_keeps_emphasis_and_colored_surface_text_readable(
    tmp_path: Path, theme: str, family: str, background: str,
    text_color: str, primary: str, accent: str,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js", "kpi-grid-v1", "cards-grid-v1",
        "timeline-horizontal-v1", "statement-focus-v1",
        "--theme", theme, "--family", family, "--design-seed", "palette-role-regression",
        "--out", str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["design_contract"] = {"version": 1, "palette": {
        "source": "inferred", "requested": ["palette-role-regression"],
        **{key: {"value": value, "source": "inferred", "requested": "palette-role-regression"}
           for key, value in {"background": background, "text": text_color,
                              "primary": primary, "accent": accent}.items()},
    }}
    deck["slides"][0]["props"]["variant"] = "cards"
    deck["slides"][1]["props"]["variant"] = "balanced"
    deck["slides"][2]["props"]["variant"] = "horizontal"
    deck["slides"][3]["props"]["emphasis"] = "poster"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert render.returncode == 0, render.stdout + render.stderr
    probe = _run("probe_deck_runtime.js", str(html_path))
    assert probe.returncode == 0, probe.stdout + probe.stderr
    report = json.loads(probe.stdout)
    assert report["editor"]["primary"] == primary
    assert report["editor"]["componentContrast"]["sampled"] > 30
    assert report["editor"]["componentContrast"]["failures"] == []


def test_runtime_probe_reports_local_component_contrast_as_advisory(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8").replace(
        "</head>",
        "<style>.content-card,.content-card *{background:#111!important;color:#111!important}</style></head>",
    )
    html_path.write_text(html, encoding="utf-8")
    report_path = tmp_path / "runtime.json"

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--report",
        str(report_path),
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")

    assert probe.returncode == 0, probe.stderr or probe.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["issues"] == []
    assert any("local text contrast" in warning for warning in report["warnings"])
    assert report["editor"]["componentContrast"]["minimum"] == 1
    assert report["editor"]["componentContrast"]["failureCount"] > 8
    assert report["editor"]["componentContrast"]["affectedSlides"] == [1]
    assert len(report["editor"]["componentContrast"]["failures"]) == 8


def test_project_case_layout_renders_metrics_and_two_compositions(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "project-case-study-v1",
        "project-case-study-v1",
        "--theme",
        "block-frame",
        "--title",
        "Portfolio",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text())
    deck["slides"][0]["props"].update(
        {
            "title": "品牌项目 A（待补充）",
            "positioning": "品牌识别与数字体验的系统化升级。",
            "metrics": [
                {"value": "待补充", "label": "业务结果"},
                {"value": "待补充", "label": "设计影响"},
            ],
            "composition": "split",
            "media_side": "left",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "title": "品牌项目 B（待补充）",
            "positioning": "从叙事策略到发布体验的一体化设计。",
            "metrics": [
                {"value": "待补充", "label": "触达范围"},
                {"value": "待补充", "label": "后续表现"},
            ],
            "composition": "poster",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"

    validation = _run("validate_deck_spec.js", str(deck_path))
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout
    assert rendered.returncode == 0, rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "layout-project-case project-split media-left" in html
    assert "layout-project-case project-poster media-right" in html
    assert 'data-prop-path="metrics.0.value"' in html
    assert "项目视觉 · 双击替换" in html


@pytest.mark.parametrize(("family", "theme"), [
    ("institutional-grid", "blue-professional"),
    ("editorial-spread", "biennale-yellow"),
    ("poster-asymmetric", "studio"),
    ("playful-collage", "daisy-days"),
    ("brutalist-frame", "block-frame"),
    ("retro-interface", "8-bit-orbit"),
    ("literary-minimal", "soft-editorial"),
    ("product-showcase", "product-console"),
    ("cinematic-canvas", "studio"),
    ("analytical-exhibit", "data-intelligence"),
    ("technical-schematic", "technical-blueprint"),
])
def test_project_poster_keeps_full_width_and_readable_copy_across_families(
    tmp_path: Path, family: str, theme: str,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js", "project-case-study-v1", "project-case-study-v1",
        "--theme", theme, "--family", family, "--design-seed", "project-poster-regression",
        "--out", str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text())
    for slide, side in zip(deck["slides"], ["right", "left"]):
        slide["props"].update({
            "composition": "poster", "media_side": side,
            "eyebrow": "01 / BRAND / 示例项目", "title": "01 / BRAND — PULSE LAB",
            "positioning": "把企业级 AI 服务做成一个有速度感、也有人味的品牌系统。定位：可信且有温度。",
            "caption": "CONCEPT PLACEHOLDER — REPLACE WITH REAL WORK",
            "metrics": [
                {"value": "42", "label": "品牌触点 / 示例"},
                {"value": "6 WKS", "label": "上线周期 / 示例"},
                {"value": "03", "label": "动态规则 / 示例"},
            ],
        })
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert render.returncode == 0, render.stdout + render.stderr
    report_path = tmp_path / "html-check.json"
    checked = _run("html_self_check.js", str(html_path), "--dom-to-pptx", "--report", str(report_path))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = json.loads(report_path.read_text())
    assert report["issues"] == []
    assert len(report["projectCaseStyles"]) == 2
    for style in report["projectCaseStyles"]:
        assert style["poster"] is True
        assert len(style["gridTemplateColumns"].split()) == 1, style
        assert style["mediaWidth"] > 1400, style
        assert style["headingWidth"] > 500, style
        assert style["copyScrollHeight"] <= style["copyHeight"] + 1, style


def test_extended_layouts_render_editable_data_and_semantic_variants(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "text-columns-v1",
        "chart-bar-v1",
        "table-data-v1",
        "closing-next-steps-v1",
        "cards-grid-v1",
        "--title",
        "Extended layout gallery",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "alignment": "center",
            "tags": ["团队协作", "Skill 生态", "移动端任务", "付费积分"],
        }
    )
    deck["slides"][1]["props"].update(
        {
            "variant": "lead",
            "sections": [
                {
                    "label": "01",
                    "title": "核心判断",
                    "body": "主文本承担完整叙事，并与右侧补充信息形成清晰主次。",
                    "bullets": ["证据一", "证据二"],
                },
                {
                    "label": "02",
                    "title": "背景",
                    "body": "补充必要背景。",
                    "bullets": [],
                },
                {
                    "label": "03",
                    "title": "影响",
                    "body": "说明判断带来的影响。",
                    "bullets": [],
                },
            ],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "variant": "columns",
            "insight": (
                "市场规模图为示意：分层数据用于表达从总体市场到优先可服务市场的递进关系。"
            ),
            "source": (
                "来源：用户提供的市场方向；具体数值为演示假设，交付前应替换为正式数据。"
            ),
            "items": [
                {"label": "A", "value": "82%", "note": "领先"},
                {"label": "B", "value": "64%", "note": ""},
                {"label": "C", "value": "47%", "note": ""},
                {"label": "D", "value": "31%", "note": ""},
            ],
        }
    )
    deck["slides"][3]["props"].update(
        {
            "variant": "comparison",
            "columns": ["项目", "A", "B", "C", "建议"],
            "rows": [
                ["成本", "低", "中", "高", "A"],
                ["速度", "快", "中", "慢", "A"],
            ],
        }
    )
    deck["slides"][4]["props"]["variant"] = "contact"
    deck["slides"][5]["props"].update(
        {
            "variant": "numbered",
            "items": [
                {
                    "kicker": f"{index:02d}",
                    "title": f"议题 {index}",
                    "body": "用于验证自动序号不会与数字标签重复。",
                }
                for index in range(1, 7)
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    validation = _run("validate_deck_spec.js", str(deck_path))
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )

    assert validation.returncode == 0, validation.stdout
    assert rendered.returncode == 0, rendered.stderr
    assert self_check.returncode == 0, self_check.stdout
    self_check_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert self_check_report["issues"] == []
    assert not any(
        "PowerPoint wrap slack" in warning
        for warning in self_check_report["warnings"]
    )
    assert not any(
        "short background text uses vertical padding" in warning
        for warning in self_check_report["warnings"]
    )
    html = html_path.read_text(encoding="utf-8")
    assert "layout-cover-editorial cover-editorial-center" in html
    assert 'data-prop-path="tags.3"' in html
    assert "editorial-cover-tags" in html
    assert "layout-text-columns text-lead text-count-3" in html
    assert "layout-chart-bar chart-columns chart-count-4" in html
    assert "data-pptx-chart" in html
    assert "data-chart-spec=" in html
    assert 'data-native-chart="true"' in html
    assert 'data-deck-runtime="echarts" data-echarts-version="6.0.0"' in html
    assert 'data-deck-runtime="chart-runtime"' in html
    assert "layout-data-table table-comparison table-columns-5" in html
    assert '<th><span class="data-table-cell-text"' in html
    assert 'data-prop-path="rows.0.4"' in html
    assert "layout-closing closing-contact" in html
    assert "layout-cards cards-numbered cards-count-6" in html
    assert (
        'class="card-kicker" data-prop-path="items.0.kicker" '
        'data-prop-kind="text"></p>'
    ) in html
    assert ".cards-numbered .cards-grid::before" in html
    assert "display: none;" in html


def test_closing_summary_keeps_editable_pptx_wrap_slack(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "closing-next-steps-v1",
        "--title",
        "Closing wrap regression",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "variant": "next-steps",
            "title": "未来看点：如何从新星走向核心",
            "subtitle": (
                "亚马尔下一阶段的关键词，是稳定输出、身体管理、战术承担和在俱乐部/"
                "国家队双线成为核心。"
            ),
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stderr
    assert self_check.returncode == 0, self_check.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert not any(
        "PowerPoint wrap slack" in warning for warning in report["warnings"]
    )


def test_animated_chart_layout_supports_seven_types_and_editable_matrix(
    tmp_path: Path,
) -> None:
    chart_types = ["bar", "column", "line", "area", "pie", "donut", "radar"]
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        *(["chart-data-v1"] * len(chart_types)),
        "--title",
        "Animated chart gallery",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    for slide, chart_type in zip(deck["slides"], chart_types, strict=True):
        slide["props"].update(
            {
                "chart_type": chart_type,
                "categories": ["Q1", "Q2", "Q3", "Q4"],
                "series": [
                    {"name": "本期", "values": ["42", "58", "71", "86"]},
                    {"name": "上期", "values": ["34", "49", "57", "69"]},
                ],
                "animation": "on",
            }
        )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    validation = _run("validate_deck_spec.js", str(deck_path))
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--report",
        str(report_path),
    )

    assert validation.returncode == 0, validation.stdout
    assert rendered.returncode == 0, rendered.stderr
    assert self_check.returncode == 0, self_check.stdout
    html = html_path.read_text(encoding="utf-8")
    assert html.count('data-echarts-version="6.0.0"') == 1
    assert html.count('<section class="slide layout-chart-data') == len(chart_types)
    assert html.count('data-native-chart="true"') >= len(chart_types)
    assert html.count('data-chart-canvas') >= len(chart_types)
    for chart_type in chart_types:
        assert f"chart-type-{chart_type}" in html
    assert "createChartDataControl" in html
    assert "add-chart-category" in html
    assert "add-chart-series" in html
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []


def test_chart_design_profiles_resolve_from_data_semantics(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "chart-data-v1",
        "chart-data-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "chart_type": "line",
            "categories": [f"D{index}" for index in range(1, 9)],
            "series": [{"name": "活跃用户", "values": [str(20 + index) for index in range(8)]}],
        }
    )
    deck["slides"][1]["props"].update(
        {
            "chart_type": "donut",
            "categories": ["自然", "推荐", "活动", "合作"],
            "series": [{"name": "来源", "values": ["34", "28", "22", "16"]}],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "chart_type": "line",
            "categories": ["Q1", "Q2", "Q3", "Q4"],
            "series": [{"name": "ARR", "values": ["120", "180", "260", "360"]}],
            "presentation": "traction",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    output = html_path.read_text(encoding="utf-8")
    first, second, third = [
        output.split(f'data-slide="{index:02d}"', 1)[1].split("</section>", 1)[0]
        for index in range(1, 4)
    ]
    assert 'data-chart-style="cool-ordinal"' in first
    assert 'data-chart-reading-mode="editorial"' in first
    assert "&quot;label_mode&quot;:&quot;endpoints&quot;" in first
    assert 'data-chart-style="botanical-categorical"' in second
    assert 'data-chart-reading-mode="glance"' in second
    assert 'data-chart-style="ink-focus"' in third
    assert 'data-chart-reading-mode="glance"' in third
    assert 'data-chart-palette-light="#173E75,#3769A8,#587DA9,#668AB6"' in first
    for markup in (first, second, third):
        light_palette = re.search(r'data-chart-palette-light="([^"]+)"', markup)
        dark_palette = re.search(r'data-chart-palette-dark="([^"]+)"', markup)
        assert light_palette is not None
        assert dark_palette is not None
        assert all(
            _contrast_ratio(color, "#F7F4E9") >= 3
            for color in light_palette.group(1).split(",")
        )
        assert all(
            _contrast_ratio(color, "#151619") >= 3
            for color in dark_palette.group(1).split(",")
        )


def test_deck_palette_overrides_local_cool_ordinal_chart_colors(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-bar-v1",
        "--theme",
        "auto",
        "--theme-model-choice",
        "bold-poster",
        "--theme-model-reason",
        "体育人物海报。",
        "--theme-model-identity",
        "巴西国家队与桑巴足球",
        "--palette-background",
        "#FFF8E7",
        "--palette-text",
        "#1A1A1A",
        "--palette-primary",
        "#009739",
        "--palette-accent",
        "#FFC400",
        "--palette-secondary",
        "#1F5AA6",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "国家队纪录",
            "title": "队史射手榜",
            "subtitle": "巴西国家队进球",
            "series_label": "进球",
            "insight": "黄绿身份色应覆盖局部冷色预设",
            "source": "FIFA",
            "chart_style": "cool-ordinal",
            "items": [
                {"label": "内马尔", "value": "80", "note": "第一"},
                {"label": "贝利", "value": "77", "note": "第二"},
                {"label": "罗纳尔多", "value": "62", "note": "第三"},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-chart-style="cool-ordinal"' in html
    assert 'data-chart-palette-source="deck"' in html
    assert (
        'data-chart-palette-light="#009739,#FFC400,#1F5AA6,#D8000F"'
        in html
    )


def test_chart_reading_modes_change_echarts_visual_grammar() -> None:
    if NODE is None:
        pytest.skip("Node.js is required to test the chart runtime")
    runtime = SKILL_DIR / "runtime" / "chart-runtime.js"
    probe = """
const api = require(process.argv[1]);
const tokens = {
  primary: '#173E75', text: '#20201D', muted: '#77766F', border: '#D6D4CB',
  background: '#F6F2E9', display: 'Arial', body: 'Arial',
  palette: ['#173E75', '#3769A8', '#587DA9', '#668AB6'],
};
const editorial = api.buildOption({
  type: 'line', categories: ['A', 'B', 'C'],
  series: [{name: 'S', values: [1, 3, 2]}],
  reading_mode: 'editorial', style_profile: 'cool-ordinal', label_mode: 'endpoints',
}, tokens);
const glance = api.buildOption({
  type: 'bar', categories: ['A', 'B', 'C'],
  series: [{name: 'S', values: [1, 3, 2]}],
  reading_mode: 'glance', style_profile: 'cool-ordinal', show_values: 'on',
}, tokens);
console.log(JSON.stringify({
  editorial: {
    lineWidth: editorial.series[0].lineStyle.width,
    lineColor: editorial.series[0].lineStyle.color,
    symbolSize: editorial.series[0].symbolSize,
    gridType: editorial.xAxis.type === 'value'
      ? editorial.xAxis.splitLine.lineStyle.type
      : editorial.yAxis.splitLine.lineStyle.type,
  },
  glance: {
    barMaxWidth: glance.series[0].barMaxWidth,
    radius: glance.series[0].itemStyle.borderRadius,
    labelSize: glance.series[0].label.fontSize,
    colors: glance.series[0].data.map(item => item.itemStyle.color),
  },
}));
"""
    result = subprocess.run(
        [str(NODE), "-e", probe, str(runtime)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["editorial"] == {
        "lineWidth": 2.25,
        "lineColor": "#173E75",
        "symbolSize": 7,
        "gridType": "solid",
    }
    assert payload["glance"] == {
        "barMaxWidth": 42,
        "radius": [0, 18, 18, 0],
        "labelSize": 19,
        "colors": ["#173E75", "#587DA9", "#668AB6"],
    }


def test_native_pptx_chart_export_uses_embedded_profile_palette() -> None:
    bundle = (SCRIPTS_DIR / "dom-to-pptx.bundle.js").read_text(encoding="utf-8")

    assert "function chartAttributePalette" in bundle
    assert "data-chart-palette-dark" in bundle
    assert "data-chart-palette-light" in bundle
    assert "chartColors," in bundle
    assert "varyColors: spec.series.length === 1" in bundle
    assert "lineSize: spec.reading_mode === 'editorial' ? 1.5 : 2.5" in bundle
    assert "markerSize: spec.reading_mode === 'editorial' ? 4 : 6" in bundle


def test_business_progress_line_chart_uses_editable_traction_presentation(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "chart-data-v1",
        "chart-data-v1",
        "--theme",
        "product-console",
        "--lock-theme",
        "--title",
        "Business progress chart",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["design"] = {
        "version": 1,
        "seed": "traction-regression",
        "family": "analytical-exhibit",
        "variant": "decision-board",
    }
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "业务进展",
            "title": "业务进展：30 家试点客户，年化收入 800 万元",
            "subtitle": "产品-市场匹配初步验证，收入保持增长趋势",
            "chart_type": "line",
            "categories": ["一季度", "二季度", "三季度", "四季度"],
            "series": [
                {
                    "name": "年化收入运行率（万元）",
                    "values": ["300", "480", "620", "800"],
                }
            ],
            "value_suffix": "万元",
            "presentation": "auto",
            "highlights": [],
            "insight": "已有 30 家试点客户，当前年化收入 800 万元。",
            "source": "来源：用户提供的客户数与年化收入。",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "季度分析",
            "title": "季度需求结构保持稳定",
            "subtitle": "观察各季度变化",
            "chart_type": "line",
            "categories": ["Q1", "Q2", "Q3", "Q4"],
            "series": [{"name": "需求量", "values": ["42", "44", "43", "45"]}],
            "presentation": "auto",
            "highlights": [],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "经营信号",
            "title": "关键经营指标与增长趋势",
            "subtitle": "核心指标与趋势数据均可编辑",
            "chart_type": "area",
            "categories": ["Q1", "Q2", "Q3", "Q4"],
            "series": [{"name": "收入", "values": ["12", "18", "25", "34"]}],
            "presentation": "traction",
            "highlights": [
                {"value": "18 家", "label": "付费客户", "note": ""},
                {"value": "34 万元", "label": "季度收入", "note": ""},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    validation = _run("validate_deck_spec.js", str(deck_path))
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert validation.returncode == 0, validation.stdout
    assert rendered.returncode == 0, rendered.stderr
    assert self_check.returncode == 0, self_check.stdout
    html = html_path.read_text(encoding="utf-8")
    rendered_markup = html.split('<script data-deck-runtime="layout-registry">', 1)[0]
    traction_slide, standard_slide, explicit_traction_slide = rendered_markup.split(
        '<section class="slide layout-chart-data', 3
    )[1:]
    assert "chart-presentation-traction" in traction_slide
    assert "chart-traction-body" in traction_slide
    assert "chart-body chart-data-body" not in traction_slide
    assert 'data-derived-highlight="value"' not in traction_slide
    assert 'data-prop-path="highlights.0.value"' in traction_slide
    assert 'data-prop-path="highlights.1.label"' in traction_slide
    assert ">30 家</strong>" in traction_slide
    assert ">800 万元</strong>" in traction_slide
    embedded_document = json.loads(
        html.split('<script type="application/json" id="deck-document">', 1)[1]
        .split("</script>", 1)[0]
    )
    assert embedded_document["slides"][0]["props"]["highlights"] == [
        {"value": "30 家", "label": "试点客户", "note": ""},
        {"value": "800 万元", "label": "年化收入", "note": ""},
    ]
    assert "&quot;label_mode&quot;:&quot;endpoints&quot;" in traction_slide
    assert 'data-native-chart="true"' in traction_slide
    assert "chart-presentation-standard" in standard_slide
    assert "chart-body chart-data-body" in standard_slide
    assert "chart-presentation-traction" in explicit_traction_slide
    assert 'data-prop-path="highlights.0.value"' in explicit_traction_slide
    assert 'data-prop-path="highlights.1.label"' in explicit_traction_slide
    assert (
        'body[data-deck-theme="product-console"] '
        ".chart-presentation-traction .eyebrow"
    ) in html
    assert (
        'body[data-deck-composition="analytical-exhibit"] '
        ".chart-presentation-traction .slide-header"
    ) in html
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []


def test_mixed_unit_chart_uses_independent_editable_small_multiples(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "chart-data-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "chart_type": "column",
            "categories": ["首次响应时间", "一次解决率", "满意度"],
            "series": [
                {"name": "改进前", "values": ["18", "68%", "4.2"]},
                {"name": "改进后", "values": ["7", "81%", "4.6"]},
            ],
            "legend": "on",
            "show_values": "on",
            "animation": "on",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    html = html_path.read_text(encoding="utf-8")
    rendered_markup = html.split('<script data-deck-runtime="layout-registry">', 1)[0]
    assert 'data-chart-scale="independent"' in html
    assert "chart-small-multiples-count-3" in html
    assert rendered_markup.count('data-native-chart="true"') == 3
    assert rendered_markup.count('class="echarts-for-pptx"') == 3
    assert "chart-small-multiple-legend" in html
    assert "&quot;value_suffix&quot;:&quot;%&quot;" in html
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []


def test_bodyless_timeline_uses_title_sequence_density_mode(tmp_path: Path) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "timeline-horizontal-v1",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "title": "采取的行动",
            "steps": [
                {"phase": "知识沉淀", "title": "统一知识库", "body": ""},
                {"phase": "工单匹配", "title": "工单自动分流", "body": ""},
                {"phase": "质量复盘", "title": "每周质检复盘", "body": ""},
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "timeline-count-3 timeline-bodyless" in html
    assert "body[data-deck-composition] .layout-timeline.timeline-bodyless .timeline-step" in html
    assert "justify-content: center" in html
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []


def test_editable_pptx_chart_export_preserves_cjk_font_fallbacks() -> None:
    bundle = (SCRIPTS_DIR / "dom-to-pptx.bundle.js").read_text(encoding="utf-8")

    assert "resolvePptxFontFace(style.fontFamily, text)" in bundle
    assert "getTextStyle(nodeStyle, config.scale, textVal)" in bundle
    assert "const chartText = [...spec.categories" in bundle
    assert "legendFontFace: bodyFont" in bundle
    assert "catAxisLabelFontFace: bodyFont" in bundle


def test_block_frame_theme_renders_builtin_visual_dna(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["theme_id"] = "block-frame"
    deck["slides"][2]["props"]["emphasis"] = "poster"
    deck_path = tmp_path / "block-frame.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "block-frame.html"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout
    assert render.returncode == 0, render.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-theme="block-frame"' in html
    assert "--deck-bg: #FFFDF5" in html
    assert "--deck-border-width: 5px" in html
    assert html.index("--deck-bg: #FDFAE7") < html.rindex("--deck-bg: #FFFDF5")
    assert 'body[data-deck-theme="block-frame"] .slide' in html
    assert "box-shadow: 12px 12px 0 var(--deck-text)" in html
    assert 'body[data-deck-theme="block-frame"] .statement-poster' in html
    assert "background-color: var(--deck-primary);" in html
    assert "color: var(--deck-inverse);" in html
    assert 'body[data-deck-theme="block-frame"] .layout-timeline' in html

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        "1440x900",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    assert probe.returncode == 0, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is True
    assert runtime["editor"]["primary"] == "#FE90E8"
    assert runtime["editor"]["inverse"] == "#000000"
    assert runtime["editor"]["editorScale"] < 1
    assert runtime["editor"]["statement"]["contrast"] >= 4.5
    assert runtime["export"] == {
        "cssWidth": 1920,
        "cssHeight": 1080,
        "renderedWidth": 1920,
        "renderedHeight": 1080,
    }


def test_priority_and_high_frequency_themes_own_dedicated_css() -> None:
    css = (SKILL_DIR / "runtime" / "deck.css").read_text(encoding="utf-8")
    minimum_selector_counts = {
        "technical-blueprint": 16,
        "product-console": 14,
        "data-intelligence": 14,
        "signal": 12,
        "soft-editorial": 18,
        "daisy-days": 16,
        "people-handbook": 14,
        "capital-ledger": 13,
        "clinical-atlas": 14,
        "civic-brief": 14,
        "research-notebook": 15,
        "factory-floor": 11,
        "legal-docket": 11,
        "property-atlas": 10,
        "commerce-pulse": 11,
        "logistics-control-tower": 11,
    }
    for theme_id, minimum in minimum_selector_counts.items():
        selector = f'body[data-deck-theme="{theme_id}"]'
        assert css.count(selector) >= minimum, theme_id
        assert f'{selector} [data-pptx-diagram]' not in css

    assert 'content: "SYSTEM BLUEPRINT / VECTOR DIAGRAM"' in css
    assert 'content: "●  ●  ●     PRODUCT CONSOLE"' in css
    assert 'content: "DATA FLOW / LIVE SYSTEM MAP"' in css
    assert 'body[data-deck-theme="signal"] .statement-narrative' in css
    assert (
        'body[data-deck-theme="soft-editorial"] .text-section-body::first-letter'
        in css
    )
    assert 'body[data-deck-theme="daisy-days"] .layout-cover-editorial::before' in css
    assert 'body[data-deck-theme="daisy-days"] .editorial-cover-copy h1' in css
    assert 'content: "PEOPLE / HANDBOOK"' in css
    assert 'content: "CAPITAL / EVIDENCE / DECISION"' in css
    assert 'content: "CLINICAL ATLAS / EVIDENCE PATHWAY"' in css
    assert 'content: "POLICY DOCKET / PUBLIC VALUE"' in css
    assert 'content: "ABSTRACT / METHOD / FINDINGS"' in css
    assert 'content: "SHOP FLOOR / QUALITY / FLOW"' in css
    assert 'content: "MATTER / EVIDENCE / DECISION"' in css
    assert 'content: "SITE / ASSET / VALUE"' in css
    assert 'content: "SKU / CONVERSION / RETENTION"' in css
    assert 'content: "ORIGIN / TRANSIT / DELIVERY"' in css


def test_near_duplicate_themes_own_distinct_composition_overrides() -> None:
    css = (SKILL_DIR / "runtime" / "deck.css").read_text(encoding="utf-8")

    assert 'body[data-deck-theme-id="coral"] .layout-cards' in css
    assert (
        'body[data-deck-theme-id="coral"][data-deck-composition] '
        ".cards-count-3 .cards-grid"
    ) in css
    assert (
        'body[data-deck-theme-id="block-frame-mono-blue"]'
        '[data-deck-composition] .layout-cards.cards-count-3 .cards-grid'
    ) in css
    assert (
        'body[data-deck-theme-id="creative-mode"][data-deck-composition] '
        ".cards-count-3 .cards-grid"
    ) in css
    assert 'body[data-deck-theme-id="creative-mode"] .layout-cover-editorial' in css
    assert 'body[data-deck-theme-id="coral"] [data-pptx-diagram]' not in css
    assert (
        'body[data-deck-theme-id="block-frame-mono-blue"] [data-pptx-diagram]'
        not in css
    )
    assert 'body[data-deck-theme-id="creative-mode"] [data-pptx-diagram]' not in css
    assert (
        'body[data-deck-theme-id="consulting-navy"] '
        ".layout-cover-editorial .editorial-cover-copy::after"
    ) in css
    assert (
        'body[data-deck-theme-id="consulting-navy"][data-deck-composition] '
        ".layout-cards.cards-count-3 .cards-grid"
    ) in css
    assert 'body[data-deck-theme-id="monochrome"] .composition-ledger-rail' in css
    assert (
        'body[data-deck-theme-id="monochrome"][data-deck-composition] '
        ".layout-cards.cards-count-3 .cards-grid"
    ) in css
    assert 'body[data-deck-theme-id="consulting-navy"] [data-pptx-diagram]' not in css
    assert 'body[data-deck-theme-id="monochrome"] [data-pptx-diagram]' not in css


@pytest.mark.parametrize(
    ("theme_id", "family", "signature", "layout_id"),
    [
        ("people-handbook", "editorial-spread", "PEOPLE / HANDBOOK", "cards-grid-v1"),
        ("capital-ledger", "analytical-exhibit", "CAPITAL / EVIDENCE / DECISION", "cards-grid-v1"),
        ("clinical-atlas", "technical-schematic", "CLINICAL ATLAS / EVIDENCE PATHWAY", "cards-grid-v1"),
        ("civic-brief", "institutional-grid", "POLICY DOCKET / PUBLIC VALUE", "cards-grid-v1"),
        ("research-notebook", "literary-minimal", "ABSTRACT / METHOD / FINDINGS", "cards-grid-v1"),
        ("factory-floor", "technical-schematic", "SHOP FLOOR / QUALITY / FLOW", "factory-process-line-v1"),
        ("legal-docket", "institutional-grid", "MATTER / EVIDENCE / DECISION", "legal-case-logic-v1"),
        ("property-atlas", "institutional-grid", "SITE / ASSET / VALUE", "property-factsheet-v1"),
        ("commerce-pulse", "product-showcase", "SKU / CONVERSION / RETENTION", "commerce-funnel-v1"),
        ("logistics-control-tower", "product-showcase", "ORIGIN / TRANSIT / DELIVERY", "supply-network-v1"),
    ],
)
def test_professional_signature_themes_render_editable_layouts_without_bounds_issues(
    tmp_path: Path,
    theme_id: str,
    family: str,
    signature: str,
    layout_id: str,
) -> None:
    deck_path = tmp_path / theme_id / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        layout_id,
        "table-data-v1",
        "--theme",
        theme_id,
        "--lock-theme",
        "--family",
        family,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    html_path = deck_path.parent / "index.html"
    report_path = deck_path.parent / "qa" / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["issues"] == []
    html = html_path.read_text(encoding="utf-8")
    assert f'data-deck-theme="{theme_id}"' in html
    assert f'content: "{signature}"' in html


def test_daisy_days_decorations_preserve_dense_card_layout_bounds(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "timeline-horizontal-v1",
        "cards-grid-v1",
        "--theme",
        "daisy-days",
        "--family",
        "playful-collage",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["design"].update(
        {"seed": "daisy_staggered_2", "variant": "staggered"}
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "课堂活动",
            "title": "一起记住太阳系",
            "subtitle": "通过观察、比较和动手活动，可以把太阳系知识记得更牢。",
            "variant": "balanced",
            "items": [
                {
                    "kicker": "看一看",
                    "title": "观察外观",
                    "body": "看颜色和大小：不同星球有不同外观特点。",
                },
                {
                    "kicker": "排一排",
                    "title": "记住顺序",
                    "body": "找位置关系：注意谁离太阳近、谁离太阳远。",
                },
                {
                    "kicker": "玩一玩",
                    "title": "课堂小游戏",
                    "body": "用卡片给行星排队，或画出自己的太阳系图。",
                },
                {
                    "kicker": "想一想",
                    "title": "课后思考",
                    "body": "如果去一颗行星旅行，你最想去哪里，为什么？",
                },
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["issues"] == []
    html = html_path.read_text(encoding="utf-8")
    assert 'body[data-deck-theme="daisy-days"] :is(' in html
    assert "box-shadow: inset -6px -6px 0" in html


def test_brutalist_ledger_three_card_layout_preserves_slide_bounds(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "cards-grid-v1",
        "table-data-v1",
        "--theme",
        "stencil-tablet",
        "--family",
        "brutalist-frame",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["design"].update(
        {"seed": "ledger_bounds_0", "variant": "ledger-frame"}
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "艺术语言",
            "title": "艺术语言：线条、色彩与叙事",
            "subtitle": "线描、设色与连续叙事共同构成敦煌壁画的独特艺术语言。",
            "variant": "balanced",
            "items": [
                {
                    "kicker": "线描",
                    "title": "线条塑造神采",
                    "body": "衣纹、姿态与神情依靠线条组织，形成富有节奏的视觉引导。",
                },
                {
                    "kicker": "设色",
                    "title": "色彩形成气韵",
                    "body": "矿物色与强对比营造装饰张力，时间沉淀后更显古雅。",
                },
                {
                    "kicker": "叙事",
                    "title": "墙面展开故事",
                    "body": "连续画面引导观众移动视线，像阅读墙面上的图像史诗。",
                },
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["issues"] == []
    html = html_path.read_text(encoding="utf-8")
    assert "grid-auto-rows: minmax(0, 1fr)" in html


def test_every_composition_variant_preserves_card_capacity_contract(
    tmp_path: Path,
) -> None:
    variants_by_family = {
        "institutional-grid": ["balanced-grid", "rail-grid", "ledger-grid"],
        "editorial-spread": ["split-spread", "feature-spread", "banded-spread"],
        "poster-asymmetric": ["offset-hero", "stacked-poster", "split-poster"],
        "playful-collage": ["mosaic", "staggered", "capsule"],
        "brutalist-frame": ["block-grid", "offset-frame", "ledger-frame"],
        "retro-interface": ["window-grid", "terminal-stack", "pixel-panels"],
        "literary-minimal": ["margin-note", "quiet-center", "asymmetric-column"],
        "product-showcase": ["device-stage", "browser-story", "annotated-flow"],
        "cinematic-canvas": ["full-bleed", "split-film", "chapter-cut"],
        "analytical-exhibit": ["exhibit-grid", "evidence-rail", "decision-board"],
        "technical-schematic": ["blueprint-canvas", "annotated-system", "spec-sheet"],
    }
    theme_by_family = {
        "institutional-grid": "blue-professional",
        "editorial-spread": "biennale-yellow",
        "poster-asymmetric": "studio",
        "playful-collage": "daisy-days",
        "brutalist-frame": "block-frame",
        "retro-interface": "8-bit-orbit",
        "literary-minimal": "soft-editorial",
        "product-showcase": "product-console",
        "cinematic-canvas": "studio",
        "analytical-exhibit": "data-intelligence",
        "technical-schematic": "technical-blueprint",
    }

    def seed_for_variant(family: str, variant: str) -> str:
        variants = variants_by_family[family]
        for index in range(10_000):
            seed = f"capacity-{family}-{index:03d}"
            digest = hashlib.sha256(
                f"controlled-deck-composition-v1:{family}:{seed}".encode()
            ).digest()
            if variants[int.from_bytes(digest[:4], "big") % len(variants)] == variant:
                return seed
        raise AssertionError(f"Unable to find seed for {family}/{variant}")

    failures: list[str] = []
    for family, variants in variants_by_family.items():
        for variant in variants:
            case_dir = tmp_path / family / variant
            deck_path = case_dir / "deck.json"
            scaffold = _run(
                "inspect_deck_contract.js",
                "cards-grid-v1",
                "cards-grid-v1",
                "cards-grid-v1",
                "cards-grid-v1",
                "--theme",
                theme_by_family[family],
                "--family",
                family,
                "--design-seed",
                seed_for_variant(family, variant),
                "--out",
                str(deck_path),
            )
            assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
            deck = json.loads(deck_path.read_text(encoding="utf-8"))
            assert deck["design"]["variant"] == variant
            for slide_index, slide in enumerate(deck["slides"]):
                item_count = slide_index + 3
                slide["props"].update(
                    {
                        "eyebrow": "CAPACITY CONTRACT",
                        "title": f"{item_count} 个并列信息单元",
                        "subtitle": "每张卡片使用正常长度的中文标题和说明，验证模板容量而不是极短占位文案。",
                        "variant": "balanced",
                        "items": [
                            {
                                "kicker": f"阶段 {item_index + 1}",
                                "title": "清晰说明这一项的核心结论",
                                "body": "补充必要的背景、动作和结果信息，保持完整可读，不依赖裁切或隐藏溢出内容。",
                            }
                            for item_index in range(item_count)
                        ],
                    }
                )
            deck_path.write_text(
                json.dumps(deck, ensure_ascii=False),
                encoding="utf-8",
            )
            html_path = case_dir / "index.html"
            report_path = case_dir / "html_self_check.json"
            rendered = _run(
                "render_deck_html.js",
                str(deck_path),
                "--out",
                str(html_path),
            )
            assert rendered.returncode == 0, rendered.stdout + rendered.stderr
            self_check = _run(
                "html_self_check.js",
                str(html_path),
                "--dom-to-pptx",
                "--report",
                str(report_path),
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if self_check.returncode != 0 or report["issues"] or report["warnings"]:
                failures.append(
                    f"{family}/{variant}: "
                    + "; ".join(report["issues"] + report["warnings"])
                )
            if family == "institutional-grid":
                styles_by_count = {
                    style["itemCount"]: style for style in report["cardGridStyles"]
                }
                for item_count in (4, 5, 6):
                    style = styles_by_count.get(item_count)
                    if style is None:
                        failures.append(
                            f"{family}/{variant}/{item_count}: missing computed style"
                        )
                        continue
                    column_count = len(style["gridTemplateColumns"].split())
                    expected_columns = 1
                    if variant != "ledger-grid":
                        expected_columns = {4: 2, 5: 6, 6: 3}[item_count]
                    if column_count != expected_columns:
                        failures.append(
                            f"{family}/{variant}/{item_count}: expected "
                            f"{expected_columns} columns, got "
                            f"{style['gridTemplateColumns']}"
                        )
                    if variant == "ledger-grid":
                        expected_ledger_styles = {
                            "gridAutoRows": "auto",
                            "rowGap": "0px",
                            "columnGap": "0px",
                            "paddingTop": "24px",
                        }
                        for property_name, expected_value in (
                            expected_ledger_styles.items()
                        ):
                            if style[property_name] != expected_value:
                                failures.append(
                                    f"{family}/{variant}/{item_count}: expected "
                                    f"{property_name}={expected_value}, got "
                                    f"{style[property_name]}"
                                )

    assert not failures, "\n".join(failures)


def test_institutional_balanced_grid_preserves_six_numbered_cards(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cards-grid-v1",
        "--theme",
        "product-console",
        "--family",
        "institutional-grid",
        "--design-seed",
        "numbered-capacity-003",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["design"]["variant"] == "balanced-grid"
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "汇报结构",
            "title": "汇报目录",
            "subtitle": (
                "本页以编号卡片网格完整呈现6个汇报板块，"
                "便于管理层快速把握整体结构。"
            ),
            "variant": "numbered",
            "items": [
                {
                    "kicker": f"{index:02d}",
                    "title": title,
                    "body": body,
                }
                for index, (title, body) in enumerate(
                    [
                        (
                            "大会概况与产品发布",
                            "概览大会基本信息、参会目标及重点产品发布内容。",
                        ),
                        (
                            "论坛核心亮点",
                            "梳理主论坛与专题论坛中的关键议题、观点输出和业务价值。",
                        ),
                        (
                            "生态合作成果",
                            "汇总合作伙伴互动、合作签约、联合发布及生态建设进展。",
                        ),
                        (
                            "展区与产品展示",
                            "呈现展区动线、重点展项、产品演示和现场反馈。",
                        ),
                        (
                            "媒体传播成效",
                            "总结媒体曝光、传播渠道、重点报道和品牌声量表现。",
                        ),
                        (
                            "总结与后续计划",
                            "提炼大会成效、待跟进事项和下一阶段行动安排。",
                        ),
                    ],
                    start=1,
                )
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []
    assert report["warnings"] == []


def test_comic_panel_theme_renders_story_panels_and_clean_diagrams(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "technical-diagram-v1",
        "closing-next-steps-v1",
        "--theme",
        "comic-panel",
        "--lock-theme",
        "--title",
        "AI 智能客服的一天",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "漫画工作日",
            "title": "AI 智能客服的一天",
            "subtitle": "用连续分镜解释一次咨询如何被接住。",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "三格分镜",
            "title": "用户提问、AI 理解、系统响应",
            "items": [
                {"kicker": "叮！", "title": "用户提问", "body": "问题带着上下文进入服务台。"},
                {"kicker": "嗡——", "title": "AI 理解", "body": "识别意图并组织可执行任务。"},
                {"kicker": "啪！", "title": "系统响应", "body": "检索知识并调用业务接口。"},
            ],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "专业技术图",
            "title": "系统架构保持清晰",
            "subtitle": "漫画语法只作用于页面外框，不污染 DiagramSpec 节点。",
            "note": "HTML 保留 DiagramSpec；PPTX 导出单个 SVG 矢量对象。",
        }
    )
    deck["slides"][3]["props"].update(
        {
            "eyebrow": "收工复盘",
            "title": "服务闭环已经跑通",
            "subtitle": "以漫画动作字结束，但保留业务信息层级。",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert render.returncode == 0, render.stdout + render.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-theme="comic-panel"' in html
    assert 'data-deck-theme-id="comic-panel"' in html
    assert "--deck-primary: #FF4D3D" in html
    assert 'body[data-deck-theme="comic-panel"] .slide' in html
    assert 'content: "OPENING!"' in html
    assert 'content: "PANEL "' in html
    assert 'content: "FIN!"' in html
    assert 'body[data-deck-theme="comic-panel"] .technical-diagram-stage' in html
    assert html.count("data-pptx-diagram") >= 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []
    assert report["diagramCount"] == 1

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        "1440x900",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    assert probe.returncode == 0, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is True
    assert runtime["editor"]["primary"] == "#FF4D3D"
    assert runtime["editor"]["diagram"]["state"] == "ready"
    assert runtime["editor"]["diagram"]["svgRoots"] == 1
    assert runtime["editor"]["diagrams"][0]["strategy"] == "layered-architecture"


def test_8_bit_orbit_theme_renders_pixel_arcade_ui_and_clean_diagrams(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-hero-v1",
        "cards-grid-v1",
        "technical-diagram-v1",
        "closing-next-steps-v1",
        "statement-focus-v1",
        "--theme",
        "8-bit-orbit",
        "--lock-theme",
        "--title",
        "代码冒险：开发者平台",
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "PLAYER 01",
            "title": "代码冒险：开发者平台",
            "subtitle": "用像素街机语言介绍工具链，但不牺牲技术表达。",
        }
    )
    deck["slides"][1]["props"].update(
        {
            "eyebrow": "LEVEL SELECT",
            "title": "三个能力关卡",
            "items": [
                {"kicker": "LV.1", "title": "开发", "body": "从模板快速启动可运行项目。"},
                {"kicker": "LV.2", "title": "验证", "body": "在真实浏览器中检查交互。"},
                {"kicker": "LV.3", "title": "交付", "body": "导出可恢复、可验证的产物。"},
            ],
        }
    )
    deck["slides"][2]["props"].update(
        {
            "eyebrow": "SYSTEM MAP",
            "title": "架构图继续保持专业",
            "subtitle": "像素语言只作用于显示器外框，不污染 DiagramSpec 节点。",
            "note": "PPTX 中仍导出为单个 SVG 矢量对象。",
        }
    )
    deck["slides"][3]["props"].update(
        {
            "eyebrow": "STAGE CLEAR",
            "title": "进入下一关",
            "subtitle": "用街机状态字收束，同时保留行动信息层级。",
        }
    )
    deck["slides"][4]["props"].update(
        {
            "eyebrow": "FINAL SCORE",
            "statement": "每个玩家都能写出自己的世界故事",
            "support": "创造让想象拥有形状，冒险让旅程拥有回忆。",
            "proofs": [
                {"value": "自由建造", "label": "把想象变成可进入的空间"},
                {"value": "探索未知", "label": "让旅程留下个人化的回忆"},
            ],
            "proof_style": "points",
            "emphasis": "poster",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "index.html"
    report_path = tmp_path / "html_self_check.json"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--report",
        str(report_path),
    )

    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert render.returncode == 0, render.stdout + render.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-theme="8-bit-orbit"' in html
    assert 'data-deck-theme-id="8-bit-orbit"' in html
    assert "--deck-primary: #5EDCF4" in html
    assert 'body[data-deck-theme="8-bit-orbit"] .slide' in html
    assert 'content: "PLAYER 01 // READY"' in html
    assert 'content: "SLOT "' in html
    assert 'content: "CONTINUE?"' in html
    assert 'body[data-deck-theme="8-bit-orbit"] .technical-diagram-stage' in html
    assert html.count("data-pptx-diagram") >= 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []
    assert report["diagramCount"] == 1

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        "1440x900",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    assert probe.returncode == 0, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is True
    assert runtime["editor"]["primary"] == "#5EDCF4"
    assert runtime["editor"]["diagram"]["state"] == "ready"
    assert runtime["editor"]["diagram"]["svgRoots"] == 1
    assert runtime["editor"]["diagrams"][0]["strategy"] == "layered-architecture"
    assert runtime["editor"]["statement"]["contrast"] >= 4.5


def test_mono_blue_block_frame_reuses_visual_dna_with_restrained_palette(
    tmp_path: Path,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["theme_id"] = "block-frame-mono-blue"
    deck["slides"][2]["props"]["emphasis"] = "poster"
    deck_path = tmp_path / "mono-blue.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "mono-blue.html"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout
    assert render.returncode == 0, render.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'data-deck-theme="block-frame"' in html
    assert 'data-deck-theme-id="block-frame-mono-blue"' in html
    assert "--deck-bg: #FFFDF7" in html
    assert "--deck-surface: #FFFFFF" in html
    assert "--deck-primary: #1E2BFA" in html
    assert "--deck-primary-soft: #E3E6FF" in html
    assert "--deck-inverse: #FFFFFF" in html
    assert "#FE90E8" not in html[html.rindex(":root {") :]

    probe = _run(
        "probe_deck_runtime.js",
        str(html_path),
        "--viewport",
        "1440x900",
    )
    if probe.returncode != 0 and (
        "Cannot find module 'playwright'" in probe.stderr
        or "Executable doesn't exist" in probe.stderr
    ):
        pytest.skip("Managed Playwright browser is unavailable")
    assert probe.returncode == 0, probe.stderr or probe.stdout
    runtime = json.loads(probe.stdout)
    assert runtime["ok"] is True
    assert runtime["editor"]["primary"] == "#1E2BFA"
    assert runtime["editor"]["inverse"] == "#FFFFFF"
    assert runtime["editor"]["statement"]["contrast"] >= 4.5


def test_validation_errors_expose_registered_contract_choices(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["theme_id"] = "not-a-theme"
    deck["slides"][0]["props"]["image"] = "assets/generated/cover.png"
    deck_path = tmp_path / "actionable-errors.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert "registered theme_ids: 8-bit-orbit, biennale-yellow" in result.stdout
    assert "signal" in result.stdout
    assert "studio" in result.stdout
    assert "vellum" in result.stdout
    assert "allowed fields for cover-hero-v1" in result.stdout
    assert "hero" in result.stdout


def test_slide_background_is_validated_normalized_and_rendered(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    deck["slides"][0]["background"] = {
        "src": "assets/generated/cover-background.png",
        "alt": "Abstract workflow atmosphere",
        "origin": "generated",
        "treatment": "wash-dark",
    }
    deck_path = tmp_path / "background.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "background.html"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout
    assert render.returncode == 0, render.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "has-background background-wash-dark" in html
    assert 'data-background-origin="generated"' in html
    assert 'src="assets/generated/cover-background.png"' in html
    assert 'data-model-root="slide" data-prop-path="background.src"' in html
    assert '"fit": "cover"' in html
    assert '"position": "center"' in html
    assert (
        ".slide-background::after {\n"
        "  content: \"\";\n"
        "  position: absolute;\n"
        "  inset: 0;\n"
        "  background: var(--deck-bg);\n"
        "  opacity: 0.7;"
    ) in html
    assert "background: rgba(253, 250, 231, 0.7);" not in html
    assert (
        ".slide.background-wash-dark .slide-background::after {\n"
        "  background: #0D0E12;\n"
        "  opacity: 0.64;"
    ) in html


def test_statement_auto_uses_wrapping_points_for_sentence_values(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    statement = deck["slides"][2]["props"]
    statement["proofs"] = [
        {"label": "事实边界", "value": "不预设未发生赛果"},
        {"label": "后续更新", "value": "保留对阵产生后的替换空间"},
        {"label": "使用场景", "value": "适合汇报、活动策划和内容预热"},
    ]
    deck_path = tmp_path / "points.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "points.html"

    validation = _run("validate_deck_spec.js", str(deck_path))
    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))

    assert validation.returncode == 0, validation.stdout
    assert render.returncode == 0, render.stderr
    html = html_path.read_text(encoding="utf-8")
    assert 'class="statement-main has-proofs proofs-points"' in html
    assert "不预设未发生赛果" in html


def test_statement_point_labels_keep_powerpoint_wrap_slack(tmp_path: Path) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    statement = deck["slides"][2]["props"]
    statement.update(
        {
            "proofs": [
                {"label": "原则层", "value": "用结论先行统一表达方向。"},
                {"label": "结构层", "value": "通过层级、分组和递进降低沟通成本。"},
                {"label": "行动层", "value": "用五步清单把方法嵌入日常工作。"},
            ],
            "proof_style": "points",
        }
    )
    deck_path = tmp_path / "statement-points.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = tmp_path / "statement-points.html"
    report_path = tmp_path / "html_self_check.json"

    render = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )

    assert render.returncode == 0, render.stdout + render.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert not any(
        "PowerPoint wrap slack" in warning for warning in report["warnings"]
    )


@pytest.mark.parametrize("family", ["analytical-exhibit", "technical-schematic"])
def test_statement_points_keep_full_width_in_structured_families(
    tmp_path: Path,
    family: str,
) -> None:
    deck_path = tmp_path / family / "deck.json"
    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "--theme",
        "blue-professional",
        "--family",
        family,
        "--out",
        str(deck_path),
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"].update(
        {
            "eyebrow": "持续看点",
            "statement": "为什么值得关注：EURO 2024后的持续看点",
            "support": (
                "UEFA将他评为EURO 2024 Young Player of the Tournament，"
                "说明其表现已经从俱乐部延伸到国家队大赛舞台。"
            ),
            "proofs": [
                {"label": "", "value": "UEFA官方奖项：EURO 2024最佳年轻球员。"},
                {"label": "", "value": "关注已验证成就，避免媒体猜测。"},
                {"label": "", "value": "表述为高潜力、仍在发展中。"},
            ],
            "proof_style": "points",
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    html_path = deck_path.parent / "index.html"
    report_path = deck_path.parent / "qa" / "html_self_check.json"

    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    self_check = _run(
        "html_self_check.js",
        str(html_path),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(report_path),
    )

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == []
    assert not any("overflow detected" in warning for warning in report["warnings"])
    assert not any(
        "PowerPoint wrap slack" in warning for warning in report["warnings"]
    )


def test_image_manifest_rejects_duplicate_generated_assets_and_checks_deck_refs(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "a.png").write_bytes(b"same-image")
    (generated / "b.png").write_bytes(b"same-image")
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "mode": "creative_image_mode",
                "image_plan": [
                    {
                        "slide": 1,
                        "decision": "generate",
                        "status": "generated",
                        "output_path": "assets/generated/a.png",
                    },
                    {
                        "slide": 2,
                        "decision": "generate",
                        "status": "generated",
                        "output_path": "assets/generated/b.png",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    deck = tmp_path / "deck.json"
    deck.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "props": {
                            "image": {
                                "src": "assets/generated/a.png",
                                "origin": "generated",
                            }
                        }
                    },
                    {
                        "props": {
                            "image": {
                                "src": "assets/generated/b.png",
                                "origin": "generated",
                            }
                        }
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    duplicate = _run(
        "validate_image_manifest.js",
        str(manifest),
        "--mode",
        "creative_image_mode",
        "--min-generated",
        "2",
        "--deck",
        str(deck),
    )
    assert duplicate.returncode == 1
    assert "reused across multiple image-plan entries" in duplicate.stdout

    (generated / "b.png").write_bytes(b"different-image")
    valid = _run(
        "validate_image_manifest.js",
        str(manifest),
        "--mode",
        "creative_image_mode",
        "--min-generated",
        "2",
        "--deck",
        str(deck),
    )
    assert valid.returncode == 0, valid.stdout


def test_auto_image_manifest_rejects_unresolved_generate_entry(tmp_path: Path) -> None:
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True)
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "mode": "auto",
                "image_plan": [
                    {
                        "slide": 1,
                        "decision": "generate",
                        "status": "pending",
                        "output_path": "assets/generated/missing.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run("validate_image_manifest.js", str(manifest))

    assert result.returncode == 1
    assert "generate entry is unresolved" in result.stdout


def test_sync_image_manifest_status_marks_existing_assets_once(tmp_path: Path) -> None:
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "new.png").write_bytes(b"new-image")
    (generated / "fixed.png").write_bytes(b"fixed-image")
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "auto",
                "image_plan": [
                    {
                        "slide": 1,
                        "decision": "generate",
                        "status": "pending",
                        "decision_reason": "cover visual",
                        "output_path": "assets/generated/new.png",
                    },
                    {
                        "slide": 2,
                        "decision": "use_existing",
                        "status": "pending",
                        "output_path": "assets/generated/fixed.png",
                    },
                    {
                        "slide": 3,
                        "decision": "skip",
                        "status": "skipped",
                        "output_path": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    first = _run("sync_image_manifest_status.js", str(manifest))
    second = _run("sync_image_manifest_status.js", str(manifest))

    assert first.returncode == 0, first.stderr
    assert '"changed": 2' in first.stdout
    assert second.returncode == 0, second.stderr
    assert '"changed": 0' in second.stdout
    payload = json.loads(manifest.read_text())
    assert payload["image_plan"][0]["status"] == "generated"
    assert payload["image_plan"][0]["decision_reason"] == "cover visual"
    assert payload["image_plan"][1]["status"] == "ready"
    assert payload["image_plan"][2]["status"] == "skipped"


def test_sync_image_manifest_status_rejects_missing_generated_asset(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True)
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "image_plan": [
                    {
                        "slide": 1,
                        "decision": "generate",
                        "status": "pending",
                        "output_path": "assets/generated/missing.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run("sync_image_manifest_status.js", str(manifest))

    assert result.returncode == 1
    assert "Cannot mark unresolved generated image" in result.stderr
    assert json.loads(manifest.read_text())["image_plan"][0]["status"] == "pending"


def test_comparison_layout_uses_flat_editorial_rules() -> None:
    css = (SKILL_DIR / "runtime" / "deck.css").read_text(encoding="utf-8")
    column_block = css.split(".comparison-column {", 1)[1].split("}", 1)[0]
    right_block = css.split(".comparison-right {", 1)[1].split("}", 1)[0]

    assert "border-radius: 0;" in column_block
    assert "border-top: var(--deck-border-width) solid var(--deck-border);" in column_block
    assert "border-bottom: var(--deck-border-width) solid var(--deck-border);" in column_block
    assert "background: transparent;" in column_block
    assert "var(--deck-primary)" not in right_block
    assert "background: transparent;" in right_block


def test_auto_image_manifest_rejects_skipping_a_required_cover(tmp_path: Path) -> None:
    generated = tmp_path / "assets" / "generated"
    generated.mkdir(parents=True)
    manifest = generated / "manifest.json"
    payload = {
        "mode": "auto",
        "image_plan": [
            {
                "slide": 1,
                "required": True,
                "decision": "skip",
                "status": "skipped",
                "output_path": None,
            }
        ],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    skipped = _run("validate_image_manifest.js", str(manifest))

    assert skipped.returncode == 1
    assert "required image entry is unresolved" in skipped.stdout

    fixed = generated / "fixed-cover.png"
    fixed.write_bytes(b"fixed-cover")
    payload["image_plan"][0].update(
        {
            "decision": "use_existing",
            "status": "fixed",
            "output_path": "assets/generated/fixed-cover.png",
        }
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    accepted = _run("validate_image_manifest.js", str(manifest))

    assert accepted.returncode == 0, accepted.stdout


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda deck: deck["slides"][0]["props"].__setitem__("unknown", "x"),
            "unknown field(s): unknown",
        ),
        (
            lambda deck: deck["slides"][0]["props"].__setitem__("title", "超" * 73),
            "exceeds maxChars 72",
        ),
        (
            lambda deck: deck["slides"][0]["props"].__setitem__(
                "hero", {"src": "https://example.com/hero.png", "alt": "hero"}
            ),
            "remote or executable URLs are not allowed",
        ),
        (
            lambda deck: deck["slides"][0].__setitem__(
                "layout_drafts", {"not-a-layout": {}}
            ),
            "layout_drafts.not-a-layout: unknown layout",
        ),
        (
            lambda deck: deck["slides"][0].__setitem__(
                "background", {"src": "https://example.com/background.png"}
            ),
            "background.src: remote or executable URLs are not allowed",
        ),
        (
            lambda deck: deck["slides"][0].__setitem__(
                "background", {"src": "assets/background.png", "origin": "magic"}
            ),
            "background.origin: expected one of generated, asset, uploaded",
        ),
    ],
)
def test_deck_validation_rejects_unsafe_or_out_of_contract_props(
    tmp_path: Path,
    mutate,
    expected_error: str,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    mutate(deck)
    deck_path = tmp_path / "invalid.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert expected_error in result.stdout


def test_explicit_palette_and_geometry_become_hard_design_contract(
    tmp_path: Path,
) -> None:
    outline = {
        "deck_goal": "解释结构化思维并给出行动计划",
        "audience": "公司内部团队",
        "source_mode": "user_provided",
        "tone": "专业、克制、浅色商务",
        "storyline": "原则到行动",
        "design_requirements": {
            "palette": "配色以深蓝、米白和少量橙色为主",
        },
        "slides": [
            {
                "page": 1,
                "title": "四个基本原则",
                "message": "顶层结论统领下层支撑。",
                "bullets": ["结论先行", "以上统下", "归类分组", "逻辑递进"],
                "layout": "pyramid",
                "visual": "可编辑金字塔结构：上方深蓝顶层块写‘结论先行’，下方三个并列支撑块依次写‘以上统下’‘归类分组’‘逻辑递进’。",
                "evidence": [],
            },
            {
                "page": 2,
                "title": "五步行动清单",
                "message": "把方法落实到每周工作。",
                "bullets": ["先写结论", "拆分问题", "检查 MECE", "组织汇报", "每周复盘"],
                "layout": "横向五步流程页，适合按顺序展示行动清单。",
                "visual": "五个可编辑流程卡片从左到右排列，用深蓝连接线串联，橙色用于编号强调。",
                "evidence": [],
            },
        ],
    }
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作两页PPT，配色以深蓝、米白和少量橙色为主。".encode()
    ).decode()

    scaffold = _run(
        "inspect_deck_contract.js",
        "technical-diagram-v1",
        "timeline-horizontal-v1",
        "--theme",
        "auto",
        "--title",
        "结构化思维",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert [slide["layout_id"] for slide in deck["slides"]] == [
        "pyramid-hierarchy-v1",
        "cards-grid-v1",
    ]
    assert len(deck["slides"][0]["props"]["items"]) == 4
    assert len(deck["slides"][1]["props"]["items"]) == 5
    palette = deck["design_contract"]["palette"]
    assert palette["background"]["value"] == "#F4EFE4"
    assert palette["primary"]["value"] == "#173B63"
    assert palette["accent"]["value"] == "#D97706"
    assert palette["accent_usage"] == "sparse"
    assert deck["design_contract"]["slides"]["slide-01"] == {
        "visual_kind": "pyramid",
        "source": "explicit",
        "item_count": 4,
        "item_dimension": "items",
        "direction": "top-down",
        "relationship": "one-to-many",
        "hierarchy_depth": 2,
    }
    assert deck["design_contract"]["slides"]["slide-02"] == {
        "visual_kind": "numbered-actions",
        "source": "explicit",
        "item_count": 5,
        "item_dimension": "cards",
        "direction": "left-to-right",
        "relationship": "ordered",
    }
    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "--deck-bg: #F4EFE4" in html
    assert "--deck-primary: #173B63" in html
    assert "--deck-accent-color: #D97706" in html
    assert 'data-deck-palette-accent-usage="sparse"' in html
    assert 'data-layout-id="pyramid-hierarchy-v1"' in html


def test_explicit_design_contract_blocks_approximate_layout(
    tmp_path: Path,
) -> None:
    deck = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    first = deck["slides"][0]
    deck["design_contract"] = {
        "version": 1,
        "slides": {
            first["id"]: {
                "visual_kind": "pyramid",
                "source": "explicit",
            }
        },
    }
    deck_path = tmp_path / "invalid-design.json"
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")

    result = _run("validate_deck_spec.js", str(deck_path))

    assert result.returncode == 1
    assert "pyramid" in result.stdout
    assert "is not supported by" in result.stdout


def test_original_exact_hex_palette_wins_over_outline_color_paraphrases(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["design_requirements"] = {
        "palette": "深色背景、白色正文、少量红色点缀",
    }
    outline["slides"][0].update(
        {
            "title": "经营决策会",
            "layout": "cover",
            "visual": "纯文字封面，红色仅用于强调",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        (
            "制作一页纯文字 PPT，不使用图片。精确配色依次为 "
            "#111827 / #F9FAFB / #EF4444，不得近似替换。"
        ).encode()
    ).decode()

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    palette = deck["design_contract"]["palette"]
    assert palette["background"]["value"] == "#111827"
    assert palette["primary"]["value"] == "#F9FAFB"
    assert palette["accent"]["value"] == "#EF4444"
    assert palette["requested"] == ["#111827", "#F9FAFB", "#EF4444"]

    html_path = tmp_path / "index.html"
    rendered = _run("render_deck_html.js", str(deck_path), "--out", str(html_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = html_path.read_text(encoding="utf-8")
    assert "--deck-bg: #111827" in html
    assert "--deck-primary: #F9FAFB" in html
    assert "--deck-text: #F9FAFB" in html
    assert "--deck-alt-bg: #111827" in html
    assert "--deck-alt-text: #F9FAFB" in html
    assert "--deck-accent-color: #EF4444" in html


def test_natural_black_white_neon_palette_is_preserved_without_palette_keyword(
    tmp_path: Path,
) -> None:
    deck_path = tmp_path / "deck.json"
    prompt = (
        "现代编辑设计，强留白，黑白为主（背景白 #FFFFFF、正文黑 #111111），"
        "荧光绿 #39FF14 点缀强调。"
    )

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--title",
        prompt,
        "--fact",
        prompt,
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    palette = json.loads(deck_path.read_text(encoding="utf-8"))["design_contract"]["palette"]
    assert palette["background"]["value"] == "#FFFFFF"
    assert palette["primary"]["value"] == "#111111"
    assert palette["accent"]["value"] == "#39FF14"


def test_slide_local_text_only_cover_does_not_disable_project_images(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=2, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "年度作品集",
            "layout": "cover",
            "visual": "纯文字编辑式封面，强留白",
        }
    )
    outline["slides"][1].update(
        {
            "title": "品牌项目 A",
            "layout": "project case",
            "visual": "项目案例：抽象几何主视觉缩略图 + 一句话定位 + 3 项醒目数字",
            "bullets": ["品牌触点 42 处", "品牌认知 +68%", "上线周期 9 周"],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        (
            "制作一份 2 页作品集。页面：1 纯文字封面；2 品牌项目 A，"
            "使用抽象几何主视觉缩略图和 3 项醒目数字。"
        ).encode()
    ).decode()

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "project-case-study-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["generation_forbidden"] is False
    assert [entry["decision"] for entry in manifest["image_plan"]] == ["skip", "generate"]


def test_project_thumbnail_remains_generated_when_visual_also_mentions_metrics(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=2, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "年度作品集",
            "layout": "cover-editorial",
            "visual": "纯文字封面，强留白",
        }
    )
    outline["slides"][1].update(
        {
            "title": "品牌项目 A",
            "layout": "project-case-study",
            "visual": "缩略图区域 + 一句话定位 + 醒目数字（项目指标，待补充）",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "project-case-study-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(encoding="utf-8")
    )
    assert [entry["decision"] for entry in manifest["image_plan"]] == ["skip", "generate"]


def test_modern_editorial_brief_outranks_one_kpi_slide_for_deck_theme(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=2, source_mode="user_provided")
    outline.update(
        {
            "deck_goal": "以现代编辑设计呈现年度作品集",
            "tone": "现代编辑设计，强留白，克制而有力",
            "design_requirements": {
                "palette": "背景白 #FFFFFF、正文黑 #111111、荧光绿 #39FF14 点缀强调"
            },
        }
    )
    outline["slides"][1].update(
        {
            "title": "年度总览",
            "layout": "dashboard",
            "visual": "KPI strip，4 项年度关键数字",
            "bullets": ["项目 24 个", "客户 16 家", "奖项 7 项", "团队 12 人"],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "kpi-grid-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    assert json.loads(deck_path.read_text(encoding="utf-8"))["theme_id"] == "soft-editorial"


def test_page_layout_tokens_do_not_override_semantic_portfolio_theme(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=2, source_mode="user_provided")
    outline.update(
        {
            "deck_goal": "呈现年度品牌、空间与数字产品三类代表项目",
            "storyline": "从封面与年度总览开篇，再展示代表项目",
        }
    )
    outline["slides"][0].update(
        {
            "title": "NOON Studio 2025 年度作品集",
            "layout": "cover-editorial",
            "visual": "强留白黑白封面，荧光绿 #39FF14 点缀标题与年份",
        }
    )
    outline["slides"][1].update(
        {
            "title": "年度总览",
            "layout": "dashboard",
            "visual": "KPI strip，4 项年度关键数字",
            "bullets": ["项目 24 个", "客户 16 家", "奖项 7 项", "团队 12 人"],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "kpi-grid-v1",
        "--theme",
        "auto",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    assert json.loads(deck_path.read_text(encoding="utf-8"))["theme_id"] == "block-frame"


def test_readability_css_keeps_folios_visible_and_four_kpis_balanced() -> None:
    deck_css = (SKILL_DIR / "runtime" / "deck.css").read_text(encoding="utf-8")
    composition_css = (SKILL_DIR / "runtime" / "composition.css").read_text(encoding="utf-8")

    deck_page_rule = deck_css.split(".deck-page {", 1)[1].split("}", 1)[0]
    assert "z-index: 3" in deck_page_rule
    assert "pointer-events: none" in deck_page_rule
    four_kpi_rule = (
        'body[data-deck-composition="literary-minimal"]'
        '[data-deck-composition-variant="asymmetric-column"] .kpis-count-4 .kpi-grid'
    )
    asymmetric_grid_rule = (
        'body[data-deck-composition="literary-minimal"]'
        '[data-deck-composition-variant="asymmetric-column"] .kpi-grid,'
    )
    assert four_kpi_rule in composition_css
    assert composition_css.rfind(four_kpi_rule) > composition_css.rfind(asymmetric_grid_rule)
    assert "cover-title-medium" in composition_css
    assert "cover-title-long" in composition_css


def test_exact_one_page_request_overrides_default_outline_minimum(
    tmp_path: Path,
) -> None:
    one_page_path = tmp_path / "one-page.json"
    _write_outline(one_page_path, page_count=1, source_mode="user_provided")
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作1页《AI治理白皮书》演示文稿；第1页为纯文字封面。".encode()
    ).decode()

    accepted = _run("validate_outline.js", str(one_page_path), env=env)

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    report = json.loads(accepted.stdout)
    assert report["slideCount"] == 1
    assert report["pageCountContract"] == {"minimum": 1, "maximum": 1}

    three_page_path = tmp_path / "three-page.json"
    _write_outline(three_page_path, page_count=3, source_mode="user_provided")
    rejected = _run("validate_outline.js", str(three_page_path), env=env)

    assert rejected.returncode == 1
    assert "Too many slides: 3; expected at most 1" in rejected.stdout


def test_explicit_total_page_count_outranks_host_range_and_ordinal_slide_span(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path, page_count=6, source_mode="user_provided")
    env = os.environ.copy()
    source_text = """<presentation_config schema_version=\"1\" confirmed_by=\"user\">
{"page_count":{"id":"page_count_5_10","label":"5-10页","source":"explicit"}}
</presentation_config>

用户问题：制作一份 6 页中文 PPT。页面：1 封面；2 总览；第 3–5 页为项目案例；6 团队与联系。
"""
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(source_text.encode()).decode()

    result = _run("validate_outline.js", str(outline_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["pageCountContract"] == {"minimum": 6, "maximum": 6}


def test_positional_preview_span_does_not_override_total_page_count(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path, page_count=8, source_mode="user_provided")
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        (
            "继续。从现有 outline.json 恢复：先完成封面、目录和前 2 页内容，"
            "再调用结构化用户决策卡；完整版 8 页为默认项。"
        ).encode()
    ).decode()

    result = _run("validate_outline.js", str(outline_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["pageCountContract"] == {"minimum": 8, "maximum": 8}


def test_positional_preview_span_alone_is_not_a_total_page_count(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    _write_outline(outline_path, page_count=8, source_mode="user_provided")
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "先完成封面、目录和前两页内容，再让我预览。".encode()
    ).decode()

    result = _run("validate_outline.js", str(outline_path), env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["pageCountContract"] is None


def test_no_image_instruction_blocks_technical_cover_generation_and_is_qa_enforced(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["deck_goal"] = "解释企业 AI 助手技术架构"
    outline["slides"][0].update(
        {
            "title": "企业 AI 助手技术架构",
            "layout": "cover",
            "visual": "技术架构线框封面，不使用图片，全部采用可编辑形状",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"
    env = os.environ.copy()
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作技术架构 PPT，不要生成图片，全部使用可编辑形状。".encode()
    ).decode()

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--outline",
        str(outline_path),
        "--image-mode",
        "auto",
        "--out",
        str(deck_path),
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["generation_forbidden"] is True
    assert manifest["image_plan"][0]["decision"] == "skip"
    assert manifest["image_plan"][0]["required"] is False
    assert manifest["image_plan"][0]["allowed_strategies"] == ["skip"]

    valid = _run(
        "validate_image_manifest.js",
        str(manifest_path),
        "--deck",
        str(deck_path),
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr

    generated = tmp_path / "assets" / "generated" / "forbidden.png"
    generated.write_bytes(b"not-a-real-image")
    manifest["image_plan"][0].update(
        {
            "decision": "generate",
            "status": "generated",
            "output_path": "assets/generated/forbidden.png",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = _run("validate_image_manifest.js", str(manifest_path))
    assert rejected.returncode == 1
    assert "generation_forbidden" in rejected.stdout


@pytest.mark.parametrize(
    ("policy", "decision_reason"),
    [
        ("forbidden", "the user explicitly forbids images for this presentation"),
        ("unavailable", "image generation service unavailable"),
    ],
)
def test_rebase_image_policy_converts_existing_required_media_deck_idempotently(
    tmp_path: Path,
    policy: str,
    decision_reason: str,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=2,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "新员工入职指南",
            "message": "快速理解团队、流程与协作方式",
            "layout": "cover",
            "visual": "简洁文字封面",
            "evidence": [],
        }
    )
    outline["slides"][1].update(
        {
            "title": "我们如何协作",
            "message": "用透明信息与明确责任减少内耗",
            "layout": "image story",
            "visual": "一张团队协作场景图配核心叙事",
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["BOX_AGENT_OUTPUT_DIR"] = str(tmp_path)
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作新员工入职指南 PPT，介绍团队协作方式。".encode()
    ).decode()
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "image-hero-split-v1",
        "--outline",
        "outline.json",
        "--out",
        "deck.json",
        cwd=tmp_path,
        env=env,
    )
    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr

    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    deck["slides"][0]["props"]["title"] = outline["slides"][0]["title"]
    deck["slides"][0]["props"]["subtitle"] = outline["slides"][0]["message"]
    deck["slides"][1]["props"]["title"] = outline["slides"][1]["title"]
    deck["slides"][1]["props"]["body"] = outline["slides"][1]["message"]
    deck_path.write_text(
        json.dumps(deck, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    original_deck = json.loads(deck_path.read_text(encoding="utf-8"))
    original_manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    rebased = _run(
        "rebase_image_policy.js",
        "deck.json",
        "--manifest",
        "assets/generated/manifest.json",
        "--policy",
        policy,
        cwd=tmp_path,
        env=env,
    )
    assert rebased.returncode == 0, rebased.stdout + rebased.stderr
    assert json.loads(rebased.stdout)["changed"] is True

    manifest_path = tmp_path / "assets" / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["generation_forbidden"] is (policy == "forbidden")
    assert all(item["decision"] == "skip" for item in manifest["image_plan"])
    assert all(item["status"] == "skipped" for item in manifest["image_plan"])
    assert all(item["required"] is False for item in manifest["image_plan"])
    assert all(
        item["decision_reason"] == decision_reason for item in manifest["image_plan"]
    )
    if policy == "unavailable":
        assert manifest["image_generation_unavailable"] is True
        recovery = manifest["image_unavailable_recovery"]
        assert recovery["schema_version"] == 1
        assert recovery["deck"] == original_deck
        assert recovery["image_plan"] == original_manifest["image_plan"]
    else:
        assert "image_generation_unavailable" not in manifest
        assert "image_unavailable_recovery" not in manifest

    rebased_deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert rebased_deck["slides"][1]["layout_id"] == "statement-focus-v1"
    assert rebased_deck["slides"][1]["props"]["statement"] == "我们如何协作"
    assert "透明信息" in rebased_deck["slides"][1]["props"]["support"]

    first_deck = deck_path.read_text(encoding="utf-8")
    first_manifest = manifest_path.read_text(encoding="utf-8")
    repeated = _run(
        "rebase_image_policy.js",
        "deck.json",
        "--manifest",
        "assets/generated/manifest.json",
        "--policy",
        policy,
        cwd=tmp_path,
        env=env,
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)["changed"] is False
    assert deck_path.read_text(encoding="utf-8") == first_deck
    assert manifest_path.read_text(encoding="utf-8") == first_manifest

    image_qa = _run(
        "validate_image_manifest.js",
        "assets/generated/manifest.json",
        "--deck",
        "deck.json",
        cwd=tmp_path,
        env=env,
    )
    assert image_qa.returncode == 0, image_qa.stdout + image_qa.stderr

    deck_qa = _run("validate_deck_spec.js", "deck.json", cwd=tmp_path, env=env)
    assert deck_qa.returncode == 0, deck_qa.stdout + deck_qa.stderr

    rendered = _run(
        "render_deck_html.js",
        "deck.json",
        "--out",
        "index.html",
        cwd=tmp_path,
        env=env,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert (tmp_path / "index.html").is_file()

    if policy == "unavailable":
        restored = _run(
            "rebase_image_policy.js",
            "deck.json",
            "--manifest",
            "assets/generated/manifest.json",
            "--policy",
            "retry",
            cwd=tmp_path,
            env=env,
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert json.loads(restored.stdout)["changed"] is True
        assert json.loads(deck_path.read_text(encoding="utf-8")) == original_deck
        restored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert restored_manifest["image_plan"] == original_manifest["image_plan"]
        assert restored_manifest["generation_forbidden"] is False
        assert "image_generation_unavailable" not in restored_manifest
        assert "image_unavailable_recovery" not in restored_manifest
        assert "image_service" not in restored_manifest


def test_no_images_scaffold_uses_registered_required_media_fallback(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(
        outline_path,
        page_count=1,
        source_mode="user_provided",
    )
    outline["slides"][0].update(
        {
            "title": "团队协作方式",
            "message": "透明信息与明确责任减少协作内耗",
            "layout": "image story",
            "visual": "团队协作场景配核心叙事",
            "evidence": [],
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["BOX_AGENT_OUTPUT_DIR"] = str(tmp_path)
    env["BOX_AGENT_SOURCE_TEXT_B64"] = base64.b64encode(
        "制作无图版团队协作 PPT，不要生成图片。".encode()
    ).decode()

    scaffold = _run(
        "inspect_deck_contract.js",
        "image-hero-split-v1",
        "--no-images",
        "--outline",
        "outline.json",
        "--out",
        "deck.json",
        cwd=tmp_path,
        env=env,
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads((tmp_path / "deck.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (tmp_path / "assets" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert deck["slides"][0]["layout_id"] == "statement-focus-v1"
    assert manifest["generation_forbidden"] is True
    assert manifest["image_plan"][0]["decision"] == "skip"
    assert manifest["image_plan"][0]["required"] is False
    assert manifest["image_plan"][0]["allowed_strategies"] == ["skip"]


def test_scaffold_normalizes_2x2_priority_matrix_to_editable_quadrant_layout(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "问题优先级矩阵",
            "message": "按影响和紧急程度划分四类问题。",
            "bullets": ["支付失败", "新手激活", "消息延迟", "视觉微调"],
            "layout": "priority_matrix_2x2",
            "visual": (
                "2×2 可编辑矩阵：高影响高紧急=支付失败，高影响低紧急=新手激活，"
                "低影响高紧急=消息延迟，低影响低紧急=视觉微调。"
            ),
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "table-data-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    slide = deck["slides"][0]
    assert slide["layout_id"] == "quadrant-matrix-v1"
    assert len(slide["props"]["items"]) == 4
    assert deck["design_contract"]["slides"]["slide-01"] == {
        "visual_kind": "quadrant",
        "source": "explicit",
        "item_count": 4,
        "item_dimension": "items",
        "direction": "x-y",
        "relationship": "matrix",
    }
    slide["props"].update(
        {
            "title": outline["slides"][0]["title"],
            "subtitle": outline["slides"][0]["message"],
            "items": [
                {"kicker": "象限", "title": item, "body": "优先级判断"}
                for item in outline["slides"][0]["bullets"]
            ],
        }
    )
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
    rendered = _run(
        "render_deck_html.js",
        str(deck_path),
        "--out",
        str(tmp_path / "index.html"),
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'data-layout-id="quadrant-matrix-v1"' in html
    assert "quadrant-grid" in html
    validation = _run("validate_deck_spec.js", str(deck_path))
    assert validation.returncode == 0, validation.stdout + validation.stderr
    self_check = _run(
        "html_self_check.js",
        str(tmp_path / "index.html"),
        "--dom-to-pptx",
        "--allow-local-images",
        "--report",
        str(tmp_path / "qa" / "html_self_check.json"),
    )
    assert self_check.returncode == 0, self_check.stdout + self_check.stderr


def test_scaffold_normalizes_four_point_statement_summary_to_cards(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "总结",
            "message": "四条管理原则形成年度运营闭环。",
            "bullets": ["目标牵引", "节奏推进", "项目聚焦", "生命周期闭环"],
            "layout": "summary_actions",
            "visual": "总结页：四条管理原则加底部行动提示，全部采用可编辑文本框。",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "statement-focus-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "cards-grid-v1"
    assert len(deck["slides"][0]["props"]["items"]) == 4


def test_typography_cover_role_wins_over_roadmap_words_in_deck_title(
    tmp_path: Path,
) -> None:
    outline_path = tmp_path / "outline.json"
    outline = _write_outline(outline_path, page_count=1, source_mode="user_provided")
    outline["slides"][0].update(
        {
            "title": "客户成功年度运营路线图",
            "message": "建立贯穿全年的运营节奏。",
            "layout": "cover_text_only",
            "visual": "纯文字封面：大标题、副标题、年度路线图定位说明。",
        }
    )
    outline_path.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    deck_path = tmp_path / "deck.json"

    scaffold = _run(
        "inspect_deck_contract.js",
        "cover-editorial-v1",
        "--outline",
        str(outline_path),
        "--out",
        str(deck_path),
    )

    assert scaffold.returncode == 0, scaffold.stdout + scaffold.stderr
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["layout_id"] == "cover-editorial-v1"
