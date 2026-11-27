"""生成覆盖中文、原生图表和素材的可复现验收样稿。"""

from pathlib import Path
from xml.sax.saxutils import escape

FONT = "Noto Sans CJK SC"
INK = "rgb(28,42,48)"
TEAL = "rgb(39,106,115)"


def text(x, y, width, height, content, size=22, color=INK, bold=False, raw=False):
    value = content if raw else escape(content)
    return f'<shape type="text" topLeftX="{x}" topLeftY="{y}" width="{width}" height="{height}"><content fontFamily="{FONT}" fontSize="{size}" color="{color}" bold="{str(bold).lower()}" lineSpacing="multiple:1.25" verticalAlign="top"><p>{value}</p></content></shape>'


def shape(x, y, width, height, fill, kind="rect"):
    return f'<shape type="{kind}" topLeftX="{x}" topLeftY="{y}" width="{width}" height="{height}"><fill><fillColor color="{fill}"/></fill></shape>'


def footer(number):
    return text(48, 506, 800, 20, "SN · 本地 XML 排版验证 / 内容与数据仅为测试示例", 10, "rgb(100,112,118)") + text(884, 506, 30, 20, f"{number:02}", 10, TEAL)


def slide(data, number, background="rgb(247,248,245)"):
    return f'<slide xmlns="https://www.larkoffice.com/sml/2.0"><style><fill><fillColor color="{background}"/></fill></style><data>{data}{footer(number)}</data><note><content><p>第 {number} 页用于验证 XML 到原生 PPTX 的实际输出。</p></content></note></slide>'


def create_deck(folder: Path) -> Path:
    from PIL import Image, ImageDraw
    pages = folder / "slides"
    pages.mkdir(parents=True, exist_ok=True)
    assets = folder / "assets"
    assets.mkdir(exist_ok=True)
    image = Image.new("RGB", (600, 400), "#DCEAE3")
    draw = ImageDraw.Draw(image)
    draw.ellipse((90, 30, 510, 450), fill="#276A73")
    draw.rectangle((0, 310, 600, 400), fill="#DE9A70")
    image.save(assets / "sample.png")
    (assets / "sources.json").write_text(
        '[{"file":"sample.png","role":"diagram","origin":"programmatic_vector","purpose":"测试图形"}]',
        encoding="utf-8",
    )

    first = text(48, 42, 780, 24, "LOCAL PRESENTATION / XML DSL", 13, TEAL, True)
    first += text(48, 120, 550, 150, "让版式稳定<br/>让内容可改", 48, raw=True, bold=True)
    first += text(48, 316, 540, 90, "坐标设计、程序校验、原生导出。<br/>把页面表达和实际成品连接起来。", 22, raw=True)
    first += shape(664, 118, 240, 314, "linear-gradient(90deg, rgb(39,106,115) 0%, rgb(77,140,130) 100%)")
    first += text(695, 156, 180, 60, "XML", 42, "rgb(255,255,255)", True)
    first += text(695, 238, 180, 45, "↓", 30, "rgb(255,255,255)")
    first += text(695, 324, 180, 60, "PPTX", 38, "rgb(255,255,255)", True)

    second = text(48, 44, 864, 60, "先检查结构，再交付真实成品", 32, bold=True)
    second += shape(48, 142, 400, 4, TEAL) + shape(510, 142, 400, 4, "rgb(218,131,71)")
    second += text(48, 170, 390, 42, "写入前：检查 XML", 26, TEAL, True)
    second += text(510, 170, 390, 42, "生成后：检查 PPTX", 26, TEAL, True)
    second += text(48, 235, 390, 170, "元素边界与相互遮挡<br/>中文换行与文本容量<br/>素材文件与格式支持", 22, raw=True)
    second += text(510, 235, 390, 170, "原生文字、表格与图表<br/>页数和完整内容<br/>实际字体与最终渲染", 22, raw=True)
    second += text(48, 442, 850, 30, "局部修改后重新检查对应页面，保持其余页面的内容和设计。", 17)

    chart = '''<chart topLeftX="48" topLeftY="160" width="550" height="285"><chartPlotArea><chartPlot type="column"/><chartAxes><chartAxis type="x"><chartLabel fontSize="14"/></chartAxis><chartAxis type="y" min="0" max="100"><chartLabel fontSize="12"/><chartGridLine color="rgb(220,228,229)"/></chartAxis></chartAxes></chartPlotArea><chartData><dim1><chartField name="阶段">初稿,修订,交付</chartField></dim1><dim2><chartField name="测试分数">48,72,91</chartField></dim2></chartData><chartStyle><chartColorTheme><color value="rgb(39,106,115)"/></chartColorTheme></chartStyle></chart>'''
    third = text(48, 44, 864, 60, "图表保留数据，后续仍然可以编辑", 32, bold=True) + chart
    third += text(670, 178, 230, 80, "91", 64, TEAL, True)
    third += text(672, 280, 218, 90, "交付阶段测试分数<br/>原生柱状图 · 示例数据", 18, raw=True)
    third += text(48, 460, 864, 28, "数据声明：本页数字仅用于验证图表功能，不代表实际性能或效果。", 12)

    rows = []
    for index, cells in enumerate([["对象", "交付方式", "可编辑"], ["正文", "原生文本", "是"], ["表格", "原生表格", "是"], ["图表", "原生图表", "是"]]):
        cell_nodes = ''.join(f'<td><fill><fillColor color="{TEAL if index == 0 else "rgb(255,255,255)"}"/></fill><content fontFamily="{FONT}" fontSize="17" color="{"rgb(255,255,255)" if index == 0 else INK}" lineSpacing="multiple:1.2"><p>{item}</p></content></td>' for item in cells)
        rows.append(f'<tr height="58">{cell_nodes}</tr>')
    table = '<table topLeftX="48" topLeftY="160" width="540" height="232"><colgroup><col width="160"/><col width="240"/><col width="140"/></colgroup>'+''.join(rows)+'</table>'
    svg = '<embed topLeftX="652" topLeftY="170" width="250" height="200"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 250 200"><circle cx="125" cy="90" r="70" fill="none" stroke="#D8E6DF" stroke-width="24"/><path d="M125 20 A70 70 0 1 1 58 110" fill="none" stroke="#276A73" stroke-width="24"/></svg></embed>'
    fourth = text(48, 44, 864, 60, "结构化信息采用原生对象", 32, bold=True) + table + svg
    fourth += text(638, 407, 266, 34, "复杂 SVG 保留为图片素材", 16)

    fifth = text(48, 44, 864, 60, "中文混排和图片裁剪也需要验收", 32, bold=True)
    fifth += text(48, 160, 500, 180, '支持 <strong>加粗文字</strong>、<span color="rgb(39,106,115)">局部强调</span>，以及 XML、PPTX、99% 等中英文混排。<br/>内容保持完整，正文不会通过静默截断来适应版式。', 22, raw=True)
    fifth += text(48, 388, 500, 56, '来源示例：<a href="https://github.com/OpenSenseNova/SenseNova-Skills">SenseNova Skills</a>', 16, raw=True)
    fifth += '<img src="../assets/sample.png" topLeftX="620" topLeftY="156" width="284" height="260"><crop type="rect"/></img>'
    for index, body in enumerate([first, second, third, fourth, fifth], 1):
        (pages / f"slide_{index:02}.xml").write_text(slide(body, index), encoding="utf-8")
    (folder / "outline.md").write_text('# 本地 XML DSL 验收\n\n'+'\n'.join(f'## 第 {i} 页：功能验收\n' for i in range(1,6)),encoding='utf-8')
    return folder
