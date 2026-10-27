"""Regressions for offline paper decks stuck in advisory and layout repair loops."""
import base64
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

from tests.test_pptx_controlled_deck import _run
from tests.test_pptx_design_regressions import js, render_case, render


def source_env(text):
    return {**os.environ, "BOX_AGENT_SOURCE_TEXT_B64": base64.b64encode(text.encode()).decode()}


def test_visual_enums_survive_truth_checks_while_visible_claims_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_SOURCE_TEXT_B64", base64.b64encode(
        "技术分享。只使用我提供的事实。".encode()).decode())
    result = js(tmp_path, r'''
const t=require(process.argv[2]+'/scripts/validate_deck_truth.js');
const deck={truth_contract:{mode:'source_bound',source_facts:['技术分享']},slides:[{
 id:'slide-01',layout_id:'cards-grid-v1',props:{variant:'featured',title:'技术分享',
 items:[{title:'featured award',body:'未经来源证实的获奖信息'}]}}]};
console.log(JSON.stringify({check:t.validateSourceBoundDeck(deck),clean:t.sanitizeStrictSourceDeck(deck)}));
''')
    diagnostics = result['check']['issues'] + result['check']['warnings']
    assert not any('.variant:' in issue for issue in diagnostics)
    assert any('.items.0.title:' in issue for issue in diagnostics)
    assert result['clean']['deck']['slides'][0]['props']['variant'] == 'featured'
    assert result['clean']['deck']['slides'][0]['props']['items'][0]['title'] == '待补充'


@pytest.mark.parametrize(('label', 'value', 'fact', 'backed'), [
    ('准确率 (%)', '82.79', '准确率为 82.79%', True),
    ('准确率 (%)', '0.8279', '准确率为 82.79%', False),
    ('准确率 (%)', '82.79', '耗时为 82.79 秒', False),
    ('耗时（秒）', '82.79', '准确率为 82.79%', False),
])
def test_bar_chart_matches_percent_only_with_displayed_unit(tmp_path, label, value, fact, backed):
    result = js(tmp_path, r'''
const t=require(process.argv[2]+'/scripts/validate_deck_truth.js');
console.log(JSON.stringify(t.validateSourceBoundDeck({truth_contract:{mode:'source_bound',source_facts:[],research_facts:[process.argv[5]]},slides:[{
 id:'slide-01',layout_id:'chart-bar-v1',props:{series_label:process.argv[3],items:[{label:'方法甲',value:process.argv[4]}]}}]})));
''', label, value, fact)
    assert (not any('.items.0.value:' in issue for issue in result['issues'] + result['warnings'])) is backed


def test_fact_additions_preserve_content_and_are_idempotent(tmp_path):
    env = source_env('论文介绍。准确率为 82.79%。')
    deck_path, deck = render_case(tmp_path, ['cards-grid-v1'])
    deck['truth_contract'] = {'mode': 'source_bound', 'source_facts': ['论文介绍'], 'research_facts': [], 'assumptions': []}
    deck_path.write_text(json.dumps(deck))
    patch = tmp_path / 'facts.json'
    patch.write_text(json.dumps({'truth_contract': {
        'add_source_facts': ['准确率为 82.79%'],
        'add_research_facts': ['论文 Table A: 方法甲准确率为 82.79%'],
    }}))
    for _ in range(2):
        result = _run('apply_deck_patch.js', str(deck_path), str(patch), env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report['patched_slides'] == []
        assert report['blocking'] is False
    actual = json.loads(deck_path.read_text())
    assert actual['truth_contract']['source_facts'] == ['论文介绍', '准确率为 82.79%']
    assert actual['truth_contract']['research_facts'] == ['论文 Table A: 方法甲准确率为 82.79%']
    actual.pop('truth_contract')
    deck.pop('truth_contract')
    assert actual == deck


@pytest.mark.parametrize(('text', 'field', 'fact'), [
    ('论文介绍。', 'add_source_facts', '捏造准确率 99%'),
    ('只使用我提供的事实。', 'add_research_facts', '外部准确率 99%'),
    ('论文介绍。', 'add_research_facts', '过长' * 200),
])
def test_invalid_fact_additions_leave_deck_unchanged(tmp_path, text, field, fact):
    deck_path, _ = render_case(tmp_path, ['cards-grid-v1'])
    before = deck_path.read_bytes()
    patch = tmp_path / 'facts.json'
    patch.write_text(json.dumps({'truth_contract': {field: [fact]}}))
    result = _run('apply_deck_patch.js', str(deck_path), str(patch), env=source_env(text))
    assert result.returncode == 1
    assert deck_path.read_bytes() == before


@pytest.mark.parametrize('variant', ['numbered', 'featured'])
@pytest.mark.parametrize('count', [5, 6])
def test_dense_card_variants_fit_without_dropping_content(tmp_path, variant, count):
    deck_path, deck = render_case(tmp_path, ['cards-grid-v1'])
    props = deck['slides'][0]['props']
    props.update(title='多路径推理与候选选择', subtitle='根据论文组织方法、实验与工程启示',
                 variant=variant, composition='standard', items=[{
                     'kicker': 'METHOD', 'title': '候选生成与选择策略',
                     'body': '通过不同推理路径构造候选查询，再执行修复与比较。保留各方法的实验设置，结合原文说明适用范围与局限。',
                 } for _ in range(count)])
    html = render(tmp_path, deck_path, deck)
    report_path = tmp_path / 'html-check.json'
    result = _run('html_self_check.js', str(html), '--report', str(report_path))
    report = json.loads(report_path.read_text())
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any('overflow' in issue or 'outside the slide' in issue
                   for issue in report['issues'] + report['warnings'])
    assert json.loads(deck_path.read_text())['slides'][0]['props']['items'] == props['items']


def test_required_pptx_overflow_returns_page_correction_and_preserves_draft(tmp_path):
    deck_path, _ = render_case(tmp_path, ['cards-grid-v1'])
    preload = tmp_path / 'overflow.cjs'
    preload.write_text(r'''
const cp=require('child_process'),fs=require('fs'),path=require('path'),original=cp.spawnSync;
cp.spawnSync=function(command,args,options){
 if(args[0].endsWith('html_self_check.js')){
  const report=args[args.indexOf('--report')+1];fs.mkdirSync(path.dirname(report),{recursive:true});
  fs.writeFileSync(report,JSON.stringify({ok:false,issues:['slide-01 div.cards-grid: visible content extends outside the slide bounds.'],warnings:[]}));
  return {status:1,stdout:'',stderr:'overflow'};
 }
 if(args[0].endsWith('html_to_editable_pptx.js'))throw Error('Must not export an overflowing deck');
 return original.call(this,command,args,options);
};
''')
    html = tmp_path / 'index.html'
    result = _run('finalize_controlled_deck.js', str(deck_path), '--out', str(html),
                  '--require-pptx', env={**os.environ, 'NODE_OPTIONS': f'--require={preload}'})
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report['stage'] == 'html_self_check'
    assert report['ok'] is False and report['blocking'] is True
    assert report['correction_required'] is True
    assert report['render_quality']['affected_pages'] == [1]
    assert '--pages 1' in report['next'] and 'already used' in report['next']
    assert report['delivery_status'] == 'partial' and report['pptx'] is None
    assert html.exists()
    assert json.loads((tmp_path / 'qa/finalization.json').read_text()) == report
    quality = json.loads((tmp_path / 'qa/render_quality.json').read_text())
    assert quality['ok'] is False and quality['advisory'] is False


def test_dense_featured_cards_export_all_text_to_pptx(tmp_path):
    deck_path, deck = render_case(tmp_path, ['cards-grid-v1'])
    items = [{'kicker': '', 'title': f'方法{index}',
              'body': '完整保留候选生成、执行修复与比较的信息，依据原文说明实验设置和适用范围。'}
             for index in range(6)]
    deck['slides'][0]['props'].update(title='技术分享', subtitle='', variant='featured',
                                    composition='standard', items=items)
    deck_path.write_text(json.dumps(deck))
    result = _run('finalize_controlled_deck.js', str(deck_path), '--out',
                  str(tmp_path / 'index.html'), '--require-pptx')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report['ok'] is True and report['blocking'] is False
    assert report['stage'] == 'complete'
    assert json.loads((tmp_path / 'qa/finalization.json').read_text()) == report
    with zipfile.ZipFile(report['pptx']) as package:
        assert package.testzip() is None
        slide = ET.fromstring(package.read('ppt/slides/slide1.xml'))
        text = ''.join(slide.itertext())
        for item in items:
            assert item['title'] in text and item['body'] in text
