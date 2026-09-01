"use strict";

const { createHash, randomBytes } = require("crypto");

const COMPOSITION_FAMILIES = Object.freeze({
  "institutional-grid": Object.freeze([
    "balanced-grid",
    "rail-grid",
    "ledger-grid",
  ]),
  "editorial-spread": Object.freeze([
    "split-spread",
    "feature-spread",
    "banded-spread",
  ]),
  "poster-asymmetric": Object.freeze([
    "offset-hero",
    "stacked-poster",
    "split-poster",
  ]),
  "playful-collage": Object.freeze([
    "mosaic",
    "staggered",
    "capsule",
  ]),
  "brutalist-frame": Object.freeze([
    "block-grid",
    "offset-frame",
    "ledger-frame",
  ]),
  "retro-interface": Object.freeze([
    "window-grid",
    "terminal-stack",
    "pixel-panels",
  ]),
  "literary-minimal": Object.freeze([
    "margin-note",
    "quiet-center",
    "asymmetric-column",
  ]),
  "product-showcase": Object.freeze([
    "device-stage",
    "browser-story",
    "annotated-flow",
  ]),
  "cinematic-canvas": Object.freeze([
    "full-bleed",
    "split-film",
    "chapter-cut",
  ]),
  "analytical-exhibit": Object.freeze([
    "exhibit-grid",
    "evidence-rail",
    "decision-board",
  ]),
  "technical-schematic": Object.freeze([
    "blueprint-canvas",
    "annotated-system",
    "spec-sheet",
  ]),
});

const COMPOSITION_FAMILY_META = Object.freeze({
  "institutional-grid": Object.freeze({
    name: "机构网格",
    direction: "structured-systems",
    preview_theme_id: "blue-professional",
    summary: "规整栏线、稳定层级和企业信息秩序。",
    selection_signals: Object.freeze(["常规商业汇报", "组织治理", "稳健信息秩序"]),
  }),
  "analytical-exhibit": Object.freeze({
    name: "分析展板",
    direction: "structured-systems",
    preview_theme_id: "blue-professional",
    summary: "证据轨道、主次指标和高密度决策展陈。",
    selection_signals: Object.freeze(["KPI 与图表", "证据对照", "决策看板"]),
  }),
  "technical-schematic": Object.freeze({
    name: "技术蓝图",
    direction: "structured-systems",
    preview_theme_id: "blue-professional",
    summary: "蓝图网格、节点连接和规格化技术表达。",
    selection_signals: Object.freeze(["系统架构", "流程节点", "接口与规格说明"]),
  }),
  "editorial-spread": Object.freeze({
    name: "编辑跨页",
    direction: "narrative-pages",
    preview_theme_id: "vellum",
    summary: "杂志跨页、图文对照与连续的编辑节奏。",
    selection_signals: Object.freeze(["研究叙事", "杂志跨页", "图文长内容"]),
  }),
  "literary-minimal": Object.freeze({
    name: "文学极简",
    direction: "narrative-pages",
    preview_theme_id: "blue-professional",
    summary: "窄栏、边注和更安静的单页阅读。",
    selection_signals: Object.freeze(["长文本", "观点文章", "安静研究报告"]),
  }),
  "poster-asymmetric": Object.freeze({
    name: "非对称海报",
    direction: "visual-impact",
    preview_theme_id: "block-frame-mono-blue",
    summary: "大标题、偏移构图和图形化视觉锚点。",
    selection_signals: Object.freeze(["品牌宣言", "强封面", "少量大字"]),
  }),
  "cinematic-canvas": Object.freeze({
    name: "电影画布",
    direction: "visual-impact",
    preview_theme_id: "studio",
    summary: "大画面、遮幅和章节切换形成演示节拍。",
    selection_signals: Object.freeze(["图片主导", "人物故事", "章节转场"]),
  }),
  "product-showcase": Object.freeze({
    name: "产品展示",
    direction: "interface-modules",
    preview_theme_id: "blue-professional",
    summary: "设备舞台、浏览器叙事和可追踪的功能流程。",
    selection_signals: Object.freeze(["UI 截图", "功能演示", "产品流程"]),
  }),
  "retro-interface": Object.freeze({
    name: "复古界面",
    direction: "interface-modules",
    preview_theme_id: "retro-windows",
    summary: "窗口、终端和像素面板形成复古系统感。",
    selection_signals: Object.freeze(["复古技术", "窗口或终端隐喻", "怀旧内容"]),
  }),
  "playful-collage": Object.freeze({
    name: "趣味拼贴",
    direction: "expressive-objects",
    preview_theme_id: "daisy-days",
    summary: "拼贴、错位和轻松的模块节奏。",
    selection_signals: Object.freeze(["教育与社区", "工作坊", "轻松表达"]),
  }),
  "brutalist-frame": Object.freeze({
    name: "粗野框架",
    direction: "expressive-objects",
    preview_theme_id: "block-frame-mono-blue",
    summary: "硬边框、块面和更直接的视觉结构。",
    selection_signals: Object.freeze(["强态度", "密集模块", "硬边视觉"]),
  }),
});

const COMPOSITION_SELECTION_RULES = Object.freeze({
  "institutional-grid": Object.freeze({
    content: Object.freeze([
      [/(?:法律意见书|案件分析|诉讼策略|争议解决|证据分析|合规审查|法务汇报|legal\s+opinion|case\s+analysis|litigation\s+strategy|compliance\s+review)/i, 5],
      [/(?:房地产|地产开发|项目投拓|土地研判|城市更新|商业地产|住宅项目|real\s+estate|property\s+development|land\s+acquisition|urban\s+renewal)/i, 5],
      [/(?:政务汇报|政府工作|公共政策|城市治理|社会治理|监管政策|公共服务|public\s+policy|government\s+(?:brief|report)|municipal\s+governance|public\s+governance)/i, 5],
      [/(?:商业汇报|经营汇报|年度汇报|组织治理|管理层|董事会|business\s+review|governance|annual\s+review)/i, 3],
      [/(?:内部沟通|内部汇报|常规汇报|业务规划|产品规划|项目规划|项目进展|月度规划|季度规划|工作规划|internal\s+(?:brief|update|review)|project\s+(?:plan|update))/i, 3],
      [/(?:新员工入职|员工入职|入职培训|内部培训|员工培训|迎新|onboarding|employee\s+orientation|internal\s+training)/i, 3],
      [/(?:企业|公司|组织|战略|规划|business|corporate|strategy)/i, 1],
    ]),
    layouts: Object.freeze([
      [/^(?:cards-grid|comparison-two-column|closing-next-steps)-v\d+$/i, 1],
    ]),
  }),
  "analytical-exhibit": Object.freeze({
    content: Object.freeze([
      [/(?:投资备忘录|投资分析|估值分析|资本配置|财报解读|投资者关系|investment\s+(?:memo|thesis|analysis)|valuation\s+analysis|capital\s+allocation|earnings\s+analysis)/i, 5],
      [/(?:经营复盘|业绩复盘|销售复盘|季度复盘|月度复盘|季度经营|经营月报|经营季报|performance\s+review|business\s+review|quarterly\s+review|monthly\s+review|sales\s+review)/i, 4],
      [/(?:KPI|指标|数据看板|决策看板|数据分析|量化|benchmark|metrics?|dashboard|evidence\s+board)/i, 3],
      [/(?:图表|热力图|风险矩阵|同比|环比|增长率|收入|成本|chart|heat\s*map|risk\s+matrix|revenue|growth|comparison)/i, 2],
      [/(?:表格|\btable\b)/i, 1],
    ]),
    layouts: Object.freeze([
      [/^(?:chart-bar|chart-data|kpi-grid|heatmap-matrix)-v\d+$/i, 3],
      [/^table-data-v\d+$/i, 1],
      [/^comparison-two-column-v\d+$/i, 1],
    ]),
  }),
  "technical-schematic": Object.freeze({
    content: Object.freeze([
      [/(?:智能制造|制造业|生产线|生产车间|工厂运营|精益生产|质量改善|\bOEE\b|manufacturing|factory\s+operations?|production\s+line|shop\s+floor|lean\s+manufacturing)/i, 5],
      [/(?:临床试验|病例分析|患者路径|诊疗路径|药物研发|clinical\s+trial|patient\s+(?:journey|pathway)|drug\s+development)/i, 5],
      [/(?:系统架构|技术架构|架构图|流程节点|协作节点|节点连接|接口规格|运行时|编译链|agent\s+loop|system\s+architecture|technical\s+architecture|runtime|compiler|API\s+contract)/i, 4],
      [/(?:代码窗口|代码片段|终端|命令行|数据流|接口|code\s+window|code\s+snippet|terminal|data\s+flow|API\s+contract)/i, 3],
    ]),
    layouts: Object.freeze([]),
  }),
  "editorial-spread": Object.freeze({
    content: Object.freeze([
      [/(?:新员工入职|员工手册|企业文化培训|人才发展|招聘宣讲|雇主品牌|employee\s+onboarding|employee\s+handbook|people\s+ops|talent\s+development|culture\s+handbook)/i, 5],
      [/(?:编辑跨页|杂志|专题报道|研究叙事|图文长内容|editorial|magazine|feature\s+story|research\s+narrative)/i, 4],
      [/(?:新员工入职|入职培训|员工培训|培训课件|迎新|onboarding|employee\s+orientation|training\s+deck)/i, 4],
      [/(?:采访|报道|章节|长篇|narrative|chapter|essay)/i, 2],
    ]),
    layouts: Object.freeze([
      [/^(?:text-columns|image-hero-split|cover-editorial)-v\d+$/i, 2],
    ]),
  }),
  "literary-minimal": Object.freeze({
    content: Object.freeze([
      [/(?:学术论文|论文答辩|开题报告|文献综述|学术研究方法|博士论文|硕士论文|thesis|dissertation|literature\s+review|research\s+methodology)/i, 5],
      [/(?:文学|诗歌|观点文章|安静研究|极简阅读|literary|poetry|quiet\s+report|long[- ]form)/i, 4],
      [/(?:长文本|文章|随笔|阅读|reflection|essay|memo)/i, 2],
    ]),
    layouts: Object.freeze([
      [/^(?:text-columns|statement-focus|cover-editorial)-v\d+$/i, 1],
    ]),
  }),
  "poster-asymmetric": Object.freeze({
    content: Object.freeze([
      [/(?:品牌宣言|强封面|视觉宣言|海报|发布会主视觉|brand\s+manifesto|poster|keynote\s+visual)/i, 4],
      [/(?:大标题|视觉冲击|少量大字|bold\s+headline|visual\s+impact)/i, 2],
    ]),
    layouts: Object.freeze([
      [/^(?:cover-hero|section-marker|statement-focus)-v\d+$/i, 1],
    ]),
  }),
  "cinematic-canvas": Object.freeze({
    content: Object.freeze([
      [/(?:人物故事|传记|体育故事|旅行故事|历史故事|电影化|大画面|biography|profile|sports?\s+story|travel\s+story|cinematic|full[- ]bleed)/i, 4],
      [/(?:照片主导|图片主导|章节转场|photo[- ]led|image[- ]led|chapter\s+cut)/i, 3],
    ]),
    layouts: Object.freeze([
      [/^image-hero-split-v\d+$/i, 3],
      [/^(?:cover-hero|section-marker)-v\d+$/i, 1],
    ]),
  }),
  "product-showcase": Object.freeze({
    content: Object.freeze([
      [/(?:零售电商|零售运营|电商运营|商品运营|\bSKU\b|\bGMV\b|动销|客单价|复购率|转化漏斗|retail\s+operations?|e-?commerce\s+operations?|merchandising|conversion\s+funnel)/i, 5],
      [/(?:供应链|物流运营|物流网络|仓储管理|库存周转|订单履约|\bOTIF\b|supply\s+chain|logistics\s+operations?|warehouse\s+management|order\s+fulfillment)/i, 5],
      [/(?:UI\s*截图|产品界面|客户端界面|主界面|工作台|编辑器界面|浏览器窗口|设备样机|产品主视觉|UI\s*screenshot|product\s+interface|client\s+interface|browser\s+window|device\s+mockup)/i, 5],
      [/(?:产品演示|功能演示|产品流程|功能流程|SaaS|软件产品|应用界面|product\s+demo|feature\s+demo|product\s+flow|user\s+flow)/i, 4],
      [/(?:产品|功能|客户端|软件|平台|product|feature|application|app\b)/i, 1],
    ]),
    layouts: Object.freeze([
      [/^project-case-study-v\d+$/i, 3],
      [/^image-hero-split-v\d+$/i, 1],
      [/^cards-grid-v\d+$/i, 1],
    ]),
  }),
  "retro-interface": Object.freeze({
    content: Object.freeze([
      [/(?:复古界面|像素|怀旧技术|老式窗口|终端隐喻|retro|pixel|nostalgia|vintage\s+interface)/i, 5],
    ]),
    layouts: Object.freeze([]),
  }),
  "playful-collage": Object.freeze({
    content: Object.freeze([
      [/(?:拼贴|工作坊|社区活动|儿童|校园|轻松表达|collage|workshop|community|kids?|campus|playful)/i, 4],
      [/(?:教育|课堂|社群|education|classroom)/i, 2],
    ]),
    layouts: Object.freeze([
      [/^cards-grid-v\d+$/i, 1],
    ]),
  }),
  "brutalist-frame": Object.freeze({
    content: Object.freeze([
      [/(?:漫画|分镜|对话气泡|对白气泡|拟声词|网点纸|波普漫画|comic(?:[- ]?book)?|comic\s+panel|graphic\s+novel|storyboard|speech\s+bubble|halftone|manga|pop[- ]?art)/i, 7],
      [/(?:粗野|硬边|强态度|密集模块|brutalist|hard[- ]edge|raw\s+grid)/i, 5],
    ]),
    layouts: Object.freeze([
      [/^(?:cards-grid|statement-focus)-v\d+$/i, 1],
    ]),
  }),
});

const COMPOSITION_DIRECTIONS = Object.freeze([
  Object.freeze({
    id: "structured-systems",
    label: "结构与证据",
    summary: "商业报告、数据决策与系统说明；AI 按内容更偏常规叙述、证据还是连接关系选择具体家族。",
    families: Object.freeze([
      "institutional-grid",
      "analytical-exhibit",
      "technical-schematic",
    ]),
  }),
  Object.freeze({
    id: "narrative-pages",
    label: "编辑与叙事",
    summary: "面向研究、长文和连续故事；在杂志式跨页与安静单页阅读之间选择。",
    families: Object.freeze(["editorial-spread", "literary-minimal"]),
  }),
  Object.freeze({
    id: "visual-impact",
    label: "视觉冲击",
    summary: "面向品牌、人物和画面驱动内容；在图形海报与电影化章节节奏之间选择。",
    families: Object.freeze(["poster-asymmetric", "cinematic-canvas"]),
  }),
  Object.freeze({
    id: "interface-modules",
    label: "产品与界面",
    summary: "面向产品功能与数字体验；在现代设备演示与复古界面叙事之间选择。",
    families: Object.freeze(["product-showcase", "retro-interface"]),
  }),
  Object.freeze({
    id: "expressive-objects",
    label: "表达性构件",
    summary: "面向需要鲜明个性的内容；在轻松拼贴与硬边框架之间选择。",
    families: Object.freeze(["playful-collage", "brutalist-frame"]),
  }),
]);

const FAMILY_DIRECTION = Object.freeze(Object.fromEntries(
  COMPOSITION_DIRECTIONS.flatMap(direction =>
    direction.families.map(family => [family, direction.id])
  )
));

if (
  Object.keys(FAMILY_DIRECTION).length !== Object.keys(COMPOSITION_FAMILIES).length
  || Object.keys(COMPOSITION_FAMILIES).some(family => !FAMILY_DIRECTION[family])
) {
  throw new Error("Every composition family must belong to exactly one direction");
}

const FAMILY_COMPATIBILITY = Object.freeze({
  "institutional-grid": Object.freeze([
    "institutional-grid",
    "analytical-exhibit",
    "product-showcase",
    "technical-schematic",
    "literary-minimal",
  ]),
  "editorial-spread": Object.freeze([
    "editorial-spread",
    "cinematic-canvas",
    "literary-minimal",
    "poster-asymmetric",
  ]),
  "poster-asymmetric": Object.freeze([
    "poster-asymmetric",
    "cinematic-canvas",
    "product-showcase",
    "editorial-spread",
  ]),
  "playful-collage": Object.freeze([
    "playful-collage",
    "cinematic-canvas",
    "product-showcase",
    "editorial-spread",
  ]),
  "brutalist-frame": Object.freeze([
    "brutalist-frame",
    "product-showcase",
    "analytical-exhibit",
    "technical-schematic",
    "poster-asymmetric",
  ]),
  "retro-interface": Object.freeze([
    "retro-interface",
    "product-showcase",
    "cinematic-canvas",
  ]),
  "literary-minimal": Object.freeze([
    "literary-minimal",
    "editorial-spread",
    "analytical-exhibit",
    "cinematic-canvas",
    "technical-schematic",
  ]),
});

const THEME_COMPOSITION_FAMILY = Object.freeze({
  "8-bit-orbit": "retro-interface",
  "biennale-yellow": "editorial-spread",
  "block-frame": "brutalist-frame",
  "block-frame-mono-blue": "brutalist-frame",
  "blue-professional": "institutional-grid",
  "bold-poster": "poster-asymmetric",
  broadside: "editorial-spread",
  capsule: "playful-collage",
  cartesian: "literary-minimal",
  "cobalt-grid": "institutional-grid",
  coral: "editorial-spread",
  "comic-panel": "brutalist-frame",
  "creative-mode": "poster-asymmetric",
  "data-intelligence": "analytical-exhibit",
  "capital-ledger": "analytical-exhibit",
  "civic-brief": "institutional-grid",
  "clinical-atlas": "technical-schematic",
  "commerce-pulse": "product-showcase",
  "daisy-days": "playful-collage",
  "editorial-tri-tone": "editorial-spread",
  "factory-floor": "technical-schematic",
  grove: "literary-minimal",
  "long-table": "editorial-spread",
  "legal-docket": "institutional-grid",
  "logistics-control-tower": "product-showcase",
  mat: "editorial-spread",
  monochrome: "institutional-grid",
  "neo-grid-bold": "brutalist-frame",
  "peoples-platform": "poster-asymmetric",
  "people-handbook": "editorial-spread",
  "pin-and-paper": "literary-minimal",
  "pink-script": "literary-minimal",
  "product-console": "product-showcase",
  "property-atlas": "institutional-grid",
  "raw-grid": "brutalist-frame",
  "retro-windows": "retro-interface",
  "retro-zine": "retro-interface",
  "research-notebook": "literary-minimal",
  "sakura-chroma": "playful-collage",
  scatterbrain: "playful-collage",
  signal: "institutional-grid",
  "soft-editorial": "literary-minimal",
  "stencil-tablet": "brutalist-frame",
  studio: "poster-asymmetric",
  "technical-blueprint": "technical-schematic",
  vellum: "literary-minimal",
});

const DESIGN_SEED_RE = /^[A-Za-z0-9_-]{8,64}$/;
const ALL_VARIANTS = new Set(Object.values(COMPOSITION_FAMILIES).flat());

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function themeIdOf(themeOrId) {
  if (isPlainObject(themeOrId) && typeof themeOrId.id === "string") {
    return themeOrId.id.trim();
  }
  return String(themeOrId || "").trim();
}

function themeCompositionPolicy(themeOrId) {
  const themeId = themeIdOf(themeOrId);
  const configured = isPlainObject(themeOrId) && isPlainObject(themeOrId.composition)
    ? themeOrId.composition
    : {};
  const legacyDefault = THEME_COMPOSITION_FAMILY[themeId] || null;
  const defaultFamily = typeof configured.default_family === "string"
    ? configured.default_family.trim()
    : legacyDefault;
  if (!defaultFamily) return null;
  if (!COMPOSITION_FAMILIES[defaultFamily]) {
    throw new Error(
      `Theme ${JSON.stringify(themeId)} declares unknown default composition family ${JSON.stringify(defaultFamily)}`
    );
  }
  const configuredAllowed = Array.isArray(configured.allowed_families)
    ? configured.allowed_families.map(value => String(value || "").trim()).filter(Boolean)
    : null;
  const candidates = configuredAllowed || FAMILY_COMPATIBILITY[defaultFamily] || [defaultFamily];
  const unknown = candidates.filter(family => !COMPOSITION_FAMILIES[family]);
  if (unknown.length) {
    throw new Error(
      `Theme ${JSON.stringify(themeId)} declares unknown allowed composition family: ${unknown.join(", ")}`
    );
  }
  return {
    theme_id: themeId,
    default_family: defaultFamily,
    allowed_families: [...new Set([defaultFamily, ...candidates])],
  };
}

function familyForTheme(themeOrId) {
  const policy = themeCompositionPolicy(themeOrId);
  return policy ? policy.default_family : null;
}

function allowedFamiliesForTheme(themeOrId) {
  const policy = themeCompositionPolicy(themeOrId);
  return policy ? [...policy.allowed_families] : [];
}

function compositionSelectionText(value) {
  const parts = [];
  const visit = item => {
    if (typeof item === "string") {
      const normalized = item.trim();
      if (normalized) parts.push(normalized);
      return;
    }
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (isPlainObject(item)) Object.values(item).forEach(visit);
  };
  visit(value);
  return parts.join("\n");
}

function inferCompositionFamily(themeOrId, content = "", layoutIds = []) {
  const policy = themeCompositionPolicy(themeOrId);
  const themeId = themeIdOf(themeOrId);
  if (!policy) throw new Error(`Theme ${JSON.stringify(themeId)} has no composition family`);
  const text = compositionSelectionText(content);
  const normalizedLayouts = Array.isArray(layoutIds)
    ? [...new Set(layoutIds.map(value => String(value || "").trim()).filter(Boolean))]
    : [];
  const scores = {};
  const matches = {};

  policy.allowed_families.forEach(family => {
    const rule = COMPOSITION_SELECTION_RULES[family] || { content: [], layouts: [] };
    let score = family === policy.default_family ? 1 : 0;
    const familyMatches = [];
    (rule.content || []).forEach(([pattern, weight]) => {
      const match = text.match(pattern);
      if (!match) return;
      score += weight;
      familyMatches.push({ kind: "content", signal: match[0], weight });
    });
    normalizedLayouts.forEach(layoutId => {
      (rule.layouts || []).forEach(([pattern, weight]) => {
        if (!pattern.test(layoutId)) return;
        score += weight;
        familyMatches.push({ kind: "layout", signal: layoutId, weight });
      });
    });
    scores[family] = score;
    matches[family] = familyMatches;
  });

  const ranked = policy.allowed_families
    .map((family, index) => ({ family, score: scores[family], index }))
    .sort((left, right) => right.score - left.score || left.index - right.index);
  const selected = ranked[0];
  const selectedMatches = matches[selected.family] || [];
  return {
    family: selected.family,
    source: selectedMatches.length ? "content_inference" : "theme_default",
    score: selected.score,
    matched_signals: selectedMatches,
    scores,
  };
}

function directionForFamily(family) {
  return FAMILY_DIRECTION[String(family || "").trim()] || null;
}

function compositionFamilyRecord(family) {
  const familyId = String(family || "").trim();
  const meta = COMPOSITION_FAMILY_META[familyId];
  const variants = COMPOSITION_FAMILIES[familyId];
  if (!meta || !variants) throw new Error(`Unknown composition family: ${familyId}`);
  return {
    family: familyId,
    name: meta.name,
    direction: meta.direction,
    preview_theme_id: meta.preview_theme_id,
    summary: meta.summary,
    selection_signals: [...meta.selection_signals],
    variants: [...variants],
  };
}

function compositionDirectionCatalog(allowedFamilies = null) {
  const allowed = allowedFamilies === null
    ? new Set(Object.keys(COMPOSITION_FAMILIES))
    : new Set(allowedFamilies);
  return COMPOSITION_DIRECTIONS.map(direction => {
    const familyIds = direction.families.filter(family => allowed.has(family));
    if (!familyIds.length) return null;
    return {
      id: direction.id,
      label: direction.label,
      summary: direction.summary,
      family_ids: [...familyIds],
      families: familyIds.map(compositionFamilyRecord),
    };
  }).filter(Boolean);
}

function variantFor(seed, family) {
  const variants = COMPOSITION_FAMILIES[family];
  if (!variants) throw new Error(`Unknown composition family: ${family}`);
  const digest = createHash("sha256")
    .update(`controlled-deck-composition-v1:${family}:${seed}`)
    .digest();
  return variants[digest.readUInt32BE(0) % variants.length];
}

function normalizedSeed(value) {
  const seed = typeof value === "string" ? value.trim() : "";
  return DESIGN_SEED_RE.test(seed) ? seed : null;
}

function createDeckDesign(themeOrId, requestedSeed = null, requestedFamily = null) {
  const policy = themeCompositionPolicy(themeOrId);
  const themeId = themeIdOf(themeOrId);
  if (!policy) throw new Error(`Theme ${JSON.stringify(themeId)} has no composition family`);
  const requested = requestedFamily === null || requestedFamily === undefined
    ? null
    : String(requestedFamily).trim();
  if (requested && !COMPOSITION_FAMILIES[requested]) {
    throw new Error(`Unknown composition family: ${requested}`);
  }
  if (requested && !policy.allowed_families.includes(requested)) {
    throw new Error(
      `Composition family ${requested} is not allowed for theme ${themeId}; ` +
      `allowed families: ${policy.allowed_families.join(", ")}`
    );
  }
  const family = requested || policy.default_family;
  const supplied = requestedSeed === null || requestedSeed === undefined
    ? null
    : normalizedSeed(requestedSeed);
  if (requestedSeed !== null && requestedSeed !== undefined && !supplied) {
    throw new Error("design seed must use 8-64 letters, numbers, '_' or '-'");
  }
  const seed = supplied || randomBytes(8).toString("hex");
  return {
    version: 1,
    seed,
    family,
    variant: variantFor(seed, family),
  };
}

function legacyDeckSeed(deck, themeOrId) {
  const themeId = themeIdOf(themeOrId);
  const title = deck && typeof deck.title === "string" ? deck.title.trim() : "";
  return createHash("sha256")
    .update(`controlled-deck-legacy-v1:${themeId}:${title}`)
    .digest("hex")
    .slice(0, 16);
}

function resolveDeckDesign(deck, themeOrId) {
  const policy = themeCompositionPolicy(themeOrId);
  const themeId = themeIdOf(themeOrId);
  const persisted = isPlainObject(deck && deck.design)
    ? normalizedSeed(deck.design.seed)
    : null;
  const persistedFamily = isPlainObject(deck && deck.design)
    && typeof deck.design.family === "string"
    && policy
    && policy.allowed_families.includes(deck.design.family)
    ? deck.design.family
    : null;
  return createDeckDesign(
    themeOrId,
    persisted || legacyDeckSeed(deck, themeId),
    persistedFamily
  );
}

function validateAndNormalizeDeckDesign(value, themeOrId, issues, warnings = []) {
  if (value === undefined) return null;
  if (!isPlainObject(value)) {
    issues.push("design: expected object");
    return null;
  }
  const allowed = ["version", "seed", "family", "variant"];
  const unknown = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknown.length) issues.push(`design: unknown field(s): ${unknown.join(", ")}`);
  if (value.version !== 1) issues.push("design.version: expected 1");
  const seed = normalizedSeed(value.seed);
  if (!seed) {
    issues.push("design.seed: use 8-64 letters, numbers, '_' or '-'");
    return null;
  }
  const policy = themeCompositionPolicy(themeOrId);
  const themeId = themeIdOf(themeOrId);
  if (!policy) return null;
  let family = policy.default_family;
  if (typeof value.family !== "string" || !COMPOSITION_FAMILIES[value.family]) {
    issues.push(
      `design.family: expected one of ${Object.keys(COMPOSITION_FAMILIES).join(", ")}`
    );
  } else if (!policy.allowed_families.includes(value.family)) {
    warnings.push(
      `design.family normalized from ${value.family} to ${family} for theme ${themeId}; ` +
      `allowed families: ${policy.allowed_families.join(", ")}`
    );
  } else {
    family = value.family;
  }
  const variant = variantFor(seed, family);
  if (typeof value.variant !== "string" || !ALL_VARIANTS.has(value.variant)) {
    issues.push("design.variant: expected a registered composition variant");
  } else if (value.variant !== variant || value.family !== family) {
    warnings.push(
      `design.variant normalized from ${value.variant} to ${variant} for seed ${seed}`
    );
  }
  return { version: 1, seed, family, variant };
}

function compositionManifestRecord(themeOrId) {
  const policy = themeCompositionPolicy(themeOrId);
  if (!policy) return null;
  const family = policy.default_family;
  const defaultDirection = directionForFamily(family);
  const directionCatalog = compositionDirectionCatalog(policy.allowed_families);
  const directions = directionCatalog.map(direction => ({
    id: direction.id,
    label: direction.label,
    families: [...direction.family_ids],
  }));
  const families = policy.allowed_families.map(allowedFamily => {
    const {
      direction: _direction,
      name: _name,
      preview_theme_id: _previewThemeId,
      summary: _summary,
      ...record
    } = compositionFamilyRecord(allowedFamily);
    return record;
  });
  return {
    family,
    direction: defaultDirection,
    variants: [...COMPOSITION_FAMILIES[family]],
    default_family: family,
    default_direction: defaultDirection,
    allowed_families: [...policy.allowed_families],
    allowed_directions: directionCatalog.map(direction => direction.id),
    directions,
    families,
  };
}

module.exports = {
  COMPOSITION_DIRECTIONS,
  COMPOSITION_FAMILY_META,
  COMPOSITION_FAMILIES,
  DESIGN_SEED_RE,
  FAMILY_COMPATIBILITY,
  THEME_COMPOSITION_FAMILY,
  allowedFamiliesForTheme,
  compositionDirectionCatalog,
  compositionFamilyRecord,
  compositionManifestRecord,
  createDeckDesign,
  directionForFamily,
  familyForTheme,
  inferCompositionFamily,
  resolveDeckDesign,
  themeCompositionPolicy,
  validateAndNormalizeDeckDesign,
  variantFor,
};
