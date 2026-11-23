// 将 SML 图表数据映射为带内嵌工作簿的原生 PowerPoint 图表。
import { child, children, plain, num, bool, color, frame, fail } from './xml-style.mjs';

function csv(text) {
  const output = []; let field = ''; let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"' && text[i + 1] === '"' && quoted) { field += '"'; i++; }
    else if (char === '"') quoted = !quoted;
    else if (char === ',' && !quoted) { output.push(field.trim()); field = ''; }
    else field += char;
  }
  if (quoted) throw new Error('图表 CSV 数据引号未闭合');
  output.push(field.trim()); return output;
}

const TYPE_MAP = { column: 'bar', bar: 'bar', line: 'line', area: 'area', pie: 'pie', radar: 'radar' };

function seriesPalette(colors, count) {
  const result = [...colors];
  const fallback = ['0072B2', 'D55E00', '009E73', 'CC79A7', 'E69F00', '56B4E9', '542788', '4D4D4D'];
  const rgb = hex => [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
  const distance = (a, b) => rgb(a).reduce((sum, channel, i) => sum + (channel - rgb(b)[i]) ** 2, 0);
  while (result.length < count) {
    const choices = fallback.filter(value => !result.includes(value));
    if (!choices.length) break;
    const nearest = value => Math.min(...result.map(prior => distance(value, prior)));
    result.push(choices.reduce((best, value) => nearest(value) > nearest(best) ? value : best));
  }
  return result;
}

export function chartSpec(node, pptx, pageLabels = new Set(), pageBackground) {
  if (num(node.attrs.rotation)) fail(node, '原生图表暂不支持旋转');
  const area = child(node, 'chartPlotArea');
  const plots = children(area, 'chartPlot');
  if (plots.length !== 1) fail(node, '本地原生图表目前每页图表只接受一个 chartPlot');
  const plot = plots[0];
  if (plot.attrs.yAxisPosition === 'right') fail(plot, '第二纵轴暂未实现，请使用单轴图表');
  const type = plot.attrs.type;
  if (!(type in TYPE_MAP) && type !== 'combo') fail(plot, `暂不支持图表类型 ${type}`);
  const dataNode = child(node, 'chartData');
  const categories = children(child(dataNode, 'dim1'), 'chartField');
  if (categories.length !== 1) fail(dataNode, '图表需要一个 dim1 分类字段');
  const labels = csv(plain(categories[0]));
  const fields = children(child(dataNode, 'dim2'), 'chartField');
  const data = fields.map(field => {
    const raw = csv(plain(field));
    if (raw.length !== labels.length || raw.some(value => !value || !Number.isFinite(Number(value)))) fail(field, '数值必须完整、有限，且与分类数量相同');
    return { name: field.attrs.name, labels, values: raw.map(Number) };
  });
  if (!data.length) fail(dataNode, '图表至少需要一个数值序列');
  const style = child(node, 'chartStyle');
  const colors = children(child(style, 'chartColorTheme'), 'color').map(n => color(n.attrs.value).color);
  const legend = child(node, 'chartLegend');
  const incompletePalette = type !== 'pie' && colors.length > 0 && colors.length < data.length;
  const needsLegend = incompletePalette && !data.every(series => pageLabels.has(series.name));
  const background = child(style, 'chartBackground');
  const chartFill = background ? color(background.attrs.color) : null;
  const surface = chartFill && !chartFill.transparency ? chartFill.color : pageBackground;
  const darkSurface = surface && /^[0-9a-f]{6}$/i.test(surface) &&
    [0.2126, 0.7152, 0.0722].reduce((sum, factor, i) => sum + factor * parseInt(surface.slice(i * 2, i * 2 + 2), 16), 0) < 110;
  const title = child(node, 'chartTitle');
  if (bool(title?.attrs.italic)) fail(title, '原生图表标题暂不支持斜体，请用独立文本框');
  const subtitle = child(node, 'chartSubTitle');
  if (subtitle) fail(subtitle, '副标题请使用独立文本框，避免原生图表丢失该内容');
  const extra = child(plot, 'chartExtra');
  const stack = child(extra, 'chartStack');
  const chartLabels = child(plot, 'chartLabels');
  const config = chartLabels?.attrs || {};
  const position = { top: 'outEnd', inside: 'ctr', outside: 'bestFit', center: 'ctr', bottom: 'inBase', auto: 'bestFit' };
  if (config.position && !position[config.position]) fail(chartLabels, `不支持数据标签位置 ${config.position}`);
  const options = {
    ...frame(node), showTitle: !!title, title: title ? plain(title) : undefined,
    titleFontSize: num(title?.attrs.fontSize, 18), titleColor: color(title?.attrs.color || 'rgb(23,33,43)').color,
    titleFontFace: title?.attrs.fontFamily || 'Noto Sans CJK SC', titleBold: bool(title?.attrs.bold),
    showLegend: !!legend || needsLegend, legendPos: { top: 't', bottom: 'b', left: 'l', right: 'r' }[legend?.attrs.position || 'bottom'],
    legendFontSize: num(legend?.attrs.fontSize, !legend && needsLegend ? 10 : 12), legendFontFace: legend?.attrs.fontFamily || 'Noto Sans CJK SC',
    legendColor: color(legend?.attrs.color || (!legend && needsLegend && darkSurface ? 'rgb(246,248,250)' : 'rgb(23,33,43)')).color,
    chartColors: incompletePalette ? seriesPalette(colors, data.length) : colors.length ? colors : ['276A73', 'DA8347', '7F9B83', '747B9E'],
    catAxisLabelFontFace: 'Noto Sans CJK SC', valAxisLabelFontFace: 'Noto Sans CJK SC',
    catAxisLabelFontSize: 12, valAxisLabelFontSize: 12,
    catAxisLineShow: false, valAxisLineShow: false, showBorder: false,
    showValue: bool(config.value, !!chartLabels), showCatName: bool(config.category), showPercent: bool(config.percentage),
    dataLabelPosition: position[config.position || 'auto'], dataLabelColor: color(config.color || 'rgb(23,33,43)').color,
    dataLabelFormatCode: config.format || '0.##', dataLabelBkgrdColor: undefined,
    dataLabelFontSize: num(config.fontSize, 12),
    lineDataSymbol: type === 'line' ? 'circle' : 'none', lineDataSymbolSize: 5, lineSize: 2,
    barDir: type === 'bar' ? 'bar' : 'col',
    barGrouping: stack ? (bool(stack.attrs.percentage) ? 'percentStacked' : 'stacked') : 'clustered',
  };
  if (type === 'pie') options.showLabel = bool(config.category);
  if (child(extra, 'chartSmooth')) options.lineSmooth = true;
  if (child(extra, 'chartAreas') && type !== 'area') fail(extra, 'chartAreas 仅在 area 图表中可用');
  const radar = child(extra, 'chartRadar');
  if (radar) {
    if (radar.attrs.shape && radar.attrs.shape !== 'polygon') fail(radar, '圆形雷达网格暂未映射，请使用 polygon');
    options.radarStyle = bool(radar.attrs.area) ? 'filled' : 'standard';
  }
  for (const axis of children(child(area, 'chartAxes'), 'chartAxis')) {
    const prefix = axis.attrs.type === 'x' ? 'catAxis' : 'valAxis';
    const label = child(axis, 'chartLabel');
    const line = child(axis, 'chartAxisLine');
    const grid = child(axis, 'chartGridLine');
    if (label?.attrs.fontSize) options[prefix + 'LabelFontSize'] = num(label.attrs.fontSize);
    if (label?.attrs.color) options[prefix + 'LabelColor'] = color(label.attrs.color).color;
    if (label?.attrs.format) options[prefix === 'catAxis' ? 'catLabelFormatCode' : 'valLabelFormatCode'] = label.attrs.format;
    for (const key of ['min', 'max']) if (axis.attrs[key] !== undefined) {
      if (prefix !== 'valAxis') fail(axis, '分类轴不支持数值 min/max');
      options[prefix + (key === 'min' ? 'MinVal' : 'MaxVal')] = num(axis.attrs[key]);
    }
    if (axis.attrs.position === 'right') fail(axis, '双纵轴图表暂未实现，请拆为两个图表');
    if (line) Object.assign(options, { [prefix + 'LineShow']: true, [prefix + 'LineColor']: color(line.attrs.color).color, [prefix + 'LineSize']: num(line.attrs.width, 1) });
    options[prefix === 'catAxis' ? 'catGridLine' : 'valGridLine'] = grid ? { color: color(grid.attrs.color || 'rgb(220,225,230)').color, width: num(grid.attrs.width, 1) } : { style: 'none' };
  }
  if (background) options.chartArea = { fill: color(background.attrs.color) };
  const edge = child(style, 'chartBorder');
  if (edge && num(edge.attrs.width) > 0) options.chartArea = { ...options.chartArea, border: { color: color(edge.attrs.color).color, pt: num(edge.attrs.width) } };
  const perSeries = children(child(plot, 'chartSeriesList'), 'chartSeries');
  let chartType = TYPE_MAP[type];
  for (const series of perSeries) {
    if (series.attrs.yAxisPosition === 'right') fail(series, '暂不支持组合图的第二纵轴');
    const sectors = child(series, 'chartSectors');
    if (sectors) {
      if (num(sectors.attrs.offsetRadius)) fail(sectors, '暂不支持扇区分离，请去掉 offsetRadius');
      if (num(sectors.attrs.innerRadius)) { chartType = 'doughnut'; options.holeSize = num(sectors.attrs.innerRadius) * 100; }
      options.firstSliceAng = num(sectors.attrs.startAngle);
    }
  }
  if (type === 'combo') {
    if (perSeries.length !== data.length) fail(plot, '组合图必须为每个序列显式指定 comboType');
    const combo = data.map((series, index) => {
      const entry = perSeries.find(n => num(n.attrs.index) === index + 1);
      const kind = TYPE_MAP[entry?.attrs.comboType];
      if (!kind) fail(plot, `组合图序列 ${index + 1} 的 comboType 无效`);
      return { type: pptx.ChartType[kind], data: [series], options: { barDir: entry.attrs.comboType === 'bar' ? 'bar' : 'col', chartColors: [options.chartColors[index % options.chartColors.length]] } };
    });
    return { type: combo, options };
  }
  return { type: pptx.ChartType[chartType], data, options };
}
