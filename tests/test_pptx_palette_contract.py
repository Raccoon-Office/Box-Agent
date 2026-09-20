"""One frozen palette for user colors and independently selected colors."""
import json
import os

import pytest

from tests.test_pptx_design_regressions import js, render_case, render
from tests.test_pptx_controlled_deck import _run, SKILL_DIR

SCHEME = {'background':'#101014','text':'#F4F4F5','primary':'#63CFE5','accent':'#B49AFF','secondary':'#F68DB9','accent_usage':'sparse'}


def evaluate(tmp_path, body):
    return js(tmp_path, r'''
const path=require('path'),root=process.argv[2];
const colors=require(path.join(root,'scripts/design_contract_core.js'));
const plans=require(path.join(root,'scripts/design_plan_core.js'));
const scheme=JSON.parse(process.argv[3]);
const outline={deck_goal:'API overview',audience:'Developers',source_mode:'user_provided',slides:[{page:1,title:'API',message:'Build an application',bullets:['Input','Process','Output'],evidence:[]}]};
function decide(source, palette=scheme) {
 const input=plans.makeInput(outline,'API',source);
 const plan=plans.canonicalPlan({theme_id:'studio',visual_requirements:{canvas:'any',heading:'any',display_font:'any',body_font:'any',shadow:'any',allow_plain_fallback:true},palette,reason:'Readable developer guide',slides:[{layout_id:'cards-grid-v1'}]},input);
 return {plan,contract:plans.validatePlan(plan,input).design_contract};
}
''' + body, json.dumps(SCHEME))


def test_equal_user_and_designer_colors_compile_to_identical_tokens(tmp_path):
    d = evaluate(tmp_path, r'''
const automatic=decide('介绍API');
const user=decide('配色：背景色 #101014，正文色 #F4F4F5，主色 #63CFE5，强调色 #B49AFF，辅助色 #F68DB9，少量点缀。');
console.log(JSON.stringify({automatic,user}));
''')
    assert d['automatic']['contract']['palette']['tokens'] == d['user']['contract']['palette']['tokens']
    assert d['automatic']['contract']['palette']['primary']['source'] == 'inferred'
    assert d['user']['contract']['palette']['primary']['source'] == 'explicit'


def test_partial_user_color_is_locked_and_other_roles_are_completed(tmp_path):
    d = evaluate(tmp_path, "console.log(JSON.stringify(decide('主色必须是 #FFAA00')));")
    p = d['contract']['palette']
    assert p['version'] == 2
    assert p['primary']['value'] == '#FFAA00'
    assert p['primary']['source'] == 'explicit'
    assert p['background']['value'] == SCHEME['background']
    assert p['background']['source'] == 'inferred'
    assert p['accent_usage'] == 'sparse'
    assert p['tokens']['primary'] == '#FFAA00'


def test_missing_designer_palette_is_rejected(tmp_path):
    d = evaluate(tmp_path, "try {decide('介绍API',null);} catch(error) {console.log(JSON.stringify({error:error.message}));}")
    assert 'palette' in d['error']
    assert 'background' in d['error']


def test_conflicting_locked_foreground_is_reported_without_replacement(tmp_path):
    d = evaluate(tmp_path, "try {decide('配色背景色 #FFFFFF，正文色 #EEEEEE');} catch(error) {console.log(JSON.stringify({error:error.message}));}")
    assert 'contrast' in d['error']


def test_frozen_palette_does_not_change_with_theme_defaults(tmp_path):
    d = evaluate(tmp_path, r'''
const contract=decide('介绍API').contract;
const a=colors.paletteWithOverrides({background:'#FFFFFF',primary:'#FF0000'},contract);
const b=colors.paletteWithOverrides({background:'#000000',primary:'#00FF00'},contract);
const altered=JSON.parse(JSON.stringify(contract));altered.palette.tokens.primary='#F00000';
const issues=[];colors.validateAndNormalizeDesignContract(altered,issues);
console.log(JSON.stringify({a,b,issues}));
''')
    assert d['a'] == d['b']
    assert any('tokens' in issue for issue in d['issues'])


@pytest.mark.parametrize('theme', [theme['id'] for theme in json.loads((SKILL_DIR/'layouts/manifest.json').read_text())['themes']])
def test_rendered_components_follow_frozen_palette(tmp_path, theme):
    contract = evaluate(tmp_path, "console.log(JSON.stringify(decide('介绍API').contract));")
    deck_path, deck = render_case(tmp_path, ['cover-editorial-v1','cards-grid-v1','timeline-horizontal-v1','chart-data-v1'], theme)
    deck['design_contract'] = contract
    html = render(tmp_path, deck_path, deck)
    report = tmp_path/'palette-report.json'
    result = _run('probe_deck_runtime.js',str(html),'--report',str(report))
    data = json.loads(report.read_text())
    assert result.returncode == 0, json.dumps(data.get('editor',{}).get('paletteCompliance'),ensure_ascii=False)
    assert data['editor']['paletteCompliance']['enforced']
    assert data['editor']['paletteCompliance']['sampled'] > 40
    assert data['editor']['paletteCompliance']['failures'] == []
    if theme == 'studio':
        html.write_text(html.read_text().replace('</head>','<style>#deck-root > .slide h1 {color:#63CFE6!important}</style></head>'))
        rejected = _run('probe_deck_runtime.js',str(html),'--report',str(report))
        assert rejected.returncode != 0
        assert any('Frozen palette mismatch' in issue for issue in json.loads(report.read_text())['issues'])
        html.write_text(html.read_text().replace('#63CFE6!important','#B49AFF!important'))
        rejected = _run('probe_deck_runtime.js',str(html),'--report',str(report))
        assert rejected.returncode != 0
        assert any(item.get('role') == 'heading' for item in json.loads(report.read_text())['editor']['paletteCompliance']['failures'])


def test_explicit_heading_color_is_not_reassigned_to_body_or_accent(tmp_path):
    d = evaluate(tmp_path, "console.log(JSON.stringify(decide('背景色 #101014，正文色 #F4F4F5，标题色 #E8B4DE')));")
    p = d['contract']['palette']
    assert p['heading']['value'] == '#E8B4DE'
    assert p['heading']['source'] == 'explicit'
    assert p['text']['value'] == '#F4F4F5'
    assert p['tokens']['heading'] == '#E8B4DE'


def test_named_background_and_exact_primary_are_both_preserved(tmp_path):
    d = evaluate(tmp_path, "console.log(JSON.stringify(decide('配色采用米白背景，主色 #216A74', {...scheme,text:'#111111'})));")
    assert d['contract']['palette']['background']['value'] == '#F4EFE4'
    assert d['contract']['palette']['primary']['value'] == '#216A74'


def test_named_terracotta_and_surface_color_become_locked_roles(tmp_path):
    d = evaluate(tmp_path, "console.log(JSON.stringify(decide('背景色 #E3E3DB，表面色 #F2E2C6，正文色 #2B303E，主色 #2B303E，陶土色作为少量强调。',{...scheme,background:'#E3E3DB',text:'#2B303E',primary:'#2B303E',accent:'#B86B4B',secondary:'#8C9A8E'})));" )
    p = d['contract']['palette']
    assert p['background']['value'] == '#E3E3DB'
    assert p['surface']['value'] == '#F2E2C6'
    assert p['accent']['value'] == '#B86B4B'
    assert p['accent_usage'] == 'sparse'


def test_finalizer_does_not_downgrade_a_frozen_palette_mismatch(tmp_path):
    contract = evaluate(tmp_path, "console.log(JSON.stringify(decide('介绍API').contract));")
    deck_path, deck = render_case(tmp_path, ['cards-grid-v1'])
    deck['design_contract'] = contract
    render(tmp_path, deck_path, deck)
    preload = tmp_path/'palette-failure.cjs'
    preload.write_text(r'''
const cp=require('child_process'),fs=require('fs'),path=require('path'),spawn=cp.spawnSync;
cp.spawnSync=function(command,args,options){
 if(String(args?.[0]||'').endsWith('probe_deck_runtime.js')){
  const file=args[args.indexOf('--report')+1];fs.mkdirSync(path.dirname(file),{recursive:true});
  fs.writeFileSync(file,JSON.stringify({ok:false,issues:['Frozen palette mismatch'],warnings:[],editor:{paletteCompliance:{enforced:true,failures:[{slide:1,element:'h1',property:'color',color:'#F00000'}]}}}));
  return {status:1,stdout:'',stderr:''};
 }
 return spawn.call(this,command,args,options);
};
''')
    result = _run('finalize_controlled_deck.js',str(deck_path),'--out',str(tmp_path/'index.html'),
                  env={**os.environ,'NODE_OPTIONS':f'--require "{preload}"'})
    assert result.returncode != 0
    assert 'FINALIZE_STOP stage=palette_contract' in result.stderr + result.stdout
    assert json.loads((tmp_path/'qa/runtime_probe.json').read_text())['ok'] is False


def test_editor_rerender_keeps_the_frozen_chart_palette(tmp_path):
    contract = evaluate(tmp_path, "console.log(JSON.stringify(decide('介绍API').contract));")
    deck_path, deck = render_case(tmp_path, ['chart-data-v1'])
    deck['design_contract'] = contract
    html = render(tmp_path, deck_path, deck)
    d = js(tmp_path, r'''
const path=require('path'),os=require('os'),Module=require('module'),{pathToFileURL}=require('url');
const root=process.argv[2],host=require(path.join(root,'scripts/playwright_host.js'));host.ensurePlaywrightBrowsersPath();
const prefix=process.env.BOX_AGENT_NODE_PREFIX||process.env.BOX_AGENT_RUNTIME_PREFIX||(process.platform==='darwin'?path.join(os.homedir(),'Library/Application Support/office-raccoon'):process.platform==='win32'?path.join(process.env.APPDATA||os.homedir(),'office-raccoon'):path.join(os.homedir(),'.config/office-raccoon'));
process.env.NODE_PATH=[path.join(prefix,'node_modules'),process.env.NODE_PATH].filter(Boolean).join(path.delimiter);Module._initPaths();
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch(host.chromiumLaunchOptions(chromium,{headless:true}).options);try{
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 await page.addInitScript(()=>Object.defineProperty(navigator,'webdriver',{configurable:true,get:()=>false}));
 await page.goto(pathToFileURL(process.argv[3]).href);await page.evaluate(()=>window.__deckTextReady);
 const result=await page.evaluate(()=>{const read=()=>document.querySelector('#deck-root [data-pptx-chart]').getAttribute('data-chart-palette-light');
 const before=read();const changed=window.__deckRuntime.setLayoutOption('chart_type','line');
 return {before,changed,after:read(),contract:window.__deckRuntime.getDocument().design_contract};});
 console.log(JSON.stringify(result));
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exit(1)});
''', html)
    assert d['changed']
    assert d['before'] == d['after'] == ','.join(contract['palette']['tokens']['chart'])
    assert d['contract'] == contract


@pytest.mark.parametrize('theme',['studio','8-bit-orbit','cobalt-grid','product-console'])
def test_light_user_palette_remains_exact_on_dark_and_light_themes(tmp_path,theme):
    contract = evaluate(tmp_path, "console.log(JSON.stringify(decide('背景色 #FAFAFA，正文色 #18181B，主色 #174B63，强调色 #A34264，辅助色 #24736D，少量点缀。',{...scheme,background:'#FAFAFA',text:'#18181B',primary:'#174B63',accent:'#A34264',secondary:'#24736D'}).contract));")
    deck_path,deck=render_case(tmp_path,['cover-editorial-v1','cards-grid-v1'],theme)
    deck['design_contract']=contract
    html=render(tmp_path,deck_path,deck)
    report=tmp_path/'light-report.json'
    result=_run('probe_deck_runtime.js',str(html),'--report',str(report))
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(report.read_text())['editor']['paletteCompliance']['failures']==[]


def test_user_colors_default_to_accents_instead_of_large_color_fills(tmp_path):
    d=evaluate(tmp_path,"console.log(JSON.stringify(decide('背景色 #101014，正文色 #F4F4F5，主色 #63CFE5，强调色 #B49AFF，辅助色 #F68DB9',{...scheme,accent_usage:'dominant'})));")
    assert d['plan']['palette']['accent_usage']=='sparse'
    assert d['contract']['palette']['accent_usage_source']=='recommended'


def test_explicit_user_color_usage_is_preserved(tmp_path):
    d=evaluate(tmp_path,"console.log(JSON.stringify(decide('背景色 #101014，正文色 #F4F4F5，主色 #63CFE5，强调色 #B49AFF，辅助色 #F68DB9，accent_usage=dominant')));")
    assert d['plan']['palette']['accent_usage']=='dominant'
    assert d['contract']['palette']['accent_usage_source']=='explicit'


@pytest.mark.parametrize('kind',['column','line','area','pie','donut','radar','diagram'])
def test_frozen_palette_checks_visible_chart_and_diagram_paints(tmp_path,kind):
    contract=evaluate(tmp_path,"console.log(JSON.stringify(decide('介绍API').contract));")
    deck_path,deck=render_case(tmp_path,['technical-diagram-v1' if kind=='diagram' else 'chart-data-v1'])
    deck['design_contract']=contract
    if kind!='diagram':
        deck['slides'][0]['props']['chart_type']=kind
        if kind in ['pie','donut']:
            deck['slides'][0]['props']['series']=deck['slides'][0]['props']['series'][:1]
    html=render(tmp_path,deck_path,deck)
    report=tmp_path/'svg-report.json'
    result=_run('probe_deck_runtime.js',str(html),'--report',str(report))
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(report.read_text())['editor']['paletteCompliance']['failures']==[]
    if kind=='column':
        html.write_text(html.read_text().replace('</head>','<style>#deck-root [data-pptx-chart] svg path {fill:#F10000!important;}</style></head>'))
        result=_run('probe_deck_runtime.js',str(html),'--report',str(report))
        assert result.returncode!=0
        assert any(item['property']=='fill' for item in json.loads(report.read_text())['editor']['paletteCompliance']['failures'])
