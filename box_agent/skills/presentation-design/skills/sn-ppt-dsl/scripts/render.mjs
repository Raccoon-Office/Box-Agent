// 编译经过校验的 XML 树，直接输出原生 PPTX 对象，不经过 HTML 页面转换。
import fs from 'node:fs/promises';
import path from 'node:path';
import PptxGenJS from 'pptxgenjs';
import JSZip from 'jszip';
import { createRequire } from 'node:module';
const sharp = createRequire(import.meta.url)('sharp');
import Icons from '@icon-park/svg';
import { child, children, plain, num, bool, color, frame, border, textBlock, validateNodes, fail } from './xml-style.mjs';
import { chartSpec } from './charts.mjs';

const SHAPES = {
  text: 'rect', rect: 'rect', 'round-rect': 'roundRect', 'slides-full-round-rect': 'roundRect',
  ellipse: 'ellipse', triangle: 'triangle', diamond: 'diamond', parallelogram: 'parallelogram',
  trapezoid: 'trapezoid', pentagon: 'pentagon', hexagon: 'hexagon', octagon: 'octagon',
  'right-arrow': 'rightArrow', 'left-arrow': 'leftArrow', 'up-arrow': 'upArrow', 'down-arrow': 'downArrow',
  chevron: 'chevron', 'flow-chart-process': 'flowChartProcess', 'flow-chart-decision': 'flowChartDecision',
  'flow-chart-terminator': 'flowChartTerminator', 'flow-chart-document': 'flowChartDocument',
};
const ARROWS = { none: undefined, arrow: 'triangle', 'solid-triangle': 'triangle', 'empty-triangle': 'triangle', 'solid-diamond': 'diamond', 'empty-diamond': 'diamond', 'empty-circle': 'oval' };

function parts(text) {
  const output = []; let depth = 0; let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '(') depth++;
    if (text[i] === ')') depth--;
    if (text[i] === ',' && !depth) { output.push(text.slice(start, i).trim()); start = i + 1; }
  }
  output.push(text.slice(start).trim()); return output;
}

function gradient(value, node) {
  const match = value.match(/^linear-gradient\((.*)\)$/s);
  if (!match) fail(node, '当前支持纯色和 linear-gradient；其他渐变请作为本地 SVG 素材');
  const args = parts(match[1]);
  const angle = args[0].match(/^(-?[\d.]+)deg$/);
  if (!angle) fail(node, '线性渐变需要明确角度，例如 90deg');
  const stops = args.slice(1).map((part, index, all) => {
    const match = part.match(/^(rgba?\([^)]*\)|#[\da-f]{6})(?:\s+([\d.]+)%)?$/i);
    if (!match) fail(node, `无法解析渐变色标 ${part}`);
    return { ...color(match[1]), position: match[2] === undefined ? index / (all.length - 1) : Number(match[2]) / 100 };
  });
  if (stops.length < 2) fail(node, '渐变至少需要两个色标');
  const stopXML = stops.map(stop => `<a:gs pos="${Math.round(stop.position * 100000)}"><a:srgbClr val="${stop.color}"><a:alpha val="${(100 - stop.transparency) * 1000}"/></a:srgbClr></a:gs>`).join('');
  return `<a:gradFill rotWithShape="1"><a:gsLst>${stopXML}</a:gsLst><a:lin ang="${Math.round(((Number(angle[1]) - 90 + 360) % 360) * 60000)}" scaled="0"/></a:gradFill>`;
}

function fillOptions(fill, gradients, node) {
  const value = child(fill, 'fillColor')?.attrs.color;
  if (!value) return { color: 'FFFFFF', transparency: 100 };
  if (value.includes('gradient(')) {
    const marker = (0xF00100 + gradients.size).toString(16).toUpperCase();
    gradients.set(marker, gradient(value, node));
    return { color: marker };
  }
  return color(value);
}

function arrowOptions(node) {
  const result = {};
  for (const [name, field] of [['startArrow', 'beginArrowType'], ['endArrow', 'endArrowType']]) {
    const arrow = child(node, name);
    if (!arrow) continue;
    if (!(arrow.attrs.type in ARROWS)) fail(arrow, `不支持的箭头 ${arrow.attrs.type}`);
    if (arrow.attrs.type?.startsWith('empty-')) fail(arrow, '空心箭头暂未实现，请使用实心箭头或 SVG');
    if (arrow.attrs.widthScale && arrow.attrs.widthScale !== 'med') fail(arrow, '暂不支持非 med 箭头尺寸');
    result[field] = ARROWS[arrow.attrs.type];
  }
  return result;
}

function shapeOptions(node, pptx, gradients) {
  const name = SHAPES[node.attrs.type];
  if (!name || !pptx.ShapeType[name]) fail(node, `本地导出暂不支持形状 ${node.attrs.type}`);
  const options = { ...frame(node), fill: fillOptions(child(node, 'fill'), gradients, node), line: border(child(node, 'border')) };
  if (name === 'roundRect') options.rectRadius = num(node.attrs.presetHandlers, node.attrs.type === 'slides-full-round-rect' ? Math.min(num(node.attrs.width), num(node.attrs.height)) / 2 : 16) / 72;
  else if (num(node.attrs.presetHandlers)) fail(node, 'presetHandlers 目前仅支持圆角矩形');
  return { name, options };
}

function tableSpec(node, theme, gradients) {
  if (num(node.attrs.rotation)) fail(node, '原生表格暂不支持旋转');
  const columns = children(child(node, 'colgroup'), 'col');
  const rows = children(node, 'tr');
  if (!rows.length) fail(node, '表格没有行');
  const count = columns.length || children(rows[0], 'td').length;
  if (!count) fail(node, '表格没有列');
  const totalW = num(node.attrs.width), totalH = num(node.attrs.height);
  const widths = columns.length ? columns.map(n => num(n.attrs.width, totalW / count)) : Array(count).fill(totalW / count);
  const heights = rows.map(n => num(n.attrs.height, totalH / rows.length));
  if (Math.abs(widths.reduce((a, b) => a + b) - totalW) > 1 || Math.abs(heights.reduce((a, b) => a + b) - totalH) > 1) fail(node, '表格列宽/行高之和必须与整体 width/height 相同');
  const texts = [];
  const data = rows.map(row => {
    const cells = children(row, 'td');
    if (cells.length !== count) fail(row, '当前本地导出要求每行单元格数量一致');
    return cells.map(cell => {
      if (num(cell.attrs.rowspan, 1) !== 1 || num(cell.attrs.colspan, 1) !== 1) fail(cell, '原生表格合并单元格暂未实现，请拆分或使用原生形状组合');
      const content = child(cell, 'content');
      const block = content ? textBlock(content, theme) : { runs: [], options: {}, text: '' };
      texts.push(block.text);
      return { text: block.runs, options: { ...block.options, margin: 5, fill: fillOptions(child(cell, 'fill'), gradients, cell), border: child(cell, 'border') ? { type: 'solid', pt: num(child(cell, 'border').attrs.width, 1), color: color(child(cell, 'border').attrs.color).color } : { type: 'solid', pt: 0.5, color: 'D9E0E4' } } };
    });
  });
  return { data, texts, options: { ...frame(node), colW: widths.map(n => n / 72), rowH: heights.map(n => n / 72), autoPage: false, margin: 5, fontFace: 'Noto Sans CJK SC', fontSize: 16, border: { type: 'solid', pt: 0.5, color: 'D9E0E4' } } };
}

async function addPicture(slide, node, pptx, checkOnly) {
  const svg = child(node, 'svg');
  let buffer;
  if (node.tag === 'icon') {
    const slug = node.attrs.iconType.split('/').at(-1).replace(/\.svg$/, '');
    const name = slug.split('-').map(word => word[0].toUpperCase() + word.slice(1)).join('');
    const draw = Icons[name];
    if (typeof draw !== 'function') fail(node, `本地图标库中没有 ${node.attrs.iconType}`);
    const ink = child(child(node, 'fill'), 'fillColor')?.attrs.color || 'rgb(39,106,115)';
    buffer = Buffer.from(draw({ theme: 'outline', size: 48, fill: '#' + color(ink).color, strokeWidth: 3 }));
  } else buffer = node.tag === 'embed' ? Buffer.from(svg?.svg || '') : await fs.readFile(node.asset);
  if (!buffer.length) fail(node, 'SVG 内容为空');
  const crop = child(node, 'crop');
  const type = crop?.attrs.type || 'rect';
  if (!['rect', 'ellipse', 'round-rect'].includes(type)) fail(node, `暂不支持裁剪形状 ${type}`);
  const meta = await sharp(buffer).metadata();
  if (!meta.width || !meta.height) fail(node, '无法读取图片尺寸');
  if (checkOnly) return;
  const f = frame(node);
  const anchor = crop?.attrs.anchor || 'center';
  const positions = { center: 'centre', top: 'north', bottom: 'south', left: 'west', right: 'east' };
  if (!positions[anchor]) fail(crop, `暂不支持裁剪锚点 ${anchor}`);
  // 固定裁剪后的位图，避免不同 Office 应用对图片裁剪关系的解释不一致。
  const width = Math.max(1, Math.round(f.w * 144)), height = Math.max(1, Math.round(f.h * 144));
  let image = sharp(buffer, { density: 144 }).resize(width, height, { fit: 'cover', position: positions[anchor] });
  if (type !== 'rect') {
    const radius = Math.min(width / 2, height / 2, num(crop?.attrs.presetHandlers, 16) * 2);
    const mask = type === 'ellipse' ? `<ellipse cx="${width / 2}" cy="${height / 2}" rx="${width / 2}" ry="${height / 2}" fill="white"/>` : `<rect width="${width}" height="${height}" rx="${radius}" fill="white"/>`;
    image = image.composite([{ input: Buffer.from(`<svg width="${width}" height="${height}">${mask}</svg>`), blend: 'dest-in' }]);
  }
  const png = await image.png().toBuffer();
  slide.addImage({ ...f, data: 'image/png;base64,' + png.toString('base64') });
  const edge = child(node, 'border');
  if (edge) slide.addShape(type === 'ellipse' ? pptx.ShapeType.ellipse : type === 'round-rect' ? pptx.ShapeType.roundRect : pptx.ShapeType.rect, { ...f, fill: { transparency: 100 }, line: border(edge) });
}

async function build(root, output) {
  validateNodes(root);
  if (num(root.attrs.width) !== 960 || num(root.attrs.height) !== 540) fail(root, '当前移植保持原协议的 960×540 画布');
  const pptx = new PptxGenJS();
  pptx.defineLayout({ name: 'SML', width: 960 / 72, height: 540 / 72 });
  pptx.layout = 'SML';
  pptx.author = 'Hermes';
  pptx.subject = '本地 XML DSL 生成';
  pptx.title = plain(child(root, 'title')) || '演示文稿';
  pptx.lang = 'zh-CN';
  pptx.theme = { headFontFace: 'Noto Sans CJK SC', bodyFontFace: 'Noto Sans CJK SC', lang: 'zh-CN' };
  const styles = child(child(root, 'theme'), 'textStyles');
  const theme = Object.fromEntries((styles?.children || []).map(n => [n.tag, n.attrs]));
  const gradients = new Map();
  const pages = children(root, 'slide');
  if (!pages.length) fail(root, '演示文稿没有页面');
  const texts = [];
  for (const page of pages) {
    const slide = pptx.addSlide();
    const background = child(child(page, 'style'), 'fill') || child(child(root, 'theme'), 'background');
    const bgFill = background?.tag === 'background' ? child(background, 'fill') : background;
    slide.background = bgFill ? fillOptions(bgFill, gradients, page) : { color: 'FFFFFF' };
    const pageTexts = [];
    const pageLabels = new Set(children(child(page, 'data'), 'shape')
      .flatMap(shape => children(child(shape, 'content'), 'p').map(p => plain(p).trim())));
    for (const node of child(page, 'data')?.children || []) {
      if (node.tag === 'shape') {
        const spec = shapeOptions(node, pptx, gradients);
        const content = child(node, 'content');
        if (content && plain(content).trim()) {
          const block = textBlock(content, theme, node.attrs.type !== 'text');
          pageTexts.push(block.text);
          slide.addText(block.runs, { ...spec.options, ...block.options, shape: pptx.ShapeType[spec.name] });
        } else slide.addShape(pptx.ShapeType[spec.name], spec.options);
      } else if (node.tag === 'line') {
        const a = node.attrs;
        const x1 = num(a.startX), y1 = num(a.startY), x2 = num(a.endX), y2 = num(a.endY);
        slide.addShape(pptx.ShapeType.line, { x: Math.min(x1, x2) / 72, y: Math.min(y1, y2) / 72, w: Math.abs(x2 - x1) / 72, h: Math.abs(y2 - y1) / 72, flipH: x2 < x1, flipV: y2 < y1, line: { ...border(child(node, 'border')), ...arrowOptions(node) }, objectName: node.path });
      } else if (node.tag === 'polyline') {
        const map = { 'bent-connector2': 'bentConnector2', 'bent-connector3': 'bentConnector3', 'bent-connector4': 'bentConnector4', 'bent-connector5': 'bentConnector5', 'curved-connector2': 'curvedConnector2', 'curved-connector3': 'curvedConnector3', 'curved-connector4': 'curvedConnector4', 'curved-connector5': 'curvedConnector5' };
        const name = map[node.attrs.type || 'bent-connector2'];
        if (!name || !pptx.ShapeType[name]) fail(node, '暂不支持该连接线');
        slide.addShape(pptx.ShapeType[name], { ...frame(node), flipH: bool(node.attrs.flipX), flipV: bool(node.attrs.flipY), line: { ...border(child(node, 'border')), ...arrowOptions(node) } });
      } else if (['img', 'embed', 'icon'].includes(node.tag)) {
        await addPicture(slide, node, pptx, !output);
      } else if (node.tag === 'table') {
        const spec = tableSpec(node, theme, gradients);
        pageTexts.push(...spec.texts);
        slide.addTable(spec.data, spec.options);
      } else if (node.tag === 'chart') {
        const spec = chartSpec(node, pptx, pageLabels, slide.background?.color);
        if (Array.isArray(spec.type)) slide.addChart(spec.type, spec.options);
        else slide.addChart(spec.type, spec.data, spec.options);
      } else fail(node, `暂不支持页面元素 ${node.tag}`);
    }
    const note = child(page, 'note');
    if (note) slide.addNotes(plain(note));
    texts.push(pageTexts);
  }
  if (output) {
    let buffer = await pptx.write({ outputType: 'nodebuffer', compression: true });
    if (gradients.size) {
      const archive = await JSZip.loadAsync(buffer);
      for (const name of Object.keys(archive.files).filter(n => /^ppt\/slides\/slide\d+\.xml$/.test(n))) {
        let xml = await archive.file(name).async('string');
        for (const [marker, replacement] of gradients) {
          const pattern = new RegExp(`<a:solidFill><a:srgbClr val="${marker}"(?:\\s*/>|>[\\s\\S]*?</a:srgbClr>)</a:solidFill>`, 'g');
          xml = xml.replace(pattern, replacement);
        }
        archive.file(name, xml);
      }
      buffer = await archive.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
    }
    await fs.writeFile(output, buffer);
  }
  return { ok: true, slides: pages.length, output: output || null, texts, source_sha256: root.source_sha256, editable: ['text', 'shapes', 'tables', 'charts'], rasterized: ['images', 'embedded_svg'] };
}

try {
  const [, , input, destination] = process.argv;
  if (!input || !destination) throw new Error('用法：通过 dsl.py check/build 调用此编译器');
  const root = JSON.parse(await fs.readFile(input, 'utf8'));
  const result = await build(root, destination === '--check' ? null : path.resolve(destination));
  console.log(JSON.stringify(result));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
