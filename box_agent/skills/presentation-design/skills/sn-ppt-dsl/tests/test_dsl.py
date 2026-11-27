"""验证真实源文件到可编辑 PPTX 的合同，以及错误不能覆盖已有成品。"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import create_deck, text, slide

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'dsl.py'


def run(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str,args)], capture_output=True, text=True, timeout=120)


def test_real_xml_build_preserves_editable_objects_and_local_changes(tmp_path):
    deck = create_deck(tmp_path / 'deck')
    # 历史任务在真实构建后迁移流程标记，同时保留用户的材料和设计选择。
    pack_file = deck / 'task_pack.json'
    pack_file.write_text(json.dumps({
        'ppt_mode': 'standard',
        'request': {'query': '保留现有内容'},
        'choices': {'output': 'static_html', 'static_postprocess': ['pptx'], 'design_richness': 'rich'},
        'state': {'artifacts': {'outline': str(deck / 'outline.md')}},
    }, ensure_ascii=False))
    result = run('build', deck)
    assert result.returncode == 0, result.stderr
    artifact = deck / 'presentation.pptx'
    pack = json.loads(pack_file.read_text())
    assert pack['ppt_mode'] == 'dsl' and pack['choices']['output'] == 'xml_pptx'
    assert 'static_postprocess' not in pack['choices']
    assert pack['choices']['design_richness'] == 'rich'
    assert pack['request']['query'] == '保留现有内容'
    assert pack['state']['artifacts']['pptx'] == str(artifact)
    assert pack['state']['artifacts']['outline'] == str(deck / 'outline.md')
    assert pack['state']['status'] == 'rendered'
    with zipfile.ZipFile(artifact) as archive:
        assert 'ppt/charts/chart1.xml' in archive.namelist()
        assert any(name.endswith('.xlsx') for name in archive.namelist())
        root = ET.fromstring(archive.read('ppt/slides/slide4.xml'))
        assert len(root.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}tbl')) == 1
        cover = archive.read('ppt/slides/slide1.xml')
        assert b'<a:gradFill' in cover
        assert '让内容可改' in cover.decode()
    untouched = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (deck/'slides').glob('*.xml') if p.name != 'slide_02.xml'}
    page = deck/'slides/slide_02.xml'
    page.write_text(page.read_text().replace('先检查结构，再交付真实成品','局部修改后重新验证成品'))
    assert run('build',deck).returncode == 0
    assert untouched == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (deck/'slides').glob('*.xml') if p.name != 'slide_02.xml'}
    with zipfile.ZipFile(artifact) as archive:
        assert '局部修改后重新验证成品' in archive.read('ppt/slides/slide2.xml').decode()


def test_invalid_layout_and_unsupported_features_never_replace_output(tmp_path):
    source = tmp_path/'source.xml'
    output = tmp_path/'preserved.pptx'
    output.write_bytes(b'previous-good-deck')
    cases = {
        '重叠': slide(text(60,100,500,90,'文字甲',32)+text(60,100,500,90,'文字乙',32),1),
        '溢出': slide(text(60,100,80,30,'这是一段肯定无法装入小文本框的中文内容',30),1),
        '未知导出属性': slide(text(60,100,500,90,'正常文字',32).replace('<p>','<p lineSpacing="multiple:1.3">'),1),
        '图片丢失': slide('<img src="missing.png" topLeftX="60" topLeftY="100" width="100" height="100"/>',1),
    }
    for name, xml in cases.items():
        source.write_text(xml)
        result = run('build',source,'--output',output)
        assert result.returncode != 0, name
        assert json.loads(result.stderr)['ok'] is False
        assert output.read_bytes() == b'previous-good-deck', name

    # 场景插画若实际是程序绘制，构建必须失败并保留旧成品。
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'scene.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><circle cx="50" cy="50" r="30"/></svg>')
    (assets / 'sources.json').write_text(json.dumps([
        {'file': 'scene.svg', 'role': 'scene_illustration', 'origin': 'programmatic_vector'}
    ]))
    source.write_text(slide('<img src="assets/scene.svg" topLeftX="60" topLeftY="100" width="100" height="100"/>', 1))
    result = run('build', source, '--output', output)
    assert result.returncode != 0
    assert '图片用途' in json.loads(result.stderr)['error']
    assert output.read_bytes() == b'previous-good-deck'
