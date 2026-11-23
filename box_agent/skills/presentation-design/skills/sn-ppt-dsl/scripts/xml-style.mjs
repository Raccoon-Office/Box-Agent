// 保持 XML 的坐标、文字样式和原生 PPTX 的单位一致：一个坐标单位等于一磅。
export const child = (node, tag) => node?.children?.find(n => n.tag === tag);
export const children = (node, tag) => (node?.children || []).filter(n => n.tag === tag);
export const plain = node => (node?.text || '') + (node?.children || []).map(n => plain(n) + (n.tail || '')).join('');
export const num = (value, fallback = 0) => value === undefined ? fallback : Number(value);
export const bool = (value, fallback = false) => value === undefined ? fallback : ['true', '1'].includes(String(value));
export const fail = (node, message) => { throw new Error(`${node.path}：${message}`); };

export function color(value = 'rgb(0,0,0)') {
  const hex = value.match(/^#?([0-9a-f]{6})([0-9a-f]{2})?$/i);
  if (hex) return { color: hex[1].toUpperCase(), transparency: hex[2] ? Math.round((1 - parseInt(hex[2], 16) / 255) * 100) : 0 };
  const rgb = value.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/);
  if (!rgb) throw new Error(`不支持的颜色：${value}；请使用 rgb/rgba 或六位十六进制颜色`);
  return { color: rgb.slice(1, 4).map(v => Math.round(Number(v)).toString(16).padStart(2, '0')).join('').toUpperCase(), transparency: Math.round((1 - Number(rgb[4] ?? 1)) * 100) };
}

export function frame(node) {
  const a = node.attrs;
  return { x: num(a.topLeftX) / 72, y: num(a.topLeftY) / 72, w: num(a.width) / 72, h: num(a.height) / 72, rotate: num(a.rotation), objectName: node.path };
}

export function border(node) {
  if (!node) return { color: 'FFFFFF', transparency: 100, width: 0 };
  const dashes = { solid: 'solid', dash: 'dash', dot: 'sysDot', 'long-dash': 'lgDash', 'round-dot': 'sysDot', 'dash-dot': 'dashDot', 'long-dash-dot': 'lgDashDot' };
  const dash = node.attrs.dashArray || 'solid';
  if (!dashes[dash]) fail(node, `暂不支持线型 ${dash}`);
  return { ...color(node.attrs.color), width: num(node.attrs.width, 1), dashType: dashes[dash] };
}

export function textStyle(attrs, inherited = {}) {
  const options = { ...inherited };
  const mapping = { fontFamily: 'fontFace', fontSize: 'fontSize', bold: 'bold', italic: 'italic', underline: 'underline', strikethrough: 'strike', letterSpacing: 'charSpacing' };
  for (const [key, target] of Object.entries(mapping)) {
    if (attrs[key] === undefined) continue;
    options[target] = ['bold', 'italic', 'underline', 'strikethrough'].includes(key) ? bool(attrs[key]) : ['fontSize', 'letterSpacing'].includes(key) ? num(attrs[key]) : attrs[key];
  }
  if (attrs.color) Object.assign(options, color(attrs.color));
  if (attrs.fontColor) Object.assign(options, color(attrs.fontColor));
  if (attrs.backgroundColor) options.highlight = color(attrs.backgroundColor).color;
  return options;
}

function inlineRuns(node, inherited) {
  let style = textStyle(node.attrs, inherited);
  const flags = { strong: 'bold', em: 'italic', u: 'underline', del: 'strike' };
  if (flags[node.tag]) style = { ...style, [flags[node.tag]]: true };
  if (node.tag === 'a') style = { ...style, hyperlink: { url: node.attrs.href } };
  if (node.tag === 'br') return [{ text: '\n', options: style }];
  const runs = node.text ? [{ text: node.text, options: style }] : [];
  for (const part of node.children || []) {
    runs.push(...inlineRuns(part, style));
    if (part.tail) runs.push({ text: part.tail, options: style });
  }
  return runs;
}

export function textBlock(content, theme = {}, isShape = false) {
  const a = content.attrs;
  const style = textStyle(a, textStyle(theme[a.textType || 'body'] || {}, { fontFace: 'Noto Sans CJK SC', fontSize: 20, color: '17212B' }));
  const paragraphs = [];
  for (const node of content.children || []) {
    if (node.tag === 'p') paragraphs.push({ node });
    if (['ul', 'ol'].includes(node.tag)) {
      let index = 1;
      for (const item of children(node, 'li')) {
        for (const paragraph of children(item, 'p')) paragraphs.push({ node: paragraph, prefix: node.tag === 'ol' ? `${index++}. ` : '• ' });
      }
    }
  }
  const runs = [];
  const spacing = a.lineSpacing || 'multiple:1.5';
  const [mode, amount] = spacing.split(':');
  const options = { ...style, margin: 0, valign: { middle: 'mid', top: 'top', bottom: 'bottom' }[a.verticalAlign || 'middle'], align: a.textAlign === 'dist' ? 'justify' : (a.textAlign || (isShape ? 'center' : 'left')), wrap: bool(a.wrap, true), fit: { 'no-auto-fit': 'none', 'normal-auto-fit': 'shrink', 'shape-auto-fit': 'resize' }[a.autoFit || 'no-auto-fit'], paraSpaceAfter: 0, paraSpaceBefore: 0, ...(mode === 'fixed' ? { lineSpacing: Number(amount) } : { lineSpacingMultiple: Number(amount) }) };
  paragraphs.forEach(({ node, prefix }, index) => {
    const paragraph = inlineRuns(node, style);
    if (prefix) paragraph.unshift({ text: prefix, options: style });
    if (!paragraph.length) paragraph.push({ text: '', options: style });
    for (const run of paragraph) {
      run.options = { ...run.options, align: node.attrs.textAlign || options.align, paraSpaceAfter: 0 };
    }
    if (index < paragraphs.length - 1) paragraph[paragraph.length - 1].options.breakLine = true;
    runs.push(...paragraph);
  });
  return { runs, options, text: paragraphs.map(({ node, prefix }) => (prefix || '') + plain(node)).join('\n') };
}

const TEXT_ATTRS = 'textType fontFamily fontSize color bold italic underline strikethrough wrap autoFit textAlign verticalAlign lineSpacing letterSpacing';
const GEO = 'id topLeftX topLeftY width height rotation';
export const ATTRS = {
  presentation: 'id width height', title: '', theme: '', background: '', textStyles: '',
  headline: 'fontFamily fontSize fontColor bold italic', 'sub-headline': 'fontFamily fontSize fontColor bold italic', body: 'fontFamily fontSize fontColor bold italic', caption: 'fontFamily fontSize fontColor bold italic',
  slide: 'id', style: '', data: '', note: '',
  shape: `${GEO} type presetHandlers`, content: TEXT_ATTRS,
  p: 'fontFamily fontSize color bold italic underline strikethrough textAlign letterSpacing',
  span: 'fontFamily fontSize color backgroundColor bold italic underline strikethrough',
  strong: '', em: '', u: '', del: '', a: 'href', br: '', ul: '', ol: '', li: '',
  fill: '', fillColor: 'color', border: 'color width dashArray',
  line: 'id startX startY endX endY', startArrow: 'type widthScale', endArrow: 'type widthScale',
  polyline: `${GEO} type flipX flipY`,
  img: `${GEO} src`, crop: 'type anchor presetHandlers', embed: GEO,
  icon: `${GEO} iconType fillColor`,
  table: `${GEO}`, colgroup: '', col: 'width', tr: 'height', td: 'rowspan colspan',
  chart: GEO, chartTitle: 'fontSize fontFamily color bold italic', chartSubTitle: 'fontSize fontFamily color bold italic',
  chartPlotArea: '', chartPlot: 'type yAxisPosition', chartExtra: '', chartData: '', dim1: '', dim2: '', chartField: 'name valueType',
  chartStyle: '', chartBackground: 'color', chartBorder: 'color width', chartColorTheme: '', color: 'value',
  chartLegend: 'position fontSize fontFamily color', chartAxes: '', chartAxis: 'type position min max', chartLabel: 'fontSize color format', chartGridLine: 'color width', chartAxisLine: 'color width',
  chartLabels: 'position category value percentage fontSize color format', chartSeriesList: '', chartSeries: 'index comboType yAxisPosition', chartSectors: 'innerRadius offsetRadius startAngle', chartStack: 'percentage', chartSmooth: '', chartRadar: 'area shape', chartAreas: '',
};

export function validateNodes(node) {
  if (node.svg) {
    if (/<(?:\w+:)?(?:animate|animateTransform|animateMotion)\b/.test(node.svg)) fail(node, '本地 PPTX 为静态页面，不支持 SVG 动画，请先固定目标画面');
    return;
  }
  if (!(node.tag in ATTRS)) fail(node, `本地导出暂不支持 <${node.tag}>，请使用受支持元素；不会静默丢弃`);
  const allowed = new Set(ATTRS[node.tag].split(' '));
  if (node.tag === 'title' && node.path.includes('/textStyles/')) for (const key of ['fontFamily', 'fontSize', 'fontColor', 'bold', 'italic']) allowed.add(key);
  for (const [key, value] of Object.entries(node.attrs)) {
    if (!allowed.has(key)) fail(node, `本地导出暂不支持属性 ${key}`);
    if (['width', 'height', 'topLeftX', 'topLeftY', 'fontSize', 'rotation'].includes(key) && !Number.isFinite(Number(value))) fail(node, `${key} 必须是有限数值`);
  }
  for (const n of node.children || []) validateNodes(n);
}
