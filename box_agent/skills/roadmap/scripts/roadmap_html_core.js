"use strict";

const fs = require("fs");
const path = require("path");

const {
  pendingQuestionsForRoadmapSpec,
  validateAndNormalizeRoadmapSpec,
} = require("./roadmap_contract_core.js");
const { layoutRoadmap } = require("./roadmap_geometry_core.js");

const ROADMAP_LAYOUT_ID = "roadmap-swimlane-v1";
const ROADMAP_RENDERER_VERSION = 1;
const ROADMAP_MIME_TYPE = "text/html";
const ROADMAP_GENERATOR = "Box Agent Roadmap Artifact v1";
const SKILL_ROOT = path.resolve(__dirname, "..");

function loadDefaultPalette() {
  const registryPath = path.join(SKILL_ROOT, "runtime", "registry.json");
  const registry = JSON.parse(fs.readFileSync(registryPath, "utf8"));
  const palette = Array.isArray(registry.palettes)
    ? registry.palettes.find(entry => entry?.id === registry.default_palette_id)
    : null;
  if (!palette || !Array.isArray(palette.colors) || palette.colors.length < 8) {
    throw new Error("Roadmap runtime registry is missing a valid default palette");
  }
  return Object.freeze({
    id: registry.default_palette_id,
    colors: Object.freeze([...palette.colors]),
  });
}

const ROADMAP_DEFAULT_PALETTE = loadDefaultPalette();

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

function cssColor(value, fallback = "var(--roadmap-primary)") {
  const normalized = String(value || "").trim();
  if (/^#[0-9a-f]{3,8}$/i.test(normalized)) return normalized;
  if (/^(?:rgb|hsl)a?\([0-9.,%\s]+\)$/i.test(normalized)) return normalized;
  return fallback;
}

function px(value) {
  const number = Number(value);
  return `${Number.isFinite(number) ? number : 0}px`;
}

function styleBox(entry) {
  return `left:${px(entry.x)};top:${px(entry.y)};width:${px(entry.width)};height:${px(entry.height)}`;
}

function progressRatio(progress) {
  return { planned: 0, doing: 0.55, done: 1, blocked: 1 }[progress] ?? 0;
}

function progressLabel(progress) {
  return { planned: "计划中", doing: "进行中", done: "已完成", blocked: "受阻" }[progress] || progress;
}

function laneAccent(index) {
  return ROADMAP_DEFAULT_PALETTE.colors[index % ROADMAP_DEFAULT_PALETTE.colors.length];
}

function compactHeaderLabel(header) {
  if (header.kind !== "half-month" || header.width >= 56) return header.label;
  return header.label.startsWith("上") ? "上" : "下";
}

function buildDiagnostics(spec, geometry) {
  const diagnostics = [];
  if (spec.items.length >= 80) {
    diagnostics.push({
      code: "capacity.items-at-limit",
      severity: "warning",
      message: "任务数量已达到 80 条上限；建议拆分为多个 Roadmap Artifact。",
    });
  } else if (spec.items.length >= 30) {
    diagnostics.push({
      code: "capacity.dense",
      severity: "warning",
      message: `当前包含 ${spec.items.length} 条任务，已启用可滚动的密集视图。`,
    });
  }
  if (geometry.canvas.height > 900) {
    diagnostics.push({
      code: "layout.vertical-scroll",
      severity: "warning",
      message: `内容高度为 ${Math.round(geometry.canvas.height)}px，预览将使用纵向滚动。`,
    });
  }
  return diagnostics;
}

function renderGeometryMarkup(spec, geometry) {
  const laneById = new Map(spec.lanes.map(lane => [lane.id, lane]));
  const laneIndexById = new Map(spec.lanes.map((lane, index) => [lane.id, index]));
  const itemById = new Map(spec.items.map(item => [item.id, item]));
  const headers = geometry.headers.map(header => (
    `<div class="roadmap-header" data-kind="${escapeHtml(header.kind)}" title="${escapeHtml(header.label)}" style="${styleBox(header)}">${escapeHtml(compactHeaderLabel(header))}</div>`
  ));
  const lanes = geometry.lanes.map((lane, index) => (
    `<div class="roadmap-lane" data-lane-id="${escapeHtml(lane.id)}" style="${styleBox(lane)};--roadmap-lane-accent:${laneAccent(index)}"><div class="roadmap-lane-label" title="${escapeHtml(laneById.get(lane.id)?.label || lane.label)}"><span class="roadmap-lane-index">${String(index + 1).padStart(2, "0")}</span><span class="roadmap-lane-title">${escapeHtml(lane.label)}</span></div></div>`
  ));
  const bars = geometry.bars.map(bar => {
    const item = itemById.get(bar.id) || bar;
    const color = cssColor(item.color, laneAccent(laneIndexById.get(bar.lane_id) || 0));
    return `<div class="roadmap-bar" data-item-id="${escapeHtml(bar.id)}" data-line-style="${escapeHtml(bar.line_style)}" data-progress="${escapeHtml(item.progress)}" title="${escapeHtml(`${bar.title} · ${bar.start} → ${bar.end} · ${progressLabel(item.progress)}`)}" style="${styleBox(bar)};--roadmap-item-accent:${color}"><div class="roadmap-bar-progress" style="width:${progressRatio(item.progress) * 100}%"></div></div>`;
  });
  const milestones = geometry.milestones.map(marker => {
    const item = itemById.get(marker.id) || marker;
    const color = cssColor(item.color, laneAccent(laneIndexById.get(marker.lane_id) || 0));
    return `<div class="roadmap-milestone" data-item-id="${escapeHtml(marker.id)}" data-line-style="${escapeHtml(marker.line_style)}" title="${escapeHtml(`${marker.title} · ${marker.date}`)}" style="left:${px(marker.x - marker.size / 2)};top:${px(marker.y - marker.size / 2)};width:${px(marker.size)};height:${px(marker.size)};--roadmap-item-accent:${color}"></div>`;
  });
  const labels = geometry.labels.map(label => (
    `<div class="roadmap-label" data-item-id="${escapeHtml(label.item_id)}" data-placement="${escapeHtml(label.placement)}" title="${escapeHtml(label.text)}" style="${styleBox(label)}">${escapeHtml(label.text)}</div>`
  ));
  const continuations = geometry.continuations.map(marker => (
    `<div class="roadmap-continuation" aria-label="${marker.direction === "before" ? "开始前仍在继续" : "结束后仍将继续"}" style="left:${px(marker.x - marker.size)};top:${px(marker.y - marker.size)}">${marker.direction === "before" ? "‹" : "›"}</div>`
  ));
  const halfMonthHeaders = geometry.headers.filter(header => header.kind === "half-month");
  const gridLineXs = [...new Set([
    ...halfMonthHeaders.map(header => header.x),
    ...halfMonthHeaders.map(header => header.x + header.width),
  ].map(round => Number(round.toFixed(3))))];
  const laneTop = geometry.canvas.header_top + geometry.canvas.header_height;
  const laneBottom = geometry.lanes.reduce((bottom, lane) => Math.max(bottom, lane.y + lane.height), laneTop);
  const gridLines = gridLineXs.map(x => (
    `<div class="roadmap-grid-line" style="left:${px(x)};top:${px(laneTop)};height:${px(laneBottom - laneTop)}"></div>`
  ));
  const legend = (spec.legend || []).map(entry => `<span>${escapeHtml(entry.label)}</span>`);
  return [
    `<div class="roadmap-axis-label" style="left:${px(geometry.lanes[0]?.x || 0)};top:${px(geometry.canvas.header_top)};width:${px(geometry.canvas.plot_left - (geometry.lanes[0]?.x || 0))};height:${px(geometry.canvas.header_height)}"><span>阶段</span><small>团队泳道</small></div>`,
    ...headers,
    ...lanes,
    ...gridLines,
    ...bars,
    ...milestones,
    ...labels,
    ...continuations,
    legend.length ? `<div class="roadmap-legend">${legend.join("")}</div>` : "",
  ].join("\n");
}

function runtimeModule(source, name, requireBody = "") {
  return [
    `(function(){const module={exports:{}};const exports=module.exports;${requireBody}`,
    safeInlineScript(source),
    `;window.${name}=module.exports;})();`,
  ].join("\n");
}

function renderRoadmapHtml(source, options = {}) {
  const result = validateAndNormalizeRoadmapSpec(source);
  if (!result.ok) {
    const error = new Error(`Roadmap spec validation failed with ${result.issues.length} issue(s)`);
    error.issues = result.issues;
    throw error;
  }
  const spec = result.normalized;
  const questionResult = pendingQuestionsForRoadmapSpec(
    spec,
    options.pendingQuestions || []
  );
  if (!questionResult.ok) {
    const error = new Error(`Roadmap pending question validation failed with ${questionResult.issues.length} issue(s)`);
    error.issues = questionResult.issues;
    throw error;
  }
  const pendingQuestions = questionResult.pending_questions;
  const viewport = options.viewport || { width: 1440, height: 900 };
  const generationVersion = Number.isInteger(options.generationVersion) && options.generationVersion > 0
    ? options.generationVersion
    : null;
  const geometry = layoutRoadmap(spec, viewport);
  const diagnostics = [...result.warnings.map(message => ({
    code: "contract.warning",
    severity: "warning",
    message,
  })), ...buildDiagnostics(spec, geometry)];
  const runtimeCss = fs.readFileSync(path.join(SKILL_ROOT, "runtime", "roadmap.css"), "utf8");
  const editorJs = fs.readFileSync(path.join(SKILL_ROOT, "runtime", "roadmap-editor.js"), "utf8");
  const contractJs = fs.readFileSync(path.join(__dirname, "roadmap_contract_core.js"), "utf8");
  const geometryJs = fs.readFileSync(path.join(__dirname, "roadmap_geometry_core.js"), "utf8");
  const contractModule = runtimeModule(
    contractJs,
    "__roadmapContractCore",
    'const require=(request)=>{if(request==="crypto")return {createHash:()=>{throw new Error("crypto hashing is unavailable in the Roadmap editor runtime")}};throw new Error(`Unsupported runtime module: ${request}`);};'
  );
  const geometryModule = runtimeModule(
    geometryJs,
    "__roadmapGeometryCore",
    'const require=(request)=>{if(request==="./roadmap_contract_core.js")return window.__roadmapContractCore;throw new Error(`Unsupported runtime module: ${request}`);};'
  );
  const html = [
    "<!doctype html>",
    '<html lang="zh-CN">',
    "<head>",
    '  <meta charset="utf-8" />',
    '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
    `  <meta name="generator" content="${ROADMAP_GENERATOR}" />`,
    `  <meta name="box-agent-artifact-layout-id" content="${ROADMAP_LAYOUT_ID}" />`,
    `  <meta name="box-agent-artifact-version" content="${ROADMAP_RENDERER_VERSION}" />`,
    ...(generationVersion
      ? [`  <meta name="box-agent-roadmap-generation-version" content="${generationVersion}" />`]
      : []),
    `  <title>${escapeHtml(spec.title)} · 路线图</title>`,
    "  <style>",
    runtimeCss,
    "  </style>",
    "</head>",
    `<body data-artifact-kind="roadmap" data-layout-id="${ROADMAP_LAYOUT_ID}" data-palette-id="${ROADMAP_DEFAULT_PALETTE.id}"${generationVersion ? ` data-generation-version="${generationVersion}"` : ""} data-schema-version="1" data-geometry-version="1" data-renderer-version="1">`,
    '  <main class="roadmap-app">',
    '    <header class="roadmap-toolbar">',
    '      <div class="roadmap-heading"><h1 data-role="roadmap-title"></h1><p>路线图 · 双层月份刻度 · 泳道排期</p></div>',
    '      <div class="roadmap-actions" data-role="roadmap-actions" hidden>',
    '        <button class="roadmap-button" type="button" data-action="adjust" disabled title="当前运行时未声明 Roadmap 编辑能力">调整</button>',
    '        <button class="roadmap-button" data-primary="true" type="button" data-action="save" disabled>保存</button>',
    "      </div>",
    "    </header>",
    `    <aside class="roadmap-diagnostics" data-role="diagnostics"${diagnostics.length ? "" : " hidden"}>${diagnostics.map(entry => escapeHtml(entry.message)).join("；")}</aside>`,
    `    <div class="roadmap-stage-shell" data-role="stage-shell" style="height:${px(geometry.canvas.height)}">`,
    `      <div class="roadmap-stage" data-role="stage" style="width:${px(geometry.canvas.width)};height:${px(geometry.canvas.height)}">`,
    renderGeometryMarkup(spec, geometry),
    "      </div>",
    "    </div>",
    "  </main>",
    '  <div class="roadmap-editor-backdrop" data-role="editor-backdrop" hidden></div>',
    '  <aside class="roadmap-editor" data-role="editor" role="dialog" aria-modal="true" aria-label="路线图调整" hidden></aside>',
    '  <div class="roadmap-toast" data-role="toast" role="status" aria-live="polite" hidden></div>',
    '  <script type="application/json" id="deck-document">',
    safeJson(spec),
    "  </script>",
    '  <script type="application/json" id="roadmap-geometry">',
    safeJson(geometry),
    "  </script>",
    '  <script type="application/json" id="roadmap-diagnostics">',
    safeJson(diagnostics),
    "  </script>",
    '  <script type="application/json" id="roadmap-pending-questions">',
    safeJson(pendingQuestions),
    "  </script>",
    '  <script type="application/json" id="roadmap-palette">',
    safeJson(ROADMAP_DEFAULT_PALETTE),
    "  </script>",
    '  <script type="application/json" id="roadmap-editor-metadata">',
    safeJson({ mode: "form-table", source_element_id: "deck-document", layout_id: ROADMAP_LAYOUT_ID, palette_id: ROADMAP_DEFAULT_PALETTE.id }),
    "  </script>",
    '  <script data-roadmap-runtime="contract-core">',
    contractModule,
    "  </script>",
    '  <script data-roadmap-runtime="geometry-core">',
    geometryModule,
    "  </script>",
    '  <script data-roadmap-runtime="editor">',
    safeInlineScript(editorJs),
    "  </script>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
  return { html, spec, geometry, diagnostics, pendingQuestions };
}

module.exports = {
  ROADMAP_GENERATOR,
  ROADMAP_DEFAULT_PALETTE,
  ROADMAP_LAYOUT_ID,
  ROADMAP_MIME_TYPE,
  ROADMAP_RENDERER_VERSION,
  buildDiagnostics,
  escapeHtml,
  renderRoadmapHtml,
  safeJson,
};
