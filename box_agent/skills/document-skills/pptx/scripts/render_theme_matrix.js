#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const { createHash } = require("crypto");
const { pathToFileURL } = require("url");
const { layouts, createEditorProps, escapeHtml } = require("../layouts/registry.js");
const { listThemes, getTheme, validateAndNormalizeDeck, resolveArtifactPath, resolveDeckDesign } = require("./deck_spec_core.js");
const { renderDocument } = require("./render_deck_html.js");
const system = require("../runtime/presentation-system.js");

const SAMPLE_TITLES = {
  "cover-hero-v1": "把复杂的问题，讲得更清楚",
  "cover-editorial-v1": "让内容成为主角",
  "section-marker-v1": "从判断走向行动",
  "statement-focus-v1": "先看见真实问题，再讨论解决方案",
  "cards-grid-v1": "三个方向，各司其职",
  "quadrant-matrix-v1": "把有限精力放在关键问题上",
  "pyramid-hierarchy-v1": "让目标与行动层层对应",
  "text-columns-v1": "把判断过程讲清楚",
  "comparison-two-column-v1": "两种路径，同一个目标",
  "kpi-grid-v1": "从关键指标看业务进展",
  "architecture-layered-v1": "围绕能力边界构建系统",
  "system-integration-v1": "让服务之间形成可靠协作",
  "technical-diagram-v1": "每一次请求，都经过明确的边界",
  "dashboard-overview-v1": "在同一视图中理解运营状态",
  "chart-bar-v1": "用一致口径比较不同方向",
  "chart-data-v1": "沿着时间轴观察变化",
  "heatmap-matrix-v1": "看见风险分布与集中区域",
  "table-data-v1": "让每项行动都有清晰记录",
  "timeline-horizontal-v1": "把共识推进到下一步",
  "swimlane-process-v1": "明确谁在何时完成交接",
  "customer-journey-map-v1": "沿着用户旅程寻找机会",
  "maturity-model-v1": "判断当前位置，明确下一阶段",
  "cause-tree-v1": "从现象出发，逐层验证原因",
  "factory-process-line-v1": "把关键工序放在同一条线上",
  "legal-case-logic-v1": "从争点到结论，展示推理过程",
  "property-factsheet-v1": "在整体与细节之间理解项目",
  "commerce-funnel-v1": "看见转化发生在哪里",
  "supply-network-v1": "连接供应节点与交付状态",
  "project-case-study-v1": "把项目证据放到叙事中心",
  "image-hero-split-v1": "让一个场景解释核心概念",
  "image-feature-v1": "从细节看见设计选择",
  "image-full-bleed-v1": "给一个重要场景完整的空间",
  "closing-next-steps-v1": "让下一次讨论更有准备",
};

const SAMPLE_IMAGE = `data:image/svg+xml;base64,${fs.readFileSync(path.join(__dirname, "../examples/presentation-preview.svg")).toString("base64")}`;

function sourceFingerprint() {
  const root = path.resolve(__dirname, "..");
  const files = ["layouts/registry.js", ...["scripts", "runtime", "themes", "examples"].flatMap(directory =>
    fs.readdirSync(path.join(root, directory)).filter(name => /\.(?:js|css|json|svg)$/.test(name)).map(name => `${directory}/${name}`))].sort();
  const hash = createHash("sha256");
  files.forEach(file => { hash.update(file); hash.update(fs.readFileSync(path.join(root, file))); });
  return hash.digest("hex");
}

function previewDeck(theme, selectedLayouts = layouts) {
  const slides = selectedLayouts.map((layout, index) => {
    const props = createEditorProps(layout.id);
    props.composition = system.preferredComposition(layout);
    if (layout.fields.title) props.title = SAMPLE_TITLES[layout.id] || layout.editor.label;
    if (layout.fields.statement) props.statement = SAMPLE_TITLES[layout.id] || layout.editor.label;
    if (layout.fields.eyebrow) props.eyebrow = "布局检视示例";
    for (const key of ["image", "hero"]) {
      if (props[key]) props[key] = { ...props[key], src: SAMPLE_IMAGE, alt: "主题检视用概念界面，非真实产品" };
    }
    if (layout.id === "cards-grid-v1") Object.assign(props, {
      subtitle: "并列讨论产品、服务和协作三个方向。",
      items: [
        { kicker: "产品", title: "使用体验", body: "围绕关键场景，观察真实的使用过程。" },
        { kicker: "服务", title: "交付质量", body: "用清楚的标准，让每次交付可以检视。" },
        { kicker: "协作", title: "团队共识", body: "记录分歧与决定，为下一次行动做准备。" },
      ],
    });
    if (layout.id === "closing-next-steps-v1") Object.assign(props, {
      subtitle: "", contact: "",
      actions: [{ label: "明确问题", detail: "" }, { label: "整理证据", detail: "" }, { label: "约定行动", detail: "" }],
    });
    return { id: `slide-${String(index + 1).padStart(2, "0")}`, layout_id: layout.id, props,
      ...(layout.id === "image-full-bleed-v1" ? { background: { src: SAMPLE_IMAGE, alt: "主题检视用概念界面", treatment: "wash-dark" } } : {}) };
  });
  return { schema_version: 1, title: `${theme.name} · 全布局检视示例`, theme_id: theme.id, slides };
}

function checkContracts(themes, selectedLayouts = layouts) {
  const issues = [];
  themes.forEach(theme => {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(theme.id || "")) issues.push("Theme id must be a lowercase slug without path separators");
  });
  themes.forEach(theme => Object.entries(theme.style || {}).forEach(([key, value]) => {
    if (!system.STYLE_VALUES[key]?.includes(value)) issues.push(`${theme.id}.style.${key}: unsupported value ${JSON.stringify(value)}`);
  }));
  const roles = new Set(["label", "display", "lead", "caption", "metric", "metric-or-point", "heading", "body", "source"]);
  function fields(value, prefix) {
    if (!value || typeof value !== "object") return;
    if (value.type === "text" && !roles.has(value.role)) issues.push(`${prefix}: text field requires a supported presentation role`);
    Object.entries(value).forEach(([key, item]) => { if (item && typeof item === "object") fields(item, `${prefix}.${key}`); });
  }
  selectedLayouts.forEach(layout => {
    if (!system.preferredComposition(layout)) issues.push(`${layout.id}: no canvas composition`);
    fields(layout.fields, layout.id);
  });
  return issues;
}

function gallery(records, selectedLayouts, checked) {
  const options = records.map(record => `<option value="${escapeHtml(record.id)}">${escapeHtml(record.name)}</option>`).join("");
  const defaultTheme = records.find(record => record.id === "sketch-whiteboard")?.id || records[0]?.id;
  const pages = records.map(record => `<section data-theme="${escapeHtml(record.id)}" ${record.id === defaultTheme ? "" : "hidden"}><h2>${escapeHtml(record.name)}</h2><p>${record.presentation.voice} · ${record.check ? `${record.check.issues.length} issues / ${record.check.warnings.length} warnings` : "未执行浏览器检查"} · <a href="${record.id}/index.html">打开 HTML</a></p><div class="grid">${selectedLayouts.map((layout, index) => {
    const src = `${record.id}/slides/${String(index + 1).padStart(2, "0")}.png`;
    return `<a class="tile" href="${record.screenshots ? src : record.id + '/index.html'}" target="_blank">${record.screenshots ? `<img loading="lazy" src="${src}" alt="${escapeHtml(layout.editor.label)}">` : '<div class="no-image">截图未生成</div>'}<b>${index + 1}. ${escapeHtml(layout.editor.label)}</b><small>${layout.id}</small></a>`;
  }).join("")}</div></section>`).join("");
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>全主题 × 全布局检查</title><style>*{box-sizing:border-box}body{margin:0;background:#f2f3f0;color:#242d30;font:16px/1.5 system-ui,sans-serif}main{max-width:1880px;margin:auto;padding:40px 32px}h1{font-size:36px;margin:0}nav{position:sticky;top:0;background:#f2f3f0f2;padding:18px 0;z-index:2;display:flex;gap:24px;align-items:center}select{font:inherit;padding:10px 14px;background:white;border:1px solid #78898c}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px}.tile{display:block;text-decoration:none;color:inherit;background:white;border:1px solid #cdd5d5}.tile img,.no-image{width:100%;aspect-ratio:16/9;object-fit:contain;display:block}.tile b,.tile small{display:block;padding:4px 12px}.tile b{font-size:15px}.tile small{color:#627176;font-size:12px;padding-bottom:12px}a{color:#245c70}section[hidden]{display:none}@media(max-width:800px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:24px 16px}}</style><main><h1>全主题 × 全布局检查</h1><p>${records.length} 个主题 · ${selectedLayouts.length} 个布局 · ${records.length * selectedLayouts.length} 个组合。所有文字与数值仅作检视示例。</p><nav><label>主题 <select id="theme">${options}</select></label><span>${checked ? "浏览器检查已执行" : "仅生成静态检视文件"}</span><a href="matrix.json">检查详情</a></nav>${pages}</main><script>const picker=document.querySelector('#theme');picker.value=${JSON.stringify(defaultTheme)};picker.addEventListener('change',()=>document.querySelectorAll('section[data-theme]').forEach(section=>section.hidden=section.dataset.theme!==picker.value));</script></html>`;
}

function loadBrowser() {
  const os = require("os"), Module = require("module");
  const host = require("./playwright_host.js");
  host.ensurePlaywrightBrowsersPath();
  const prefix = process.env.BOX_AGENT_NODE_PREFIX || process.env.BOX_AGENT_RUNTIME_PREFIX
    || (process.platform === "darwin" ? path.join(os.homedir(), "Library/Application Support/office-raccoon")
      : process.platform === "win32" ? path.join(process.env.APPDATA || os.homedir(), "office-raccoon") : path.join(os.homedir(), ".config/office-raccoon"));
  process.env.NODE_PATH = [path.join(prefix, "node_modules"), process.env.NODE_PATH].filter(Boolean).join(path.delimiter);
  Module._initPaths();
  const { chromium } = host.loadPlaywright();
  return chromium.launch(host.chromiumLaunchOptions(chromium, { headless: true }).options);
}

async function main() {
  const args = process.argv.slice(2), opts = { check: false, screenshots: false };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (["--check", "--screenshots"].includes(arg)) opts[arg.slice(2)] = true;
    else if (["--out", "--themes", "--layouts"].includes(arg) && args[i + 1]) opts[arg.slice(2)] = args[++i];
    else throw new Error("Usage: render_theme_matrix.js --out DIRECTORY [--themes id,id] [--layouts id,id] [--check] [--screenshots]");
  }
  if (!opts.out) throw new Error("--out DIRECTORY is required");
  const out = resolveArtifactPath(opts.out);
  const themes = opts.themes ? opts.themes.split(",").map(id => getTheme(id)) : listThemes();
  const selectedLayouts = opts.layouts ? opts.layouts.split(",").map(id => layouts.find(layout => layout.id === id)) : layouts;
  if (themes.some(theme => !theme) || selectedLayouts.some(layout => !layout)) throw new Error("Unknown theme or layout id");
  const contractIssues = checkContracts(themes, selectedLayouts);
  if (contractIssues.length) throw new Error(contractIssues.join("\n"));
  fs.mkdirSync(out, { recursive: true });
  const fingerprint = sourceFingerprint();
  const records = [];
  let browser;
  try {
    if (opts.screenshots) browser = await loadBrowser();
    const page = browser ? await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: .25 }) : null;
    for (const theme of themes) {
      const directory = path.join(out, theme.id);
      fs.mkdirSync(directory, { recursive: true });
      const deck = validateAndNormalizeDeck(previewDeck(theme, selectedLayouts));
      if (!deck.ok) throw new Error(deck.issues.join("\n"));
      const htmlPath = path.join(directory, "index.html");
      fs.writeFileSync(path.join(directory, "deck.json"), JSON.stringify(deck.normalized, null, 2));
      fs.writeFileSync(htmlPath, renderDocument(deck.normalized, theme));
      const record = { id: theme.id, name: theme.name, presentation: system.resolveTheme(theme, resolveDeckDesign(deck.normalized, theme).family), screenshots: false };
      if (opts.check) {
        record.check = { issues: [], warnings: [] };
        for (const [script, filename] of [["html_self_check.js", "geometry.json"], ["probe_deck_runtime.js", "runtime.json"]]) {
          const report = path.join(directory, filename);
          const result = spawnSync(process.execPath, [path.join(__dirname, script), htmlPath, "--report", report], { encoding: "utf8", timeout: 90000 });
          if (!fs.existsSync(report)) throw new Error(`${theme.id}: ${result.error || result.stderr || result.stdout}`);
          const checked = JSON.parse(fs.readFileSync(report, "utf8"));
          record.check.issues.push(...checked.issues);
          record.check.warnings.push(...checked.warnings);
          if (script === "probe_deck_runtime.js") record.contrast = checked.editor?.componentContrast;
        }
      }
      if (page) {
        await page.goto(pathToFileURL(htmlPath).href + "?mode=export");
        await page.evaluate(async () => { await document.fonts.ready; await window.__deckTextReady; await window.__deckDiagramReady; });
        const shots = path.join(directory, "slides");
        fs.mkdirSync(shots, { recursive: true });
        for (let i = 0; i < selectedLayouts.length; i += 1) await page.locator("#deck-root > .slide").nth(i)
          .screenshot({ path: path.join(shots, `${String(i + 1).padStart(2, "0")}.png`) });
        record.screenshots = true;
      }
      records.push(record);
      fs.writeFileSync(path.join(out, "matrix.json"), JSON.stringify({ themes: records, layouts: selectedLayouts.map(layout => layout.id), contractIssues, sourceFingerprint: fingerprint }, null, 2));
      console.log(`${theme.id}: ${selectedLayouts.length} layouts${record.check ? `, ${record.check.issues.length} issues, ${record.check.warnings.length} warnings` : ""}`);
    }
  } finally { if (browser) await browser.close(); }
  fs.writeFileSync(path.join(out, "index.html"), gallery(records, selectedLayouts, opts.check));
  if (sourceFingerprint() !== fingerprint) throw new Error("Presentation sources changed during the matrix run; rerun against a consistent source snapshot.");
  if (records.some(record => record.check?.issues.length || record.contrast?.failureCount
    || record.check?.warnings.some(warning => /overflow|contrast|could not fit|numeric value and unit wrap/i.test(warning)))) process.exitCode = 1;
}

module.exports = { previewDeck, checkContracts, gallery };
if (require.main === module) main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
