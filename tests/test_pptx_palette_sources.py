"""Focused checks for deterministic palette sources used by the PPTX designer."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT_DIR = ROOT / "box_agent/skills/document-skills/pptx/scripts"


def probe(expression: str):
    result = subprocess.run(
        ["node", "-e", f"const p=require('./palette_harmony.js'); console.log(JSON.stringify({expression}));"],
        cwd=SCRIPT_DIR,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_primary_color_derives_complete_harmony_roles():
    palette = probe("p.deriveHarmony('#F2C94C')")
    assert palette["primary"] == "#F2C94C"
    assert palette["background"].startswith("#")
    assert palette["text"] in {"#111111", "#FFFFFF"}
    assert palette["accent"] != palette["primary"]
    assert palette["secondary"] != palette["primary"]
    assert palette["contrast_checked"] is True


def test_premium_palette_lookup_returns_curated_roles():
    palette = probe("p.findPremiumPalette('暖石米色')")
    assert palette["id"] == "warm-stone-navy"
    assert palette["background"] == "#E3E3DB"
    assert palette["surface"] == "#F2E2C6"
    assert palette["text"] == "#2B303E"
    assert palette["accent_usage"] == "sparse"


def test_unknown_premium_palette_does_not_guess():
    assert probe("p.findPremiumPalette('不存在的色板')") is None


def test_correction_original_decision_path_is_normalized():
    result = subprocess.run(
        ["node", "-e", "const r=require('./design_recovery.js'); console.log(JSON.stringify(r.normalizeCorrectionUpdate([{op:'replace',path:'/original_decision/theme_id',value:'product-console'}],{theme_id:'studio'})));"],
        cwd=SCRIPT_DIR,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["theme_id"] == "product-console"


def test_wrapped_correction_patch_is_normalized():
    result = subprocess.run(
        ["node", "-e", "const r=require('./design_recovery.js'); console.log(JSON.stringify(r.normalizeCorrectionUpdate({patch:[{op:'replace',path:'/theme_id',value:'block-frame'},{op:'replace',path:'/pages',value:[{layout_id:'cards-grid-v1'}]},{op:'replace',path:'/allow_plain_fallback',value:false}]},{theme_id:'studio'})));"],
        cwd=SCRIPT_DIR,
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["theme_id"] == "block-frame"
    assert data["slides"] == [{"layout_id": "cards-grid-v1"}]
    assert data["visual_requirements"]["allow_plain_fallback"] is False
