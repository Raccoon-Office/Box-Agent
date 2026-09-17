"""A failed designer cannot erase the content-backed delivery floor."""
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from tests.test_pptx_design_plan import SKILL, design_case, record_response, run, write, scaffold


def fresh(root, outline):
    outline['tone'] = 'New recovery case'
    write(root / 'outline.json', outline)
    result = run('design_plan.js', 'prepare', 'outline.json', cwd=root)
    assert result.returncode == 0, result.stderr


def decision(count=3):
    return {'theme_id':'studio','visual_requirements':{'canvas':'solid','heading':'standard','display_font':'sans-serif','body_font':'sans-serif','shadow':'none','allow_plain_fallback':True},'slides':[{'layout_id':'cards-grid-v1'}]*count}


def test_prepare_already_produces_editable_content(design_case):
    root, outline, _, _ = design_case
    html = (root/'fallback.html').read_text()
    assert 'contenteditable="true"' in html
    assert outline['slides'][0]['bullets'][0] in html
    assert (root/'index.html').exists()


def test_theme_patch_cannot_change_page_count_or_colors(design_case):
    root, outline, _, _ = design_case
    fresh(root, outline)
    record_response(root, decision())
    first=run('design_plan.js','accept','design_input.json',cwd=root)
    assert first.returncode != 0
    patch_path=Path(json.loads(first.stderr)['correction_file'])
    assert json.loads(patch_path.read_text())['editable_fields']==['theme_id']
    corrected=decision(4)
    corrected.update(theme_id='blue-professional',palette={'background':'#000000','text':'#FFFFFF','primary':'#FF0000','accent':'#FF0000','secondary':'#FF0000','accent_usage':'dominant'})
    record_response(root,corrected)
    result=run('design_plan.js','accept','design_input.json',cwd=root)
    assert result.returncode == 0, result.stderr
    plan=json.loads((root/'design_plan.json').read_text())
    assert len(plan['slides'])==3
    assert plan['palette']['background']=='#F4EFE4'
    assert scaffold(root).returncode==0


def test_failed_correction_delivers_extra_pages_without_inventing_facts(design_case):
    root, outline, _, _ = design_case
    fresh(root,outline)
    record_response(root,decision(4))
    run('design_plan.js','accept','design_input.json',cwd=root)
    record_response(root,{'theme_id':'not-registered','slides':[{}]*4})
    result=run('design_plan.js','accept','design_input.json',cwd=root)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='degraded' and report['actual_pages']>=4
    html=Path(report['artifact']).read_text()
    deck=json.loads(Path(report['deck']).read_text())
    assert len(deck['slides'])==report['actual_pages']
    content=json.dumps([slide['props'] for slide in deck['slides']])
    for page in outline['slides']:
        for bullet in page['bullets']:
            assert content.count(bullet)==1


def test_missing_response_delivers_fallback_and_preserves_existing_html(design_case):
    root,outline,_,_=design_case
    (root/'index.html').write_text('<html>User edited</html>')
    fresh(root,outline)
    result=run('design_plan.js','accept','design_input.json',cwd=root)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['status']=='degraded'
    assert (root/'index.html').read_text()=='<html>User edited</html>'
    assert (root/'fallback.html').exists()


def test_degraded_html_exports_required_pptx_without_reauthoring(design_case):
    root, outline, _, _ = design_case
    # Keep a user's saved deck while exporting the separate recovery artifact.
    saved = (root / 'index.html').read_text().replace('Topic A', 'User edited Topic A')
    (root / 'index.html').write_text(saved)
    fresh(root, outline)
    record_response(root, decision(4))
    first = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert first.returncode != 0
    record_response(root, {'theme_id': 'not-registered', 'slides': [{}] * 4})
    accepted = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert accepted.returncode == 0, accepted.stderr
    report = json.loads(accepted.stdout)
    assert report['status'] == 'degraded' and report['terminal'] is True
    primary = Path(report['primary_artifact'])
    html_before = primary.read_bytes()
    assert primary != root / 'index.html'
    preflight = run('check_html_export_env.js', cwd=root)
    assert preflight.returncode == 0, preflight.stdout + preflight.stderr
    pptx = root / 'recovery.pptx'
    exported = run('html_to_editable_pptx.js', primary, pptx, cwd=root)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    validated = subprocess.run(
        [sys.executable, str(SKILL / 'scripts/validate_pptx_package.py'), str(pptx)],
        capture_output=True, text=True,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
    with zipfile.ZipFile(pptx) as archive:
        slides = [ET.fromstring(archive.read(name)) for name in archive.namelist()
                  if re.fullmatch(r'ppt/slides/slide\d+\.xml', name)]
    assert len(slides) == report['actual_pages']
    texts = '\n'.join(node.text or '' for slide in slides for node in slide.iter()
                      if node.tag.endswith('}t'))
    for page in outline['slides']:
        for bullet in page['bullets']:
            assert bullet in texts
    assert primary.read_bytes() == html_before
    assert (root / 'index.html').read_text() == saved


def test_outline_validation_failure_still_produces_an_artifact(design_case):
    root,outline,_,_=design_case
    outline['slides'][0]['message']=''
    write(root/'outline.json',outline)
    result=run('design_plan.js','prepare','outline.json',cwd=root)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='degraded'
    assert 'not verified' in report['reason']
    assert Path(report['artifact']).exists()


def test_recovery_keeps_normal_editor_playback_and_serialization(design_case):
    from tests.test_pptx_design_regressions import js
    root,_,_,_=design_case
    data=js(root,r'''
const path=require('path'),os=require('os'),Module=require('module'),{pathToFileURL}=require('url');
const root=process.argv[2],host=require(path.join(root,'scripts/playwright_host.js'));host.ensurePlaywrightBrowsersPath();
const prefix=process.env.BOX_AGENT_NODE_PREFIX||process.env.BOX_AGENT_RUNTIME_PREFIX||(process.platform==='darwin'?path.join(os.homedir(),'Library/Application Support/office-raccoon'):process.platform==='win32'?path.join(process.env.APPDATA||os.homedir(),'office-raccoon'):path.join(os.homedir(),'.config/office-raccoon'));
process.env.NODE_PATH=[path.join(prefix,'node_modules'),process.env.NODE_PATH].filter(Boolean).join(path.delimiter);Module._initPaths();
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch(host.chromiumLaunchOptions(chromium,{headless:true}).options);try{
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 await page.addInitScript(()=>Object.defineProperty(navigator,'webdriver',{configurable:true,get:()=>false}));
 await page.goto(pathToFileURL(process.argv[3]).href);await page.evaluate(()=>window.__deckTextReady);

 const result=await page.evaluate(()=>{
   const api=window.__deckRuntime;
   const count=api.getDocument().slides.length;
   api.enterPresentation();const presenting=api.isPresenting();api.exitPresentation();
   api.addSlide('cards-grid-v1');
   const changed=api.setLayoutOption('composition','open');
   return {count,after:api.getDocument().slides.length,presenting,changed,
     saved:api.serializeHtml(),exportButton:!!document.querySelector('[data-action="export-pptx"]')};
 });
 console.log(JSON.stringify(result));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
''',root/'fallback.html')
    assert data['after']==data['count']+1
    assert data['presenting'] and data['changed'] and data['exportButton']
    assert 'id="deck-document"' in data['saved']
    assert '__deckRuntime' in data['saved']


def test_recovery_preserves_manual_edits_to_both_html_files(design_case):
    root,outline,_,_=design_case
    for name in ['index.html','fallback.html']:
        (root/name).write_text('<html>Manual edit '+name+'</html>')
    fresh(root,outline)
    for name in ['index.html','fallback.html']:
        assert (root/name).read_text()=='<html>Manual edit '+name+'</html>'
    report=json.loads((root/'qa/design_delivery.json').read_text())
    assert Path(report['artifact']).name not in ['index.html','fallback.html']
