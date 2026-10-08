"""Regressions from the minimal Forbidden City brief in the desktop client."""
import json
import re

import pytest
from PIL import Image

from tests.test_pptx_design_regressions import js, render_case, render, probe
from tests.test_pptx_html_export import _export_svg_background
from tests.test_pptx_controlled_deck import _run


@pytest.mark.parametrize('primary', ['#8F1D22', '#F1EE2E'])
@pytest.mark.parametrize('layout', ['image-full-bleed-v1', 'cover-hero-v1'])
def test_dark_image_cover_uses_readable_palette_color_without_changing_inner_pages(tmp_path, primary, layout):
    contract = js(tmp_path, r'''
const root=process.argv[2],c=require(root+'/scripts/design_contract_core.js');
console.log(JSON.stringify(c.frozenPaletteContract(null,{background:'#F4F0E8',text:'#24333A',primary:process.argv[3],accent:'#B2764A',secondary:'#E3BE58',accent_usage:'sparse'},'cultural exhibit',[])));
''', primary)
    deck_path, deck = render_case(tmp_path, [layout, 'comparison-two-column-v1'], 'biennale-yellow')
    Image.new('RGB', (1920, 1080), '#241111').save(tmp_path/'background.png')
    deck['design_contract'] = contract
    deck['slides'][0]['layout_id'] = layout
    deck['slides'][0]['background'] = {'src':'background.png','alt':'Palace concept','treatment':'wash-dark','fit':'cover'}
    deck['slides'][0]['props'] = (dict(eyebrow='建筑与生活', title='故宫：读懂一座城', body='穿过宫门，读懂建筑与生活', caption='AI概念视觉')
                                if layout == 'image-full-bleed-v1' else
                                dict(eyebrow='建筑与生活', title='故宫：读懂一座城', subtitle='穿过宫门，读懂建筑与生活', meta='AI概念视觉', hero=None, media_side='right', composition='open'))
    data = probe(render(tmp_path, deck_path, deck))
    assert data['editor']['componentContrast']['failureCount'] == 0
    assert data['editor']['paletteCompliance']['failures'] == []
    assert data['editor']['palette']['primary'] == primary


@pytest.mark.parametrize('count', [4, 5, 6])
def test_multi_row_metrics_do_not_use_an_oversized_sparse_type_scale(tmp_path, count):
    deck_path, deck = render_case(tmp_path, ['kpi-grid-v1'], 'biennale-yellow')
    slide = deck['slides'][0]
    slide['props'] = dict(eyebrow='宫城尺度', title='先把它当作一座城',
                          subtitle='城墙、护城河、四门与分区，共同构成一座边界清晰、秩序严整的宫城。',
                          variant='ledger', composition='open',
                          items=[dict(label='南北长度', value='961米', detail='从午门方向延伸至神武门方向') for _ in range(count)])
    html = render(tmp_path, deck_path, deck)
    assert 'data-presentation-density="regular"' in html.read_text()
    report = tmp_path / 'html-check.json'
    check = _run('html_self_check.js', str(html), '--report', str(report))
    assert check.returncode == 0, check.stdout + check.stderr
    assert not any('overflow' in warning for warning in json.loads(report.read_text())['warnings'])


def test_diagram_drops_template_authoring_help_but_keeps_a_custom_note(tmp_path):
    deck_path, deck = render_case(tmp_path, ['technical-diagram-v1'], 'biennale-yellow')
    help_text = deck['slides'][0]['props']['note']
    assert 'DiagramSpec' in help_text
    def visible_note():
        html = render(tmp_path, deck_path, deck).read_text()
        return re.search(r'<p[^>]*class="technical-diagram-note"[^>]*>(.*?)</p>', html, re.S).group(1)
    assert help_text not in visible_note()
    custom = '宫门与院落的序列，体现空间从开放到封闭的转变。'
    deck['slides'][0]['props']['note'] = custom
    assert custom in visible_note()


def test_timeline_decoration_survives_hidden_text_ancestors_without_revealing_hidden_shapes(tmp_path):
    background = _export_svg_background(tmp_path, '''<div class="graphic"><p>Native route label</p>
      <span style="position:absolute;left:0;top:40px;width:300px;height:8px;background:#ff0000"></span>
      <span style="position:absolute;left:40px;top:60px;width:24px;height:24px;background:#00ff00;border-radius:50%"></span>
      <span style="visibility:hidden;position:absolute;left:100px;top:100px;width:40px;height:40px;background:#ff00ff"></span>
    </div>''')
    assert background.getpixel((200, 203)) == (255, 0, 0)
    assert background.getpixel((152, 232)) == (0, 255, 0)
    assert background.getpixel((220, 280)) == (255, 255, 255)


@pytest.mark.parametrize('primary', ['#9E2A22', '#F1EE2E'])
def test_accent_surface_keeps_readable_body_ink_under_a_frozen_palette(tmp_path, primary):
    contract = js(tmp_path, r'''
const c=require(process.argv[2]+'/scripts/design_contract_core.js');
console.log(JSON.stringify(c.frozenPaletteContract(null,{background:'#F4F0E6',text:'#211E1B',primary:process.argv[3],accent:'#C79A32',secondary:'#315F68',accent_usage:'sparse'},'palace space',[])));
''', primary)
    deck_path, deck = render_case(tmp_path, ['system-integration-v1'], 'property-atlas')
    deck['design_contract'] = contract
    data = probe(render(tmp_path, deck_path, deck))
    assert data['editor']['componentContrast']['failureCount'] == 0
    assert data['editor']['paletteCompliance']['failures'] == []
    assert data['editor']['palette']['primary'] == primary


def test_confirmed_render_defects_request_only_affected_pages_and_optional_vision_does_not(tmp_path):
    data = js(tmp_path, r'''
const q=require(process.argv[2]+'/scripts/render_quality.js').renderQuality;
console.log(JSON.stringify({bad:q({warnings:['slide-02 span.card-index: text/content overflow detected (xy, 43px).']},{editor:{componentContrast:{failures:[{slide:1,element:'h1',ratio:1.24,text:'故宫'}, {slide:3,element:'p',ratio:3.8,text:'Secondary note'}]}}}),
 optional:q({warnings:['Optional vision unavailable']},{warnings:['transparent gradient/image background']})}));
''')
    assert data['bad']['ok'] is False
    assert data['bad']['affected_pages'] == [1, 2]
    assert {issue['kind'] for issue in data['bad']['issues']} == {'unreadable_text','text_overflow'}
    assert data['optional']['ok'] is True


def test_subject_expression_is_preserved_and_invalid_page_expression_is_rejected(tmp_path):
    data = js(tmp_path, r'''
const p=require(process.argv[2]+'/scripts/design_plan_core.js');
const input=p.makeInput({deck_goal:'故宫',slides:[{title:'中轴',message:'空间秩序',bullets:['宫门','宫殿']}]},'故宫');
const d={theme_id:'biennale-yellow',palette:{background:'#F4F0E8',text:'#24333A',primary:'#8F1D22',accent:'#B2764A',secondary:'#E3BE58',accent_usage:'sparse'},reason:'Palace spatial order',slides:[{layout_id:'timeline-horizontal-v1',subject_expression:'沿宫门至宫殿的空间路线组织节点，表现故宫中轴递进'}]};
d.visual_requirements={canvas:'any',heading:'any',display_font:'any',body_font:'any',shadow:'any',allow_plain_fallback:true};
const plan=p.canonicalPlan(d,input);d.slides[0].subject_expression='红色';
let error;try{p.canonicalPlan(d,input)}catch(e){error=e.message}
console.log(JSON.stringify({plan,error}));
''')
    assert '中轴递进' in data['plan']['slides'][0]['subject_expression']
    assert 'subject_expression' in data['error']
