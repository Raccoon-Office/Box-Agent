"""Brand visual profiles are optional, composable design-plan metadata."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT_DIR = ROOT / "box_agent/skills/document-skills/pptx/scripts"


def test_profile_catalog_contains_composable_examples():
    result = subprocess.run(
        ["node", "-e", "console.log(JSON.stringify(require('./design_plan_core.js').catalog().brand_profiles || []));"],
        cwd=SCRIPT_DIR, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == []


def test_design_catalog_exposes_brand_profiles():
    result = subprocess.run(
        ["node", "-e", "const c=require('./design_plan_core.js').catalog(); console.log(JSON.stringify((c.brand_profiles || []).map(p=>p.id)));"],
        cwd=SCRIPT_DIR, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == []


def test_inline_visual_profile_is_accepted_without_catalog_id():
    result = subprocess.run(
        ["node", "-e", "const p=require('./design_plan_core.js'); const i=p.makeInput({deck_goal:'Dance',audience:'Audience',source_mode:'user_provided',slides:[{title:'Dance',message:'A stage',bullets:['Move','Rhythm','Light']}]},'Dance'); const d={theme_id:'product-console',visual_profile:{id:'custom-flamenco-2026',semantic_tags:['舞台','舞蹈'],motifs:['fan-fold'],geometry:['vertical-stage'],modules:['media']},visual_requirements:{canvas:'solid',heading:'standard',display_font:'sans-serif',body_font:'sans-serif',shadow:'soft',allow_plain_fallback:true},palette:{background:'#F4EFE4',text:'#111111',primary:'#7A1F35',accent:'#B86B4B',secondary:'#2B303E',accent_usage:'sparse'},slides:[{layout_id:'cards-grid-v1'}],reason:'custom profile'}; const x=p.canonicalPlan(d,i); console.log(JSON.stringify(x.visual_profile));"],
        cwd=SCRIPT_DIR, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout)["id"] == "custom-flamenco-2026"


def test_inline_profile_string_dimensions_are_normalized():
    result = subprocess.run(
        ["node", "-e", "const p=require('./design_plan_core.js'); const i=p.makeInput({deck_goal:'Sport',audience:'Audience',source_mode:'user_provided',slides:[{title:'Sport',message:'A weekend',bullets:['Play','Rest','Meet']}]},'Sport'); const d={theme_id:'product-console',visual_profile:{id:'custom-badminton',semantic_tags:['运动'],motifs:['racket'],geometry:'rounded-capsule',composition:'airy-grid'},visual_requirements:{canvas:'solid',heading:'standard',display_font:'sans-serif',body_font:'sans-serif',shadow:'soft',allow_plain_fallback:true},palette:{background:'#FAF8F2',text:'#2E3A48',primary:'#3FBF9F',accent:'#FF8A3D',secondary:'#7EC8E3',accent_usage:'sparse'},slides:[{layout_id:'cards-grid-v1'}],reason:'custom profile'}; const x=p.canonicalPlan(d,i); console.log(JSON.stringify(x.visual_profile));"],
        cwd=SCRIPT_DIR, text=True, capture_output=True, check=True,
    )
    profile = json.loads(result.stdout)
    assert profile["geometry"] == ["rounded-capsule"]
    assert profile["composition"] == ["airy-grid"]
