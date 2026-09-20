"""Regressions from the five-topic independent-design CLI batch."""
import json

import pytest

from tests.test_pptx_controlled_deck import _run, SKILL_DIR


def js(tmp_path, source, *args):
    path = tmp_path / "probe.cjs"
    path.write_text(source, encoding="utf-8")
    result = _run(str(path), str(SKILL_DIR), *map(str, args))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_every_registered_layout_receives_only_existing_content_bindings(tmp_path):
    data = js(tmp_path, r'''
const path=require('path'),root=process.argv[2],reg=require(path.join(root,'layouts/registry.js'));
const plan=require(path.join(root,'scripts/design_plan_core.js'));
console.log(JSON.stringify(reg.layouts.map(layout=>({id:layout.id,bindings:plan.defaultBindings(layout),
 invalid:Object.keys(plan.defaultBindings(layout)).filter(key=>!layout.fields[key]||layout.fields[key].type==='enum')}))));
''')
    assert data and all(not layout['invalid'] for layout in data)
    statement = next(layout for layout in data if layout['id'] == 'statement-focus-v1')
    assert statement['bindings']['eyebrow'] == ['title']
    assert statement['bindings']['statement'] == ['message']
    assert statement['bindings']['proofs'] == ['bullets']


@pytest.mark.parametrize(('brief', 'primary', 'accent'), [
    ('黄色绿色为主', '#15803D', '#F5C518'),
    ('浅蓝和明黄色，文字少、字号大', '#DCEFFA', '#F5C518'),
    ('配色米白、咖啡棕和少量橄榄绿', '#6B4F3A', '#71814B'),
    ('白底、蓝色强调', '#2563EB', '#2563EB'),
    ('深蓝和米白配色', '#173B63', '#173B63'),
])
def test_user_color_roles_control_chart_colors_without_theme_leakage(tmp_path, brief, primary, accent):
    data = js(tmp_path, r'''
const path=require('path'),root=process.argv[2],design=require(path.join(root,'scripts/design_contract_core.js'));
const theme=require(path.join(root,'scripts/deck_spec_core.js')).getTheme('creative-mode');
const contract=design.inferPaletteContract(process.argv[3]);
console.log(JSON.stringify({contract,palette:design.paletteWithOverrides(theme.palette,{palette:contract})}));
''', brief)
    assert data['contract']['source'] == 'explicit'
    assert data['palette']['primary'] == primary
    assert data['palette']['accent'] == accent
    assert len(set(data['palette']['chart'])) == 4
    assert '#F06CA8' not in data['palette']['chart']
    assert '#E85A1F' not in data['palette']['chart']


def render_case(tmp_path, layouts, theme='technical-blueprint'):
    deck_path = tmp_path / 'deck.json'
    result = _run('inspect_deck_contract.js', *layouts, '--theme', theme, '--no-images', '--out', str(deck_path))
    assert result.returncode == 0, result.stdout + result.stderr
    return deck_path, json.loads(deck_path.read_text())


def render(tmp_path, deck_path, deck):
    deck_path.write_text(json.dumps(deck, ensure_ascii=False), encoding='utf-8')
    html = tmp_path / 'index.html'
    result = _run('render_deck_html.js', str(deck_path), '--out', str(html))
    assert result.returncode == 0, result.stdout + result.stderr
    return html


def probe(html):
    result = _run('probe_deck_runtime.js', str(html), '--viewport', '1440x900')
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_light_background_cover_has_readable_ink_without_vision(tmp_path):
    deck_path, deck = render_case(tmp_path, ['cover-editorial-v1'])
    image = tmp_path / 'white.svg'
    image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><rect width="1920" height="1080" fill="white"/></svg>')
    deck['slides'][0]['background'] = {'src': image.name, 'alt': 'Light technical background', 'treatment': 'wash-light', 'fit': 'cover'}
    deck['slides'][0]['props'].update(title='订单系统技术架构说明', composition='poster')
    html = render(tmp_path, deck_path, deck)
    healthy = probe(html)
    assert healthy['editor']['componentContrast']['failureCount'] == 0
    # The same QA must detect a white-ink regression over the background image.
    html.write_text(html.read_text().replace('</head>', '<style>#deck-root > .slide h1 {color:white!important}</style></head>'))
    broken = probe(html)
    assert any(f['element'] == 'h1' and f['ratio'] < 1.5 for f in broken['editor']['componentContrast']['failures'])


def test_numeric_values_with_different_category_units_get_separate_axes(tmp_path):
    deck_path, deck = render_case(tmp_path, ['chart-data-v1'])
    deck['slides'][0]['props'].update(categories=['收入（万元）', '客户（家）'],
        series=[{'name': '上季度', 'values': ['100','80']}, {'name': '本季度', 'values': ['120','100']}],
        chart_type='column', value_suffix='', stacked='off')
    html = render(tmp_path, deck_path, deck)
    assert 'data-chart-scale="independent"' in html.read_text()


@pytest.mark.parametrize('kind', ['pipeline', 'architecture'])
def test_small_diagrams_keep_labels_legible_and_edges_clear(tmp_path, kind):
    deck_path, deck = render_case(tmp_path, ['technical-diagram-v1'])
    if kind == 'pipeline':
        labels = ['蒸发','凝结','降水']
        edges = [(0,1),(1,2)]
    else:
        labels = ['Web客户端','API网关','订单服务','库存服务','支付服务','数据库']
        edges = [(0,1),(1,2),(1,3),(1,4),(2,5),(3,5),(4,5)]
    deck['slides'][0]['props'].update(diagram_kind=kind,direction='DOWN' if kind=='architecture' else 'RIGHT',
        nodes=[{'id':f'n{i}', 'label':label,'detail':'','kind':'service'} for i,label in enumerate(labels)],
        edges=[{'id':f'e{i}','source':f'n{a}','target':f'n{b}','label':''} for i,(a,b) in enumerate(edges)])
    result = probe(render(tmp_path, deck_path, deck))
    diagram = result['editor']['diagrams'][0]
    assert diagram['nodes'] == len(labels)
    assert diagram['minimumLabelSize'] >= 24
    assert diagram['labelNodeOverlapCount'] == 0
    assert diagram['labelLabelOverlapCount'] == 0


def test_single_problem_and_action_need_no_repeated_filler(tmp_path):
    deck_path, deck = render_case(tmp_path, ['comparison-two-column-v1'])
    for side, title in [('left', '客户反馈整理不及时'), ('right', '每周归档')]:
        deck['slides'][0]['props'][side].update(title=title, items=[])
    html = render(tmp_path, deck_path, deck)
    result = _run('html_self_check.js', str(html), '--report', str(tmp_path / 'qa.json'))
    assert result.returncode == 0, result.stdout + result.stderr
