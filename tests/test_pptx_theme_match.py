"""Theme selection uses visual traits rather than palette proximity."""
import json
import subprocess

from tests.test_pptx_design_plan import NODE, SKILL


def probe(code):
    result = subprocess.run([NODE, '-e', '''
const match=require('./scripts/theme_match.js');
const core=require('./scripts/deck_spec_core.js');
const themes=core.listThemes();
const req={canvas:'solid',heading:'standard',display_font:'sans-serif',body_font:'sans-serif',shadow:'none',allow_plain_fallback:true};
''' + code], cwd=SKILL, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_editorial_theme_rejected_even_with_matching_colors():
    error = probe("try {match.select(themes,'monochrome',req)} catch(e) {console.log(JSON.stringify(e.message))}")
    assert 'canvas' in error and 'heading' in error and 'display_font' in error
    assert 'plain-neutral' in error


def test_original_palette_does_not_exclude_compatible_theme():
    result = probe("console.log(JSON.stringify(match.select(themes,'blue-professional',req)))")
    assert result['status'] == 'matched'


def test_no_matching_theme_uses_plain_fallback():
    result = probe("console.log(JSON.stringify(match.select(themes,'monochrome',{...req,canvas:'pixel',heading:'handwritten'})))")
    assert result['theme_id'] == 'plain-neutral'
    assert result['status'] == 'plain_fallback'
    assert set(result['conflicts']) == {'canvas', 'heading'}


def test_no_match_does_not_relax_user_constraints():
    result = probe("try {match.select(themes,'monochrome',{...req,canvas:'pixel',heading:'handwritten',allow_plain_fallback:false})} catch(e) {console.log(JSON.stringify(e.message))}")
    assert 'cannot silently relax user constraints' in result


def test_fallback_keeps_palette_and_layout_and_detects_tampering():
    result = probe('''
const plans=require('./scripts/design_plan_core.js');
const input=plans.makeInput({slides:[{title:'Product',message:'A product overview',bullets:['One fact']}]},'Product','A product overview');
const palette={background:'#FFFFFF',text:'#1D1D1F',primary:'#1D1D1F',accent:'#0071E3',secondary:'#6E6E73',accent_usage:'sparse'};
const plan=plans.canonicalPlan({theme_id:'monochrome',visual_requirements:{...req,canvas:'pixel',heading:'handwritten'},palette,reason:'Product overview',slides:[{layout_id:'cards-grid-v1'}]},input);
const before=plans.validatePlan(plan,input);
plan.theme_match.status='matched';
console.log(JSON.stringify({palette:plan.palette,theme:plan.theme_id,layout:plan.slides[0].layout_id,before:before.ok,after:plans.validatePlan(plan,input).ok}));
''')
    assert result['theme'] == 'plain-neutral'
    assert result['palette']['background'] == '#FFFFFF'
    assert result['palette']['accent'] == '#0071E3'
    assert result['layout'] == 'cards-grid-v1'
    assert result['before'] and not result['after']


def test_user_selected_theme_is_not_replaced_by_fallback():
    result = probe("try {match.select(themes,'monochrome',{...req,canvas:'pixel',heading:'handwritten'},'monochrome')} catch(e) {console.log(JSON.stringify(e.message))}")
    assert 'cannot silently relax user constraints' in result


def test_missing_visual_requirements_are_rejected():
    result = probe("try {match.select(themes,'monochrome',null)} catch(e) {console.log(JSON.stringify(e.message))}")
    assert 'expected structured visual requirements' in result


def test_plain_fallback_renders_without_texture_and_with_fixed_colors(tmp_path):
    from tests.test_pptx_design_regressions import js, render_case, render
    from tests.test_pptx_palette_contract import evaluate
    contract = evaluate(tmp_path, "console.log(JSON.stringify(decide('背景色 #FFFFFF，正文色 #1D1D1F，主色 #1D1D1F').contract));")
    deck_path, deck = render_case(tmp_path, ['cards-grid-v1'], 'plain-neutral')
    deck['design_contract'] = contract
    html = render(tmp_path, deck_path, deck)
    data = js(tmp_path, r'''
const path=require('path'),os=require('os'),Module=require('module'),{pathToFileURL}=require('url');
const root=process.argv[2],host=require(path.join(root,'scripts/playwright_host.js'));host.ensurePlaywrightBrowsersPath();
const prefix=process.env.BOX_AGENT_NODE_PREFIX||process.env.BOX_AGENT_RUNTIME_PREFIX||(process.platform==='darwin'?path.join(os.homedir(),'Library/Application Support/office-raccoon'):process.platform==='win32'?path.join(process.env.APPDATA||os.homedir(),'office-raccoon'):path.join(os.homedir(),'.config/office-raccoon'));
process.env.NODE_PATH=[path.join(prefix,'node_modules'),process.env.NODE_PATH].filter(Boolean).join(path.delimiter);Module._initPaths();
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch(host.chromiumLaunchOptions(chromium,{headless:true}).options);try{
 const page=await browser.newPage(); await page.goto('file://'+process.argv[3]);
 await page.evaluate(()=>window.__deckTextReady);
 console.log(JSON.stringify(await page.evaluate(()=>{
 const slide=document.querySelector('#deck-root > .slide');
 const title=slide.querySelector('h1,h2');
 return {canvas:document.body.dataset.deckCanvas,bg:getComputedStyle(slide).backgroundColor,image:getComputedStyle(slide).backgroundImage,font:getComputedStyle(title).fontFamily};
 })));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
''', html)
    assert data['canvas'] == 'solid'
    assert data['bg'] == 'rgb(255, 255, 255)'
    assert data['image'] == 'none'
    assert data['font'].endswith('sans-serif')
