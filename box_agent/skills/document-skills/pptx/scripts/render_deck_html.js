#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { escapeHtml, getLayout } = require("../layouts/registry.js");
const {
  SKILL_ROOT,
  getTheme,
  readJson,
  resolveArtifactPath,
  resolveDeckDesign,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const {
  paletteWithOverrides,
  readableForeground,
} = require("./design_contract_core.js");

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") {
    console.log("Usage: render_deck_html.js deck.json --out index.html");
    process.exit(argv[0] ? 0 : 2);
  }
  const opts = { deck: argv[0], out: null };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      opts.out = value;
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!opts.out) throw new Error("--out is required");
  return opts;
}

function cssValue(value) {
  return String(value).replace(/[;{}]/g, "").trim();
}

function themeColor(palette, key, fallback) {
  const value = palette && palette[key];
  return typeof value === "string" && value.trim() ? cssValue(value) : fallback;
}

function themeVariables(theme, designContract = null) {
  const palette = paletteWithOverrides(theme.palette, designContract);
  const typography = theme.typography;
  const shape = theme.shape;
  const chart = Array.isArray(palette.chart) ? palette.chart : [];
  const chartColor = (index, fallback) => {
    const value = chart[index];
    return typeof value === "string" && value.trim() ? cssValue(value) : fallback;
  };
  const chartForeground = index => readableForeground(
    chartColor(index, cssValue(palette.background)),
    cssValue(palette.text),
  );
  return [
    ":root {",
    `  --deck-bg: ${cssValue(palette.background)};`,
    `  --deck-surface: ${cssValue(palette.surface)};`,
    `  --deck-surface-strong: ${cssValue(palette.surface_strong)};`,
    `  --deck-primary: ${cssValue(palette.primary)};`,
    `  --deck-secondary: ${themeColor(palette, "secondary", cssValue(palette.primary))};`,
    `  --deck-primary-text: ${themeColor(palette, "primary_text", cssValue(palette.primary))};`,
    `  --deck-primary-soft: ${cssValue(palette.primary_soft)};`,
    `  --deck-accent-color: ${themeColor(palette, "accent", cssValue(palette.primary))};`,
    `  --deck-text: ${cssValue(palette.text)};`,
    `  --deck-muted: ${cssValue(palette.muted)};`,
    `  --deck-base-text: ${cssValue(palette.text)};`,
    `  --deck-base-muted: ${cssValue(palette.muted)};`,
    `  --deck-primary-soft-text: ${readableForeground(palette.primary_soft, palette.text)};`,
    `  --deck-surface-text: ${readableForeground(palette.surface, palette.text)};`,
    `  --deck-surface-strong-text: ${readableForeground(palette.surface_strong, palette.text)};`,
    `  --deck-border: ${cssValue(palette.border)};`,
    `  --deck-inverse: ${cssValue(palette.inverse)};`,
    `  --deck-alt-bg: ${themeColor(palette, "alt_background", cssValue(palette.surface_strong))};`,
    `  --deck-alt-surface: ${themeColor(palette, "alt_surface", cssValue(palette.surface))};`,
    `  --deck-alt-text: ${themeColor(palette, "alt_text", cssValue(palette.text))};`,
    `  --deck-alt-muted: ${themeColor(palette, "alt_muted", cssValue(palette.muted))};`,
    `  --deck-alt-border: ${themeColor(palette, "alt_border", cssValue(palette.border))};`,
    `  --deck-alt-primary: ${themeColor(palette, "alt_primary", cssValue(palette.primary))};`,
    `  --deck-alt-primary-text: ${themeColor(palette, "alt_primary_text", themeColor(palette, "alt_primary", cssValue(palette.primary)))};`,
    `  --deck-chart-1: ${chartColor(0, cssValue(palette.primary))};`,
    `  --deck-chart-2: ${chartColor(1, cssValue(palette.surface_strong))};`,
    `  --deck-chart-3: ${chartColor(2, cssValue(palette.text))};`,
    `  --deck-chart-4: ${chartColor(3, cssValue(palette.muted))};`,
    `  --deck-chart-text-1: ${chartForeground(0)};`,
    `  --deck-chart-text-2: ${chartForeground(1)};`,
    `  --deck-chart-text-3: ${chartForeground(2)};`,
    `  --deck-chart-text-4: ${chartForeground(3)};`,
    `  --deck-display: ${cssValue(typography.display)};`,
    `  --deck-body: ${cssValue(typography.body)};`,
    `  --deck-label: ${cssValue(typography.label || typography.body)};`,
    `  --deck-radius-small: ${Number(shape.radius_small)}px;`,
    `  --deck-radius-medium: ${Number(shape.radius_medium)}px;`,
    `  --deck-radius-large: ${Number(shape.radius_large)}px;`,
    `  --deck-border-width: ${Number(shape.border_width)}px;`,
    "}",
  ].join("\n");
}

function styleOverrideAttributes(designContract) {
  const overrides = designContract
    && designContract.style_overrides
    && typeof designContract.style_overrides === "object"
    ? designContract.style_overrides
    : {};
  return Object.entries(overrides)
    .map(([key, value]) => (
      ` data-deck-style-${escapeHtml(key.replace(/_/g, "-"))}="${escapeHtml(value)}"`
    ))
    .join("");
}

const THEME_STYLE_VALUES = Object.freeze({
  canvas: new Set(["solid", "grid", "dots", "paper", "pixel", "gradient", "window"]),
  surface: new Set(["soft", "outline", "hard", "pill", "paper", "window", "note"]),
  shadow: new Set(["none", "soft", "hard", "glow"]),
  heading: new Set(["standard", "editorial", "poster", "condensed", "italic", "pixel", "handwritten", "stencil"]),
  label: new Set(["plain", "pill", "boxed", "mono", "tape"]),
  accent: new Set(["line", "block", "underline", "bracket", "dot"]),
  alternation: new Set(["none", "section"]),
});

function themeStyleAttributes(theme) {
  const style = theme && theme.style && typeof theme.style === "object" ? theme.style : {};
  return Object.entries(THEME_STYLE_VALUES).map(([key, allowed]) => {
    const requested = String(style[key] || "").trim();
    const fallback = key === "alternation" ? "none" : {
      canvas: "solid",
      surface: "soft",
      shadow: "none",
      heading: "standard",
      label: "plain",
      accent: "line",
    }[key];
    const value = allowed.has(requested) ? requested : fallback;
    return ` data-deck-${key}="${escapeHtml(value)}"`;
  }).join("");
}

function safeJson(value) {
  return JSON.stringify(value, null, 2)
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

function safeInlineScript(value) {
  return String(value).replace(/<\/script/gi, "<\\/script");
}

function renderLayoutPicker() {
  return [
    '<section class="deck-layout-picker" id="deck-layout-picker" data-role="layout-picker" aria-label="受控版式" hidden>',
    '  <header class="layout-picker-header">',
    '    <div>',
    '      <p class="layout-picker-kicker" data-role="layout-picker-kicker">新增页面</p>',
    '      <h2 data-role="layout-picker-title">选择受控版式</h2>',
    '      <p class="layout-picker-description" data-role="layout-picker-description">新页面会插入到当前页之后。</p>',
    '    </div>',
    '    <button type="button" class="layout-picker-close" data-layout-action="close" aria-label="关闭版式选择">关闭</button>',
    '  </header>',
    '  <div class="deck-layout-options" data-role="layout-options"></div>',
    '  <p class="layout-picker-note">切换版式会保留原版式内容；切换回来即可恢复。</p>',
    '</section>',
  ].join("\n");
}

function renderLayoutControls() {
  return [
    '<section class="deck-layout-controls" id="deck-layout-controls" data-role="layout-controls" aria-label="调整当前页版式" hidden>',
    '  <header class="layout-controls-header">',
    '    <div>',
    '      <p class="layout-controls-kicker" data-role="layout-controls-kicker">当前版式</p>',
    '      <h2 data-role="layout-controls-title">调整版式</h2>',
    '      <p class="layout-controls-description" data-role="layout-controls-description"></p>',
    '    </div>',
    '    <button type="button" class="layout-controls-close" data-control-action="close" aria-label="关闭版式调整">关闭</button>',
    '  </header>',
    '  <div class="layout-control-groups" data-role="layout-control-groups"></div>',
    '  <p class="layout-controls-note">只开放当前版式支持的参数；条目数量受版式容量约束。</p>',
    '</section>',
  ].join("\n");
}

function renderPresentationControls() {
  return [
    '<nav class="deck-present-controls" data-role="present-controls" aria-label="播放控制">',
    '  <button type="button" data-present-action="previous" aria-label="上一页" title="上一页">←</button>',
    '  <div class="present-location" aria-live="polite" aria-atomic="true">',
    '    <strong data-role="present-page">01</strong>',
    '    <span>/</span>',
    '    <span data-role="present-total">01</span>',
    '    <span class="present-title" data-role="present-title">未命名页面</span>',
    '  </div>',
    '  <button type="button" data-present-action="next" aria-label="下一页" title="下一页">→</button>',
    '  <button type="button" class="present-exit" data-present-action="exit">退出播放</button>',
    '</nav>',
    '<div class="deck-present-progress" data-role="present-progress" aria-hidden="true"></div>',
  ].join("\n");
}

function renderThumbnailNavigation() {
  return [
    '<aside class="deck-thumbnails" data-role="thumbnails" aria-label="幻灯片导航">',
    '  <div class="deck-thumbnails-list" data-role="thumbnail-list"></div>',
    '</aside>',
  ].join("\n");
}

function renderToolbar() {
  return [
    '<nav class="deck-toolbar" aria-label="Deck editor">',
    '  <div class="toolbar-location" aria-live="polite" aria-atomic="true">',
    '    <span class="toolbar-current-label">当前页</span>',
    '    <strong class="toolbar-current-page" data-role="current-page">01</strong>',
    '    <span class="toolbar-total"><span class="toolbar-total-separator" aria-hidden="true">/</span><span class="toolbar-total-prefix"> 共 </span><span data-role="total-pages">01</span><span class="toolbar-total-suffix"> 页</span></span>',
    '    <span class="toolbar-current-title" data-role="current-title">未命名页面</span>',
    '  </div>',
    '  <span class="toolbar-divider" aria-hidden="true"></span>',
    '  <button type="button" class="toolbar-edit" data-action="edit" aria-label="编辑" title="编辑内容" aria-pressed="false"><span class="toolbar-compact-symbol" aria-hidden="true">✎</span><span class="toolbar-action-label">编辑</span></button>',
    '  <div class="toolbar-popover" data-toolbar-menu="design">',
    '    <button type="button" class="toolbar-popover-trigger" data-toolbar-menu-trigger aria-label="设计" title="设计操作" aria-haspopup="menu" aria-expanded="false"><span class="toolbar-compact-symbol" aria-hidden="true">◇</span><span class="toolbar-action-label">设计</span><span class="toolbar-popover-caret" aria-hidden="true">⌃</span></button>',
    '    <span class="toolbar-popover-bridge" aria-hidden="true"></span>',
    '    <div class="toolbar-popover-menu" role="menu" aria-label="设计操作">',
    '      <button type="button" data-action="layout" role="menuitem" aria-expanded="false" aria-controls="deck-layout-picker">版式</button>',
    '      <button type="button" data-action="adjust" role="menuitem" aria-expanded="false" aria-controls="deck-layout-controls">调整</button>',
    '    </div>',
    '  </div>',
    '  <button type="button" class="toolbar-icon" data-action="present" aria-label="播放" title="一页一屏播放">▶</button>',
    '  <div class="toolbar-popover" data-toolbar-menu="page">',
    '    <button type="button" class="toolbar-popover-trigger" data-toolbar-menu-trigger aria-label="页面" title="页面操作" aria-haspopup="menu" aria-expanded="false"><span class="toolbar-compact-symbol" aria-hidden="true">⋯</span><span class="toolbar-action-label">页面</span><span class="toolbar-popover-caret" aria-hidden="true">⌃</span></button>',
    '    <span class="toolbar-popover-bridge" aria-hidden="true"></span>',
    '    <div class="toolbar-popover-menu toolbar-page-menu" role="menu" aria-label="页面操作">',
    '      <button type="button" data-action="add-slide" role="menuitem" aria-expanded="false" aria-controls="deck-layout-picker"><span aria-hidden="true">＋</span> 新页</button>',
    '      <span class="toolbar-popover-divider" role="separator"></span>',
    '      <button type="button" class="toolbar-move" data-action="move-up" role="menuitem" title="将当前页向前移动一位"><span aria-hidden="true">↑</span> 前移</button>',
    '      <button type="button" class="toolbar-move" data-action="move-down" role="menuitem" title="将当前页向后移动一位"><span aria-hidden="true">↓</span> 后移</button>',
    '      <button type="button" data-action="duplicate" role="menuitem">复制</button>',
    '      <button type="button" class="toolbar-danger" data-action="delete" role="menuitem">删除</button>',
    '    </div>',
    '  </div>',
    '  <span class="toolbar-divider" aria-hidden="true"></span>',
    '  <button type="button" class="toolbar-export" data-action="export-pptx" data-export-state="unavailable" data-compact-label="PPT" aria-busy="false">导出 PPT</button>',
    '  <button type="button" class="toolbar-primary" data-action="save" data-save-state="download" data-compact-label="HTML" aria-busy="false">另存 HTML</button>',
    "</nav>",
    '<div class="deck-toast" role="status" aria-live="polite"></div>',
  ].join("\n");
}

function renderDocument(deck, theme) {
  const runtimeCss = fs.readFileSync(path.join(SKILL_ROOT, "runtime", "deck.css"), "utf8");
  const compositionCss = fs.readFileSync(
    path.join(SKILL_ROOT, "runtime", "composition.css"),
    "utf8"
  );
  const editorJs = fs.readFileSync(path.join(SKILL_ROOT, "runtime", "deck-editor.js"), "utf8");
  const layoutRegistryJs = fs.readFileSync(
    path.join(SKILL_ROOT, "layouts", "registry.js"),
    "utf8"
  );
  const design = resolveDeckDesign(deck, theme);
  const effectivePalette = paletteWithOverrides(theme.palette, deck.design_contract);
  const renderContext = {
    palette: effectivePalette,
    useDeckPalette: Boolean(deck.design_contract && deck.design_contract.palette),
  };
  const renderedDeck = { ...deck, design };
  const slideHtml = deck.slides.map((slide, index) => {
    const layout = getLayout(slide.layout_id);
    if (!layout) throw new Error(`Unknown layout during render: ${slide.layout_id}`);
    return layout.render(slide, index, design, renderContext);
  }).join("\n\n");
  const hasCharts = slideHtml.includes("data-pptx-chart");
  const chartScripts = hasCharts
    ? [
        '  <script data-deck-runtime="echarts" data-echarts-version="6.0.0">',
        safeInlineScript(fs.readFileSync(
          path.join(SKILL_ROOT, "runtime", "vendor", "echarts", "echarts.min.js"),
          "utf8"
        )),
        "  </script>",
        '  <script data-deck-runtime="chart-runtime">',
        safeInlineScript(fs.readFileSync(
          path.join(SKILL_ROOT, "runtime", "chart-runtime.js"),
          "utf8"
        )),
        "  </script>",
      ]
    : [];
  const hasDiagrams = slideHtml.includes("data-pptx-diagram");
  const diagramScripts = hasDiagrams
    ? [
        '  <script data-deck-runtime="elkjs" data-elk-version="0.12.0">',
        safeInlineScript(fs.readFileSync(
          path.join(SKILL_ROOT, "runtime", "vendor", "elkjs", "elk.bundled.js"),
          "utf8"
        )),
        "  </script>",
        '  <script data-deck-runtime="diagram-runtime">',
        safeInlineScript(fs.readFileSync(
          path.join(SKILL_ROOT, "runtime", "diagram-runtime.js"),
          "utf8"
        )),
        "  </script>",
      ]
    : [];
  const visualDnaIds = theme.selection && Array.isArray(theme.selection.visual_dna_ids)
    ? theme.selection.visual_dna_ids.filter(value => typeof value === "string" && value.trim())
    : [];
  const visualDnaId = visualDnaIds[0] || theme.id;
  const paletteAccentUsage = deck.design_contract
    && deck.design_contract.palette
    && deck.design_contract.palette.accent_usage
    ? ` data-deck-palette-accent-usage="${escapeHtml(deck.design_contract.palette.accent_usage)}"`
    : "";
  const styleOverrides = styleOverrideAttributes(deck.design_contract);
  return [
    "<!doctype html>",
    '<html lang="zh-CN">',
    "<head>",
    '  <meta charset="utf-8" />',
    '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
    `  <title>${escapeHtml(deck.title)}</title>`,
    '  <meta name="generator" content="Box Agent controlled deck v1" />',
    "  <style>",
    runtimeCss,
    compositionCss,
    themeVariables(theme, deck.design_contract),
    "  </style>",
    "</head>",
    `<body data-deck-schema-version="1" data-deck-theme="${escapeHtml(visualDnaId)}" data-deck-theme-id="${escapeHtml(theme.id)}" data-deck-composition="${escapeHtml(design.family)}" data-deck-composition-variant="${escapeHtml(design.variant)}" data-deck-design-seed="${escapeHtml(design.seed)}"${paletteAccentUsage}${styleOverrides}${themeStyleAttributes(theme)}>`,
    '  <main id="deck-root">',
    slideHtml,
    "  </main>",
    renderThumbnailNavigation(),
    renderLayoutPicker(),
    renderLayoutControls(),
    renderPresentationControls(),
    renderToolbar(),
    '  <script type="application/json" id="deck-document">',
    safeJson(renderedDeck),
    "  </script>",
    '  <script data-deck-runtime="layout-registry">',
    safeInlineScript(layoutRegistryJs),
    "  </script>",
    ...chartScripts,
    ...diagramScripts,
    "  <script>",
    editorJs,
    "  </script>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const source = readJson(opts.deck);
  const result = validateAndNormalizeDeck(source);
  if (!result.ok) {
    result.issues.forEach(issue => console.error(`- ${issue}`));
    throw new Error(`Deck spec validation failed with ${result.issues.length} issue(s)`);
  }
  const theme = getTheme(result.normalized.theme_id);
  const html = renderDocument(result.normalized, theme);
  const outputPath = resolveArtifactPath(opts.out);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, html, "utf8");
  console.log(
    JSON.stringify({
      html: outputPath,
      slideCount: result.normalized.slides.length,
      theme: theme.id,
      composition: resolveDeckDesign(result.normalized, theme),
      source: resolveArtifactPath(opts.deck),
      editable: true,
    }, null, 2)
  );
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }
}

module.exports = {
  renderDocument,
  themeVariables,
};
