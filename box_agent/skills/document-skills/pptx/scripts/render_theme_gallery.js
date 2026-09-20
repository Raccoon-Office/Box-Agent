#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { createEditorProps, getLayout } = require("../layouts/registry.js");
const {
  createDeckDesign,
  getTheme,
  listThemes,
  resolveArtifactPath,
  themeManifestRecord,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const { renderDocument } = require("./render_deck_html.js");
const { preferredComposition } = require("../runtime/presentation-system.js");

const DEFAULT_PREVIEW_THEME_IDS = Object.freeze([
  "technical-blueprint",
  "product-console",
  "data-intelligence",
  "blue-professional",
  "signal",
  "biennale-yellow",
  "studio",
  "daisy-days",
  "comic-panel",
  "8-bit-orbit",
  "block-frame-mono-blue",
  "retro-windows",
  "soft-editorial",
  "impact-field",
  "stadium-score",
  "destination-atlas",
  "tasting-menu",
  "sketch-whiteboard",
]);

function parseArgs(argv) {
  const opts = {
    out: "theme-previews/index.html",
    themeIds: null,
    all: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--help" || arg === "-h") {
      console.log(
        "Usage: render_theme_gallery.js [--out theme-previews/index.html] " +
        "[--themes id,id,... | --all]"
      );
      process.exit(0);
    }
    if (arg === "--out" && value) {
      opts.out = value;
      index += 1;
    } else if (arg === "--themes" && value) {
      opts.themeIds = value.split(",").map(item => item.trim()).filter(Boolean);
      index += 1;
    } else if (arg === "--all") {
      opts.all = true;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (opts.all && opts.themeIds) throw new Error("Use either --themes or --all, not both");
  return opts;
}

function truncate(value, maxChars) {
  return Array.from(String(value || "")).slice(0, maxChars).join("");
}

function previewDeck(theme) {
  const manifest = themeManifestRecord(theme);
  const cover = createEditorProps("cover-editorial-v1");
  Object.assign(cover, {
    eyebrow: "THEME PREVIEW",
    title: truncate(theme.name || theme.id, 84),
    subtitle: truncate(theme.description || "内置受控主题预览", 160),
    marker: "01",
    meta: truncate(`${theme.id} · ${manifest.composition.family}`, 72),
  });

  const cards = createEditorProps("cards-grid-v1");
  Object.assign(cards, {
    eyebrow: "VISUAL LANGUAGE",
    title: "同一内容，不同视觉语言",
    subtitle: "查看字体、色彩、表面与构图节奏如何协同工作。",
    items: [
      { kicker: "01", title: "核心观点", body: "让主题先建立识别度，再承载具体叙事。" },
      { kicker: "02", title: "信息层级", body: "标题、正文和标签保持清晰的阅读顺序。" },
      { kicker: "03", title: "视觉节奏", body: "用结构变化组织页面，而不是只替换颜色。" },
    ],
  });

  const chart = createEditorProps("chart-data-v1");
  Object.assign(chart, {
    eyebrow: "DATA PREVIEW",
    title: "数据页也继承同一主题",
    subtitle: "图表保持可编辑数据与统一视觉语法。",
    categories: ["策略", "设计", "内容", "交付"],
    series: [
      { name: "当前", values: ["42", "58", "66", "74"] },
      { name: "目标", values: ["68", "76", "84", "92"] },
    ],
    chart_type: "column",
    insight: "主题会影响图表色板与页面构图，但不会牺牲数据可编辑性。",
    source: "示意数据",
  });

  let middleSlide = { id: "preview-content", layout_id: "cards-grid-v1", props: cards };
  let finalSlide = { id: "preview-chart", layout_id: "chart-data-v1", props: chart };

  if (theme.id === "sketch-whiteboard") {
    Object.assign(cover, {
      title: "把想法画出来，再一起讲清楚", subtitle: "手绘白板 · 头脑风暴与概念讲解",
      eyebrow: "SKETCH / DISCUSS / TRY", meta: "手画笔触 · 可编辑文字 · 一起完善",
    });
    Object.assign(cards, {
      eyebrow: "先观察，再动手", title: "三个问题，把讨论往前推", subtitle: "边框可以随性，信息要清楚。",
      items: [
        { kicker: "观察", title: "遇到了什么？", body: "用具体例子描述问题，把事实和猜测分开。" },
        { kicker: "构思", title: "可以怎么做？", body: "先展开几种可能，圈出值得讨论的关键点。" },
        { kicker: "验证", title: "先试哪一步？", body: "选一个小实验，写清负责人和反馈方式。" },
      ],
    });
    const comparison = createEditorProps("comparison-two-column-v1");
    Object.assign(comparison, {
      eyebrow: "从便签到实验", title: "让模糊的点子，变成具体行动",
      left: { label: "想法", title: "现在的假设", items: ["用户需要更清楚的下一步", "我们还不知道哪种提示有效"], footer: "先把不确定的地方圈出来" },
      right: { label: "行动", title: "一个小实验", items: ["画出两种提示草图", "请用户试用，记录真实反馈"], footer: "用观察结果继续修改" },
    });
    finalSlide = { id: "preview-sketch-action", layout_id: "comparison-two-column-v1", props: comparison };
  } else if (theme.id === "impact-field") {
    Object.assign(cover, { title: "让减排进展可追溯", subtitle: "可持续发展报告 · 场景示例" });
    Object.assign(cards, {
      eyebrow: "MEASURE / ACT / REVIEW", title: "从核算边界到行动记录", subtitle: "先明确口径，再呈现进展。",
      items: [
        { kicker: "核算", title: "定义基准", body: "说明报告周期、组织边界与计量单位。" },
        { kicker: "行动", title: "资源循环", body: "展示能源、材料与供应链的改善措施。" },
        { kicker: "复核", title: "证据记录", body: "分别标注已核实结果、目标与待核验内容。" },
      ],
    });
    Object.assign(chart, {
      title: "同一口径比较基准与目标", subtitle: "排放强度指数 · 基准年 = 100 · 仅为布局示意",
      categories: ["能源", "运输", "材料"], series: [{ name: "基准", values: ["100", "100", "100"] }, { name: "目标", values: ["75", "85", "80"] }],
      insight: "目标值不代表已经实现的减排成果。", source: "示意数据，非真实企业披露",
    });
  } else if (theme.id === "stadium-score") {
    Object.assign(cover, { title: "每一回合，都有进步", subtitle: "赛事复盘与训练总结 · 场景示例" });
    const kpis = createEditorProps("kpi-grid-v1");
    Object.assign(kpis, {
      title: "比赛表现速览", subtitle: "示意数据，非真实赛果",
      items: [
        { label: "得分", value: "86", detail: "呈现本场核心结果。", delta: "" },
        { label: "助攻", value: "24", detail: "观察团队配合质量。", delta: "" },
        { label: "篮板", value: "42", detail: "回看攻防回合表现。", delta: "" },
      ],
    });
    middleSlide = { id: "preview-score", layout_id: "kpi-grid-v1", props: kpis };
    Object.assign(cards, {
      title: "复盘落到训练动作", subtitle: "把观察转化为下一次训练的重点。",
      items: [
        { kicker: "进攻", title: "出球选择", body: "用关键回合解释传球和投篮决策。" },
        { kicker: "防守", title: "转换落位", body: "从回放记录中寻找重复出现的问题。" },
        { kicker: "训练", title: "专项练习", body: "为下一阶段明确动作、负责人和复测条件。" },
      ],
    });
    finalSlide = { id: "preview-review", layout_id: "cards-grid-v1", props: cards };
  } else if (theme.id === "destination-atlas") {
    Object.assign(cover, { title: "沿着海岸，读懂一座城", subtitle: "文旅推介与城市漫游 · 概念路线示例" });
    Object.assign(cards, {
      title: "一条路线，三种记忆", subtitle: "以真实目的地资料替换以下概念节点。",
      items: [
        { kicker: "街巷", title: "旧城漫步", body: "沿街区纹理认识城市的日常生活。" },
        { kicker: "文化", title: "地方展馆", body: "让藏品、工艺与在地故事互相呼应。" },
        { kicker: "海岸", title: "滨水慢行", body: "用开阔风景收束一天的城市体验。" },
      ],
    });
    const route = createEditorProps("timeline-horizontal-v1");
    Object.assign(route, {
      title: "一天的行程节奏", subtitle: "示例顺序，不代表已核实交通或开放时间",
      steps: [
        { phase: "上午", title: "街区", body: "步行探索与在地早餐。" },
        { phase: "午后", title: "展馆", body: "文化体验与短暂停留。" },
        { phase: "傍晚", title: "海岸", body: "滨水慢行与落日观景。" },
      ],
    });
    finalSlide = { id: "preview-route", layout_id: "timeline-horizontal-v1", props: route };
  } else if (theme.id === "tasting-menu") {
    Object.assign(cover, { title: "把季节，端上餐桌", subtitle: "餐饮品牌与风味故事 · 概念菜单示例" });
    Object.assign(cards, {
      title: "一道菜的三层表达", subtitle: "从食材来源讲到餐桌体验。",
      items: [
        { kicker: "食材", title: "当季选择", body: "说明食材来源、季节特点与替换原则。" },
        { kicker: "手艺", title: "烹饪表达", body: "用简短文字讲清技法与口感层次。" },
        { kicker: "体验", title: "上菜节奏", body: "让器皿、服务与空间共同支持品牌故事。" },
      ],
    });
    const menu = createEditorProps("comparison-two-column-v1");
    Object.assign(menu, {
      title: "两种餐桌体验", eyebrow: "MENU CONCEPT",
      left: { label: "午间", title: "轻盈与明快", items: ["当季蔬菜与清爽汤品", "适合短暂停留的上菜节奏"], footer: "概念示例" },
      right: { label: "晚间", title: "层次与分享", items: ["小份多道与风味递进", "适合交流的共享餐桌"], footer: "概念示例" },
    });
    finalSlide = { id: "preview-menu", layout_id: "comparison-two-column-v1", props: menu };
  } else if (theme.id === "technical-blueprint") {
    const diagram = createEditorProps("technical-diagram-v1");
    Object.assign(diagram, {
      eyebrow: "SYSTEM BLUEPRINT",
      title: "企业 AI 平台技术架构",
      subtitle: "架构节点、连接关系与 DiagramSpec 保持可恢复编辑。",
      note: "HTML 可增删节点和边；PPTX 以单个 SVG 矢量对象导出。",
    });
    middleSlide = {
      id: "preview-architecture",
      layout_id: "technical-diagram-v1",
      props: diagram,
    };
    finalSlide = { id: "preview-modules", layout_id: "cards-grid-v1", props: cards };
  } else if (theme.id === "product-console") {
    const product = createEditorProps("project-case-study-v1");
    Object.assign(product, {
      eyebrow: "PRODUCT CONSOLE",
      title: "一站式 AI 工作台",
      positioning: "用浏览器壳、状态芯片和功能舞台讲清产品价值与关键交互。",
      metrics: [
        { value: "3×", label: "核心工作流" },
        { value: "1 个", label: "统一控制台" },
        { value: "Live", label: "运行状态" },
      ],
      caption: "产品界面示意 · 可替换真实截图",
    });
    middleSlide = {
      id: "preview-product",
      layout_id: "project-case-study-v1",
      props: product,
    };
    finalSlide = { id: "preview-features", layout_id: "cards-grid-v1", props: cards };
  } else if (theme.id === "data-intelligence") {
    const kpis = createEditorProps("kpi-grid-v1");
    Object.assign(kpis, {
      eyebrow: "INTELLIGENCE OVERVIEW",
      title: "经营信号一屏读懂",
      subtitle: "高密度指标、趋势与证据轨道形成决策上下文。",
      items: [
        { label: "增长动能", value: "+18%", detail: "核心业务保持正向增长。", delta: "同比 +6pt" },
        { label: "转化效率", value: "32%", detail: "关键漏斗环节继续改善。", delta: "环比 +4pt" },
        { label: "风险信号", value: "03", detail: "三项指标需要持续跟踪。", delta: "本周新增 1 项" },
      ],
    });
    middleSlide = { id: "preview-kpis", layout_id: "kpi-grid-v1", props: kpis };
  }

  return {
    schema_version: 1,
    title: `${theme.name || theme.id} theme preview`,
    theme_id: theme.id,
    design: createDeckDesign(theme),
    slides: [
      { id: "preview-cover", layout_id: "cover-editorial-v1", props: cover },
      middleSlide,
      finalSlide,
    ].map(slide => {
      slide.props.composition = preferredComposition(getLayout(slide.layout_id));
      for (const field of ["image", "hero"]) {
        if (slide.props[field]) slide.props[field] = { ...slide.props[field],
          src: `data:image/svg+xml;base64,${fs.readFileSync(path.join(__dirname, "../examples/presentation-preview.svg")).toString("base64")}`,
          alt: "主题预览用概念界面，非真实产品" };
      }
      return slide;
    }),
  };
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function galleryDocument(themes) {
  const cards = themes.map(theme => {
    const manifest = themeManifestRecord(theme);
    const moods = Array.isArray(manifest.selection.mood_keywords)
      ? manifest.selection.mood_keywords.slice(0, 3).join(" · ")
      : "";
    const fileName = `${theme.id}.html`;
    return [
      '<article class="theme-card">',
      '  <div class="preview-stage">',
      `    <iframe src="./${escapeHtml(fileName)}?mode=gallery" title="${escapeHtml(theme.name || theme.id)} 主题预览" loading="lazy"></iframe>`,
      "  </div>",
      '  <div class="theme-copy">',
      `    <p class="family">${escapeHtml(manifest.composition.family)}</p>`,
      `    <h2>${escapeHtml(theme.name || theme.id)}</h2>`,
      `    <code>${escapeHtml(theme.id)}</code>`,
      `    <p class="description">${escapeHtml(theme.description || "")}</p>`,
      `    <p class="moods">${escapeHtml(moods)}</p>`,
      `    <a href="./${escapeHtml(fileName)}?mode=export" target="_blank" rel="noreferrer">打开 3 页完整预览 →</a>`,
      "  </div>",
      "</article>",
    ].join("\n");
  }).join("\n");

  return [
    "<!doctype html>",
    '<html lang="zh-CN">',
    "<head>",
    '  <meta charset="utf-8" />',
    '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
    '  <link rel="icon" href="data:," />',
    "  <title>PPT 内置主题预览</title>",
    "  <style>",
    "    :root { color-scheme: light; font-family: Inter, Aptos, Arial, 'PingFang SC', sans-serif; color: #161616; background: #f2f1ed; }",
    "    * { box-sizing: border-box; }",
    "    body { margin: 0; padding: 48px; }",
    "    header { max-width: 1040px; margin: 0 auto 36px; }",
    "    .kicker { margin: 0 0 12px; font-size: 13px; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; color: #575752; }",
    "    h1 { margin: 0; font-size: clamp(36px, 5vw, 72px); line-height: .98; letter-spacing: -.045em; }",
    "    .intro { max-width: 760px; margin: 20px 0 0; color: #555550; font-size: 18px; line-height: 1.55; }",
    "    .instruction { margin: 16px 0 0; padding-left: 14px; border-left: 3px solid #202020; font-weight: 650; }",
    "    .gallery { width: min(1500px, 100%); margin: 0 auto; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 28px; }",
    "    .theme-card { overflow: hidden; border: 1px solid #c9c8c2; background: #fff; }",
    "    .preview-stage { position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #d8d8d2; }",
    "    iframe { position: absolute; left: 0; top: 0; width: 1920px; height: 1080px; border: 0; transform: scale(.25); transform-origin: top left; pointer-events: none; }",
    "    .theme-copy { padding: 22px 24px 24px; }",
    "    .family { margin: 0 0 8px; color: #676762; font-size: 12px; font-weight: 750; letter-spacing: .1em; text-transform: uppercase; }",
    "    h2 { display: inline; margin: 0 12px 0 0; font-size: 25px; letter-spacing: -.025em; }",
    "    code { color: #666; font-size: 13px; }",
    "    .description { min-height: 48px; margin: 14px 0 0; color: #444; line-height: 1.5; }",
    "    .moods { min-height: 20px; margin: 10px 0 18px; color: #777; font-size: 13px; }",
    "    a { color: #111; font-weight: 700; text-underline-offset: 4px; }",
    "    @media (max-width: 980px) { body { padding: 28px 18px; } .gallery { grid-template-columns: 1fr; } }",
    "  </style>",
    "</head>",
    "<body>",
    "  <header>",
    '    <p class="kicker">Built-in controlled themes</p>',
    "    <h1>先看主题，再开始做 PPT</h1>",
    `    <p class="intro">这里展示 ${themes.length} 个代表性内置主题。每个主题都使用真实受控渲染器生成，包含自己的色彩、字体、视觉语法与 HTML 构图家族。</p>`,
    '    <p class="instruction">看完后，回复卡片上的 theme_id；如果都不合适，也可以描述你想要的气质。</p>',
    "  </header>",
    `  <main class="gallery">${cards}</main>`,
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

function selectedThemes(opts) {
  const ids = opts.all
    ? listThemes().map(theme => theme.id)
    : opts.themeIds || DEFAULT_PREVIEW_THEME_IDS;
  const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
  if (duplicates.length) throw new Error(`Duplicate theme id(s): ${[...new Set(duplicates)].join(", ")}`);
  return ids.map(id => {
    const theme = getTheme(id);
    if (!theme) throw new Error(`Unknown theme_id: ${JSON.stringify(id)}`);
    return theme;
  });
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const outputPath = resolveArtifactPath(opts.out);
  if (!/\.html?$/i.test(outputPath)) throw new Error("--out must name an .html file");
  const outputDir = path.dirname(outputPath);
  const themes = selectedThemes(opts);
  fs.mkdirSync(outputDir, { recursive: true });

  themes.forEach(theme => {
    const result = validateAndNormalizeDeck(previewDeck(theme));
    if (!result.ok) {
      throw new Error(`${theme.id} preview is invalid:\n- ${result.issues.join("\n- ")}`);
    }
    fs.writeFileSync(
      path.join(outputDir, `${theme.id}.html`),
      renderDocument(result.normalized, theme),
      "utf8"
    );
  });
  fs.writeFileSync(outputPath, galleryDocument(themes), "utf8");
  console.log(JSON.stringify({
    gallery: outputPath,
    theme_count: themes.length,
    themes: themes.map(theme => theme.id),
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
  DEFAULT_PREVIEW_THEME_IDS,
  galleryDocument,
  previewDeck,
};
