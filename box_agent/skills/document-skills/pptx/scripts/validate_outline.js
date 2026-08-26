#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const {
  OUTLINE_INTENT_TEXT_LIMITS,
  TRUTH_TEXT_MAX_CHARACTERS,
  resolveArtifactPath,
  runtimeSourceBinding,
} = require("./deck_spec_core.js");
const { slideContract } = require("./design_contract_core.js");
const {
  analyzeOutlineLayoutIntent,
  expectedVisualItemContract,
} = require("./outline_layout_contract.js");

const PAGE_TOKEN_SOURCE = "(?:\\d{1,2}|[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])";
const PAGE_RANGE_RE = new RegExp(
  `(?<!第)(${PAGE_TOKEN_SOURCE})\\s*(?:-|~|～|—|–|至|到)\\s*(${PAGE_TOKEN_SOURCE})\\s*(?:页|pages?|slides?)`,
  "i"
);
const PAGE_COUNT_RE = new RegExp(
  `(?<!第)(${PAGE_TOKEN_SOURCE})\\s*(?:页|pages?|slides?)`,
  "i"
);
const POSITIONAL_PAGE_SPAN_RE = new RegExp(
  `(?:前|首|最前)\\s*${PAGE_TOKEN_SOURCE}` +
    `(?:\\s*(?:-|~|～|—|–|至|到)\\s*${PAGE_TOKEN_SOURCE})?\\s*页(?:内容)?` +
    `|\\b(?:first|initial)\\s+${PAGE_TOKEN_SOURCE}` +
    `(?:\\s*(?:-|~|to|through)\\s*${PAGE_TOKEN_SOURCE})?\\s*(?:pages?|slides?)\\b`,
  "gi"
);

function parsePageToken(value) {
  const token = String(value || "").trim();
  if (/^\d{1,2}$/.test(token)) return Number(token);
  const digits = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  if (token === "十") return 10;
  if (token === "二十") return 20;
  if (token.startsWith("十") && token.length === 2) return 10 + (digits[token[1]] || 0);
  if (token.endsWith("十") && token.length === 2) return (digits[token[0]] || 0) * 10 || null;
  if (token.length === 3 && token[1] === "十") {
    const tens = digits[token[0]];
    const ones = digits[token[2]];
    return tens && ones ? tens * 10 + ones : null;
  }
  return digits[token] || null;
}

function explicitPageCountContract(sourceText) {
  let normalized = String(sourceText || "").normalize("NFKC");
  const userQuestionMarkers = [...normalized.matchAll(
    /(?:^|\n)(?:用户问题|user\s+(?:question|request))\s*[:：]\s*/gi
  )];
  if (userQuestionMarkers.length) {
    const marker = userQuestionMarkers[userQuestionMarkers.length - 1];
    normalized = normalized.slice(marker.index + marker[0].length);
  }
  const ordinalPageReference = new RegExp(
    `第\\s*${PAGE_TOKEN_SOURCE}(?:\\s*(?:-|~|～|—|–|至|到)\\s*${PAGE_TOKEN_SOURCE})?\\s*页`,
    "gi"
  );
  const requestText = normalized
    .replace(ordinalPageReference, " ")
    .replace(POSITIONAL_PAGE_SPAN_RE, " ");
  const range = requestText.match(PAGE_RANGE_RE);
  if (range) {
    const minimum = parsePageToken(range[1]);
    const maximum = parsePageToken(range[2]);
    if (minimum && maximum && minimum <= maximum) return { minimum, maximum };
  }
  const exact = requestText.match(PAGE_COUNT_RE);
  const count = exact ? parsePageToken(exact[1]) : null;
  return count ? { minimum: count, maximum: count } : null;
}

function outlineBulletCount(slide) {
  const bullets = Array.isArray(slide && slide.bullets) ? slide.bullets : [];
  const contract = slideContract(slide);
  if (
    contract
    && contract.visual_kind === "numbered-actions"
    && Number.isInteger(contract.item_count)
  ) {
    const numberedItems = bullets.filter(item =>
      /^\s*(?:\d{1,2}|[一二三四五六七八九十]+)[.)、．:：-]\s*/u.test(String(item || ""))
    );
    if (numberedItems.length === contract.item_count) return numberedItems.length;
  }
  return bullets.length;
}

function declaredAgendaItemCounts(slide) {
  const words = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
  const counts = new Set();
  [slide && slide.title, slide && slide.message, slide && slide.visual]
    .filter(value => typeof value === "string")
    .forEach(value => {
      const pattern = /(?:^|[^0-9一二三四五六七八九十])([0-9]{1,2}|[一二三四五六七八九十])\s*(?:个|项|条|张|站|视角|节点|卡片|标签)/gu;
      for (const match of value.matchAll(pattern)) {
        const count = /^\d+$/.test(match[1]) ? Number(match[1]) : words[match[1]];
        if (Number.isInteger(count) && count >= 2) counts.add(count);
      }
    });
  return [...counts].sort((left, right) => left - right);
}

function usage() {
  console.error(
    "Usage: validate_outline.js outline.json [--min-slides N] " +
    "[--max-slides N] [--research-handoff research/qa/topic_research_check.json] " +
    "[--report qa/outline_check.json]"
  );
  process.exit(2);
}

function parseArgs(argv) {
  if (argv.length < 1) usage();
  const opts = {
    outlinePath: argv[0],
    minSlides: 3,
    maxSlides: 40,
    minSlidesExplicit: false,
    maxSlidesExplicit: false,
    researchHandoff: null,
    report: null,
  };
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--min-slides" && value) {
      opts.minSlides = Number(value);
      opts.minSlidesExplicit = true;
      i += 1;
    } else if (arg === "--max-slides" && value) {
      opts.maxSlides = Number(value);
      opts.maxSlidesExplicit = true;
      i += 1;
    } else if (arg === "--report" && value) {
      opts.report = value;
      i += 1;
    } else if (["--research-handoff", "--research-report"].includes(arg) && value) {
      // --research-report remains a compatibility alias for existing sessions.
      opts.researchHandoff = value;
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isInteger(opts.minSlides) || opts.minSlides < 1) usage();
  if (!Number.isInteger(opts.maxSlides) || opts.maxSlides < opts.minSlides) usage();
  return opts;
}

function readOutline(outlinePath) {
  const resolved = resolveArtifactPath(outlinePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`Outline file not found: ${resolved}`);
  }
  try {
    return { outline: JSON.parse(fs.readFileSync(resolved, "utf8")), resolved };
  } catch (error) {
    throw new Error(`Invalid JSON in ${resolved}: ${error.message}`);
  }
}

function readPresentationHandoff(handoffPath) {
  if (!handoffPath) return null;
  const resolved = resolveArtifactPath(handoffPath);
  let container;
  try {
    container = JSON.parse(fs.readFileSync(resolved, "utf8"));
  } catch (error) {
    throw new Error(`Invalid presentation research handoff ${resolved}: ${error.message}`);
  }
  const generic = container && container.presentation_handoff
    ? container.presentation_handoff
    : (container && container.delivery_mode ? container : null);
  let deliveryMode;
  let verifiedFacts;
  let qualityOk;
  let legacySupported = true;
  if (generic) {
    deliveryMode = generic.delivery_mode;
    verifiedFacts = Array.isArray(generic.verified_facts)
      ? generic.verified_facts
      : null;
    qualityOk = Boolean(
      generic.quality_summary && generic.quality_summary.quality_ok === true
    );
  } else {
    // Compatibility adapter for reports written before presentation_handoff v1.
    legacySupported = container && container.validator === "research-synthesis";
    const legacyFull = container
      && container.delivery_allowed === undefined
      && container.ok === true;
    const deliveryAllowed = legacyFull || container.delivery_allowed === true;
    deliveryMode = deliveryAllowed
      ? (legacyFull ? "full" : container.handoff_status)
      : "invalid";
    verifiedFacts = Array.isArray(container && container.verified_evidence)
      ? container.verified_evidence
      : null;
    qualityOk = container && container.quality_ok === true;
  }
  if (
    !container
    || !legacySupported
    || (generic && generic.schema_version !== 1)
    || !["full", "partial", "framework"].includes(deliveryMode)
    || !verifiedFacts
    || (["full", "partial"].includes(deliveryMode) && verifiedFacts.length < 1)
    || (deliveryMode === "framework" && verifiedFacts.length !== 0)
  ) {
    throw new Error(
      "Presentation research handoff is not a valid full/partial/framework delivery"
    );
  }
  const verified = new Map();
  verifiedFacts.forEach((item, index) => {
    if (
      !item
      || (item.status !== undefined && item.status !== "verified")
      || !text(item.entity)
      || !text(item.claim)
      || !text(item.source_url)
      || !text(item.canonical)
    ) {
      throw new Error(`Presentation handoff verified_facts.${index} is invalid`);
    }
    verified.set(text(item.canonical), item);
  });
  return { resolved, verified, deliveryMode, qualityOk };
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function narrativeText(value) {
  if (Array.isArray(value)) {
    return value.map(text).filter(Boolean).join("\n");
  }
  return text(value);
}

function wordLikeLength(value) {
  return Array.from(text(value)).length;
}

function normalize(value) {
  return text(value).toLowerCase().replace(/\s+/g, " ");
}

function includesAny(value, needles) {
  const normalized = normalize(value);
  return needles.some(needle => normalized.includes(needle));
}

function numberTokens(value) {
  const claimText = String(value || "")
    // Campaign and membership names contain digits but do not assert metrics.
    // Keep actual dates/percentages elsewhere in the sentence visible.
    .replace(/双\s*11/giu, "双十一")
    .replace(/88\s*VIP/giu, "VIP");
  return (claimText.match(/\d+(?:,\d{3})*(?:\.\d+)?%?/g) || [])
    .map(token => {
      const percent = token.endsWith("%");
      const bare = token.replace(/,/g, "").replace(/%$/, "");
      const numeric = Number(bare);
      return `${Number.isFinite(numeric) ? numeric : bare}${percent ? "%" : ""}`;
    });
}

function hasHttpUrl(value) {
  return /https?:\/\/[^\s|]+/i.test(String(value || ""));
}

function isPublicResearchOutline(outline) {
  const sourceMode = normalize(outline && outline.source_mode);
  if (!sourceMode) return false;
  if (includesAny(sourceMode, ["illustrative", "fictional", "hypothetical", "示意", "虚构"])) {
    return false;
  }
  return includesAny(sourceMode, [
    "public",
    "research",
    "authoritative",
    "公开",
    "研究",
    "权威",
  ]);
}

function hasEvidence(slide) {
  return Array.isArray(slide.evidence) && slide.evidence.some(item => text(item));
}

function isStructuralEvidenceExemptSlide(slide, index) {
  const role = `${slide && slide.layout || ""} ${slide && slide.visual || ""}`;
  if (index === 0 && includesAny(role, ["cover", "hero", "封面"])) return true;
  return includesAny(role, [
    "agenda",
    "table of contents",
    "section divider",
    "section-divider",
    "目录",
    "议程",
    "章节页",
    "章节过渡",
    "过渡页",
  ]);
}

const ASSUMPTION_EVIDENCE_RE = /假设|示意|假定|assum(?:e|ed|ption)|illustrative|hypothetical/i;
const UNAVAILABLE_FACT_PLACEHOLDER_RE = /未提供|未给出|待补充|待确认|缺失|未知|暂无可验证公开数据|not\s+provided|not\s+supplied|missing|unknown|tbd|no\s+verifiable\s+public\s+data/i;
const FRAMEWORK_UNAVAILABLE_FACT_PLACEHOLDER = "暂无可验证公开数据";
const PRIVATE_IDENTITY_FACT_RE = /(?:融资(?:阶段|轮次)|(?:种子|天使|成长)轮|pre[-\s]?a|series\s+[a-z]|[a-f]\s*轮|(?:公司|项目|产品)(?:名称|名为)|成立(?:年份|时间)|创始人|团队(?:成员姓名|姓名|履历|规模|人数|来源)|客户(?:名称|名单)|奖项|获奖|排名)/i;

function validate(outline, opts) {
  const issues = [];
  const warnings = [];
  const publicResearch = isPublicResearchOutline(outline);
  const verifiedResearch = opts.verifiedResearch;

  if (
    verifiedResearch
    && verifiedResearch.deliveryMode === "framework"
    && !publicResearch
  ) {
    issues.push(
      "source_mode must remain public_authoritative_research when a presentation " +
      "research fallback handoff is supplied; do not relabel framework fallback as " +
      "user_provided"
    );
  }

  for (const field of ["deck_goal", "source_mode"]) {
    if (!text(outline[field])) issues.push(`Missing top-level field: ${field}`);
  }
  for (const field of ["audience", "storyline"]) {
    if (!narrativeText(outline[field])) {
      issues.push(`Missing top-level field: ${field}`);
    }
  }

  const slides = Array.isArray(outline.slides) ? outline.slides : null;
  if (!slides) {
    issues.push("Missing or invalid top-level field: slides must be an array");
    return { ok: false, issues, warnings, slideCount: 0 };
  }

  if (slides.length < opts.minSlides) {
    issues.push(`Too few slides: ${slides.length}; expected at least ${opts.minSlides}`);
  }
  if (slides.length > opts.maxSlides) {
    issues.push(`Too many slides: ${slides.length}; expected at most ${opts.maxSlides}`);
  }

  const seenTitles = new Map();
  const seenMessages = new Map();
  const evidenceUsage = new Map();
  const dataHeavyTerms = [
    "market",
    "市场",
    "tam",
    "sam",
    "som",
    "growth",
    "增长",
    "traction",
    "收入",
    "revenue",
    "financial",
    "融资",
    "成本",
    "cost",
    "roi",
    "chart",
    "图表",
    "benchmark",
    "竞品",
    "competition",
  ];
  const dataDisplayTerms = [
    "chart",
    "graph",
    "plot",
    "table",
    "dashboard",
    "scorecard",
    "kpi",
    "metric",
    "bar",
    "line",
    "area",
    "pie",
    "donut",
    "scatter",
    "waterfall",
    "funnel",
    "heatmap",
    "matrix",
    "图表",
    "表格",
    "看板",
    "仪表盘",
    "指标",
    "柱状",
    "条形",
    "折线",
    "面积",
    "饼图",
    "散点",
    "瀑布",
    "漏斗",
    "热力",
    "矩阵",
    "规模图",
    "曲线",
    "环形",
    "用途图",
  ];
  const chartWorthyTerms = [
    "market",
    "市场规模",
    "tam",
    "sam",
    "som",
    "growth",
    "增长",
    "traction",
    "financial",
    "财务",
    "cost",
    "成本",
    "roi",
    "benchmark",
    "竞品",
    "competition",
    "chart",
    "图表",
  ];

  slides.forEach((slide, index) => {
    const label = `slide-${String(index + 1).padStart(2, "0")}`;
    const expectedPage = index + 1;

    if (!slide || typeof slide !== "object" || Array.isArray(slide)) {
      issues.push(`${label}: slide must be an object`);
      return;
    }

    if (slide.page !== expectedPage) {
      issues.push(`${label}: page must be ${expectedPage}, got ${JSON.stringify(slide.page)}`);
    }

    for (const field of ["title", "message", "layout", "visual"]) {
      if (!text(slide[field])) issues.push(`${label}: missing ${field}`);
      const length = wordLikeLength(slide[field]);
      const maxChars = OUTLINE_INTENT_TEXT_LIMITS[field];
      if (length > maxChars) {
        issues.push(
          `${label}: ${field} has ${length} characters; maximum is ${maxChars}`
        );
      }
    }

    if (!Array.isArray(slide.bullets)) {
      issues.push(`${label}: bullets must be an array with 2 or more supporting points`);
    } else if (outlineBulletCount(slide) < 2) {
      warnings.push(`${label}: bullets has fewer than 2 items; add more substance`);
    }
    const semantic = analyzeOutlineLayoutIntent(slide, outline.source_mode);
    const requestedItems = expectedVisualItemContract(slide);
    const declaredAgendaCounts = semantic && semantic.kind === "agenda"
      ? declaredAgendaItemCounts(slide)
      : [];
    if (declaredAgendaCounts.length > 1) {
      issues.push(
        `${label}: conflicting agenda item counts ${declaredAgendaCounts.join(", ")} ` +
        "across title/message/visual; use one consistent count"
      );
    }
    if (
      semantic
      && semantic.kind === "agenda"
      && requestedItems
      && Array.isArray(slide.bullets)
      && outlineBulletCount(slide) !== requestedItems.count
    ) {
      issues.push(
        `${label}: agenda requests ${requestedItems.count} visual items but bullets ` +
        `contains ${outlineBulletCount(slide)}; provide one bullet per agenda item`
      );
    }

    const structuralEvidenceExempt = isStructuralEvidenceExemptSlide(slide, index);
    const frameworkDelivery = verifiedResearch
      && verifiedResearch.deliveryMode === "framework";

    if (!Array.isArray(slide.evidence)) {
      issues.push(`${label}: evidence must be an array, use [] for non-evidence slides`);
    } else {
      if ((publicResearch || frameworkDelivery) && slide.evidence.length === 0) {
        const exactFrameworkGap = [
          slide.message,
          ...(Array.isArray(slide.bullets) ? slide.bullets : []),
        ].some(value => text(value).includes(FRAMEWORK_UNAVAILABLE_FACT_PLACEHOLDER));
        const slideNarrative = [
          slide.title,
          slide.message,
          ...(Array.isArray(slide.bullets) ? slide.bullets : []),
          slide.notes,
        ].map(text).join(" ");
        if (frameworkDelivery && structuralEvidenceExempt) {
          warnings.push(
            `${label}: structural public-research page has no factual evidence; ` +
            "cover, agenda, and section-divider pages may keep evidence empty"
          );
        } else if (frameworkDelivery && exactFrameworkGap) {
          warnings.push(
            `${label}: framework research page has no evidence because message or ` +
            "bullets contain the exact unavailable-data placeholder"
          );
        } else if (frameworkDelivery) {
          issues.push(
            `${label}: framework research page without evidence must put the exact ` +
            `${FRAMEWORK_UNAVAILABLE_FACT_PLACEHOLDER} placeholder in message or bullets`
          );
        } else if (UNAVAILABLE_FACT_PLACEHOLDER_RE.test(slideNarrative)) {
          warnings.push(
            `${label}: public-research page has no evidence because it explicitly ` +
            "marks a required fact as unavailable"
          );
        } else if (structuralEvidenceExempt) {
          warnings.push(
            `${label}: structural public-research page has no factual evidence; ` +
            "cover, agenda, and section-divider pages may keep evidence empty"
          );
        } else if (verifiedResearch) {
          issues.push(
            `${label}: entity-bound research handoff requires at least one exact ` +
            "verified_facts canonical item unless a required fact is explicitly unavailable"
          );
        } else {
          warnings.push(
            `${label}: public-authoritative research requires at least one ` +
            "claim | source | http(s) URL evidence item on every slide unless the " +
            "page explicitly marks a required fact as unavailable"
          );
        }
      }
      slide.evidence.forEach((item, evidenceIndex) => {
        const key = normalize(item);
        if (!key) return;
        const verifiedItem = verifiedResearch
          ? verifiedResearch.verified.get(text(item))
          : null;
        if (verifiedResearch && !verifiedItem) {
          issues.push(
            `${label}: evidence.${evidenceIndex} is not an exact canonical item from ` +
            "the presentation research handoff"
          );
        }
        if (verifiedItem) {
          const slideNarrative = [
            slide.title,
            slide.message,
            ...(Array.isArray(slide.bullets) ? slide.bullets : []),
          ].map(text).join(" ");
          if (!normalize(slideNarrative).includes(normalize(verifiedItem.entity))) {
            issues.push(
              `${label}: evidence.${evidenceIndex} is bound to entity ` +
              `${JSON.stringify(verifiedItem.entity)}, but the slide narrative does not ` +
              "name that entity"
            );
          }
        }
        if (wordLikeLength(item) > TRUTH_TEXT_MAX_CHARACTERS) {
          issues.push(
            `${label}: evidence.${evidenceIndex} exceeds ` +
            `${TRUTH_TEXT_MAX_CHARACTERS} characters; the scaffold imports each ` +
            "evidence entry as one truth-contract fact, so split it into separate " +
            "evidence items and keep the source URL on each item"
          );
        }
        const labels = evidenceUsage.get(key) || [];
        labels.push(label);
        evidenceUsage.set(key, labels);
        if (publicResearch && !hasHttpUrl(item)) {
          warnings.push(
            `${label}: evidence.${evidenceIndex} must include the actual http(s) ` +
            "source URL used for this public-research claim; do not relabel an " +
            "unbound search snippet as official/authoritative evidence"
          );
        }
        if (
          ASSUMPTION_EVIDENCE_RE.test(String(item || ""))
          && PRIVATE_IDENTITY_FACT_RE.test(String(item || ""))
          && !UNAVAILABLE_FACT_PLACEHOLDER_RE.test(String(item || ""))
        ) {
          warnings.push(
            `${label}: evidence.${evidenceIndex} assumes a private identity fact ` +
            "(such as a company/project name, financing round, founding/team/client " +
            "fact, award, or ranking). Assumptions are allowed only for visibly " +
            "disclosed illustrative metrics/scenarios; use 待补充 for a required " +
            "field or omit this private fact without pausing"
          );
        }
      });
      if (publicResearch) {
        const evidenceNumbers = new Set(numberTokens(slide.evidence.join(" ")));
        const claimEntries = [
          { path: "title", value: slide.title },
          { path: "message", value: slide.message },
          ...(Array.isArray(slide.bullets)
            ? slide.bullets.map((value, bulletIndex) => ({
              path: `bullets.${bulletIndex}`,
              value,
            }))
            : []),
        ];
        claimEntries.forEach(entry => {
          numberTokens(entry.value).forEach(token => {
            if (!evidenceNumbers.has(token)) {
              warnings.push(
                `${label}: ${entry.path} numeric literal ${JSON.stringify(token)} ` +
                "is not present in this page's evidence; add an exact evidence fact " +
                "or remove the unsupported/decorative number"
              );
            }
          });
        });
      }
    }

    const title = text(slide.title);
    const message = text(slide.message);
    const titleKey = normalize(title);
    const messageKey = normalize(message);

    if (wordLikeLength(title) > 42) {
      warnings.push(`${label}: title is long (${wordLikeLength(title)} chars); make it presentation-ready`);
    }
    if (wordLikeLength(message) > 120) {
      warnings.push(`${label}: message is long (${wordLikeLength(message)} chars); keep one core claim`);
    }
    if (message && message === title) {
      issues.push(`${label}: message duplicates title; use a claim, not a topic label`);
    }

    if (titleKey) {
      const firstSeen = seenTitles.get(titleKey);
      if (firstSeen) warnings.push(`${label}: title duplicates ${firstSeen}`);
      else seenTitles.set(titleKey, label);
    }
    if (messageKey) {
      const firstSeen = seenMessages.get(messageKey);
      if (firstSeen) issues.push(`${label}: message duplicates ${firstSeen}`);
      else seenMessages.set(messageKey, label);
    }

    const combined = [slide.title, slide.message, slide.layout, slide.visual, slide.notes].map(text).join(" ");
    const isDataHeavy = includesAny(combined, dataHeavyTerms);
    const chartWorthy = numberTokens(combined).length > 0
      || includesAny(combined, chartWorthyTerms);
    if (
      isDataHeavy
      && publicResearch
      && !structuralEvidenceExempt
      && !hasEvidence(slide)
    ) {
      warnings.push(`${label}: appears data/evidence-heavy but evidence is empty`);
    }
    if (
      isDataHeavy
      && !structuralEvidenceExempt
      && chartWorthy
      && !includesAny(slide.visual, dataDisplayTerms)
    ) {
      warnings.push(`${label}: appears data-heavy but visual does not name a chart/table/KPI/dashboard data display`);
    }

    const quantitativeSummary = numberTokens(message).length >= 2
      && includesAny(slide.visual, dataDisplayTerms);
    if (
      !quantitativeSummary
      && includesAny(message, [" and ", "；", ";", "、"])
      && wordLikeLength(message) > 60
    ) {
      warnings.push(`${label}: message may contain multiple claims; consider splitting`);
    }
  });

  const storyline = narrativeText(outline.storyline);
  if (slides.length >= 6 && wordLikeLength(storyline) < 20) {
    warnings.push("storyline is very short for a multi-slide deck; make the narrative arc explicit");
  }
  evidenceUsage.forEach(labels => {
    if (labels.length > 2) {
      warnings.push(
        `evidence is reused across ${labels.length} slides (${labels.join(", ")}); ` +
        "use distinct evidence or combine repetitive pages"
      );
    }
  });

  return { ok: issues.length === 0, issues, warnings, slideCount: slides.length };
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const sourceBinding = runtimeSourceBinding();
  const pageCountContract = sourceBinding.available
    ? explicitPageCountContract(sourceBinding.source_text)
    : null;
  if (pageCountContract) {
    if (!opts.minSlidesExplicit) opts.minSlides = pageCountContract.minimum;
    if (!opts.maxSlidesExplicit) opts.maxSlides = pageCountContract.maximum;
  }
  opts.verifiedResearch = readPresentationHandoff(opts.researchHandoff);
  const { outline, resolved } = readOutline(opts.outlinePath);
  const result = validate(outline, opts);
  const output = {
    ...result,
    outline: resolved,
    researchHandoff: opts.verifiedResearch ? opts.verifiedResearch.resolved : null,
    pageCountContract,
  };
  const outputText = JSON.stringify(output, null, 2);
  if (opts.report) {
    const reportPath = resolveArtifactPath(opts.report);
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, `${outputText}\n`);
  }
  console.log(outputText);
  if (!result.ok) process.exit(1);
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
