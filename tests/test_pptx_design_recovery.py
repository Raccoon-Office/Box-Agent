"""A failed designer cannot erase the content-backed delivery floor."""
import json
import hashlib
import os
from pathlib import Path
import pytest
from tests.test_pptx_design_plan import design_case, record_response, run, write, scaffold


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


def test_unread_parseable_first_response_requires_full_second_read(design_case):
    root, outline, _, _ = design_case
    fresh(root, outline)
    selected = {**decision(), 'theme_id': 'blue-professional'}
    record_response(root, selected, read_brief=False)
    first = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert first.returncode != 0
    correction = Path(json.loads(first.stderr)['correction_file'])
    packet = json.loads(correction.read_text())
    sid = record_response(root, selected, read_brief=True)
    log = Path(os.environ['BOX_AGENT_HOME']) / 'sessions' / hashlib.sha256(sid.encode()).hexdigest() / 'session.jsonl'
    with log.open() as stream:
        rows = [json.loads(line) for line in stream]
    for row in rows:
        if row['type'] == 'user/message': row['data']['content'] += f' Read correction_file: {correction}'
    count = len(correction.read_text().splitlines())
    rows.insert(2, {'type': 'tool/result', 'data': {'result': {'success': True, 'rawOutput': {
        'context_resource': {'resource_id': str(correction), 'content_version': hashlib.sha256(correction.read_bytes()).hexdigest(),
                             'start_line': 1, 'end_line': count, 'total_lines': count}}}}})
    log.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    accepted = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert packet['requires_full_read'] is True
    assert accepted.returncode == 0, accepted.stderr
    assert 'plan' in json.loads(accepted.stdout), accepted.stdout
    assert json.loads((root / 'qa/design_delivery.json').read_text())['status'] == 'design_accepted'


def test_recovery_at_schema_limit_preserves_overflow_content_in_partial_delivery(design_case):
    root, outline, _, _ = design_case
    template = outline['slides'][0]
    outline['slides'] = [
        {**template, 'page': 1, 'layout': 'cover', 'title': 'Forty-page cover', 'message': 'Three supporting facts',
         'bullets': [f'{index}-' + '证' * 58 for index in range(3)]},
        *[{**template, 'page': index, 'layout': 'cards', 'title': f'Page {index}', 'message': f'Message {index}',
           'bullets': [f'Unique remaining evidence {index}']} for index in range(2, 41)],
    ]
    fresh(root, outline)
    report = json.loads((root / 'qa/design_delivery.json').read_text())
    assert report['status'] == 'partial'
    assert report['actual_pages'] == report['outline_pages'] == 40
    assert report['page_count_satisfied'] is True
    assert report['cover_layout_degraded'] is True
    assert any('cover' in warning.lower() for warning in report['warnings'])
    html = Path(report['artifact']).read_text()
    deck = json.loads(Path(report['deck']).read_text())
    assert len(deck['slides']) == 40
    assert deck['slides'][0]['layout_id'] == 'cards-grid-v1'
    assert [page['source_outline_page'] for page in deck['slides']] == list(range(1, 41))
    assert 'contenteditable="true"' in html
    content = [page['props'] for page in deck['slides']]
    text = json.dumps(content, ensure_ascii=False)
    assert content[0]['title'] == outline['slides'][0]['title']
    assert content[0]['subtitle'] == outline['slides'][0]['message']
    for page in outline['slides']:
        for bullet in page['bullets']:
            assert text.count(json.dumps(bullet, ensure_ascii=False)) == 1


def test_recovery_capacity_failure_keeps_existing_html_and_complete_input(design_case):
    root, outline, _, _ = design_case
    original_html = (root / 'index.html').read_bytes()
    template = outline['slides'][0]
    outline['slides'] = [{**template, 'page': index, 'title': f'Page {index}',
                          'bullets': [f'Evidence {index}-{item}: ' + '文' * 90 for item in range(13)]}
                         for index in range(1, 41)]
    fresh(root, outline)
    report = json.loads((root / 'qa/design_delivery.json').read_text())
    assert report['status'] == 'partial' and report['terminal'] is True
    assert report['content_complete'] is False and report['page_count_satisfied'] is False
    assert (root / 'index.html').read_bytes() == original_html
    assert Path(report['primary_artifact']).is_file()
    retained_input = json.loads(Path(report['input_artifact']).read_text())
    assert retained_input.get('outline', retained_input)['slides'] == outline['slides']


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


def test_failed_correction_does_not_expand_pages_to_match_rejected_design(design_case):
    root, outline, _, _ = design_case
    fresh(root,outline)
    record_response(root,decision(4))
    run('design_plan.js','accept','design_input.json',cwd=root)
    record_response(root,{'theme_id':'not-registered','slides':[{}]*4})
    result=run('design_plan.js','accept','design_input.json',cwd=root)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='degraded' and report['actual_pages']==len(outline['slides'])
    html=Path(report['artifact']).read_text()
    deck=json.loads(Path(report['deck']).read_text())
    assert len(deck['slides'])==report['actual_pages']
    content=json.dumps([slide['props'] for slide in deck['slides']])
    for page in outline['slides']:
        for bullet in page['bullets']:
            assert content.count(bullet)==1


@pytest.mark.parametrize('cover_layout', [None, 'cover', 'cover-editorial-v1'])
def test_recovery_keeps_outline_page_count_and_cover_content(design_case, cover_layout):
    root, outline, _, _ = design_case
    if cover_layout:
        outline['slides'][0].update(layout=cover_layout, title='Cover: Three topics')
    fresh(root, outline)
    result = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    deck = json.loads(Path(report['deck']).read_text())
    assert report['actual_pages'] == report['outline_pages'] == 3
    assert [page['source_outline_page'] for page in deck['slides']] == [1, 2, 3]
    if cover_layout:
        assert deck['slides'][0]['layout_id'] == 'cover-editorial-v1'
    for original, page in zip(outline['slides'], deck['slides']):
        content = json.dumps(page['props'])
        for text in [original['title'], original['message'], *original['bullets']]:
            assert content.count(text) == 1


def test_recovery_reports_page_count_mismatch_when_content_requires_more_pages(design_case):
    root, outline, _, _ = design_case
    outline['slides'][0]['bullets'] = [f'Unique evidence item {index:02d}' for index in range(13)]
    fresh(root, outline)
    report = json.loads((root/'qa/design_delivery.json').read_text())
    assert report['status'] == 'partial'
    assert report['page_count_satisfied'] is False
    assert report['actual_pages'] > report['outline_pages']
    assert any('page count' in warning.lower() for warning in report['warnings'])
    deck = json.loads(Path(report['deck']).read_text())
    content = json.dumps([page['props'] for page in deck['slides']])
    for original in outline['slides']:
        for bullet in original['bullets']:
            assert content.count(bullet) == 1


@pytest.mark.parametrize('bullet_count', [2, 7])
def test_recovery_retains_cover_bullets_that_exceed_tag_capacity(design_case, bullet_count):
    root, outline, _, _ = design_case
    outline['slides'][0].update(layout='cover', bullets=[
        f'Full supporting evidence for cover item {index}' for index in range(bullet_count)
    ])
    fresh(root, outline)
    report = json.loads((root/'qa/design_delivery.json').read_text())
    deck = json.loads(Path(report['deck']).read_text())
    assert deck['slides'][0]['layout_id'] == 'cover-editorial-v1'
    content = json.dumps([page['props'] for page in deck['slides']])
    for original in outline['slides']:
        for bullet in original['bullets']:
            assert content.count(bullet) == 1
    if bullet_count == 2:
        assert report['actual_pages'] == 3
    else:
        assert report['status'] == 'partial'
        assert report['page_count_satisfied'] is False


def replace_response_text(session_id, text):
    log = Path(os.environ['BOX_AGENT_HOME'])/'sessions'/hashlib.sha256(session_id.encode()).hexdigest()/'session.jsonl'
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    for row in rows:
        if row['type'] == 'assistant/message':
            row['data']['message']['content'] = text
    log.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')


@pytest.mark.parametrize('read_first_brief', [False, True])
def test_complete_second_response_succeeds_after_non_json_first_attempt(design_case, read_first_brief):
    root, outline, _, _ = design_case
    fresh(root, outline)
    first_id = record_response(root, decision(), read_brief=read_first_brief)
    replace_response_text(first_id, 'I could not finish')
    failed = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert failed.returncode != 0
    assert json.loads(failed.stderr)['can_retry'] is True
    complete = decision()
    complete['theme_id'] = 'blue-professional'
    second_id = record_response(root, complete)
    accepted = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout).get('source_session') == second_id
    assert scaffold(root).returncode == 0


def test_second_response_cannot_patch_unread_first_design(design_case):
    root, outline, _, _ = design_case
    fresh(root, outline)
    complete = decision()
    complete['theme_id'] = 'blue-professional'
    record_response(root, complete, read_brief=False)
    failed = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert failed.returncode != 0
    second_id = record_response(root, complete)
    replace_response_text(second_id, json.dumps([
        {'op': 'replace', 'path': '/slides', 'value': complete['slides']},
    ]))
    rejected = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert rejected.returncode == 0, rejected.stderr
    assert json.loads(rejected.stdout).get('status') == 'degraded'


@pytest.mark.parametrize('second_failure', ['unread', 'invalid_design'])
def test_second_response_after_non_json_first_attempt_must_pass_normal_validation(design_case, second_failure):
    root, outline, _, _ = design_case
    fresh(root, outline)
    first_id = record_response(root, decision(), read_brief=False)
    replace_response_text(first_id, 'I could not finish')
    run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    second = decision()
    second['theme_id'] = 'blue-professional' if second_failure == 'unread' else 'not-registered'
    record_response(root, second, read_brief=second_failure != 'unread')
    rejected = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert rejected.returncode == 0, rejected.stderr
    assert json.loads(rejected.stdout).get('status') == 'degraded'
    record_response(root, {**second, 'theme_id': 'blue-professional'})
    third = run('design_plan.js', 'accept', 'design_input.json', cwd=root)
    assert json.loads(third.stdout).get('status') == 'degraded'


def test_missing_response_delivers_fallback_and_preserves_existing_html(design_case):
    root,outline,_,_=design_case
    (root/'index.html').write_text('<html>User edited</html>')
    fresh(root,outline)
    result=run('design_plan.js','accept','design_input.json',cwd=root)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['status']=='degraded'
    assert (root/'index.html').read_text()=='<html>User edited</html>'
    assert (root/'fallback.html').exists()


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
