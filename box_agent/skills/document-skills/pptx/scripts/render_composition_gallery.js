#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { createEditorProps } = require("../layouts/registry.js");
const {
  COMPOSITION_FAMILIES,
  compositionDirectionCatalog,
  createDeckDesign,
} = require("./composition_core.js");
const {
  getTheme,
  resolveArtifactPath,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const { renderDocument } = require("./render_deck_html.js");

const COMPARISON_HERO = Object.freeze({
  src: `data:image/svg+xml;base64,${Buffer.from([
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900">',
    '<rect width="1200" height="900" fill="#171a1d"/>',
    '<path d="M0 715 1200 210v255L0 900Z" fill="#2f39ff"/>',
    '<circle cx="910" cy="230" r="190" fill="#f2d45c"/>',
    '<rect x="78" y="82" width="260" height="260" fill="#f4f0e8"/>',
    '<path d="M78 390h500M78 430h350M78 470h420" stroke="#f4f0e8" stroke-width="18"/>',
    '<path d="M1020 610v210M950 680h210" stroke="#f4f0e8" stroke-width="12"/>',
    '</svg>',
  ].join("")).toString("base64")}`,
  alt: "用于比较构图位置的统一抽象主视觉",
  origin: "asset",
  fit: "cover",
  position: "center",
  treatment: "none",
});

const DIRECTION_CATALOG = compositionDirectionCatalog();
const FAMILY_GROUPS = Object.freeze(DIRECTION_CATALOG.map(direction => Object.freeze({
  id: direction.id,
  label: direction.label,
  summary: direction.summary,
})));
const FAMILY_CATALOG = Object.freeze(DIRECTION_CATALOG.flatMap(direction =>
  direction.families.map(family => Object.freeze({
    id: family.family,
    name: family.name,
    themeId: family.preview_theme_id,
    group: direction.id,
    summary: family.summary,
    selectionSignals: Object.freeze([...family.selection_signals]),
  }))
));

const VARIANT_LAYOUTS = Object.freeze({
  "balanced-grid": "cover-hero-v1",
  "rail-grid": "table-data-v1",
  "ledger-grid": "kpi-grid-v1",
  "split-spread": "cover-hero-v1",
  "feature-spread": "cards-grid-v1",
  "banded-spread": "table-data-v1",
  "offset-hero": "cover-hero-v1",
  "stacked-poster": "cover-hero-v1",
  "split-poster": "comparison-two-column-v1",
  mosaic: "cards-grid-v1",
  staggered: "timeline-horizontal-v1",
  capsule: "kpi-grid-v1",
  "block-grid": "cards-grid-v1",
  "offset-frame": "cover-hero-v1",
  "ledger-frame": "table-data-v1",
  "window-grid": "cover-hero-v1",
  "terminal-stack": "cards-grid-v1",
  "pixel-panels": "kpi-grid-v1",
  "margin-note": "table-data-v1",
  "quiet-center": "cover-hero-v1",
  "asymmetric-column": "cards-grid-v1",
  "device-stage": "cover-hero-v1",
  "browser-story": "cover-hero-v1",
  "annotated-flow": "cards-grid-v1",
  "full-bleed": "cover-hero-v1",
  "split-film": "cover-hero-v1",
  "chapter-cut": "section-marker-v1",
  "exhibit-grid": "kpi-grid-v1",
  "evidence-rail": "table-data-v1",
  "decision-board": "cards-grid-v1",
  "blueprint-canvas": "cover-hero-v1",
  "annotated-system": "timeline-horizontal-v1",
  "spec-sheet": "table-data-v1",
});

const LAYOUT_LABELS = Object.freeze({
  "cover-hero-v1": "封面",
  "cards-grid-v1": "内容",
  "comparison-two-column-v1": "对比",
  "kpi-grid-v1": "数据",
  "section-marker-v1": "章节",
  "table-data-v1": "表格",
  "timeline-horizontal-v1": "流程",
});

function parseArgs(argv) {
  const opts = { out: "composition-previews/index.html" };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--help" || arg === "-h") {
      console.log(
        "Usage: render_composition_gallery.js " +
        "[--out composition-previews/index.html]"
      );
      process.exit(0);
    }
    if (arg === "--out" && value) {
      opts.out = value;
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return opts;
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function previewProps(layoutId, familyRecord, variant) {
  const props = createEditorProps(layoutId);
  if (layoutId === "cover-hero-v1") {
    Object.assign(props, {
      eyebrow: "COMPOSITION STUDY",
      title: "同一内容，结构不同",
      subtitle: "排除文案差异，直接比较标题、视觉与留白的组织方式。",
      meta: `${familyRecord.id} · ${variant}`,
      hero: COMPARISON_HERO,
      media_side: "right",
    });
  } else if (layoutId === "cards-grid-v1") {
    Object.assign(props, {
      eyebrow: "CONTENT STRUCTURE",
      title: "三个信息单元如何建立阅读顺序",
      subtitle: "内容完全一致，只观察模块、间距和强调关系。",
      variant: "balanced",
      items: [
        { kicker: "01", title: "识别", body: "先让读者知道这一页正在回答什么。" },
        { kicker: "02", title: "组织", body: "再用结构说明信息之间的关系。" },
        { kicker: "03", title: "落点", body: "最后留下一个可以复述的结论。" },
      ],
    });
  } else if (layoutId === "kpi-grid-v1") {
    Object.assign(props, {
      eyebrow: "DATA STRUCTURE",
      title: "同一组指标，不同的数据展陈方式",
      subtitle: "数字、标签和解释文字保持一致。",
      variant: "cards",
      items: [
        { label: "理解速度", value: "42%", detail: "首屏建立信息方向", delta: "+8%" },
        { label: "结构识别", value: "76", detail: "模块关系保持清晰", delta: "+12" },
        { label: "行动记忆", value: "3.2×", detail: "结论更容易被复述", delta: "+0.6×" },
      ],
    });
  } else if (layoutId === "timeline-horizontal-v1") {
    Object.assign(props, {
      eyebrow: "PROCESS STRUCTURE",
      title: "相同流程如何形成前后关系",
      subtitle: "阶段名称和说明不变，只比较路径表达。",
      variant: "horizontal",
      steps: [
        { phase: "阶段 1", title: "识别", body: "确定页面的唯一任务。" },
        { phase: "阶段 2", title: "组织", body: "建立信息之间的顺序。" },
        { phase: "阶段 3", title: "落点", body: "形成可以执行的结论。" },
      ],
    });
  } else if (layoutId === "table-data-v1") {
    Object.assign(props, {
      eyebrow: "EVIDENCE STRUCTURE",
      title: "相同证据如何被扫描和比较",
      subtitle: "列、行和值完全一致，只观察表格与页面的关系。",
      variant: "ledger",
      columns: ["指标", "当前", "目标", "判断"],
      rows: [
        ["理解速度", "42%", "68%", "需提升"],
        ["结构识别", "76", "84", "接近"],
        ["行动记忆", "3.2×", "4.0×", "需强化"],
      ],
      source: "统一示意数据",
    });
  } else if (layoutId === "comparison-two-column-v1") {
    Object.assign(props, {
      eyebrow: "COMPARISON STRUCTURE",
      title: "相同观点如何建立对照",
      variant: "contrast",
      left: {
        label: "之前",
        title: "信息平铺",
        items: ["优先级不清楚", "页面之间相似", "结论难以复述"],
        footer: "结构只是容器",
      },
      right: {
        label: "之后",
        title: "结构叙事",
        items: ["单页任务明确", "阅读路径可感知", "结论形成落点"],
        footer: "结构参与表达",
      },
    });
  } else if (layoutId === "section-marker-v1") {
    Object.assign(props, {
      eyebrow: "CHAPTER STRUCTURE",
      number: "02",
      title: "从内容进入关键章节",
      subtitle: "观察章节页如何暂停、转场并重新建立注意力。",
      alignment: "left",
    });
  }
  return props;
}

function previewDeck(familyRecord, variant) {
  const theme = getTheme(familyRecord.themeId);
  if (!theme) throw new Error(`Unknown comparison theme: ${familyRecord.themeId}`);
  const layoutId = VARIANT_LAYOUTS[variant];
  if (!layoutId) throw new Error(`No comparison layout registered for variant ${variant}`);
  const design = createDeckDesign(theme, variant, familyRecord.id);
  if (design.variant !== variant) {
    throw new Error(`Gallery resolved ${design.variant}, expected ${variant}`);
  }
  return {
    theme,
    layoutId,
    deck: {
      schema_version: 1,
      title: `${familyRecord.name} · ${variant}`,
      theme_id: theme.id,
      design,
      slides: [
        {
          id: `preview-${familyRecord.id}-${variant}`,
          layout_id: layoutId,
          props: previewProps(layoutId, familyRecord, variant),
        },
      ],
    },
  };
}

function previewFileName(family, variant) {
  return `${family}--${variant}.html`;
}

function familyCard(familyRecord) {
  const variants = COMPOSITION_FAMILIES[familyRecord.id];
  const previews = variants.map(variant => {
    const layoutId = VARIANT_LAYOUTS[variant];
    const fileName = previewFileName(familyRecord.id, variant);
    return [
      '<figure class="variant-card">',
      '  <div class="preview-stage">',
      `    <iframe src="./${escapeHtml(fileName)}?mode=gallery" title="${escapeHtml(familyRecord.name)} ${escapeHtml(variant)} 预览" loading="eager"></iframe>`,
      "  </div>",
      '  <figcaption>',
      `    <span class="variant-name">${escapeHtml(variant)}</span>`,
      `    <span class="layout-role">${escapeHtml(LAYOUT_LABELS[layoutId] || layoutId)}</span>`,
      `    <a href="./${escapeHtml(fileName)}?mode=export" target="_blank" rel="noreferrer">放大查看</a>`,
      "  </figcaption>",
      "</figure>",
    ].join("\n");
  }).join("\n");
  return [
    '<article class="family-card">',
    '  <header class="family-head">',
    '    <div>',
    `      <p class="family-number">${escapeHtml(String(FAMILY_CATALOG.indexOf(familyRecord) + 1).padStart(2, "0"))}</p>`,
    `      <h3>${escapeHtml(familyRecord.name)}</h3>`,
    `      <code>${escapeHtml(familyRecord.id)}</code>`,
    "    </div>",
    '    <div class="family-description">',
    `      <p class="family-summary">${escapeHtml(familyRecord.summary)}</p>`,
    `      <p class="family-signals">内容信号：${escapeHtml(familyRecord.selectionSignals.join(" / "))}</p>`,
    "    </div>",
    `    <p class="theme-id">对比主题：${escapeHtml(familyRecord.themeId)}</p>`,
    "  </header>",
    `  <div class="variant-grid">${previews}</div>`,
    "</article>",
  ].join("\n");
}

function galleryDocument() {
  const groupIndex = FAMILY_GROUPS.map(group => {
    const count = FAMILY_CATALOG.filter(family => family.group === group.id).length;
    return [
      `<a href="#${escapeHtml(group.id)}">`,
      `  <span>${escapeHtml(group.label)}</span>`,
      `  <strong>${count}</strong>`,
      "</a>",
    ].join("\n");
  }).join("\n");
  const groups = FAMILY_GROUPS.map((group, groupIndexValue) => {
    const cards = FAMILY_CATALOG
      .filter(family => family.group === group.id)
      .map(familyCard)
      .join("\n");
    return [
      `<section class="family-group" id="${escapeHtml(group.id)}">`,
      '  <div class="group-head">',
      `    <p>DIRECTION ${String(groupIndexValue + 1).padStart(2, "0")}</p>`,
      `    <h2>${escapeHtml(group.label)}</h2>`,
      `    <div>${escapeHtml(group.summary)}</div>`,
      "  </div>",
      `  <div class="family-list">${cards}</div>`,
      "</section>",
    ].join("\n");
  }).join("\n");

  return [
    "<!doctype html>",
    '<html lang="zh-CN">',
    "<head>",
    '  <meta charset="utf-8" />',
    '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
    '  <link rel="icon" href="data:," />',
    "  <title>PPT 构图方向与家族对比馆</title>",
    "  <style>",
    "    :root { color-scheme: light; font-family: Inter, Aptos, Arial, 'PingFang SC', sans-serif; color: #151515; background: #ecece7; }",
    "    * { box-sizing: border-box; }",
    "    html { scroll-behavior: smooth; }",
    "    body { margin: 0; padding: 52px 40px 90px; }",
    "    .hero { width: min(1540px, 100%); margin: 0 auto 54px; display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); gap: 72px; padding: 22px 0 38px; border-top: 8px solid #151515; border-bottom: 1px solid #aaa9a2; }",
    "    .kicker, .group-head > p { margin: 0 0 13px; font-size: 12px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; color: #5e5e59; }",
    "    h1 { max-width: 950px; margin: 0; font-size: clamp(48px, 6.5vw, 100px); line-height: .92; letter-spacing: -.065em; }",
    "    .hero-copy { align-self: end; }",
    "    .hero-copy p { max-width: 680px; margin: 18px 0 0; color: #50504b; font-size: 18px; line-height: 1.6; }",
    "    .method { align-self: end; padding: 24px 0 4px; border-top: 1px solid #aaa9a2; }",
    "    .method strong { display: block; margin-bottom: 10px; font-size: 14px; letter-spacing: .06em; }",
    "    .method p { margin: 0; color: #555550; line-height: 1.6; }",
    "    .group-index { width: min(1540px, 100%); margin: 0 auto 74px; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-top: 1px solid #aaa9a2; border-left: 1px solid #aaa9a2; }",
    "    .group-index a { display: flex; justify-content: space-between; gap: 20px; padding: 18px 20px; border-right: 1px solid #aaa9a2; border-bottom: 1px solid #aaa9a2; color: #191919; text-decoration: none; }",
    "    .group-index a:hover { background: #fff; }",
    "    .group-index span { font-weight: 700; }",
    "    .group-index strong { color: #666660; font-variant-numeric: tabular-nums; }",
    "    .family-group { width: min(1540px, 100%); margin: 0 auto 92px; scroll-margin-top: 26px; }",
    "    .group-head { display: grid; grid-template-columns: 160px minmax(260px, .55fr) minmax(0, 1fr); gap: 30px; align-items: baseline; padding: 18px 0 24px; border-top: 4px solid #151515; }",
    "    .group-head > p { margin: 0; }",
    "    .group-head h2 { margin: 0; font-size: 35px; letter-spacing: -.035em; }",
    "    .group-head > div { color: #555550; font-size: 16px; line-height: 1.55; }",
    "    .family-list { display: grid; gap: 26px; }",
    "    .family-card { border: 1px solid #bdbcb5; background: #f8f8f4; }",
    "    .family-head { display: grid; grid-template-columns: minmax(320px, .72fr) minmax(0, 1fr) auto; gap: 34px; align-items: end; padding: 22px 24px 20px; border-bottom: 1px solid #bdbcb5; }",
    "    .family-number { display: inline-block; min-width: 38px; margin: 0 12px 0 0; color: #666660; font-size: 12px; font-weight: 800; vertical-align: middle; }",
    "    .family-head h3 { display: inline; margin: 0 12px 0 0; font-size: 27px; letter-spacing: -.035em; }",
    "    code { color: #5e5e59; font-size: 12px; }",
    "    .family-description { display: grid; gap: 8px; }",
    "    .family-summary { margin: 0; color: #44443f; line-height: 1.5; }",
    "    .family-signals { margin: 0; color: #666660; font-size: 12px; line-height: 1.45; }",
    "    .theme-id { margin: 0; color: #666660; font-size: 12px; white-space: nowrap; }",
    "    .variant-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }",
    "    .variant-card { min-width: 0; margin: 0; border-right: 1px solid #bdbcb5; }",
    "    .variant-card:last-child { border-right: 0; }",
    "    .preview-stage { position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #d6d6cf; }",
    "    iframe { position: absolute; left: 0; top: 0; width: 1920px; height: 1080px; border: 0; transform: scale(.25); transform-origin: top left; pointer-events: none; }",
    "    figcaption { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; align-items: center; min-height: 52px; padding: 12px 14px; border-top: 1px solid #bdbcb5; background: #fff; }",
    "    .variant-name { min-width: 0; overflow: hidden; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }",
    "    .layout-role { padding-left: 12px; border-left: 1px solid #bdbcb5; color: #666660; font-size: 12px; }",
    "    figcaption a { color: #151515; font-size: 12px; font-weight: 750; text-underline-offset: 3px; }",
    "    @media (max-width: 1080px) { body { padding: 30px 18px 70px; } .hero { grid-template-columns: 1fr; gap: 28px; } .group-index { grid-template-columns: repeat(2, minmax(0, 1fr)); } .group-head { grid-template-columns: 110px 1fr; } .group-head > div { grid-column: 2; } .family-head { grid-template-columns: 1fr; gap: 12px; } .variant-grid { grid-template-columns: 1fr; } .variant-card { border-right: 0; border-bottom: 1px solid #bdbcb5; } .variant-card:last-child { border-bottom: 0; } }",
    "  </style>",
    "</head>",
    "<body>",
    '  <header class="hero">',
    "    <div>",
    '      <p class="kicker">Controlled composition atlas · 11 families / 33 variants</p>',
    "      <h1>看骨架，不看换色</h1>",
    "    </div>",
    '    <div class="hero-copy">',
    "      <p>用户只需要理解 5 个方向；AI 再按内容信号从 11 个内部家族中选择具体骨架。每个家族展示三个 variant，并使用最容易看出差异的语义布局。</p>",
    '      <div class="method"><strong>判断方法</strong><p>先选方向，再看标题与主体占比、模块关系、数据区域和页面锚点。主题颜色只作为真实运行条件，不作为家族差异。</p></div>',
    "    </div>",
    "  </header>",
    `  <nav class="group-index" aria-label="五个构图方向">${groupIndex}</nav>`,
    `  <main>${groups}</main>`,
    "  <script>",
    "    (() => {",
    "      const fit = stage => {",
    "        const frame = stage.querySelector('iframe');",
    "        if (frame) frame.style.transform = `scale(${stage.clientWidth / 1920})`;",
    "      };",
    "      const stages = Array.from(document.querySelectorAll('.preview-stage'));",
    "      stages.forEach(fit);",
    "      if (window.ResizeObserver) {",
    "        const observer = new ResizeObserver(entries => entries.forEach(entry => fit(entry.target)));",
    "        stages.forEach(stage => observer.observe(stage));",
    "      } else {",
    "        window.addEventListener('resize', () => stages.forEach(fit));",
    "      }",
    "    })();",
    "  </script>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const outputPath = resolveArtifactPath(opts.out);
  if (!/\.html?$/i.test(outputPath)) throw new Error("--out must name an .html file");
  const outputDir = path.dirname(outputPath);
  fs.mkdirSync(outputDir, { recursive: true });

  let variantCount = 0;
  FAMILY_CATALOG.forEach(familyRecord => {
    const registeredVariants = COMPOSITION_FAMILIES[familyRecord.id];
    if (!registeredVariants) throw new Error(`Unknown composition family: ${familyRecord.id}`);
    registeredVariants.forEach(variant => {
      const preview = previewDeck(familyRecord, variant);
      const result = validateAndNormalizeDeck(preview.deck);
      if (!result.ok) {
        throw new Error(
          `${familyRecord.id}/${variant} preview is invalid:\n- ${result.issues.join("\n- ")}`
        );
      }
      fs.writeFileSync(
        path.join(outputDir, previewFileName(familyRecord.id, variant)),
        renderDocument(result.normalized, preview.theme),
        "utf8"
      );
      variantCount += 1;
    });
  });
  fs.writeFileSync(outputPath, galleryDocument(), "utf8");
  console.log(JSON.stringify({
    gallery: outputPath,
    family_count: FAMILY_CATALOG.length,
    variant_count: variantCount,
    groups: FAMILY_GROUPS.map(group => ({
      id: group.id,
      families: FAMILY_CATALOG
        .filter(family => family.group === group.id)
        .map(family => family.id),
    })),
  }, null, 2));
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
  FAMILY_CATALOG,
  FAMILY_GROUPS,
  VARIANT_LAYOUTS,
  galleryDocument,
  previewDeck,
};
