#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");

const {
  createEditorProps,
  createTechnicalDiagramPreset,
  getLayout,
  getVisualCollectionContract,
  manifestRecord,
} = require("../layouts/registry.js");
const {
  DEFAULT_THEME_ID,
  TRUTH_TEXT_MAX_CHARACTERS,
  createDeckDesign,
  getTheme,
  listThemes,
  normalizeLayoutId,
  resolveArtifactPath,
  runtimeSourceBinding,
  themeManifestRecord,
  validateAssumptionsAgainstRuntime,
  validateResearchFactsAgainstRuntime,
  validateSourceFactsAgainstRuntime,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const {
  compositionDirectionCatalog,
  directionForFamily,
  inferCompositionFamily,
} = require("./composition_core.js");
const {
  analyzeOutlineLayoutIntent,
  expectedVisualItemCount,
  expectedVisualItemContract,
  outlineHasPlottableChartEvidence,
  outlineHasQuantitativeEvidence,
  outlineIntentRecord,
} = require("./outline_layout_contract.js");
const {
  evaluateModelThemeChoice,
  inferTheme,
} = require("./theme_selection_core.js");
const { inferDesignContract } = require("./design_contract_core.js");

const AUTO_COVER_IMAGE_BRIEF_RE = /(?:融资|路演|投资人|\bvc\b|fundrais|investor|pitch\s*deck|发布会|产品发布|品牌提案|高端|premium)/i;
const AUTO_COVER_VISUAL_STORY_RE = /(?:传奇|故事|人物|传记|纪实|biograph|profile|legend|story|documentary)/i;
const AUTO_COVER_PRODUCT_VISUAL_RE = /(?:UI\s*截图|产品界面|客户端界面|主界面|工作台|编辑器界面|浏览器窗口|设备样机|产品主视觉|产品演示|功能演示|产品流程|UI\s*screenshot|product\s+interface|client\s+interface|browser\s+window|device\s+mockup|product\s+demo|feature\s+demo|product\s+flow)/i;
const AUTO_COVER_TECH_VISUAL_RE = /(?:代码窗口|代码片段|协作节点|节点连接|系统架构|技术架构|架构图|运行时|编译链|code\s+window|code\s+snippet|collaboration\s+nodes?|system\s+architecture|technical\s+architecture|runtime|compiler)/i;
const AUTO_GENERATIVE_VISUAL_MEDIUM_RE = /(?:主视觉|缩略图|实景|照片|插画|卡通(?:形象|插画|插图)?|儿童插画|儿童插图|概念图|效果图|界面|截图|样机|地图|地理分布|空间分布|场景|实物|特写|肖像|包装视觉|hero\s+image|thumbnail|photo|illustration|cartoon(?:\s+illustration)?|concept\s+art|interface|screenshot|mockup|map|geographic\s+distribution|scene|product\s+shot|object\s+study|close[- ]?up|portrait|packaging\s+visual)/i;
const AUTO_PRIMARY_BITMAP_VISUAL_RE = /(?:主视觉|缩略图|实景|照片|插画|卡通(?:形象|插画|插图)?|儿童插画|儿童插图|概念图|效果图|样机|地图|场景|实物|特写|肖像|包装视觉|hero\s+image|thumbnail|photo|illustration|cartoon(?:\s+illustration)?|concept\s+art|mockup|map|scene|product\s+shot|object\s+study|close[- ]?up|portrait|packaging\s+visual)/i;
const AUTO_DATA_VISUAL_RE = /(?:图表|表格|数据看板|KPI|指标|chart|table|dashboard|metrics?)/i;
const AUTO_COVER_IMAGE_OPTOUT_RE = /(?:不要|无需|不需要|不得|禁止|不)(?:生成|使用|添加)?(?:图片|生图|视觉图)|(?:纯文字|仅文字)|\b(?:no\s+(?:generated\s+)?images?|without\s+images?|text[- ]only)\b/i;
const AUTO_SLIDE_LOCAL_IMAGE_OPTOUT_RE = /(?:第?\s*\d{1,2}\s*页|(?:页面|slide)\s*[:：#-]?\s*\d{1,2}|封面|首页|cover)[^。；;!?！？\n]{0,48}(?:纯文字|仅文字|无图片|不要图片|不使用图片|text[- ]only|without\s+images?)|(?:纯文字|仅文字|无图片|不要图片|不使用图片|text[- ]only|without\s+images?)[^。；;!?！？\n]{0,48}(?:封面|首页|cover)/i;
const STRUCTURED_NEXT_STEPS_MATRIX_RE = /(?:表格|矩阵|table|matrix)|(?:(?:执行)?角色|负责人|责任人|owners?|assignees?|responsibilit(?:y|ies))[^\n。；;]{0,48}(?:姓名|成员|人员|names?|members?)/i;
const THEME_ID_ALIASES = Object.freeze({
  carnival: "bold-poster",
  comic: "comic-panel",
  manga: "comic-panel",
  storyboard: "comic-panel",
  pixel: "8-bit-orbit",
  arcade: "8-bit-orbit",
  "pixel-art": "8-bit-orbit",
  "8bit": "8-bit-orbit",
});
const REQUIRED_FIELD_ALIASES = Object.freeze({
  "cards-grid-v1": Object.freeze({ cards: "items" }),
  "kpi-grid-v1": Object.freeze({ cards: "items", metrics: "items" }),
  "chart-bar-v1": Object.freeze({ chart: "items", data: "items" }),
  "chart-data-v1": Object.freeze({ chart: "series", data: "series" }),
  "table-data-v1": Object.freeze({ items: "rows", matrix: "rows", table: "rows" }),
  "swimlane-process-v1": Object.freeze({ items: "lanes", rows: "lanes", steps: "columns", phases: "columns" }),
  "customer-journey-map-v1": Object.freeze({ items: "stages", steps: "stages", phases: "stages" }),
  "maturity-model-v1": Object.freeze({ items: "levels", steps: "levels", layers: "levels" }),
  "cause-tree-v1": Object.freeze({ items: "causes", branches: "causes", categories: "causes" }),
  "factory-process-line-v1": Object.freeze({ items: "stations", steps: "stations" }),
  "legal-case-logic-v1": Object.freeze({ items: "sections", steps: "sections" }),
  "property-factsheet-v1": Object.freeze({ items: "zones", sections: "zones" }),
  "commerce-funnel-v1": Object.freeze({ items: "stages", steps: "stages" }),
  "supply-network-v1": Object.freeze({ items: "nodes", steps: "nodes" }),
});
const RELAXABLE_DECORATIVE_FIELDS = new Set(["tags"]);
const EXPLICIT_TAG_CONTENT_RE = /(?:标签|关键词|主线卡|\btags?\b|\bchips?\b)/i;

function normalizeSourceText(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, "")
    .trim();
}

function sourceNumberTokens(value) {
  return (String(value || "").match(/\d+(?:,\d{3})*(?:\.\d+)?%?/g) || [])
    .map(token => {
      const percent = token.endsWith("%");
      const bare = token.replace(/,/g, "").replace(/%$/, "");
      const numeric = Number(bare);
      return `${Number.isFinite(numeric) ? numeric : bare}${percent ? "%" : ""}`;
    });
}

function sourceBigramOverlap(value, sourceText) {
  const normalize = text => String(text || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "")
    .trim();
  const candidate = normalize(value);
  const source = normalize(sourceText);
  if (candidate.length < 4 || source.length < 4) return 0;
  const bigrams = new Set();
  for (let index = 0; index < candidate.length - 1; index += 1) {
    bigrams.add(candidate.slice(index, index + 2));
  }
  const matched = [...bigrams].filter(bigram => source.includes(bigram)).length;
  return bigrams.size ? matched / bigrams.size : 0;
}

function themeDiscoveryRecord(theme) {
  const manifest = themeManifestRecord(theme);
  return {
    id: theme.id,
    name: theme.name,
    description: theme.description,
    selection: theme.selection,
    composition: {
      family: manifest.composition.default_family,
      alternatives: Math.max(0, manifest.composition.allowed_families.length - 1),
    },
  };
}

function resolveThemeInput(themeId) {
  const exact = getTheme(themeId);
  if (exact) return { theme: exact, normalization: null };
  const normalizedInput = String(themeId || "")
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-");
  const normalizedTheme = getTheme(normalizedInput);
  if (normalizedTheme) {
    return {
      theme: normalizedTheme,
      normalization: { from: themeId, to: normalizedTheme.id },
    };
  }
  const aliasTarget = THEME_ID_ALIASES[normalizedInput];
  const aliasTheme = aliasTarget ? getTheme(aliasTarget) : null;
  return aliasTheme
    ? { theme: aliasTheme, normalization: { from: themeId, to: aliasTheme.id } }
    : { theme: null, normalization: null };
}

function selectTheme(opts, context) {
  const inference = inferTheme(listThemes(), context, DEFAULT_THEME_ID);
  if (String(opts.themeId || "").trim().toLowerCase() === "auto") {
    if (opts.themeModelChoice) {
      const evaluation = evaluateModelThemeChoice(
        inference,
        opts.themeModelChoice
      );
      const deterministicRecommendation = {
        theme_id: inference.theme_id,
        source: inference.source,
        confidence: inference.confidence,
        score: inference.score,
        margin: inference.margin,
      };
      if (evaluation.accepted) {
        return {
          theme: getTheme(evaluation.candidate.theme_id),
          normalization: null,
          selection: {
            theme_id: evaluation.candidate.theme_id,
            source: "model_reranked",
            confidence: null,
            score: evaluation.candidate.score,
            margin: null,
            matched_signals: evaluation.candidate.matched_signals,
            ranking: inference.ranking,
            shortlist: inference.shortlist,
            requested_theme_id: "auto",
            deterministic_recommendation: deterministicRecommendation,
            model_choice: {
              theme_id: evaluation.candidate.theme_id,
              reason: opts.themeModelReason,
              accepted: true,
            },
          },
        };
      }
      return {
        theme: getTheme(inference.theme_id),
        normalization: null,
        selection: {
          ...inference,
          source: "model_choice_rejected",
          requested_theme_id: "auto",
          deterministic_source: inference.source,
          deterministic_recommendation: deterministicRecommendation,
          model_choice: {
            theme_id: opts.themeModelChoice,
            reason: opts.themeModelReason,
            accepted: false,
            rejection_reason: evaluation.reason,
          },
        },
      };
    }
    return {
      theme: getTheme(inference.theme_id),
      normalization: null,
      selection: {
        ...inference,
        requested_theme_id: "auto",
      },
    };
  }

  const explicit = resolveThemeInput(opts.themeId);
  if (!explicit.theme) {
    return {
      ...explicit,
      selection: {
        source: "invalid_explicit",
        requested_theme_id: opts.themeId,
        auto_recommendation: inference,
      },
    };
  }

  if (
    !opts.themeLocked
    && explicit.theme.id === DEFAULT_THEME_ID
    && inference.confidence === "high"
    && inference.theme_id !== DEFAULT_THEME_ID
  ) {
    return {
      theme: getTheme(inference.theme_id),
      normalization: explicit.normalization,
      selection: {
        ...inference,
        source: "auto_corrected_default",
        requested_theme_id: explicit.theme.id,
      },
    };
  }

  return {
    ...explicit,
    selection: {
      theme_id: explicit.theme.id,
      requested_theme_id: opts.themeId,
      source: opts.themeLocked ? "explicit_locked" : "explicit",
      confidence: null,
      score: null,
      margin: null,
      matched_signals: [],
      ranking: inference.ranking,
      auto_recommendation: {
        theme_id: inference.theme_id,
        confidence: inference.confidence,
        score: inference.score,
        margin: inference.margin,
      },
    },
  };
}

function themeShortlistPayload(context) {
  const inference = inferTheme(listThemes(), context, DEFAULT_THEME_ID);
  return {
    mode: "theme_shortlist",
    default_theme_id: DEFAULT_THEME_ID,
    deterministic_recommendation: {
      theme_id: inference.theme_id,
      source: inference.source,
      confidence: inference.confidence,
      score: inference.score,
      margin: inference.margin,
      matched_signals: inference.matched_signals,
    },
    candidate_count: inference.shortlist.length,
    candidates: inference.shortlist.map(candidate => {
      const theme = getTheme(candidate.theme_id);
      const discovery = themeDiscoveryRecord(theme);
      return {
        ...discovery,
        theme_id: candidate.theme_id,
        deterministic_rank: candidate.rank,
        deterministic_score: candidate.score,
        matched_signals: candidate.matched_signals,
        hard_conflicts: candidate.hard_conflicts,
        protected_signals: candidate.protected_signals,
        eligible_for_model_choice: candidate.eligible_for_model_choice,
      };
    }),
    model_choice_contract: {
      choose_from_candidates_only: true,
      hard_conflicts_are_ineligible: true,
      protected_deterministic_signals_limit_override: true,
      submit_with: "--theme auto --theme-model-choice THEME_ID --theme-model-reason REASON",
    },
  };
}

function compactThemeSelection(selection) {
  return {
    requested_theme_id: selection.requested_theme_id,
    selected_theme_id: selection.theme_id,
    source: selection.source,
    confidence: selection.confidence,
    ...(Array.isArray(selection.matched_signals) && selection.matched_signals.length
      ? {
        matched_signals: selection.matched_signals.map(item => item.signal),
      }
      : {}),
    ...(selection.model_choice
      ? {
        model_choice: selection.model_choice,
        deterministic_recommendation: selection.deterministic_recommendation,
      }
      : {}),
  };
}

function normalizeRequiredField(layout, field) {
  if (Object.prototype.hasOwnProperty.call(layout.fields, field)) return field;
  const aliases = REQUIRED_FIELD_ALIASES[layout.id] || {};
  return aliases[field] || field;
}

function outlineExplicitlyRequiresField(outlineSlide, field) {
  if (!outlineSlide || !RELAXABLE_DECORATIVE_FIELDS.has(field)) return true;
  const content = [
    outlineSlide.title,
    outlineSlide.message,
    outlineSlide.visual,
    ...(Array.isArray(outlineSlide.bullets) ? outlineSlide.bullets : []),
  ]
    .map(value => String(value || "").trim())
    .filter(Boolean)
    .join("\n");
  if (field === "tags") return EXPLICIT_TAG_CONTENT_RE.test(content);
  return true;
}

function canRelaxMissingRequiredField(requirement, outlineSlide) {
  return (
    RELAXABLE_DECORATIVE_FIELDS.has(requirement.field)
    && outlineSlide
    && !outlineExplicitlyRequiresField(outlineSlide, requirement.field)
  );
}

function canonicalizeSourceFacts(sourceFacts) {
  const binding = runtimeSourceBinding();
  const source = normalizeSourceText(binding.source_text);
  const changes = [];
  const facts = sourceFacts.map(value => String(value || "").trim()).filter(Boolean)
    .map(fact => {
      if (!binding.available || source.includes(normalizeSourceText(fact))) return fact;
      const labeled = /^[^:：]{1,32}[:：]\s*(.+)$/.exec(fact);
      const candidate = labeled ? labeled[1].trim() : "";
      if (
        candidate.length >= 2
        && source.includes(normalizeSourceText(candidate))
      ) {
        changes.push({ from: fact, to: candidate });
        return candidate;
      }
      const sourceNumbers = new Set(sourceNumberTokens(binding.source_text));
      const factNumbers = sourceNumberTokens(fact);
      if (
        binding.strict
        && factNumbers.every(token => sourceNumbers.has(token))
        && sourceBigramOverlap(fact, binding.source_text) >= 0.75
      ) {
        changes.push({
          from: fact,
          to: binding.source_text,
          reason: "restored exact runtime source text after a non-numeric copy drift",
        });
        return binding.source_text;
      }
      return fact;
    });
  return { facts: [...new Set(facts)], changes };
}

function splitDefaultRuntimeSourceFacts(value) {
  const text = String(value || "").trim();
  if (!text) return [];
  const chunks = [];
  let remaining = text;
  const preferredBoundaryStart = Math.floor(TRUTH_TEXT_MAX_CHARACTERS * 0.6);
  while (Array.from(remaining).length > TRUTH_TEXT_MAX_CHARACTERS) {
    const characters = Array.from(remaining);
    let splitAt = TRUTH_TEXT_MAX_CHARACTERS;
    for (let index = splitAt; index >= preferredBoundaryStart; index -= 1) {
      if (/[\n。！？!?；;]/.test(characters[index - 1])) {
        splitAt = index;
        break;
      }
    }
    const chunk = characters.slice(0, splitAt).join("").trim();
    if (chunk) chunks.push(chunk);
    remaining = characters.slice(splitAt).join("").trimStart();
  }
  if (remaining.trim()) chunks.push(remaining.trim());
  return chunks;
}

function parseArgs(argv) {
  const opts = {
    layoutIds: [],
    themeId: "auto",
    themeLocked: false,
    themeModelChoice: null,
    themeModelReason: null,
    designSeed: null,
    family: null,
    title: "Untitled deck",
    truthMode: "source_bound",
    imageMode: "auto",
    noImages: false,
    sourceFacts: [],
    researchFacts: [],
    assumptions: [],
    requiredFields: [],
    imageAssets: [],
    outline: null,
    out: null,
    report: null,
    force: false,
    listThemes: false,
    rankThemes: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--theme" && value) {
      opts.themeId = value;
      index += 1;
    } else if (arg === "--lock-theme") {
      opts.themeLocked = true;
    } else if (arg === "--theme-model-choice" && value) {
      opts.themeModelChoice = value;
      index += 1;
    } else if (arg === "--theme-model-reason" && value) {
      opts.themeModelReason = value;
      index += 1;
    } else if (arg === "--design-seed" && value) {
      opts.designSeed = value;
      index += 1;
    } else if (arg === "--family" && value) {
      opts.family = value;
      index += 1;
    } else if (arg === "--title" && value) {
      opts.title = value;
      index += 1;
    } else if (arg === "--truth-mode" && value) {
      opts.truthMode = value;
      index += 1;
    } else if (arg === "--image-mode" && value) {
      opts.imageMode = value;
      index += 1;
    } else if (arg === "--no-images") {
      opts.noImages = true;
    } else if (arg === "--fact" && value) {
      opts.sourceFacts.push(value);
      index += 1;
    } else if (arg === "--research-fact" && value) {
      opts.researchFacts.push(value);
      index += 1;
    } else if (arg === "--assumption" && value) {
      opts.assumptions.push(value);
      index += 1;
    } else if (arg === "--require-field" && value) {
      const match = /^(\d+):([a-zA-Z0-9_]+)$/.exec(value);
      if (!match) {
        throw new Error("--require-field must use SLIDE_NUMBER:FIELD, for example 4:metrics");
      }
      opts.requiredFields.push({ slide: Number(match[1]), field: match[2] });
      index += 1;
    } else if (arg === "--image-asset" && value) {
      const match = /^(\d+):([a-zA-Z0-9_-]+)=(.+)$/.exec(value);
      if (!match) {
        throw new Error(
          "--image-asset must use SLIDE:SLOT=PATH, for example 1:hero=/path/to/cover.png"
        );
      }
      opts.imageAssets.push({
        slide: Number(match[1]),
        slot: match[2],
        sourcePath: match[3],
      });
      index += 1;
    } else if (arg === "--outline" && value) {
      opts.outline = value;
      index += 1;
    } else if (arg === "--out" && value) {
      opts.out = value;
      index += 1;
    } else if (arg === "--report" && value) {
      opts.report = value;
      index += 1;
    } else if (arg === "--force") {
      opts.force = true;
    } else if (arg === "--list-themes") {
      opts.listThemes = true;
    } else if (arg === "--rank-themes") {
      opts.rankThemes = true;
    } else if (arg === "--help" || arg === "-h") {
      console.log(
        "Usage: inspect_deck_contract.js [LAYOUT_ID ...] " +
        "[--theme auto|THEME_ID] [--lock-theme] [--theme-model-choice THEME_ID] " +
        "[--theme-model-reason REASON] [--family FAMILY_ID] [--design-seed SEED] [--title TITLE] [--truth-mode MODE] " +
        "[--image-mode auto|creative_image_mode] [--no-images] " +
        "[--image-asset SLIDE:SLOT=PATH ...] " +
        "[--fact TEXT ...] [--research-fact TEXT ...] [--assumption TEXT ...] " +
        "[--require-field SLIDE:FIELD ...] [--outline outline.json] [--out deck.json] " +
        "[--report qa/deck_contract.json] [--force] [--list-themes | --rank-themes]"
      );
      process.exit(0);
    } else if (arg.startsWith("-")) {
      throw new Error(`Unknown argument: ${arg}`);
    } else {
      opts.layoutIds.push(normalizeLayoutId(arg));
    }
  }
  return opts;
}

function buildSlide(layoutId, index, outlineSlide = null) {
  const props = createEditorProps(layoutId);
  const semantic = outlineSlide ? analyzeOutlineLayoutIntent(outlineSlide) : null;
  const outlineVisual = [
    outlineSlide && outlineSlide.layout,
    outlineSlide && outlineSlide.visual,
  ].filter(Boolean).join("\n");
  if (
    layoutId === "cards-grid-v1"
    && expectedVisualItemCount(outlineSlide)
    && /(?:流程|路径|阶段|节点|时间轴|路线图|里程碑|process|journey|timeline|roadmap)/i.test(outlineVisual)
  ) {
    props.variant = "numbered";
  }
  if (layoutId === "table-data-v1" && semantic && semantic.kind === "gantt") {
    props.variant = "gantt";
  }
  if (layoutId === "technical-diagram-v1" && semantic) {
    const diagramKind = {
      "technical-architecture": "architecture",
      "system-integration": "integration",
      "data-pipeline": "pipeline",
    }[semantic.kind];
    if (diagramKind) Object.assign(props, createTechnicalDiagramPreset(diagramKind));
  }
  alignScaffoldVisualCardinality(layoutId, props, outlineSlide);
  return {
    id: `slide-${String(index + 1).padStart(2, "0")}`,
    layout_id: layoutId,
    props,
    ...(outlineSlide
      ? {
        source_outline_page: outlineSlide.page,
        outline_intent: outlineIntentRecord(outlineSlide),
      }
      : {}),
  };
}

function nestedValue(value, fieldPath) {
  return String(fieldPath || "")
    .split(".")
    .filter(Boolean)
    .reduce((current, part) => (
      current && typeof current === "object" ? current[part] : undefined
    ), value);
}

function visualCollectionCapacity(layoutId, outlineSlide = null) {
  const requested = expectedVisualItemContract(outlineSlide);
  const collection = getVisualCollectionContract(
    layoutId,
    requested && requested.dimension
  );
  const layout = getLayout(layoutId);
  const contract = layout && layout.fields && collection
    ? nestedValue(layout.fields, collection.path)
    : null;
  return contract && Number.isInteger(contract.minItems) && Number.isInteger(contract.maxItems)
    ? { minItems: contract.minItems, maxItems: contract.maxItems }
    : null;
}

function visualCollectionLimit(layoutId, outlineSlide = null) {
  const capacity = visualCollectionCapacity(layoutId, outlineSlide);
  return capacity ? capacity.maxItems : null;
}

function alignScaffoldVisualCardinality(layoutId, props, outlineSlide) {
  const requested = expectedVisualItemContract(outlineSlide);
  const expected = requested && requested.count;
  const collectionContract = getVisualCollectionContract(
    layoutId,
    requested && requested.dimension
  );
  const field = collectionContract && collectionContract.path;
  const layout = getLayout(layoutId);
  const contract = layout && layout.fields && field
    ? nestedValue(layout.fields, field)
    : null;
  const collection = field ? nestedValue(props, field) : null;
  if (
    !expected
    || !field
    || !contract
    || !collection
    || (Number.isInteger(contract.minItems) && expected < contract.minItems)
    || (Number.isInteger(contract.maxItems) && expected > contract.maxItems)
  ) return;
  if (collection.length > expected) collection.splice(expected);
  const collectionName = field.split(".").filter(Boolean).at(-1);
  const editorCollection = layout
    && layout.editor
    && layout.editor.controls
    && layout.editor.controls.collections
    && layout.editor.controls.collections[collectionName];
  const editorItemDefault = editorCollection && editorCollection.itemDefault;
  const seed = collection.length
    ? collection[collection.length - 1]
    : editorItemDefault || null;
  while (collection.length < expected) {
    const item = seed === null ? "待填充" : JSON.parse(JSON.stringify(seed));
    const ordinal = collection.length + 1;
    if (item && typeof item === "object" && !Array.isArray(item)) {
      if (Object.prototype.hasOwnProperty.call(item, "kicker")) {
        item.kicker = String(ordinal).padStart(2, "0");
      }
      if (Object.prototype.hasOwnProperty.call(item, "phase")) {
        item.phase = `阶段 ${ordinal}`;
      }
      if (Object.prototype.hasOwnProperty.call(item, "title")) {
        item.title = `第 ${ordinal} 个视觉项`;
      }
      if (Object.prototype.hasOwnProperty.call(item, "id")) {
        item.id = `${(requested && requested.dimension) || "item"}-${ordinal}`;
      }
      if (Object.prototype.hasOwnProperty.call(item, "label")) {
        item.label = `视觉项 ${ordinal}`;
      }
      if (Object.prototype.hasOwnProperty.call(item, "name")) {
        item.name = `序列 ${ordinal}`;
      }
    } else if (Array.isArray(item) && item.length) {
      item[0] = `视觉项 ${ordinal}`;
    } else if (typeof item === "string") {
      collection.push(`视觉项 ${ordinal}`);
      continue;
    }
    collection.push(item);
  }
  if (layoutId === "chart-data-v1" && field === "categories") {
    (props.series || []).forEach(series => {
      if (!Array.isArray(series.values)) series.values = [];
      series.values.splice(expected);
      while (series.values.length < expected) {
        series.values.push(series.values[series.values.length - 1] || "0");
      }
    });
  }
  if (["table-data-v1", "heatmap-matrix-v1"].includes(layoutId) && field === "columns") {
    (props.rows || []).forEach(row => {
      if (!Array.isArray(row)) return;
      row.splice(expected);
      while (row.length < expected) row.push("—");
    });
  }
  if (layoutId === "technical-diagram-v1" && field === "nodes") {
    const nodeIds = new Set((props.nodes || []).map(node => node.id));
    props.edges = (props.edges || []).filter(edge => (
      nodeIds.has(edge.source) && nodeIds.has(edge.target)
    ));
  }
}

function nonEmptyText(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function narrativeText(value) {
  if (Array.isArray(value)) {
    return value
      .map(item => String(item || "").trim())
      .filter(Boolean)
      .join("\n");
  }
  return typeof value === "string" ? value.trim() : "";
}

function readOutlineBinding(outlineInput, expectedSlideCount = null) {
  const outlineFile = resolveArtifactPath(outlineInput);
  if (!isNonEmptyFile(outlineFile)) {
    throw new Error(`Outline file not found or empty: ${outlineFile}`);
  }
  const raw = fs.readFileSync(outlineFile, "utf8");
  let outline;
  try {
    outline = JSON.parse(raw);
  } catch (error) {
    throw new Error(`Invalid JSON in ${outlineFile}: ${error.message}`);
  }
  const issues = [];
  if (!nonEmptyText(outline && outline.deck_goal)) {
    issues.push("outline.deck_goal: required non-empty text");
  }
  ["audience", "storyline"].forEach(field => {
    if (!narrativeText(outline && outline[field])) {
      issues.push(`outline.${field}: required non-empty text or text array`);
    }
  });
  if (!nonEmptyText(outline && outline.source_mode)) {
    issues.push("outline.source_mode: required non-empty text");
  }
  const slides = outline && Array.isArray(outline.slides) ? outline.slides : null;
  if (!slides) {
    issues.push("outline.slides: required array");
  } else {
    if (Number.isInteger(expectedSlideCount) && slides.length !== expectedSlideCount) {
      issues.push(
        `outline.slides: contains ${slides.length} page(s), but the ordered layout plan ` +
        `contains ${expectedSlideCount}`
      );
    }
    slides.forEach((slide, index) => {
      const prefix = `outline.slides.${index}`;
      if (!slide || typeof slide !== "object" || Array.isArray(slide)) {
        issues.push(`${prefix}: required object`);
        return;
      }
      if (slide.page !== index + 1) {
        issues.push(`${prefix}.page: expected ${index + 1}`);
      }
      ["title", "message", "layout", "visual"].forEach(field => {
        if (!nonEmptyText(slide[field])) issues.push(`${prefix}.${field}: required non-empty text`);
      });
      if (!Array.isArray(slide.bullets)) {
        issues.push(`${prefix}.bullets: required array`);
      }
      if (!Array.isArray(slide.evidence)) {
        issues.push(`${prefix}.evidence: required array`);
      }
    });
  }
  if (issues.length) {
    throw new Error(`Outline binding failed:\n${issues.join("\n")}`);
  }
  const sourceMode = outline.source_mode.trim();
  const importedResearchFacts = sourceMode === "public_authoritative_research"
    ? [...new Set(slides.flatMap(slide => slide.evidence)
      .map(value => String(value || "").trim())
      .filter(Boolean))]
    : [];
  return {
    file: outlineFile,
    hash: createHash("sha256").update(raw).digest("hex"),
    sourceMode,
    content: {
      deck_goal: outline.deck_goal,
      audience: narrativeText(outline.audience),
      storyline: narrativeText(outline.storyline),
      tone: outline.tone || "",
      design_requirements: outline.design_requirements || null,
      slides,
    },
    slides,
    importedResearchFacts,
  };
}

function normalizeOutlineDrivenLayoutIds(
  layoutIds,
  outlineBinding,
  layoutPolicy = {}
) {
  const normalizations = [];
  if (!outlineBinding) return { layoutIds: layoutIds.slice(), normalizations };
  const effective = layoutIds.map((layoutId, index) => {
    const slide = outlineBinding.slides[index];
    const outlineText = [
      slide && slide.title,
      slide && slide.message,
      slide && slide.layout,
      slide && slide.visual,
      ...(Array.isArray(slide && slide.bullets) ? slide.bullets : []),
    ].filter(Boolean).join("\n");
    if (
      layoutId === "closing-next-steps-v1"
      && STRUCTURED_NEXT_STEPS_MATRIX_RE.test(outlineText)
    ) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: "table-data-v1",
        reason: "outline requires parallel next-step, role/owner, and identity fields",
      });
      return "table-data-v1";
    }
    const explicitVisualItemCount = expectedVisualItemCount(slide);
    const timelineMaxItems = visualCollectionLimit("timeline-horizontal-v1", slide);
    const cardsMaxItems = visualCollectionLimit("cards-grid-v1", slide);
    if (
      layoutId === "timeline-horizontal-v1"
      && Number.isInteger(timelineMaxItems)
      && Number.isInteger(cardsMaxItems)
      && explicitVisualItemCount > timelineMaxItems
      && explicitVisualItemCount <= cardsMaxItems
    ) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: "cards-grid-v1",
        reason: (
          `outline requests ${explicitVisualItemCount} ordered visual items, ` +
          `which exceeds the timeline layout capacity of ${timelineMaxItems}`
        ),
      });
      return "cards-grid-v1";
    }
    const selectedMaxItems = visualCollectionLimit(layoutId, slide);
    if (
      layoutId === "statement-focus-v1"
      && Number.isInteger(selectedMaxItems)
      && explicitVisualItemCount > selectedMaxItems
      && explicitVisualItemCount <= cardsMaxItems
    ) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: "cards-grid-v1",
        reason: (
          `outline requests ${explicitVisualItemCount} parallel summary items, ` +
          `which exceeds the statement layout capacity of ${selectedMaxItems}`
        ),
      });
      return "cards-grid-v1";
    }
    if (
      layoutId === "closing-next-steps-v1"
      && explicitVisualItemCount > 4
      && explicitVisualItemCount <= 6
    ) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: "cards-grid-v1",
        reason: (
          `outline requests ${explicitVisualItemCount} closing value items, ` +
          "which exceeds the four-action closing layout"
        ),
      });
      return "cards-grid-v1";
    }
    const semantic = analyzeOutlineLayoutIntent(
      slide,
      outlineBinding.sourceMode,
      layoutPolicy
    );
    if (semantic && !semantic.allowed_layout_ids.includes(layoutId)) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: semantic.preferred_layout_id,
        reason: semantic.reason,
      });
      return semantic.preferred_layout_id;
    }
    if (
      ["chart-bar-v1", "chart-data-v1", "kpi-grid-v1"].includes(layoutId)
      && !(layoutId === "chart-data-v1"
        ? outlineHasPlottableChartEvidence(slide, outlineBinding.sourceMode)
        : outlineHasQuantitativeEvidence(slide, outlineBinding.sourceMode))
      && !layoutPolicy.allowIllustrativeQuantitative
    ) {
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: "cards-grid-v1",
        reason: "qualitative outline pages use a safe editable cards layout instead of invented chart or KPI values",
      });
      return "cards-grid-v1";
    }
    return layoutId;
  });
  const capacityChecked = effective.map((layoutId, index) => {
    const slide = outlineBinding.slides[index];
    const requested = expectedVisualItemContract(slide);
    const expected = requested && requested.count;
    const selectedCapacity = visualCollectionCapacity(layoutId, slide);
    if (
      !Number.isInteger(expected)
      || !selectedCapacity
      || (
        expected >= selectedCapacity.minItems
        && expected <= selectedCapacity.maxItems
      )
    ) {
      return layoutId;
    }
    const semantic = analyzeOutlineLayoutIntent(
      slide,
      outlineBinding.sourceMode,
      layoutPolicy
    );
    const compatible = semantic
      ? semantic.allowed_layout_ids.find(candidateId => {
        const capacity = visualCollectionCapacity(candidateId, slide);
        return capacity
          && expected >= capacity.minItems
          && expected <= capacity.maxItems;
      })
      : null;
    if (!compatible) {
      const partCount = Math.ceil(expected / selectedCapacity.maxItems);
      normalizations.push({
        slide: index + 1,
        from: layoutId,
        to: layoutId,
        strategy: "split",
        item_count: expected,
        part_count: partCount,
        reason: (
          `outline requests ${expected} visual items outside ${layoutId} capacity ` +
          `${selectedCapacity.minItems}-${selectedCapacity.maxItems}; split before authoring`
        ),
      });
      return layoutId;
    }
    normalizations.push({
      slide: index + 1,
      from: layoutId,
      to: compatible,
      reason: (
        `outline requests ${expected} visual items outside ${layoutId} capacity ` +
        `${selectedCapacity.minItems}-${selectedCapacity.maxItems}`
      ),
    });
    return compatible;
  });
  return { layoutIds: capacityChecked, normalizations };
}

function balancedChunkSizes(total, maximum, minimum) {
  const partCount = Math.ceil(total / maximum);
  const base = Math.floor(total / partCount);
  const remainder = total % partCount;
  const sizes = Array.from(
    { length: partCount },
    (_, index) => base + (index < remainder ? 1 : 0)
  );
  return sizes.every(size => size >= minimum && size <= maximum) ? sizes : null;
}

function expandOutlineDrivenPlan(layoutIds, outlineBinding) {
  if (!outlineBinding) {
    return layoutIds.map((layoutId, index) => ({ layoutId, outlineSlide: null, index }));
  }
  const plan = [];
  layoutIds.forEach((layoutId, index) => {
    const outlineSlide = outlineBinding.slides[index];
    const requested = expectedVisualItemContract(outlineSlide);
    const capacity = visualCollectionCapacity(layoutId, outlineSlide);
    if (
      !requested
      || !capacity
      || (
        requested.count >= capacity.minItems
        && requested.count <= capacity.maxItems
      )
    ) {
      plan.push({ layoutId, outlineSlide, sourceOutlinePage: outlineSlide.page });
      return;
    }
    const bullets = Array.isArray(outlineSlide.bullets)
      ? outlineSlide.bullets.filter(item => String(item || "").trim())
      : [];
    const sizes = balancedChunkSizes(
      requested.count,
      capacity.maxItems,
      capacity.minItems
    );
    if (!sizes || bullets.length !== requested.count) {
      throw new Error(
        `Slide ${index + 1} requires controlled outline expansion for ${requested.count} ` +
        `${requested.dimension} item(s), but deterministic splitting requires exactly ` +
        `${requested.count} outline bullets and chunks within ${layoutId} capacity ` +
        `${capacity.minItems}-${capacity.maxItems}`
      );
    }
    let offset = 0;
    sizes.forEach((size, partIndex) => {
      const start = offset + 1;
      const end = offset + size;
      const authoringSlide = JSON.parse(JSON.stringify(outlineSlide));
      authoringSlide.bullets = bullets.slice(offset, end);
      authoringSlide.visual_item_contract = {
        dimension: requested.dimension,
        count: size,
      };
      plan.push({
        layoutId,
        outlineSlide: authoringSlide,
        boundOutlineSlide: outlineSlide,
        sourceOutlinePage: outlineSlide.page,
        sourceOutlineItemRange: {
          start,
          end,
          total: requested.count,
          part: partIndex + 1,
          parts: sizes.length,
        },
      });
      offset = end;
    });
  });
  return plan;
}

function validateOutlineLayoutFit(orderedLayouts, outlineBinding, layoutPolicy = {}) {
  if (!outlineBinding) return;
  const quantitativeLayouts = new Set(["chart-bar-v1", "chart-data-v1", "kpi-grid-v1"]);
  const issues = [];
  orderedLayouts.forEach((layout, index) => {
    const semantic = analyzeOutlineLayoutIntent(
      outlineBinding.slides[index],
      outlineBinding.sourceMode,
      layoutPolicy
    );
    if (semantic && !semantic.allowed_layout_ids.includes(layout.id)) {
      issues.push(
        `slide ${index + 1}: ${layout.id} does not express outline visual intent ` +
        `${JSON.stringify(outlineBinding.slides[index].visual)}; use one of ` +
        semantic.allowed_layout_ids.join(", ")
      );
    }
    if (
      quantitativeLayouts.has(layout.id)
      && !(layout.id === "chart-data-v1"
        ? outlineHasPlottableChartEvidence(
          outlineBinding.slides[index],
          outlineBinding.sourceMode
        )
        : outlineHasQuantitativeEvidence(
          outlineBinding.slides[index],
          outlineBinding.sourceMode
        ))
      && !layoutPolicy.allowIllustrativeQuantitative
    ) {
      issues.push(
        `slide ${index + 1}: ${layout.id} requires quantitative evidence, but outline page ` +
        `${index + 1} is qualitative; choose a semantic text/cards layout instead of ` +
        "inventing chart or KPI values"
      );
    }
  });
  if (issues.length) throw new Error(`Outline/layout fit failed:\n${issues.join("\n")}`);
}

function imagePrompt(context, slotRole) {
  const slide = context && context.slide ? context.slide : {};
  const visualContext = String(
    context && (context.slideText || context.briefText) || ""
  );
  const parts = [
    `Deck context: ${String(context && context.deckTitle || "presentation").trim()}.`,
    slide.title ? `Slide title: ${slide.title}.` : "",
    slide.message ? `Page intent: ${slide.message}.` : "",
    slide.visual ? `Visual direction: ${slide.visual}.` : "",
    `Create one ${slotRole || "presentation"} visual with a clear focal subject and room for adjacent slide copy.`,
    AUTO_COVER_PRODUCT_VISUAL_RE.test(visualContext)
      ? "If showing software, make it an explicitly conceptual product-interface illustration rather than claiming to reproduce a real screenshot."
      : "",
    "No embedded text, no logos, no watermark, and no decorative filler.",
  ].filter(Boolean);
  return parts.join(" ");
}

function hasDeckWideImageOptOut(text) {
  return String(text || "")
    .split(/[。；;!?！？\n]+/)
    .map(clause => clause.trim())
    .filter(Boolean)
    .some(clause => (
      AUTO_COVER_IMAGE_OPTOUT_RE.test(clause)
      && !AUTO_SLIDE_LOCAL_IMAGE_OPTOUT_RE.test(clause)
    ));
}

function buildImagePlanEntry(
  layout,
  index,
  imageMode,
  context = {},
  existingAsset = null,
) {
  const slideNumber = index + 1;
  const slideId = `slide-${String(slideNumber).padStart(2, "0")}`;
  const slots = layout.mediaSlots && Array.isArray(layout.mediaSlots.slots)
    ? layout.mediaSlots.slots
    : [];
  const background = layout.mediaSlots && layout.mediaSlots.background
    ? layout.mediaSlots.background
    : null;
  const requestedAssetSlot = existingAsset && existingAsset.slot
    ? existingAsset.slot
    : null;
  const slot = requestedAssetSlot && requestedAssetSlot !== "background"
    ? slots.find(item => item && item.id === requestedAssetSlot) || null
    : requestedAssetSlot === "background"
      ? null
      : slots[0] || null;
  const targetId = requestedAssetSlot || (slot ? slot.id : "background");
  const propPath = targetId === "background" ? "background" : slot.propPath;
  const strategies = targetId !== "background" && slot && Array.isArray(slot.strategies)
    ? slot.strategies
    : background && Array.isArray(background.strategies)
      ? background.strategies
      : ["generate", "skip"];
  const briefText = String(context.briefText || "");
  const slideText = String(context.slideText || briefText);
  const slideVisualText = String(
    context.slide && context.slide.visual
      ? context.slide.visual
      : slideText
  );
  const generationForbidden = context.generationForbidden === true
    || AUTO_COVER_IMAGE_OPTOUT_RE.test(slideText);
  const creativeCover = !generationForbidden
    && imageMode === "creative_image_mode"
    && index === 0;
  const investorCoverBrief = AUTO_COVER_IMAGE_BRIEF_RE.test(briefText);
  // Cover-specific visual intent lives on the bound outline page. Looking only
  // at the deck-level goal misses concrete subjects such as a named athlete
  // when the goal merely says "introduce X" but the cover says "人物海报".
  const visualStoryBrief = AUTO_COVER_VISUAL_STORY_RE.test(slideText);
  const productVisualBrief = AUTO_COVER_PRODUCT_VISUAL_RE.test(slideText);
  const technicalVisualBrief = AUTO_COVER_TECH_VISUAL_RE.test(slideText);
  const explicitGenerativeVisual = AUTO_GENERATIVE_VISUAL_MEDIUM_RE.test(slideVisualText)
    && (
      !AUTO_DATA_VISUAL_RE.test(slideVisualText)
      || AUTO_PRIMARY_BITMAP_VISUAL_RE.test(slideVisualText)
    );
  const explicitOptionalVisual = Boolean(slot) && explicitGenerativeVisual;
  const autoCover = imageMode === "auto"
    && index === 0
    && !generationForbidden
    && (
      investorCoverBrief
      || visualStoryBrief
      || productVisualBrief
      || technicalVisualBrief
      || explicitGenerativeVisual
    )
    && strategies.includes("generate");
  const autoOptional = imageMode === "auto"
    && index > 0
    && !generationForbidden
    && explicitOptionalVisual
    && strategies.includes("generate");
  const creativeOptional = imageMode === "creative_image_mode"
    && index > 0
    && !generationForbidden
    && explicitOptionalVisual
    && strategies.includes("generate");
  const useExisting = !generationForbidden && Boolean(existingAsset)
    && !creativeCover
    && strategies.includes("use_existing");
  const plannedGeneration = (slot && slot.required)
    || creativeCover
    || creativeOptional
    || autoCover
    || autoOptional;
  const generate = !generationForbidden && !useExisting && Boolean(
    plannedGeneration
  );
  const required = !generationForbidden && Boolean(
    plannedGeneration
  );
  const decision = useExisting ? "use_existing" : generate ? "generate" : "skip";
  const status = useExisting ? "ready" : generate ? "pending" : "skipped";
  let decisionReason = "editable text, data, or shapes communicate this page more clearly than bitmap media";
  if (useExisting) {
    decisionReason = "a user-provided source asset is available for this declared media slot";
  } else if (generationForbidden) {
    decisionReason = "the user explicitly forbids generated images for this presentation";
  } else if (creativeCover) {
    decisionReason = "creative_image_mode requires a generated cover visual";
  } else if (slot && slot.required) {
    decisionReason = "the selected layout requires this media slot";
  } else if (autoCover) {
    if (productVisualBrief) {
      decisionReason = "the brief or outline explicitly calls for a product or interface cover visual";
    } else if (technicalVisualBrief) {
      decisionReason = "the brief or outline explicitly calls for a code or technical-system cover visual";
    } else if (investorCoverBrief) {
      decisionReason = "investor/pitch/launch brief benefits from a generated cover visual";
    } else if (explicitGenerativeVisual) {
      decisionReason = "the outline explicitly requests a generative visual medium such as a map, scene, photograph, or object study";
    } else {
      decisionReason = "visual story brief benefits from a generated cover visual";
    }
  } else if (creativeOptional || autoOptional) {
    decisionReason = "the page visual intent explicitly requests a generative visual medium";
  } else if (index === 0) {
    decisionReason = "the outline supports a typography-led cover and does not request a concrete bitmap visual";
  }
  return {
    slide: slideNumber,
    slide_id: slideId,
    layout_id: layout.id,
    slot: targetId,
    prop_path: propPath,
    required,
    decision,
    status,
    decision_reason: decisionReason,
    prompt: generate ? imagePrompt(context, slot ? slot.role : "background") : "",
    output_path: useExisting
      ? existingAsset.outputPath
      : generate
        ? `assets/generated/${slideId}-${targetId}.png`
        : null,
    ...(useExisting
      ? {
        origin: "uploaded",
        asset_hash: existingAsset.hash,
      }
      : {}),
    allowed_strategies: generationForbidden ? ["skip"] : strategies,
  };
}

function prepareExistingImageAssets(specs, orderedLayouts, deckFile, force = false) {
  const prepared = new Map();
  const deckDir = path.dirname(deckFile);
  const allowedExtensions = new Set([".png", ".jpg", ".jpeg", ".webp"]);
  specs.forEach(spec => {
    const layout = orderedLayouts[spec.slide - 1];
    if (!layout) {
      throw new Error(`--image-asset targets missing slide ${spec.slide}`);
    }
    const slots = layout.mediaSlots && Array.isArray(layout.mediaSlots.slots)
      ? layout.mediaSlots.slots
      : [];
    const slot = slots.find(item => item && item.id === spec.slot) || null;
    const background = spec.slot === "background"
      && layout.mediaSlots
      && layout.mediaSlots.background
      ? layout.mediaSlots.background
      : null;
    const strategies = slot && Array.isArray(slot.strategies)
      ? slot.strategies
      : background && Array.isArray(background.strategies)
        ? background.strategies
        : [];
    if (!slot && !background) {
      throw new Error(
        `--image-asset ${spec.slide}:${spec.slot} does not match a media slot or background ` +
        `for layout ${layout.id}`
      );
    }
    if (!strategies.includes("use_existing")) {
      throw new Error(
        `--image-asset ${spec.slide}:${spec.slot} is not allowed by layout ${layout.id}`
      );
    }
    const key = `${spec.slide}:${spec.slot}`;
    if (prepared.has(key)) throw new Error(`Duplicate --image-asset binding: ${key}`);
    if ([...prepared.values()].some(item => item.slide === spec.slide)) {
      throw new Error(
        `Only one primary --image-asset is supported per slide; slide ${spec.slide} has multiple bindings`
      );
    }
    const sourcePath = path.resolve(spec.sourcePath);
    if (!isNonEmptyFile(sourcePath)) {
      throw new Error(`Existing image asset not found or empty: ${sourcePath}`);
    }
    const extension = path.extname(sourcePath).toLowerCase();
    if (!allowedExtensions.has(extension)) {
      throw new Error(
        `Existing image asset must be PNG, JPG, JPEG, or WEBP: ${sourcePath}`
      );
    }
    const slideId = `slide-${String(spec.slide).padStart(2, "0")}`;
    const outputPath = `assets/source/${slideId}-${spec.slot}${extension}`;
    const destination = path.join(deckDir, ...outputPath.split("/"));
    if (fs.existsSync(destination) && !force && path.resolve(destination) !== sourcePath) {
      throw new Error(`Refusing to overwrite existing copied image asset: ${destination}`);
    }
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    if (path.resolve(destination) !== sourcePath) fs.copyFileSync(sourcePath, destination);
    prepared.set(key, {
      slide: spec.slide,
      slot: spec.slot,
      outputPath,
      hash: createHash("sha256").update(fs.readFileSync(destination)).digest("hex"),
    });
  });
  return prepared;
}

function isNonEmptyFile(filePath) {
  try {
    return fs.statSync(filePath).isFile() && fs.statSync(filePath).size > 0;
  } catch (_error) {
    return false;
  }
}

function findDownstreamArtifacts(deckFile) {
  const artifactRoot = path.dirname(deckFile);
  const found = [];
  const directFiles = [
    "deck.patch.json",
    "index.html",
    path.join("qa", "deck_spec.json"),
    path.join("qa", "truth_check.json"),
    path.join("qa", "image_manifest.json"),
    path.join("qa", "html_self_check.json"),
    path.join("qa", "runtime_probe.json"),
  ];
  directFiles.forEach(relativePath => {
    if (isNonEmptyFile(path.join(artifactRoot, relativePath))) {
      found.push(relativePath);
    }
  });

  const generatedDir = path.join(artifactRoot, "assets", "generated");
  if (fs.existsSync(generatedDir)) {
    const imageExtensions = new Set([".png", ".jpg", ".jpeg", ".webp"]);
    fs.readdirSync(generatedDir, { withFileTypes: true })
      .filter(entry => entry.isFile() && imageExtensions.has(path.extname(entry.name).toLowerCase()))
      .forEach(entry => found.push(path.join("assets", "generated", entry.name)));
  }

  const manifestPath = path.join(generatedDir, "manifest.json");
  if (isNonEmptyFile(manifestPath)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
      const imagePlan = Array.isArray(manifest.image_plan) ? manifest.image_plan : [];
      const hasProgress = imagePlan.some(entry => {
        if (!entry || typeof entry !== "object") return false;
        const status = String(entry.status || "").toLowerCase();
        return ["generated", "ready", "complete", "completed", "reused", "fixed"].includes(status);
      });
      if (hasProgress) {
        found.push(path.join("assets", "generated", "manifest.json (updated image state)"));
      }
    } catch (_error) {
      // A malformed manifest is handled by the normal validation path. It is
      // not sufficient evidence by itself to lock an otherwise bare scaffold.
    }
  }
  return found;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.listThemes && opts.rankThemes) {
    throw new Error("Use either --list-themes or --rank-themes, not both");
  }
  if (opts.listThemes) {
    if (opts.layoutIds.length || opts.out || opts.report) {
      throw new Error("--list-themes cannot be combined with layout ids, --out, or --report");
    }
    const compositionDirections = compositionDirectionCatalog().map(
      direction => direction.id
    );
    console.log(JSON.stringify({
      default_theme_id: DEFAULT_THEME_ID,
      composition_directions: compositionDirections,
      themes: listThemes().map(themeDiscoveryRecord),
    }));
    return;
  }
  if (opts.themeModelChoice && String(opts.themeId).toLowerCase() !== "auto") {
    throw new Error("--theme-model-choice requires --theme auto");
  }
  if (opts.themeModelChoice && opts.themeLocked) {
    throw new Error("--theme-model-choice cannot be combined with --lock-theme");
  }
  if (opts.themeModelChoice && !String(opts.themeModelReason || "").trim()) {
    throw new Error("--theme-model-choice requires --theme-model-reason");
  }
  if (!opts.themeModelChoice && opts.themeModelReason) {
    throw new Error("--theme-model-reason requires --theme-model-choice");
  }
  if (String(opts.themeModelReason || "").length > 240) {
    throw new Error("--theme-model-reason must be 240 characters or fewer");
  }
  if (
    opts.rankThemes
    && (
      opts.layoutIds.length
      || opts.out
      || opts.report
      || opts.themeLocked
      || opts.themeModelChoice
      || opts.family
      || opts.designSeed
      || opts.noImages
      || opts.imageAssets.length
      || opts.requiredFields.length
      || String(opts.themeId).toLowerCase() !== "auto"
    )
  ) {
    throw new Error(
      "--rank-themes accepts brief inputs only and cannot be combined with layouts, output, explicit themes, or model choice"
    );
  }
  if (opts.report && !opts.out) {
    throw new Error("--report requires --out deck.json");
  }
  if (opts.out && !opts.outline) {
    const deckFile = resolveArtifactPath(opts.out);
    const siblingOutline = path.join(path.dirname(deckFile), "outline.json");
    if (isNonEmptyFile(siblingOutline)) {
      throw new Error(
        `A validated outline already exists at ${siblingOutline}; pass ` +
        "--outline outline.json so the scaffold cannot detach from its page plan"
      );
    }
  }
  if (!["source_bound", "illustrative"].includes(opts.truthMode)) {
    throw new Error("--truth-mode must be source_bound or illustrative");
  }
  if (!["auto", "creative_image_mode"].includes(opts.imageMode)) {
    throw new Error("--image-mode must be auto or creative_image_mode");
  }
  if (opts.imageAssets.length && !opts.out) {
    throw new Error("--image-asset requires --out deck.json so the asset can be copied portably");
  }
  if (opts.noImages && opts.imageAssets.length) {
    throw new Error("--no-images cannot be combined with --image-asset");
  }
  opts.layoutIds.forEach(layoutId => {
    const layout = getLayout(layoutId);
    if (!layout) {
      throw new Error(
        `Unknown layout_id: ${layoutId}; registered layout_ids: ` +
        require("../layouts/registry.js").layouts.map(item => item.id).join(", ")
      );
    }
  });
  const outlineBinding = opts.outline
    ? readOutlineBinding(opts.outline, opts.layoutIds.length || null)
    : null;
  const runtimeBinding = runtimeSourceBinding();
  const designContext = {
    title: opts.title,
    source_facts: opts.sourceFacts,
    source_text: runtimeBinding.source_text,
    outline: outlineBinding ? outlineBinding.content : null,
  };
  if (opts.rankThemes) {
    console.log(JSON.stringify(themeShortlistPayload(designContext), null, 2));
    return;
  }
  const assumptions = [...new Set(
    opts.assumptions.map(value => value.trim()).filter(Boolean)
  )];
  const assumptionBinding = validateAssumptionsAgainstRuntime(assumptions);
  const truthAdvisories = [...assumptionBinding.issues];
  const layoutPolicy = {
    allowIllustrativeQuantitative: (
      opts.truthMode === "illustrative"
      || assumptionBinding.assumption_count > 0
    ),
  };
  if (outlineBinding && opts.layoutIds.length === 0) {
    opts.layoutIds = outlineBinding.slides.map(slide => {
      const semantic = analyzeOutlineLayoutIntent(
        slide,
        outlineBinding.sourceMode,
        layoutPolicy
      );
      return semantic && semantic.preferred_layout_id
        ? semantic.preferred_layout_id
        : "cards-grid-v1";
    });
  }
  const layoutResolution = normalizeOutlineDrivenLayoutIds(
    opts.layoutIds,
    outlineBinding,
    layoutPolicy
  );
  const authoringPlan = expandOutlineDrivenPlan(
    layoutResolution.layoutIds,
    outlineBinding
  );
  const effectiveLayoutIds = authoringPlan.map(entry => {
    const layoutId = entry.layoutId;
    if (!opts.noImages) return layoutId;
    const layout = getLayout(layoutId);
    const requiredSlots = layout && layout.mediaSlots && Array.isArray(layout.mediaSlots.slots)
      ? layout.mediaSlots.slots.filter(slot => slot && slot.required === true)
      : [];
    if (!requiredSlots.length) return layoutId;
    if (!layout.noImageFallbackLayoutId || !getLayout(layout.noImageFallbackLayoutId)) {
      throw new Error(`Layout ${layoutId} requires media and has no registered no-image fallback.`);
    }
    return layout.noImageFallbackLayoutId;
  });
  const orderedLayouts = effectiveLayoutIds.map(layoutId => getLayout(layoutId));
  const authoringSlides = authoringPlan.map(entry => entry.outlineSlide);
  validateOutlineLayoutFit(
    orderedLayouts,
    outlineBinding ? { ...outlineBinding, slides: authoringSlides } : null,
    layoutPolicy
  );
  const themeResolution = selectTheme(opts, designContext);
  const designContract = inferDesignContract(
    designContext,
    outlineBinding ? authoringSlides : []
  );
  const theme = themeResolution.theme;
  if (!theme) {
    throw new Error(
      `Unknown theme_id: ${JSON.stringify(opts.themeId)}; ` +
      `registered theme_ids: ${listThemes().map(item => item.id).join(", ")}`
    );
  }
  const themeSelection = themeResolution.selection;
  const designSelection = opts.family
    ? {
      family: opts.family,
      source: "explicit_family",
      score: null,
      matched_signals: [],
      scores: null,
    }
    : inferCompositionFamily(
      theme,
      {
        title: opts.title,
        source_text: outlineBinding ? "" : runtimeBinding.source_text,
        outline: outlineBinding ? outlineBinding.content : null,
      },
      effectiveLayoutIds,
    );
  const design = createDeckDesign(theme, opts.designSeed, designSelection.family);
  const selectedLayouts = [...new Set(effectiveLayoutIds)].map(layoutId => getLayout(layoutId));
  const requiredFieldNormalizations = [];
  const requiredFieldRelaxations = [];
  const normalizedRequiredFields = opts.requiredFields.flatMap(requirement => {
    const layout = orderedLayouts[requirement.slide - 1];
    if (!layout) {
      throw new Error(
        `Required field targets missing slide ${requirement.slide}; ` +
        `the ordered plan contains ${orderedLayouts.length} slide(s)`
      );
    }
    const field = normalizeRequiredField(layout, requirement.field);
    if (!Object.prototype.hasOwnProperty.call(layout.fields, field)) {
      const outlineSlide = outlineBinding
        ? authoringSlides[requirement.slide - 1]
        : null;
      if (canRelaxMissingRequiredField(requirement, outlineSlide)) {
        requiredFieldRelaxations.push({
          slide: requirement.slide,
          field: requirement.field,
          requested_layout_id: opts.layoutIds[requirement.slide - 1],
          effective_layout_id: layout.id,
          reason: (
            "decorative field is not an explicit semantic outline requirement " +
            "and is unsupported by the effective layout"
          ),
        });
        return [];
      }
      throw new Error(
        `Slide ${requirement.slide} layout ${layout.id} does not provide required field ` +
        `${requirement.field}; available fields: ${Object.keys(layout.fields).join(", ")}`
      );
    }
    if (field !== requirement.field) {
      requiredFieldNormalizations.push({
        slide: requirement.slide,
        from: requirement.field,
        to: field,
      });
    }
    return [{ slide: requirement.slide, field }];
  });
  const researchFacts = [...new Set([
    ...opts.researchFacts.map(value => value.trim()).filter(Boolean),
    ...(outlineBinding ? outlineBinding.importedResearchFacts : []),
  ])];
  // A source-bound brief is itself the only user-provided provenance.
  // Models should still pass --fact explicitly when they extract multiple
  // claims, but never let an omitted flag produce an empty truth contract when
  // the ACP runtime has bound the exact current user request. Research evidence
  // remains in its separate bucket and suppresses this fallback. Long runtime
  // requests are split into exact contiguous phrases so the deterministic
  // fallback cannot violate the per-fact truth-contract limit.
  const defaultedSourceFacts = Boolean(
    opts.truthMode === "source_bound"
    && opts.sourceFacts.length === 0
    && researchFacts.length === 0
    && runtimeBinding.available
    && runtimeBinding.source_text.trim()
  );
  const sourceFactNormalization = canonicalizeSourceFacts(
    defaultedSourceFacts
      ? splitDefaultRuntimeSourceFacts(runtimeBinding.source_text)
      : opts.sourceFacts
  );
  const skeleton = orderedLayouts.length
    ? {
      schema_version: 1,
      title: opts.title,
      theme_id: theme.id,
      design,
      ...(designContract ? { design_contract: designContract } : {}),
      truth_contract: {
        mode: opts.truthMode,
        source_facts: sourceFactNormalization.facts,
        ...(researchFacts.length
          ? { research_facts: researchFacts }
          : {}),
        assumptions,
      },
      slides: orderedLayouts.map((layout, index) => {
        const entry = authoringPlan[index];
        const slide = buildSlide(
          layout.id,
          index,
          entry && entry.outlineSlide
        );
        if (entry && entry.boundOutlineSlide) {
          slide.outline_intent = outlineIntentRecord(entry.boundOutlineSlide);
        }
        if (entry && entry.sourceOutlinePage) {
          slide.source_outline_page = entry.sourceOutlinePage;
        }
        if (entry && entry.sourceOutlineItemRange) {
          slide.source_outline_item_range = entry.sourceOutlineItemRange;
        }
        return slide;
      }),
    }
    : null;
  if (skeleton) {
    const validation = validateAndNormalizeDeck(skeleton);
    if (!validation.ok) {
      throw new Error(`Generated skeleton is invalid:\n${validation.issues.join("\n")}`);
    }
  }
  const sourceBinding = validateSourceFactsAgainstRuntime(
    skeleton ? skeleton.truth_contract.source_facts : []
  );
  truthAdvisories.push(...sourceBinding.issues);
  const researchBinding = validateResearchFactsAgainstRuntime(
    skeleton && Array.isArray(skeleton.truth_contract.research_facts)
      ? skeleton.truth_contract.research_facts
      : []
  );
  truthAdvisories.push(...researchBinding.issues);
  let deckFile = null;
  let contractReport = null;
  let imageManifest = null;
  if (opts.out) {
    if (!skeleton) {
      throw new Error("--out requires at least one ordered LAYOUT_ID");
    }
    deckFile = resolveArtifactPath(opts.out);
    if (opts.force) {
      const downstreamArtifacts = findDownstreamArtifacts(deckFile);
      if (downstreamArtifacts.length) {
        throw new Error(
          "Refusing --force reset because downstream deck artifacts already exist: " +
          `${downstreamArtifacts.join(", ")}. Resume from the existing deck/patch ` +
          "and render/QA stages instead."
        );
      }
    }
    if (fs.existsSync(deckFile) && !opts.force) {
      throw new Error(
        `Refusing to overwrite existing deck skeleton: ${deckFile}; ` +
        "patch the existing deck.json, or pass --force only for an intentional reset"
      );
    }
    imageManifest = path.join(path.dirname(deckFile), "assets", "generated", "manifest.json");
    if (fs.existsSync(imageManifest) && !opts.force) {
      throw new Error(
        `Refusing to overwrite existing image manifest: ${imageManifest}; ` +
        "keep one deck/manifest pair in the canonical output root"
      );
    }
    const existingImageAssets = prepareExistingImageAssets(
      opts.imageAssets,
      orderedLayouts,
      deckFile,
      opts.force,
    );
    fs.mkdirSync(path.dirname(deckFile), { recursive: true });
    fs.writeFileSync(deckFile, `${JSON.stringify(skeleton, null, 2)}\n`, "utf8");

    const globalBriefText = [
      opts.title,
      runtimeBinding.source_text,
      outlineBinding && outlineBinding.content
        ? outlineBinding.content.deck_goal
        : "",
      outlineBinding && outlineBinding.content
        ? outlineBinding.content.storyline
        : "",
    ].filter(Boolean).join("\n");
    // A typography-led cover or another slide-local no-image direction must
    // only skip that slide. Persist generation_forbidden=true solely for an
    // explicit deck-wide constraint; buildImagePlanEntry applies local
    // opt-outs independently to each slide.
    const generationForbidden = opts.noImages || hasDeckWideImageOptOut(globalBriefText);
    const imageManifestPayload = {
      schema_version: 1,
      mode: opts.imageMode,
      generation_forbidden: generationForbidden,
      deck: {
        title: skeleton.title,
        theme_id: skeleton.theme_id,
        design: skeleton.design,
        ...(skeleton.design_contract ? { design_contract: skeleton.design_contract } : {}),
      },
      image_plan: orderedLayouts.map((layout, index) => (
        buildImagePlanEntry(
          layout,
          index,
          opts.imageMode,
          {
            deckTitle: opts.title,
            briefText: globalBriefText,
            slideText: [
              outlineBinding ? authoringSlides[index].title : "",
              outlineBinding ? authoringSlides[index].message : "",
              outlineBinding ? authoringSlides[index].visual : "",
              outlineBinding ? authoringSlides[index].bullets.join("\n") : "",
            ].filter(Boolean).join("\n"),
            slide: outlineBinding ? authoringSlides[index] : null,
            generationForbidden,
          },
          [...existingImageAssets.values()].find(
            asset => asset.slide === index + 1
          ) || null,
        )
      )),
    };
    fs.mkdirSync(path.dirname(imageManifest), { recursive: true });
    fs.writeFileSync(imageManifest, `${JSON.stringify(imageManifestPayload, null, 2)}\n`, "utf8");

    contractReport = opts.report
      ? resolveArtifactPath(opts.report)
      : path.join(path.dirname(deckFile), "qa", "deck_contract.json");
    const contractHash = createHash("sha256").update(JSON.stringify({
      contract_version: 2,
      theme: themeManifestRecord(theme),
      design: skeleton.design,
      design_selection: designSelection,
      design_contract: skeleton.design_contract || null,
      layouts: selectedLayouts.map(manifestRecord),
      layout_plan: effectiveLayoutIds,
      layout_plan_requested: opts.layoutIds,
      layout_normalizations: layoutResolution.normalizations,
      image_mode: opts.imageMode,
      truth_contract: skeleton.truth_contract,
      source_binding_hash: sourceBinding.source_hash,
      required_fields: normalizedRequiredFields,
      required_field_relaxations: requiredFieldRelaxations,
      theme_id_normalization: themeResolution.normalization,
      theme_selection: themeSelection,
      outline_binding: outlineBinding
        ? {
          outline_hash: outlineBinding.hash,
          source_mode: outlineBinding.sourceMode,
          evidence_import_count: outlineBinding.importedResearchFacts.length,
        }
        : null,
    })).digest("hex");
    const reportPayload = {
      ok: true,
      contract_version: 2,
      contract_hash: contractHash,
      deck_file: deckFile,
      image_manifest: imageManifest,
      theme_id: theme.id,
      design: skeleton.design,
      design_contract: skeleton.design_contract || null,
      design_selection: designSelection,
      theme_selection: themeSelection,
      theme_id_normalization: themeResolution.normalization,
      truth_mode: skeleton.truth_contract.mode,
      image_mode: opts.imageMode,
      source_fact_count: skeleton.truth_contract.source_facts.length,
      research_fact_count: Array.isArray(skeleton.truth_contract.research_facts)
        ? skeleton.truth_contract.research_facts.length
        : 0,
      assumption_count: skeleton.truth_contract.assumptions.length,
      source_binding: {
        available: sourceBinding.available,
        strict: sourceBinding.strict,
        allows_assumptions: sourceBinding.allows_assumptions,
        source_hash: sourceBinding.source_hash,
        verified_fact_count: sourceBinding.verified_fact_count,
      },
      source_fact_normalizations: sourceFactNormalization.changes,
      source_fact_defaulted_from_runtime: defaultedSourceFacts,
      slide_count: skeleton.slides.length,
      layout_plan: effectiveLayoutIds,
      layout_plan_requested: opts.layoutIds,
      layout_normalizations: layoutResolution.normalizations,
      required_fields: normalizedRequiredFields,
      required_field_normalizations: requiredFieldNormalizations,
      required_field_relaxations: requiredFieldRelaxations,
      warnings: [
        ...requiredFieldRelaxations.map(item => (
          `Slide ${item.slide} ignored decorative --require-field ${item.field} because ` +
          `${item.effective_layout_id} does not expose it and the outline does not ` +
          "require semantic tag content"
        )),
        ...truthAdvisories,
      ],
      outline_binding: outlineBinding
        ? {
          outline_file: outlineBinding.file,
          outline_hash: outlineBinding.hash,
          source_mode: outlineBinding.sourceMode,
          page_count: outlineBinding.slides.length,
          deck_slide_count: authoringSlides.length,
          evidence_import_count: outlineBinding.importedResearchFacts.length,
        }
        : null,
    };
    fs.mkdirSync(path.dirname(contractReport), { recursive: true });
    fs.writeFileSync(contractReport, `${JSON.stringify(reportPayload, null, 2)}\n`, "utf8");
  }

  console.log(JSON.stringify({
    contract_version: 2,
    authoring_rules: {
      theme_source: "available_theme_ids is built into pptx; html-templates is optional",
      layout_contract_path: "layouts[].fields",
      layout_defaults_path: "layouts[].editor.defaultProps",
      media_value_example: {
        src: "assets/generated/image.png",
        alt: "image description",
        origin: "generated",
      },
      write_policy: {
        scaffold_command: "Pass every slide's ordered layout id (including repeats), --outline outline.json, and --out deck.json; then edit props only.",
        artifact_root: process.env.BOX_AGENT_OUTPUT_DIR || process.cwd(),
        initial_full_deck_writes: 0,
        initial_scaffold_writes: 1,
        next_step: "The selected layout fields/defaults below are complete. Patch deck.json and assets/generated/manifest.json in place; do not call inspect_layout again for these layout ids.",
        batch_patch_command: "Write deck.patch.json once, then run ${BOX_AGENT_NODE:-node} scripts/apply_deck_patch.js deck.json deck.patch.json to update all slide props in one validated mutation.",
        after_validation_failure: "Patch only the paths named in the validation report; do not rewrite the whole deck.",
        repeated_issue: "If the same issue class appears twice, re-read this contract and stop full-file rewrites.",
      },
      design_policy: {
        path: "design",
        rule: "Explicit --family wins. Otherwise infer one allowed family from the title, bound outline, and ordered layouts; keep the theme default when signals are weak. Persist design through patch/render/export. A fresh scaffold may choose another allowed family or variant.",
        user_choice_path: "selected_theme.composition.directions",
        family_selection_path: "selected_theme.composition.families[].selection_signals",
        selection_source: designSelection.source,
        selected_direction: directionForFamily(design.family),
        selected_family: design.family,
        reproducible_scaffold: "Pass --design-seed SEED only for tests or an intentionally reproducible new deck.",
      },
      ...(themeSelection.source !== "fallback_default"
        ? {
          theme_policy: {
            rule: "Use --theme auto normally; --lock-theme only for an exact user choice.",
            selection_source: themeSelection.source,
            selected_theme_id: theme.id,
          },
        }
        : {}),
      image_policy: {
        mode: opts.imageMode,
        rule: opts.imageMode === "creative_image_mode"
          ? "At least the cover is scaffolded as generate and one real generated asset is mandatory. Batch independent generate_image calls in one model turn."
          : "Resolve required media entries. The scaffold reads outline visual intent: product/interface, code/system, investor/launch, and visual-story covers promote a concrete image job; user-provided --image-asset bindings win through use_existing, while text/data pages stay image-free when bitmap media adds no narrative value.",
        validation_command: opts.imageMode === "creative_image_mode"
          ? "node scripts/validate_image_manifest.js assets/generated/manifest.json --mode creative_image_mode --min-generated 1 --deck deck.json --report qa/image_manifest.json"
          : "node scripts/validate_image_manifest.js assets/generated/manifest.json --deck deck.json --report qa/image_manifest.json",
      },
      truth_policy: {
        source_contract_path: "truth_contract.source_facts",
        research_contract_path: "truth_contract.research_facts",
        assumption_contract_path: "truth_contract.assumptions",
        validation_command: "node scripts/validate_deck_truth.js deck.json --report qa/truth_check.json",
        rule: "Prefer verbatim --fact values and researched --research-fact values. Missing or mismatched provenance is advisory: continue to HTML, use a placeholder or omit an unavailable optional fact, and report the limitation after generation.",
        source_binding: {
          available: sourceBinding.available,
          strict: sourceBinding.strict,
          allows_assumptions: sourceBinding.allows_assumptions,
          source_hash: sourceBinding.source_hash,
          verified_fact_count: sourceBinding.verified_fact_count,
        },
        label_normalizations: sourceFactNormalization.changes,
        advisories: truthAdvisories,
      },
      outline_policy: outlineBinding
        ? {
          rule: "Every deck slide must preserve its source_outline_page. When source_outline_item_range is present, author only that contiguous bullet range and preserve the original outline title; the complete split group must cover every requested item exactly once. Quantitative values may be split across KPI/chart fields when every value and matching label is preserved; qualitative pages need an exact atomic message/bullet fragment. Fill the typed visual item contract exactly. Do not duplicate full source sentences in every cell, swap page topics, truncate collections, or invent quantitative values for qualitative pages.",
          source_mode: outlineBinding.sourceMode,
          evidence_import_count: outlineBinding.importedResearchFacts.length,
          pages: authoringSlides.map((slide, index) => ({
            page: index + 1,
            source_outline_page: authoringPlan[index].sourceOutlinePage,
            ...(authoringPlan[index].sourceOutlineItemRange
              ? { source_outline_item_range: authoringPlan[index].sourceOutlineItemRange }
              : {}),
            title: slide.title,
            message: slide.message,
            bullets: slide.bullets,
            layout: slide.layout,
            visual: slide.visual,
            expected_visual_item_count: expectedVisualItemCount(slide),
            evidence: slide.evidence,
          })),
        }
        : null,
      ...(skeleton.design_contract
        ? {
          design_contract_policy: {
            contract_path: "design_contract",
            rule: "Explicit palette, visual kind, relationship, direction, and item-count requirements are hard constraints. Resolve them exactly or stop before delivery; theme and composition matching remain soft after hard constraints are satisfied.",
            palette_override: skeleton.design_contract.palette || null,
            slides: skeleton.design_contract.slides || {},
          },
        }
        : {}),
      content_requirements: {
        command: "Use --require-field SLIDE:FIELD only for explicit semantic content (for example 4:metrics), never decoration.",
        enforced: normalizedRequiredFields,
        normalizations: requiredFieldNormalizations,
        ...(requiredFieldRelaxations.length
          ? { relaxations: requiredFieldRelaxations }
          : {}),
      },
      ...(layoutResolution.normalizations.length
        ? {
          layout_policy: {
            rule: "Use the effective layout contract below; a responsibility matrix was normalized to table-data-v1.",
            normalizations: layoutResolution.normalizations,
          },
        }
        : {}),
    },
    default_theme_id: DEFAULT_THEME_ID,
    available_theme_ids: listThemes().map(theme => theme.id),
    selected_theme: themeManifestRecord(theme),
    ...(themeSelection.source !== "fallback_default"
      ? { theme_selection: compactThemeSelection(themeSelection) }
      : {}),
    theme_id_normalization: themeResolution.normalization,
    outline_binding: outlineBinding
      ? {
        outline_file: outlineBinding.file,
        outline_hash: outlineBinding.hash,
        source_mode: outlineBinding.sourceMode,
        evidence_import_count: outlineBinding.importedResearchFacts.length,
      }
      : null,
    ...(layoutResolution.normalizations.length
      ? {
        layout_plan_requested: opts.layoutIds,
        layout_plan: effectiveLayoutIds,
        layout_normalizations: layoutResolution.normalizations,
      }
      : {}),
    layouts: selectedLayouts.map(manifestRecord),
    deck_skeleton: skeleton,
    deck_file: deckFile,
    image_manifest: imageManifest,
    contract_report: contractReport,
  }));
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
