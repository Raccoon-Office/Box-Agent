"""Independent design handoff, immutable AI choices, and seed-free documents."""

import json
import hashlib
import time
import uuid
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.pptx_test_support import skip_unavailable_pptx_runtime

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "box_agent/skills/document-skills/pptx"
NODE = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")


def run(script, *args, cwd):
    if not NODE:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [NODE, str(SKILL / "scripts" / script), *map(str, args)],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    skip_unavailable_pptx_runtime(result)
    return result


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def record_response(root, decision, *, workspace=None, read_brief=True):
    data = json.loads((root / "design_input.json").read_text())
    if "palette" not in decision:
        decision = {**decision, "palette": {"background": "#F4EFE4", "text": "#111111", "primary": "#173B63", "accent": "#B45309", "secondary": "#357B75", "accent_usage": "sparse"}}
    elif isinstance(decision["palette"], dict):
        decision = {**decision, "palette": {"secondary": "#357B75", "accent_usage": "sparse", **decision["palette"]}}
    decision = {"visual_requirements": {"canvas": "any", "heading": "any", "display_font": "any", "body_font": "any", "shadow": "any", "allow_plain_fallback": True}, **decision}
    brief = Path(data["request_file"])
    sid = "subagent-" + uuid.uuid4().hex
    sessions = Path(os.environ["BOX_AGENT_HOME"]) / "sessions"
    directory = sessions / hashlib.sha256(sid.encode()).hexdigest()
    directory.mkdir(parents=True)
    now = max(int(time.time() * 1000), data["request_created_at"] + 1)
    rows = [
        {"type": "session", "version": 1, "id": sid, "createdAt": now,
         "cwd": str(workspace or root), "parentSession": "cli-test", "origin": "subagent"},
        {"type": "user/message", "data": {"content": f"Read the designer role and {brief}"}},
    ]
    if read_brief:
        resources = [brief, *[Path(item["path"]) for item in json.loads(brief.read_text()).get("required_read_files", [])]]
        for resource in resources:
            line_count = len(resource.read_text().splitlines())
            rows.append({"type": "tool/result", "data": {"result": {"success": True, "rawOutput": {
                "context_resource": {"resource_id": str(resource), "content_version": hashlib.sha256(resource.read_bytes()).hexdigest(),
                                     "start_line": 1, "end_line": line_count, "total_lines": line_count}}}}})
    rows.extend([
        {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}}},
        {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
    ])
    (directory / "session.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    return sid


def import_plan(root, plan):
    decision = {key: plan[key] for key in ["theme_id", "palette", "reason"] if key in plan}
    decision["slides"] = [{"layout_id": slide["layout_id"], "visual_options": slide.get("visual_options", {})} for slide in plan["slides"]]
    record_response(root, decision)
    return run("design_plan.js", "accept", "design_input.json", cwd=root)


@pytest.fixture
def design_case(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / ".profile"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".box-agent/browsers")))
    outline = {
        "deck_goal": "演示已提供的材料", "audience": "一般读者", "source_mode": "user_provided",
        "storyline": "按原始顺序介绍三个互相独立的主题，每页保留自己的主要观点和支持材料。",
        "slides": [
            {"page": i, "title": f"Topic {chr(64+i)}", "message": f"Main idea for topic {chr(64+i)}",
             "bullets": [f"Evidence alpha for {chr(64+i)}", f"Evidence bravo for {chr(64+i)}", f"Evidence charlie for {chr(64+i)}"],
             "layout": "cards", "visual": "卡片展示支持材料", "evidence": []}
            for i in range(1, 4)
        ],
    }
    write(tmp_path / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=tmp_path)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    data = json.loads((tmp_path / "design_input.json").read_text())
    plan = {
        "schema_version": 1, "input_hash": data["input_hash"], "catalog_hash": data["catalog_hash"],
        "theme_id": "blue-professional@rail-grid", "reason": "清晰的机构风格承载顺序明确的主题材料",
        "slides": [{"page": i, "layout_id": "cards-grid-v1", "visual_options": {"composition": "open"},
                    "content_bindings": {"title": ["title"], "items": ["message", "bullets"]}}
                   for i in range(1, 4)],
    }
    accepted = import_plan(tmp_path, plan)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    plan = json.loads((tmp_path / "design_plan.json").read_text())
    detail = Path(data["request_file"]).parent / "catalog"
    data["catalog"] = {kind: [json.loads(p.read_text()) for p in sorted((detail / kind).glob("*.json"))] for kind in ["themes", "layouts"]}
    return tmp_path, outline, data, plan


def scaffold(root):
    return run("inspect_deck_contract.js", "--design-plan", "design_plan.json", "--out", "deck.json", cwd=root)


def test_validated_design_scaffolds_exact_choices_without_parent_catalog(design_case):
    root, _, _, _ = design_case
    result = scaffold(root)
    assert result.returncode == 0, result.stdout + result.stderr
    deck = json.loads((root / "deck.json").read_text())
    assert deck["design"] == {"version": 2, "family": "institutional-grid", "variant": "rail-grid"}
    assert all(slide["props"]["composition"] == "open" for slide in deck["slides"])
    result = json.loads(result.stdout)
    assert "available_theme_ids" not in result
    assert "composition" not in result["slides"][0]["content_fields"]
    assert result["slides"][0]["content_bindings"]["items"] == ["bullets"]


def test_nested_workspace_relation_is_accepted():
    result = subprocess.run(
        [NODE, "-e", "const s=require('./scripts/design_response_source.js'); console.log(JSON.stringify({parent:s.workspaceRelated('/tmp/task','/tmp/task/deck'), child:s.workspaceRelated('/tmp/task/deck','/tmp/task'), sibling:s.workspaceRelated('/tmp/task-a','/tmp/task-b')}));"],
        cwd=SKILL, capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data == {"parent": True, "child": False, "sibling": False}


@pytest.mark.parametrize("mutation,field", [
    (lambda p: p.update(theme_id="not-registered"), "theme_id"),
    (lambda p: p.update(family="poster-asymmetric"), "family"),
    (lambda p: p.update(seed="seed-1234"), "seed"),
    (lambda p: p.update(palette={"background": "#FFFFFF", "text": "#FFFFFF", "primary": "#222222", "accent": "#333333"}), "palette"),
    (lambda p: p["slides"][0]["visual_options"].update(title="invented content"), "visual_options.title"),
    (lambda p: p["slides"][0]["content_bindings"].update(items=["bullets.99"]), "content_bindings.items"),
])
def test_invalid_design_returns_field_error_without_fallback(design_case, mutation, field):
    root, _, _, plan = design_case
    mutation(plan)
    write(root / "design_plan.json", plan)
    result = scaffold(root)
    assert result.returncode != 0
    assert field in result.stderr
    assert not (root / "deck.json").exists()


def test_unchanged_input_reuses_design_and_changed_content_invalidates_it(design_case):
    root, outline, _, _ = design_case
    result = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert json.loads(result.stdout)["reusable"] is True
    outline["slides"][0]["bullets"].append("必须保留的新增条目")
    write(root / "outline.json", outline)
    stale = scaffold(root)
    assert stale.returncode != 0
    assert "stale" in stale.stderr
    refreshed = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert json.loads(refreshed.stdout)["reusable"] is False


def test_main_cannot_override_designer_with_cli_flags(design_case):
    root, _, _, _ = design_case
    result = run("inspect_deck_contract.js", "--design-plan", "design_plan.json", "--family", "editorial-spread", "--out", "deck.json", cwd=root)
    assert result.returncode != 0
    assert "owns all visual choices" in result.stderr


def test_content_patch_preserves_design_and_rejects_visual_mutation(design_case):
    root, outline, _, _ = design_case
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    before = (root / "deck.json").read_bytes()
    write(root / "deck.patch.json", {"slides": {"slide-01": {"props": {"composition": "standard"}}}})
    rejected = run("apply_deck_patch.js", "deck.json", "deck.patch.json", cwd=root)
    assert rejected.returncode != 0
    assert "locked design field" in rejected.stderr
    assert (root / "deck.json").read_bytes() == before
    patch = {"slides": {
        f"slide-{i:02d}": {"props": {"title": slide["title"], "items": [
            {"kicker": "A", "title": slide["message"], "body": slide["bullets"][0]},
            {"kicker": "B", "title": slide["bullets"][1], "body": ""},
            {"kicker": "C", "title": slide["bullets"][2], "body": ""},
        ]}} for i, slide in enumerate(outline["slides"], 1)
    }}
    write(root / "deck.patch.json", patch)
    applied = run("apply_deck_patch.js", "deck.json", "deck.patch.json", cwd=root)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    deck = json.loads((root / "deck.json").read_text())
    assert deck["design"] == json.loads(before)["design"]
    assert deck["design_plan"] == json.loads(before)["design_plan"]
    write(root / "qa/visual_inspection.json", {"ok": False, "status": "unverified", "reason": "model cannot view images"})
    finalized = run("finalize_controlled_deck.js", "deck.json", "--out", "index.html", cwd=root)
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    html = (root / "index.html").read_text()
    assert "data-deck-design-seed" not in html
    report = json.loads((root / "qa/design_review_check.json").read_text())
    assert report["review_performed"] is False
    assert report["required"] is False


def test_legacy_seed_document_preserves_saved_variant_on_render(tmp_path):
    deck = json.loads((SKILL / "examples/controlled-deck/deck.json").read_text())
    deck["theme_id"] = "blue-professional"
    deck["design"] = {"version": 1, "seed": "legacy_seed", "family": "institutional-grid", "variant": "ledger-grid"}
    write(tmp_path / "deck.json", deck)
    result = run("render_deck_html.js", "deck.json", "--out", "index.html", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    html = (tmp_path / "index.html").read_text()
    assert 'data-deck-composition-variant="ledger-grid"' in html
    assert "data-deck-design-seed" not in html
    import re
    model = json.loads(re.search(r'id="deck-document">\s*([\s\S]*?)</script>', html)[1])
    assert model["design"] == {"version": 2, "family": "institutional-grid", "variant": "ledger-grid"}


def test_plan_revision_is_the_only_automatic_redesign_path(design_case):
    root, _, _, plan = design_case
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    before = json.loads((root / "deck.json").read_text())
    for slide, page in zip(before["slides"], json.loads((root / "outline.json").read_text())["slides"]):
        slide["props"]["title"] = page["title"]
        slide["props"]["items"][0]["body"] = page["message"]
    write(root / "deck.json", before)
    write(root / "redesign.json", {"design": {"variant": "ledger-grid"}})
    bypass = run("apply_deck_redesign.js", "deck.json", "redesign.json", cwd=root)
    assert bypass.returncode != 0
    assert "design_plan.js apply" in bypass.stderr
    plan["theme_id"] = "blue-professional@ledger-grid"
    accepted = import_plan(root, plan)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    applied = run("design_plan.js", "apply", "design_plan.json", "--deck", "deck.json", cwd=root)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    after = json.loads((root / "deck.json").read_text())
    assert after["design"]["variant"] == "ledger-grid"
    assert after["slides"] == before["slides"]
    assert after["truth_contract"] == before["truth_contract"]
    assert after["design_plan"]["plan_hash"] != before["design_plan"]["plan_hash"]


def test_plan_validation_catches_layout_capacity_before_scaffold(design_case):
    root, outline, _, plan = design_case
    outline["slides"][1]["visual"] = "四象限矩阵，横轴影响程度，纵轴紧急程度"
    outline["slides"][1]["layout"] = "matrix"
    write(root / "outline.json", outline)
    prepare = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepare.returncode == 0, prepare.stderr
    data = json.loads((root / "design_input.json").read_text())
    plan.update(input_hash=data["input_hash"], catalog_hash=data["catalog_hash"])
    write(root / "design_plan.json", plan)
    result = import_plan(root, plan)
    assert result.returncode != 0
    assert "design_plan.slides.1.layout_id" in result.stdout + result.stderr
    assert not (root / "deck.json").exists()


def test_user_can_change_plan_owned_layout_and_save_without_losing_design(design_case):
    root, _, _, _ = design_case
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    rendered = run("render_deck_html.js", "deck.json", "--out", "index.html", cwd=root)
    assert rendered.returncode == 0, rendered.stderr
    probe = root / "edit-plan.cjs"
    probe.write_text(r'''
const fs = require('fs'), path = require('path'), os = require('os'), Module = require('module');
const {pathToFileURL} = require('url');
const scripts = process.argv[2], html = process.argv[3];
const host = require(path.join(scripts, 'playwright_host.js'));
host.ensurePlaywrightBrowsersPath();
const prefix = process.env.BOX_AGENT_NODE_PREFIX || process.env.BOX_AGENT_RUNTIME_PREFIX ||
  (process.platform === 'darwin' ? path.join(os.homedir(), 'Library/Application Support/office-raccoon') :
    process.platform === 'win32' ? path.join(process.env.APPDATA || os.homedir(), 'office-raccoon') :
    path.join(os.homedir(), '.config/office-raccoon'));
process.env.NODE_PATH = [path.join(prefix, 'node_modules'), process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
Module._initPaths();
const {chromium} = require('playwright');
(async () => {
  const browser = await chromium.launch(host.chromiumLaunchOptions(chromium, {headless:true}).options);
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    await page.addInitScript(() => Object.defineProperty(navigator,'webdriver',{configurable:true,get:()=>false}));
    await page.goto(pathToFileURL(html).href);
    await page.evaluate(() => window.__deckTextReady);
    const before = await page.evaluate(() => window.__deckRuntime.getDocument());
    const changed = await page.evaluate(() => window.__deckRuntime.setLayoutOption('composition','standard'));
    await page.evaluate(() => window.__deckRuntime.changeLayout('statement-focus-v1'));
    await page.evaluate(() => window.__deckRuntime.addSlide('cards-grid-v1'));
    const saved = await page.evaluate(() => window.__deckRuntime.getDocument());
    fs.writeFileSync(html, await page.evaluate(() => window.__deckRuntime.serializeHtml()));
    await page.goto(pathToFileURL(html).href);
    const reopened = await page.evaluate(() => window.__deckRuntime.getDocument());
    console.log(JSON.stringify({before,changed,saved,reopened}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exit(1)});
''', encoding="utf-8")
    result = run(probe, SKILL / "scripts", root / "index.html", cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["changed"] is True
    assert data["saved"] == data["reopened"]
    assert data["reopened"]["design"] == data["before"]["design"]
    assert data["reopened"]["design_plan"]["user_edited"] is True
    assert len(data["reopened"]["slides"]) == 4
    assert data["reopened"]["slides"][0]["layout_id"] == "statement-focus-v1"
    assert "cards-grid-v1" in data["reopened"]["slides"][0]["layout_drafts"]
    refreshed = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert refreshed.returncode == 0, refreshed.stderr
    assert json.loads(refreshed.stdout)["reusable"] is False


def test_malformed_saved_plan_can_be_replaced_without_research_restart(design_case):
    root, _, _, _ = design_case
    (root / "design_plan.json").write_text('{"partial":')
    result = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reusable"] is False


def test_explicit_palette_and_theme_constraints_cannot_be_replaced(design_case):
    root, outline, _, plan = design_case
    outline["design_requirements"] = {"theme_id": "blue-professional", "palette": "配色使用深蓝、米白，少量橙色点缀"}
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    data = json.loads((root / "design_input.json").read_text())
    plan.update(input_hash=data["input_hash"], catalog_hash=data["catalog_hash"])
    write(root / "design_plan.json", plan)
    rejected = scaffold(root)
    assert rejected.returncode != 0
    assert "user's selected theme" in rejected.stderr
    plan["theme_id"] = "blue-professional"
    plan["palette"] = {"background": "#FFFFFF", "text": "#111111", "primary": "#222222", "accent": "#AA0000"}
    write(root / "design_plan.json", plan)
    rejected = scaffold(root)
    assert rejected.returncode != 0
    assert "explicit user color" in rejected.stderr
    assert not (root / "deck.json").exists()


def test_explicit_no_image_requirement_survives_design_handoff(design_case):
    root, outline, _, plan = design_case
    outline["design_requirements"] = {"images": "不用图片，只使用提供的文字"}
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    data = json.loads((root / "design_input.json").read_text())
    plan.update(input_hash=data["input_hash"], catalog_hash=data["catalog_hash"])
    accepted = import_plan(root, plan)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    manifest = json.loads((root / "assets/generated/manifest.json").read_text())
    assert manifest["generation_forbidden"] is True
    assert all(row["decision"] == "skip" for row in manifest["image_plan"])


def test_cover_only_image_opt_out_does_not_disable_whole_deck(design_case):
    root, outline, _, plan = design_case
    outline["design_requirements"] = {"images": "封面不用图片"}
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    data = json.loads((root / "design_input.json").read_text())
    plan.update(input_hash=data["input_hash"], catalog_hash=data["catalog_hash"])
    accepted = import_plan(root, plan)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    manifest = json.loads((root / "assets/generated/manifest.json").read_text())
    assert manifest["generation_forbidden"] is False


def test_complete_theme_presets_preserve_non_default_composition_choices(design_case):
    root, _, data, plan = design_case
    theme = next(theme for theme in data["catalog"]["themes"] if theme["id"] == "blue-professional")
    assert any(preset["id"] == "blue-professional@decision-board" for preset in theme["presets"])
    plan["theme_id"] = "blue-professional@decision-board"
    accepted = import_plan(root, plan)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    deck = json.loads((root / "deck.json").read_text())
    assert deck["design"] == {"version": 2, "family": "analytical-exhibit", "variant": "decision-board"}


def test_designer_handoff_uses_a_tool_capable_loop_for_catalog_reads():
    import re
    from box_agent.tools.sub_agent_capabilities import parse_delegation_spec, DelegationSpec

    instructions = (SKILL / "SKILL.md").read_text()
    tools = json.loads(re.search(r'required_tools: (\[[^\]]+\])', instructions)[1])
    parsed = parse_delegation_spec(task="Read the role and designer input; produce a design plan", required_tools=tools)
    assert isinstance(parsed, DelegationSpec)
    assert parsed.strategy == "general_loop"
    assert "read_file" in parsed.required_tools


def test_designer_input_catalog_records_are_readable_with_file_pagination(design_case):
    root, _, data, _ = design_case
    lines = (root / "design_input.json").read_text().splitlines()
    assert len(lines) < 500
    assert max(map(len, lines)) < 100_000
    assert len(data["catalog"]["themes"]) >= 52
    assert len(data["catalog"]["layouts"]) == 33
    chart = next(layout for layout in data["catalog"]["layouts"] if layout["id"] == "chart-data-v1")
    assert "column" in chart["visual_options"]["chart_type"]
    assert chart["fields"]["categories"]["minItems"] == 2


def test_program_fills_mechanical_fields_from_the_bound_input(design_case):
    root, _, _, plan = design_case
    record_response(root, {"theme_id": "blue-professional", "schema_version": "1.0", "input_hash": "invented",
                           "slides": [{"page": "wrong", "layout_id": row["layout_id"]} for row in plan["slides"]]})
    result = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    accepted = json.loads((root / "design_plan.json").read_text())
    assert accepted["schema_version"] == 1
    assert accepted["input_hash"] == plan["input_hash"]
    assert [slide["page"] for slide in accepted["slides"]] == [1, 2, 3]
    assert all(slide["content_bindings"]["title"] == ["title"] for slide in accepted["slides"])


def test_main_cannot_replace_an_accepted_design_or_use_legacy_scaffold(design_case):
    root, _, _, plan = design_case
    plan["theme_id"] = "blue-professional@decision-board"
    write(root / "design_plan.json", plan)
    result = scaffold(root)
    assert result.returncode != 0
    assert "theme_match: differs from program-selected match" in result.stderr
    legacy = run("inspect_deck_contract.js", "cards-grid-v1", "--outline", "outline.json", "--out", "deck.json", cwd=root)
    assert legacy.returncode != 0
    assert "Main-agent visual overrides" in legacy.stderr


def test_wrong_workspace_or_unread_brief_is_not_accepted(design_case):
    root, outline, _, _ = design_case
    outline["tone"] = "新的视觉方向"
    write(root / "outline.json", outline)
    run("design_plan.js", "prepare", "outline.json", cwd=root)
    decision = {"theme_id": "blue-professional", "slides": [{"layout_id": "cards-grid-v1"}] * 3}
    record_response(root, decision, workspace=root / "other")
    rejected = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert rejected.returncode == 0
    assert "No completed designer response" in json.loads(rejected.stdout)["reason"]
    assert json.loads(rejected.stdout)["status"] == "degraded"
    record_response(root, decision, read_brief=False)
    rejected = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert rejected.returncode != 0
    assert "did not read the complete current brief" in rejected.stdout + rejected.stderr


def test_two_failed_design_attempts_cannot_be_replaced_by_a_third(design_case):
    root, outline, _, _ = design_case
    outline["tone"] = "第二个独立设计请求"
    write(root / "outline.json", outline)
    run("design_plan.js", "prepare", "outline.json", cwd=root)
    for index in range(2):
        record_response(root, {"theme_id": "not-registered", "slides": []})
        result = run("design_plan.js", "accept", "design_input.json", cwd=root)
        if index == 0:
            assert result.returncode != 0
            assert json.loads(result.stderr)["can_retry"] is True
        else:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["status"] == "degraded"
            assert (root / "fallback.html").exists()
    record_response(root, {"theme_id": "blue-professional", "slides": [{"layout_id": "cards-grid-v1"}] * 3})
    result = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "degraded"


def test_brief_is_bounded_and_details_are_loaded_only_when_needed(design_case):
    root, _, data, _ = design_case
    metadata = json.loads((root / "design_input.json").read_text())
    brief = json.loads(Path(metadata["request_file"]).read_text())
    assert "catalog" not in metadata
    assert len(Path(metadata["request_file"]).read_text()) < 8000
    assert "themes" not in brief and "outline" not in brief
    for item in brief["required_read_files"]:
        packet = Path(item["path"])
        assert len(packet.read_text()) < 8000
        assert len(packet.read_text().splitlines()) <= 500
    themes = [item for file in brief["theme_index_files"] for item in json.loads(Path(file).read_text())]
    layouts = [item for file in brief["layout_index_files"] for item in json.loads(Path(file).read_text())]
    assert len(themes) == len(data["catalog"]["themes"])
    assert len(layouts) == len(data["catalog"]["layouts"])
    assert all("presets" not in theme for theme in themes)
    assert all("fields" not in layout for layout in layouts)


def test_unread_packet_is_rejected_and_complete_retry_can_succeed(design_case):
    root, outline, _, _ = design_case
    outline["tone"] = "Packet read regression"
    write(root / "outline.json", outline)
    run("design_plan.js", "prepare", "outline.json", cwd=root)
    decision = {"theme_id": "blue-professional", "slides": [{"layout_id": "cards-grid-v1"}] * 3}
    sid = record_response(root, decision)
    log = Path(os.environ["BOX_AGENT_HOME"]) / "sessions" / hashlib.sha256(sid.encode()).hexdigest() / "session.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    rows = [row for row in rows if not (row["type"] == "tool/result" and
        "pages-1.json" in row["data"]["result"]["rawOutput"]["context_resource"]["resource_id"])]
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    rejected = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert rejected.returncode != 0
    assert "required packets: pages-1.json" in rejected.stderr
    record_response(root, decision)
    accepted = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert accepted.returncode == 0, accepted.stderr
    assert "plan" in json.loads(accepted.stdout)
    assert scaffold(root).returncode == 0


@pytest.mark.parametrize("wrong_base", [False, True])
def test_local_correction_needs_only_its_bound_packet(design_case, wrong_base):
    root, outline, _, _ = design_case
    outline["tone"] = "Local correction regression"
    write(root / "outline.json", outline)
    run("design_plan.js", "prepare", "outline.json", cwd=root)
    slides = [{"layout_id": "cards-grid-v1", "visual_options": {"composition": "invalid"}}] * 3
    record_response(root, {"theme_id": "blue-professional", "slides": slides})
    failed = run("design_plan.js", "accept", "design_input.json", cwd=root)
    correction = Path(json.loads(failed.stderr)["correction_file"])
    if wrong_base:
        data = json.loads(correction.read_text())
        data["base_response_hash"] = "wrong"
        write(correction, data)
    patch = {"slides": [{"layout_id": "cards-grid-v1", "visual_options": {"composition": "open"}}] * 3}
    sid = record_response(root, patch)
    log = Path(os.environ["BOX_AGENT_HOME"]) / "sessions" / hashlib.sha256(sid.encode()).hexdigest() / "session.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)["type"] != "tool/result"]
    for row in rows:
        if row["type"] == "user/message": row["data"]["content"] = f"Read {correction} and return a patch"
        if row["type"] == "assistant/message": row["data"]["message"]["content"] = json.dumps(patch)
    count = len(correction.read_text().splitlines())
    rows.insert(2, {"type": "tool/result", "data": {"result": {"success": True, "rawOutput": {
        "context_resource": {"resource_id": str(correction), "content_version": hashlib.sha256(correction.read_bytes()).hexdigest(),
            "start_line": 1, "end_line": count, "total_lines": count}}}}})
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert result.returncode == 0, result.stderr
    if wrong_base:
        assert json.loads(result.stdout)["status"] == "degraded"
    else:
        assert "plan" in json.loads(result.stdout)
        assert scaffold(root).returncode == 0


@pytest.mark.parametrize("read_brief", [False, True])
def test_unparseable_first_response_requires_a_fully_read_complete_correction(design_case, read_brief):
    root, outline, _, _ = design_case
    outline["tone"] = "Recover an unparseable first decision"
    write(root / "outline.json", outline)
    assert run("design_plan.js", "prepare", "outline.json", cwd=root).returncode == 0
    decision = {"theme_id": "blue-professional", "slides": [{"layout_id": "cards-grid-v1"}] * 3}
    first = record_response(root, decision)
    sessions = Path(os.environ["BOX_AGENT_HOME"]) / "sessions"
    log = sessions / hashlib.sha256(first.encode()).hexdigest() / "session.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    for row in rows:
        if row["type"] == "assistant/message":
            row["data"]["message"]["content"] = "采用蓝色主题，三页均用卡片布局。"
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    failed = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert failed.returncode != 0
    correction = Path(json.loads(failed.stderr)["correction_file"])
    packet = json.loads(correction.read_text())
    assert packet["requires_full_read"] is True
    assert packet["original_decision"] is None

    second = record_response(root, decision, read_brief=read_brief)
    log = sessions / hashlib.sha256(second.encode()).hexdigest() / "session.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    for row in rows:
        if row["type"] == "user/message":
            row["data"]["content"] = f"Read {correction} and correct the design"
    count = len(correction.read_text().splitlines())
    rows.insert(2, {"type": "tool/result", "data": {"result": {"success": True, "rawOutput": {
        "context_resource": {"resource_id": str(correction), "content_version": hashlib.sha256(correction.read_bytes()).hexdigest(),
                             "start_line": 1, "end_line": count, "total_lines": count}}}}})
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert result.returncode == 0, result.stderr
    accepted = json.loads(result.stdout)
    if read_brief:
        assert accepted["attempts"] == 2
        assert "plan" in accepted
        assert len(json.loads((root / "design_plan.json").read_text())["slides"]) == 3
    else:
        assert accepted["status"] == "degraded"
        assert "did not read the complete current brief" in accepted["reason"]


def test_main_visual_hints_do_not_override_design_or_reset_request(design_case, monkeypatch):
    import base64
    root, outline, _, plan = design_case
    monkeypatch.setenv("BOX_AGENT_SOURCE_TEXT_B64", base64.b64encode("请制作三页人物介绍，使用真实照片。".encode()).decode())
    outline["slides"][0].update(layout="cover", visual="wide image feature with player portrait")
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    meta = json.loads((root / "design_input.json").read_text())
    plan["slides"][0]["layout_id"] = "cover-hero-v1"
    plan["slides"][0]["visual_options"] = {}
    result = import_plan(root, plan)
    assert result.returncode == 0, result.stdout + result.stderr
    accepted = json.loads((root / "design_plan.json").read_text())
    assert accepted["layout_hints_only"] is True
    brief = Path(meta["request_file"]).read_bytes()
    outline["slides"][0]["visual"] = "hero cover with player portrait"
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads(prepared.stdout)["reusable"] is True
    assert json.loads((root / "design_input.json").read_text())["input_hash"] == meta["input_hash"]
    assert Path(meta["request_file"]).read_bytes() == brief


def test_verbatim_user_geometry_remains_a_hard_constraint(design_case, monkeypatch):
    import base64
    root, outline, _, plan = design_case
    requirement = "第1页必须使用金字塔结构"
    monkeypatch.setenv("BOX_AGENT_SOURCE_TEXT_B64", base64.b64encode(f"请做三页演示，{requirement}。".encode()).decode())
    outline["slides"][0]["hard_requirements"] = requirement
    write(root / "outline.json", outline)
    prepared = run("design_plan.js", "prepare", "outline.json", cwd=root)
    assert prepared.returncode == 0, prepared.stderr
    result = import_plan(root, plan)
    assert result.returncode != 0
    assert "design_contract" in result.stdout + result.stderr


def test_response_import_accepts_one_json_object_wrapped_in_prose(design_case):
    root, _, data, plan = design_case
    decision = {"theme_id": "blue-professional", "palette": plan["palette"], "slides": [{"layout_id": slide["layout_id"]} for slide in plan["slides"]]}
    sid = record_response(root, decision)
    log = Path(os.environ["BOX_AGENT_HOME"]) / "sessions" / hashlib.sha256(sid.encode()).hexdigest() / "session.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    for row in rows:
        if row["type"] == "assistant/message":
            row["data"]["message"]["content"] = "设计已完成：\n```json\n" + json.dumps(json.loads(row["data"]["message"]["content"])) + "\n```\n以上为设计方案。"
    log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = run("design_plan.js", "accept", "design_input.json", cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_palette_cannot_displace_program_bound_user_colors(design_case):
    root, outline, _, plan = design_case
    outline["design_requirements"] = {"palette": "配色使用深蓝、米白"}
    write(root / "outline.json", outline)
    run("design_plan.js", "prepare", "outline.json", cwd=root)
    plan["palette"] = {"background": "#000000", "text": "#111111", "primary": "#FF0000", "accent": "#FFFF00"}
    result = import_plan(root, plan)
    assert result.returncode == 0, result.stdout + result.stderr
    accepted = json.loads((root / "design_plan.json").read_text())
    assert accepted["palette"]["background"] == "#F4EFE4"
    assert accepted["palette"]["primary"] == "#173B63"
    assert accepted["palette"]["text"] == "#111111"
    created = scaffold(root)
    assert created.returncode == 0, created.stderr
    palette = json.loads((root / "deck.json").read_text())["design_contract"]["palette"]
    assert palette["background"]["value"] == "#F4EFE4"
    assert palette["primary"]["value"] == "#173B63"
