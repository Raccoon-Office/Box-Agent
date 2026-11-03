#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { resolveSourceCopy } = require("./source_copy_core.js");

const {
  getLayout,
  readJson,
  resolveArtifactPath,
  runtimeSourceBinding,
  validateAssumptionsAgainstRuntime,
  validateResearchFactsAgainstRuntime,
  validateSourceFactsAgainstRuntime,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");

const PLACEHOLDER_RE = /待补充|待确认|暂无可验证公开数据|tbd|to be confirmed|unknown|no verifiable public data/i;
const PERFORMANCE_RE = /复购|转化(?!为)|增长|提升|提高|降低|下降|减少|节省|缩短|扩大|翻倍|达到|超过|覆盖|排名|获奖|赢得|刊载|published|featured|award(?:ed)?|\bwon\b|growth|increase|decrease|conversion|retention|repeat purchase|saved|reduced|improved|grew/i;
const SOURCE_ONLY_PERFORMANCE_RE = /排名|获奖|赢得|刊载|published|featured|award(?:ed)?|\bwon\b|ranking/i;
const OBSERVED_CLAIM_CONTEXT_RE = /已|已经|当前|截至|实际|实现|结果|成果|成功|达到|超过|项目(?:交付|成果|结果)|客户案例|案例|业绩|收入|营收|复购率|转化率|留存率|同比|环比|过去|already|current|actual|achiev|result|outcome|case\s*study|delivered|revenue|retention|conversion/i;
const EXPECTED_PERFORMANCE_CONTEXT_RE = /目标|预期|预计|计划|旨在|有助于|可以|可将|可在|能够|便于|支持|帮助|期望|力争|争取|target|expected|projected|planned|aim(?:s|ed)?|can|could|may|helps?|supports?|enables?/i;
const NEGATED_PERFORMANCE_RE = /(?:不|未|无|非|并非|不是|不代表|不等于|not|no|without)[^。；;\n]{0,24}(?:排名|获奖|赢得|刊载|published|featured|award(?:ed)?|\bwon\b|ranking)/i;
const PERFORMANCE_SIGNAL_GROUPS = Object.freeze([
  /复购|留存|retention|repeat\s+purchase/i,
  /转化(?!为)|conversion/i,
  /增长|扩大|翻倍|growth|grew/i,
  /提升|提高|increase|improved/i,
  /降低|下降|减少|节省|缩短|decrease|saved|reduced/i,
  /达到|超过/i,
  /覆盖/i,
  /排名|获奖|赢得|刊载|published|featured|award(?:ed)?|\bwon\b|ranking/i,
]);
const CALENDAR_YEAR_TOKEN_RE = /^(?:19|20)\d{2}$/;
const TEAM_SIZE_RE = /(?:团队|team)[^。；;,\n]{0,28}\d+\s*(?:人|people|members?)|\d+\s*(?:人|people|members?)[^。；;,\n]{0,28}(?:团队|team)/i;
const CLIENT_SECTION_HEADING_RE = /^(?:合作)?客户(?:名单|列表|案例|概览|墙)?$|^clients?$/i;
const AWARD_SECTION_HEADING_RE = /^(?:获奖与刊载|奖项|刊载|荣誉|媒体报道|awards?|publications?|pressmentions?)$/i;
const GENERIC_RESEARCH_AWARD_HEADING_RE = /^(?:(?:已获|个人|主要|外部|国际|公开)?(?:奖项|荣誉|认可))(?:与|和|及|·|\/)?(?:可信)?(?:总结|结论|概览|认可)?$/i;
const GENERIC_RESEARCH_AWARD_SYNTHESIS_RE = /^(?:以|由|基于).{0,18}(?:纪录|事实|冠军|经历).{0,24}(?:已获)?(?:奖项|荣誉|认可).{0,28}(?:支撑|说明|体现|共同验证|构成).{0,20}(?:定位|形象|结论|人物|代表)/i;
const RESEARCH_AWARD_EVIDENCE_RE = /(?:kopa|goldenboy|trophy|awards?|获评|获得|荣获|最佳(?:年轻)?球员|奖项|荣誉)/i;
const RESEARCH_ACHIEVEMENT_RE = /赢得|夺得|夺冠|捧杯|问鼎|获胜|击败|战胜|斩获|摘得|荣获|获评|评为|获得|\bwon\b|\bwin(?:s|ning)?\b|\bdefeat(?:ed|s|ing)?\b/gi;
const RESEARCH_ACHIEVEMENT_SUBJECT_RE = /(?:^|[。；;！？!?\n])\s*([^，,。；;！？!?|]{2,24}?)(?:再次|再度|重新|成功)?(?:赢得|夺得|夺冠|捧杯|问鼎|获胜|击败|战胜|斩获|摘得|荣获|获评|评为|获得)/u;
const RESEARCH_OBJECT_PREFIX_RE = /(?:击败|战胜|淘汰|力克|负于|不敌)\s*$/u;
const RESEARCH_SEMANTIC_STOPWORDS = new Set([
  "报道",
  "显示",
  "指出",
  "认为",
  "确认",
  "资料",
  "结果",
  "摘要",
  "专题",
  "已经",
  "当前",
  "本届",
  "来到",
]);
const TEAM_SECTION_HEADING_RE = /^(?:核心|管理|创始)?团队(?:成员|阵容|介绍)?$|^(?:team|leadership|team-members?)$/i;
const TEAM_CONTEXT_RE = /团队|\bteam\b/i;
const TEAM_CAPABILITY_CONTEXT_RE = /能力结构|复合能力|角色结构|职能结构|姓名.{0,8}未提供|不虚构.{0,8}(?:个人|姓名|履历)|capability|role\s*mix|functional/i;
const TEAM_CAPABILITY_TITLE_RE = /算法|工程|工艺|现场|制造|企业服务|产品|销售|交付|技术|研发|设计|策略|运营|市场|财务|合规|能力|职能|角色|\bai\b|\bsaas\b|\bgtm\b/i;
const PROCESS_CONTEXT_RE = /流程|步骤|阶段|行动|措施|举措|工作方法|process|workflow|method|actions?/i;
const GENERATED_PROJECT_SRC_RE = /^(?:\.\/)?assets\/generated\//i;
const CONCEPT_MEDIA_LABEL_RE = /ai\s*概念|概念视觉|概念图|示意|占位|非真实|concept|illustrative|placeholder/i;
const ASSUMPTION_DISCLOSURE_RE = /假设|示意|模拟数据|仅作演示|非真实数据|assum(?:e|ed|ption)|illustrative|hypothetical|sample data/i;
const DOMAIN_SUFFIX_RE = /(?:领域|行业|客户)$/i;
const FUTURE_CONTEXT_RE = /明年|展望|下一年|next year|future|roadmap/i;
const PROJECT_CASE_CONTEXT_RE = /项目|案例|作品集|客户案例|case\s*study|portfolio|project/i;
const PRESENTATION_DIRECTIVE_RE = /(?:必须|需要|要求|默认交付|全部为可编辑|不要|不需要|不联网|不生图|不补充|直接完成)[^。；;\n]{0,72}(?:可编辑|html|pptx?|图表|表格|流程图|kpi|图片|生图|形状|版式|布局|联网|事实(?:来源|校验)?|交付)|^(?:请)?用[^。；;\n]{0,72}(?:表达|呈现|展示)$/i;
const CHINESE_NUMERALS = new Map([
  [1, "一"],
  [2, "二"],
  [3, "三"],
  [4, "四"],
  [5, "五"],
  [6, "六"],
  [7, "七"],
  [8, "八"],
  [9, "九"],
  [10, "十"],
]);

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") {
    console.log("Usage: validate_deck_truth.js deck.json [--report qa/truth_check.json]");
    process.exit(argv[0] ? 0 : 2);
  }
  const opts = { deck: argv[0], report: null };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--report" && value) {
      opts.report = value;
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return opts;
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "")
    .trim();
}

function numberTokens(value) {
  const text = String(value || "")
    // Preserve CJK prose separators before NFKC turns them into ASCII commas,
    // otherwise "13，2014" is misread as the grouped number "13,201" plus "4".
    .replace(/[，、]/gu, " ")
    .normalize("NFKC")
    // A dotted calendar date is three integers, not two decimal claims.
    // Keep actual decimals such as 4.2/5 unchanged.
    .replace(/\b((?:19|20)\d{2})\.(\d{1,2})\.(\d{1,2})\b/g, "$1 $2 $3");
  return (text.match(/\d+(?:,\d{3})*(?:\.\d+)?%?/g) || [])
    .map(token => {
      const percent = token.endsWith("%");
      const bare = token.replace(/,/g, "").replace(/%$/, "");
      const numeric = Number(bare);
      return `${Number.isFinite(numeric) ? numeric : bare}${percent ? "%" : ""}`;
    });
}

function isNumberBackedForSlide(token, statements, slide, fieldPath = "") {
  const props = slide && slide.props ? slide.props : {};
  const suffix = String(props.value_suffix || "").trim();
  const categories = Array.isArray(props.categories) ? props.categories : [];
  const valueIndexMatch = String(fieldPath).match(/\.series\.\d+\.values\.(\d+)$/);
  const valueIndex = valueIndexMatch ? Number(valueIndexMatch[1]) : -1;
  const localCategory = valueIndex >= 0 ? String(categories[valueIndex] || "") : "";
  const chartValue = slide?.layout_id === "chart-data-v1" && valueIndex >= 0;
  const barValue = slide?.layout_id === "chart-bar-v1"
    && /\.props\.items\.\d+\.value$/.test(fieldPath);
  const percentContext = chartValue && (suffix === "%" || suffix === "％"
    || /%|％|百分|(?:比率|率|占比|份额|percentage|percent|rate|ratio|share)/i.test(localCategory))
    || barValue && /[%％]/.test(String(props.series_label || ""));
  // A chart value inherits its displayed unit; prose and unrelated numbers do not.
  const claim = percentContext && !token.endsWith("%") ? `${token}%` : token;
  return isNumberSourceBacked(claim, statements);
}

function isVisualEnumEntry(entry, slide) {
  const parts = String(entry.path).split(".props.")[1]?.split(".") || [];
  let field = { shape: getLayout(slide?.layout_id)?.fields || {} };
  for (const part of parts) field = /^\d+$/.test(part)
    ? { shape: field?.itemShape } : field?.shape?.[part];
  return field?.type === "enum";
}

function slideDisclosesAssumptions(slide) {
  return collectTextEntries(slide && slide.props ? slide.props : {}, "props")
    .some(entry => ASSUMPTION_DISCLOSURE_RE.test(entry.text));
}

function isChartAssumptionDataEntry(entry, slide) {
  if (!slide || !entry) return false;
  if (slide.layout_id === "chart-data-v1") {
    return /\.props\.(?:categories\.\d+|series\.\d+\.(?:name|values\.\d+)|highlights\.\d+\.(?:value|label))$/
      .test(entry.path);
  }
  if (slide.layout_id === "chart-bar-v1") {
    return /\.props\.(?:series_label|items\.\d+\.(?:label|value))$/.test(entry.path);
  }
  return false;
}

function isMediaObject(value) {
  return isPlainObject(value) && typeof value.src === "string";
}

function collectTextEntries(value, fieldPath, inheritedPlaceholder = false, entries = []) {
  if (typeof value === "string") {
    entries.push({ path: fieldPath, text: value, placeholderContext: inheritedPlaceholder });
    return entries;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => {
      collectTextEntries(item, `${fieldPath}.${index}`, inheritedPlaceholder, entries);
    });
    return entries;
  }
  if (!isPlainObject(value) || isMediaObject(value)) return entries;
  const localPlaceholder = inheritedPlaceholder || Object.values(value)
    .some(item => typeof item === "string" && PLACEHOLDER_RE.test(item));
  Object.entries(value).forEach(([key, item]) => {
    collectTextEntries(item, `${fieldPath}.${key}`, localPlaceholder, entries);
  });
  return entries;
}

function isSourceBacked(text, normalizedFacts) {
  const normalized = normalizeText(text);
  if (!normalized) return true;
  if (normalizedFacts.some(fact => fact.includes(normalized))) return true;
  const clauses = String(text || "")
    .split(/[。！？!?；;\n]+/u)
    .map(clause => normalizeText(clause))
    .filter(Boolean);
  return clauses.length > 1 && clauses.every(clause =>
    normalizedFacts.some(fact => fact.includes(clause))
  );
}

function researchClaimCores(text) {
  return String(text || "")
    .split(/[。！？!?；;｜|\n:：]+/u)
    .flatMap(clause => {
      const normalized = normalizeText(clause);
      if (!normalized) return [];
      const withoutAttribution = normalized.replace(
        /^(?:据)?(?:(?:uefa|fifa|rfef|fcbarcelona|巴萨官网|欧足联|西班牙足协)(?:官网|官方)?(?:报道|资料|结果|摘要|专题)?|(?:官网|官方)(?:报道|资料|结果|摘要|专题)|(?:报道|资料|结果|摘要|专题))(?:显示|指出|称|确认)?/i,
        ""
      );
      const withoutSubject = withoutAttribution.replace(
        /^(?:(?:拉明)?亚马尔|lamineyamal|他|其)(?:被)?/i,
        ""
      );
      const withoutReportingVerb = withoutSubject.replace(
        /^(?:获评|评为|获得|荣获)/i,
        ""
      );
      const withoutLabelSuffix = withoutSubject.replace(
        /(?:成员|节点|故事|专题|叙事|表现|贡献|旁证|依据)$/i,
        ""
      );
      return [...new Set([
        normalized,
        withoutAttribution,
        withoutSubject,
        withoutReportingVerb,
        withoutLabelSuffix,
      ].filter(core => core.length >= 4))];
    });
}

function isResearchBacked(text, researchFacts) {
  const normalizedFacts = researchFacts.map(normalizeText).filter(Boolean);
  if (isSourceBacked(text, normalizedFacts)) return true;
  const normalized = normalizeText(text);
  const isGenericAwardSummary = (
    GENERIC_RESEARCH_AWARD_HEADING_RE.test(normalized)
    || GENERIC_RESEARCH_AWARD_SYNTHESIS_RE.test(normalized)
  );
  if (
    isGenericAwardSummary
    && !numberTokens(text).length
    && normalizedFacts.some(fact => RESEARCH_AWARD_EVIDENCE_RE.test(fact))
  ) return true;
  if (researchClaimCores(text).some(core =>
    normalizedFacts.some(fact => fact.includes(core))
  )) return true;
  return researchFacts.some(fact => isResearchSemanticParaphrase(text, fact));
}

const CANONICAL_RESEARCH_SOURCE_TYPES = new Set([
  "first_party",
  "government",
  "regulator",
  "filing",
  "standards_body",
  "academic",
  "reputable_media",
  "secondary",
  "user_input",
]);

function parseCanonicalResearchFact(value) {
  const raw = String(value || "").trim();
  const parts = raw.split(/\s+\|\s+/u);
  if (parts.length !== 4) return null;
  const [entity, claim, sourceType, sourceUrl] = parts.map(part => part.trim());
  if (
    !entity
    || !claim
    || !CANONICAL_RESEARCH_SOURCE_TYPES.has(sourceType)
    || !/^https?:\/\/\S+$/i.test(sourceUrl)
  ) return null;
  return {
    raw,
    entity,
    entityKey: normalizeText(entity),
    claim,
    sourceType,
    sourceUrl,
  };
}

function researchSemanticTokens(text) {
  const claim = String(text || "")
    .split(/\s+\|\s+/u, 1)[0]
    .normalize("NFKC")
    .replace(RESEARCH_ACHIEVEMENT_RE, " achievement ");
  const segments = typeof Intl !== "undefined" && typeof Intl.Segmenter === "function"
    ? [...new Intl.Segmenter("zh-CN", { granularity: "word" }).segment(claim)]
      .filter(segment => segment.isWordLike)
      .map(segment => segment.segment)
    : claim.match(/[\p{L}\p{N}·_-]+/gu) || [];
  return new Set(segments
    .map(token => normalizeText(token))
    .filter(token => {
      if (!token || /^\d+(?:\.\d+)?%?$/.test(token)) return false;
      if (RESEARCH_SEMANTIC_STOPWORDS.has(token)) return false;
      return Array.from(token).length >= 2;
    }));
}

function researchAchievementSubject(text) {
  const match = String(text || "").match(RESEARCH_ACHIEVEMENT_SUBJECT_RE);
  return match ? String(match[1] || "").trim() : "";
}

function factSupportsAchievementSubject(subject, fact) {
  if (!subject) return true;
  const source = String(fact || "").split(/\s+\|\s+/u, 1)[0];
  let cursor = source.indexOf(subject);
  while (cursor >= 0) {
    const prefix = source.slice(Math.max(0, cursor - 8), cursor);
    const suffix = source.slice(cursor + subject.length, cursor + subject.length + 28);
    if (!RESEARCH_OBJECT_PREFIX_RE.test(prefix) && RESEARCH_ACHIEVEMENT_RE.test(suffix)) {
      RESEARCH_ACHIEVEMENT_RE.lastIndex = 0;
      return true;
    }
    RESEARCH_ACHIEVEMENT_RE.lastIndex = 0;
    cursor = source.indexOf(subject, cursor + subject.length);
  }
  return false;
}

function isResearchSemanticParaphrase(text, fact) {
  const candidateTokens = researchSemanticTokens(text);
  if (candidateTokens.size < 3) return false;
  const factTokens = researchSemanticTokens(fact);
  const shared = [...candidateTokens].filter(token => factTokens.has(token));
  if (shared.length < 3 || shared.length / candidateTokens.size < 0.8) return false;
  if (numberTokens(text).some(token => !isNumberSourceBacked(token, [fact]))) return false;
  return factSupportsAchievementSubject(researchAchievementSubject(text), fact);
}

function isNumberSourceBacked(token, sourceFacts) {
  if (sourceFacts.flatMap(numberTokens).includes(token)) return true;
  if (token.endsWith("%")) return false;
  const numeric = Number(token);
  const chinese = CHINESE_NUMERALS.get(numeric);
  if (!chinese) return false;
  const source = sourceFacts.join(" ");
  return new RegExp(`(?:第${chinese}|${chinese}(?:个|项|次|年|条|页|种|类|大|位|名|人|家|领域))`).test(source);
}

function isHonestPlaceholder(text) {
  return !String(text || "").trim() || PLACEHOLDER_RE.test(String(text));
}

function stripPresentationDirectives(value) {
  if (typeof value !== "string" || !value.trim()) return value;
  const segments = value
    .split(/[｜|。；;\n]+/u)
    .map(segment => segment.trim())
    .filter(Boolean);
  const kept = segments.filter(segment => !PRESENTATION_DIRECTIVE_RE.test(segment));
  if (kept.length === segments.length) return value;
  return kept.join("｜");
}

function isDomainCategoryBacked(text, normalizedSources) {
  const withoutSuffix = String(text || "").trim().replace(DOMAIN_SUFFIX_RE, "");
  return Boolean(withoutSuffix) && isSourceBacked(withoutSuffix, normalizedSources);
}

function isTeamNameSourceBacked(text, sourceText) {
  const normalized = normalizeText(text);
  if (!normalized) return true;
  return String(sourceText || "")
    .split(/[。！？!?；;\n]+/)
    .some(clause => TEAM_CONTEXT_RE.test(clause) && normalizeText(clause).includes(normalized));
}

function isProcessTextSourceBacked(text, sourceText) {
  const normalized = normalizeText(text);
  if (!normalized) return true;
  return String(sourceText || "")
    .split(/[。！？!?；;\n]+/)
    .some(clause => PROCESS_CONTEXT_RE.test(clause) && normalizeText(clause).includes(normalized));
}

function isShortSourceConceptBacked(text, sourceText) {
  const needle = normalizeText(text);
  const haystack = normalizeText(sourceText);
  if (!needle || needle.length > 12) return false;
  let cursor = 0;
  for (const character of haystack) {
    if (character === needle[cursor]) cursor += 1;
    if (cursor === needle.length) return true;
  }
  return false;
}

function slidePropsPath(slide, index) {
  const slideId = slide && typeof slide.id === "string" && slide.id.trim()
    ? slide.id.trim()
    : String(index);
  return `slides.${slideId}.props`;
}

function sectionHeadingLabels(props) {
  return [props && props.eyebrow, props && props.title]
    .filter(value => typeof value === "string" && value.trim())
    .flatMap(value => {
      const firstLabel = value.split(/[｜|/:：·—-]/, 1)[0];
      return [normalizeText(value), normalizeText(firstLabel)];
    })
    .filter(Boolean);
}

function detectNamedSectionKind(props, namedItemTitles) {
  const labels = sectionHeadingLabels(props);
  if (labels.some(label => CLIENT_SECTION_HEADING_RE.test(label))) return "client";
  if (labels.some(label => AWARD_SECTION_HEADING_RE.test(label))) {
    return "award/publication";
  }
  if (
    namedItemTitles.length
    && labels.some(label => TEAM_SECTION_HEADING_RE.test(label))
  ) return "team-member";
  return null;
}

function isTeamCapabilityContext(sectionText) {
  return TEAM_CAPABILITY_CONTEXT_RE.test(String(sectionText || ""));
}

function isTeamCapabilityTitle(text) {
  return TEAM_CAPABILITY_TITLE_RE.test(String(text || ""));
}

function isNegatedPerformanceClaim(text) {
  return NEGATED_PERFORMANCE_RE.test(String(text || ""));
}

function requiresPerformanceBacking(text) {
  if (!PERFORMANCE_RE.test(text) || isNegatedPerformanceClaim(text)) return false;
  if (SOURCE_ONLY_PERFORMANCE_RE.test(text)) return true;
  if (numberTokens(text).length > 0) return true;
  if (OBSERVED_CLAIM_CONTEXT_RE.test(text)) return true;
  if (EXPECTED_PERFORMANCE_CONTEXT_RE.test(text)) return false;
  // Qualitative solution value is allowed by default. Without an observed-result
  // marker or a number, wording such as "降低风险" is a proposed benefit,
  // not evidence that a measured outcome has already occurred.
  return false;
}

function performanceSignalIndexes(text) {
  return PERFORMANCE_SIGNAL_GROUPS
    .map((pattern, index) => pattern.test(String(text || "")) ? index : -1)
    .filter(index => index >= 0);
}

function isSourceSemanticPerformanceParaphrase(text, sourceFacts) {
  const candidateSignals = performanceSignalIndexes(text);
  if (!candidateSignals.length) return false;
  const candidateTokens = researchSemanticTokens(text);
  if (candidateTokens.size < 3) return false;
  return sourceFacts.some(fact => {
    if (!requiresPerformanceBacking(fact)) return false;
    const factSignals = new Set(performanceSignalIndexes(fact));
    if (!candidateSignals.every(signal => factSignals.has(signal))) return false;
    if (numberTokens(text).some(token => !isNumberSourceBacked(token, [fact]))) {
      return false;
    }
    const factTokens = researchSemanticTokens(fact);
    if (factTokens.size < 3) return false;
    const shared = [...factTokens].filter(token => candidateTokens.has(token));
    return shared.length >= 3 && shared.length / factTokens.size >= 0.7;
  });
}

function requireStrictSourceBacked(value, fieldPath, normalizedSources, issues) {
  if (typeof value !== "string" || isHonestPlaceholder(value)) return;
  if (!isSourceBacked(value, normalizedSources)) {
    issues.push(
      `${fieldPath}: strict source-only field is not source-backed; ` +
      "use exact source wording, 待补充, or omit it"
    );
  }
}

function validateStrictNarrativeFields(
  slide,
  basePath,
  normalizedSources,
  issues,
  sourceText = ""
) {
  const props = slide.props;
  if (slide.layout_id === "statement-focus-v1") {
    requireStrictSourceBacked(props.statement, `${basePath}.statement`, normalizedSources, issues);
    requireStrictSourceBacked(props.support, `${basePath}.support`, normalizedSources, issues);
    (Array.isArray(props.proofs) ? props.proofs : []).forEach((proof, proofIndex) => {
      requireStrictSourceBacked(
        proof && proof.value,
        `${basePath}.proofs.${proofIndex}.value`,
        normalizedSources,
        issues
      );
    });
  } else if (slide.layout_id === "kpi-grid-v1") {
    (Array.isArray(props.items) ? props.items : []).forEach((item, itemIndex) => {
      requireStrictSourceBacked(
        item && item.detail,
        `${basePath}.items.${itemIndex}.detail`,
        normalizedSources,
        issues
      );
    });
  } else if (slide.layout_id === "table-data-v1") {
    requireStrictSourceBacked(
      props.subtitle,
      `${basePath}.subtitle`,
      normalizedSources,
      issues
    );
    (Array.isArray(props.rows) ? props.rows : []).forEach((row, rowIndex) => {
      (Array.isArray(row) ? row : []).forEach((cell, cellIndex) => {
        requireStrictSourceBacked(
          cell,
          `${basePath}.rows.${rowIndex}.${cellIndex}`,
          normalizedSources,
          issues
        );
      });
    });
  } else if (slide.layout_id === "closing-next-steps-v1") {
    requireStrictSourceBacked(
      props.subtitle,
      `${basePath}.subtitle`,
      normalizedSources,
      issues
    );
    (Array.isArray(props.actions) ? props.actions : []).forEach((action, actionIndex) => {
      ["label", "detail"].forEach(key => {
        requireStrictSourceBacked(
          action && action[key],
          `${basePath}.actions.${actionIndex}.${key}`,
          normalizedSources,
          issues
        );
      });
    });
    requireStrictSourceBacked(
      props.contact,
      `${basePath}.contact`,
      normalizedSources,
      issues
    );
  } else if (
    slide.layout_id === "project-case-study-v1"
    && isProjectCaseContext(slide)
  ) {
    requireStrictSourceBacked(
      props.positioning,
      `${basePath}.positioning`,
      normalizedSources,
      issues
    );
    requireStrictSourceBacked(
      props.caption,
      `${basePath}.caption`,
      normalizedSources,
      issues
    );
    const image = props.image;
    if (isMediaObject(image) && GENERATED_PROJECT_SRC_RE.test(image.src.trim())) {
      if (image.origin !== "generated") {
        issues.push(
          `${basePath}.image.origin: generated project media must declare origin \"generated\"`
        );
      }
      if (!CONCEPT_MEDIA_LABEL_RE.test(String(image.alt || ""))) {
        issues.push(
          `${basePath}.image.alt: generated project media must be labeled as ` +
          "AI concept/placeholder so it cannot be mistaken for a real project image"
        );
      }
    }
  } else if (slide.layout_id === "timeline-horizontal-v1") {
    requireStrictSectionTitle(
      props.title,
      `${basePath}.title`,
      "process",
      normalizedSources,
      issues
    );
    requireStrictSourceBacked(
      props.subtitle,
      `${basePath}.subtitle`,
      normalizedSources,
      issues
    );
    (Array.isArray(props.steps) ? props.steps : []).forEach((item, itemIndex) => {
      ["title", "body"].forEach(key => {
        const value = item && item[key];
        if (
          typeof value === "string"
          && !isHonestPlaceholder(value)
          && !isProcessTextSourceBacked(value, sourceText)
        ) {
          issues.push(
            `${basePath}.steps.${itemIndex}.${key}: strict process field is not ` +
            "source-backed by a process clause; use exact source wording, 待补充, or omit it"
          );
        }
      });
    });
  } else if (slide.layout_id === "cards-grid-v1") {
    (Array.isArray(props.items) ? props.items : []).forEach((item, itemIndex) => {
      requireStrictSourceBacked(
        item && item.body,
        `${basePath}.items.${itemIndex}.body`,
        normalizedSources,
        issues
      );
    });
  }
}

function expectedEyebrowOrdinal(slides, slideIndex) {
  const firstSlide = Array.isArray(slides) ? slides[0] : null;
  const startsWithCover = Boolean(
    firstSlide
    && typeof firstSlide.layout_id === "string"
    && /^cover-/i.test(firstSlide.layout_id)
  );
  return startsWithCover ? slideIndex : slideIndex + 1;
}

function isStructuralNumber(
  entry,
  token,
  slide,
  slideCount = 0,
  eyebrowOrdinal = null
) {
  const numeric = Number(token.replace(/%$/, ""));
  if (!Number.isInteger(numeric) || numeric < 1 || numeric > 40) return false;
  const entryText = String(entry && entry.text ? entry.text : "").normalize("NFKC");
  const eyebrowMatch = entryText.match(
    /^\s*(0\d{1,2})(?=$|[\s｜|·:：—–-])/u
  );
  if (
    eyebrowMatch
    && Number.isInteger(eyebrowOrdinal)
    && eyebrowOrdinal > 0
    && /\.props\.eyebrow$/.test(entry.path)
    && numeric === Number(eyebrowMatch[1])
    && numeric === eyebrowOrdinal
  ) {
    return true;
  }
  const itemValueMatch = String(entry.path).match(/\.props\.items\.(\d+)\.value$/);
  const itemOrdinalMatch = entryText.match(/^\s*(0\d{1,2})\s*$/u);
  if (
    slide
    && slide.layout_id === "kpi-grid-v1"
    && itemValueMatch
    && itemOrdinalMatch
    && numeric === Number(itemOrdinalMatch[1])
    && numeric === Number(itemValueMatch[1]) + 1
  ) {
    return true;
  }
  if (
    numeric === slideCount
    && /\.props\.meta$/.test(entry.path)
    && /(?:页|slides?)/i.test(entry.text)
  ) {
    return true;
  }
  if (slide && slide.layout_id === "section-marker-v1" && /\.number$/.test(entry.path)) {
    return true;
  }
  if (/\.(?:kicker|phase)$/.test(entry.path)) return true;
  return /项目|project|阶段|phase|步骤|step/i.test(entry.text);
}

function isDiagramStructuralEntry(entry, slide) {
  if (!slide || slide.layout_id !== "technical-diagram-v1") return false;
  return /\.props\.(?:diagram_kind|direction)$/.test(entry.path)
    || /\.props\.nodes\.\d+\.(?:id|kind)$/.test(entry.path)
    || /\.props\.edges\.\d+\.(?:id|source|target)$/.test(entry.path);
}

function isGenericProjectTitle(text) {
  const compact = String(text || "").trim();
  return /^(?:精选\s*)?项目\s*[一二三四五六七八九十\dA-Da-d]*\s*(?:[·:：—-]\s*)?(?:品牌|产品|空间|文化|案例)?$/i.test(compact)
    || /^(?:selected\s+)?project\s*\d*\s*(?:[·:：—-]\s*)?(?:brand|product|space|culture|case)?$/i.test(compact);
}

function isProjectCaseContext(slide) {
  if (!slide || slide.layout_id !== "project-case-study-v1") return false;
  const props = slide.props || {};
  return PROJECT_CASE_CONTEXT_RE.test([
    props.eyebrow,
    props.title,
    props.positioning,
    props.caption,
  ].filter(Boolean).join(" "));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function sourceOverlapScore(candidate, fact) {
  const left = normalizeText(candidate);
  const right = normalizeText(fact);
  if (!left || !right) return 0;
  if (left.includes(right) || right.includes(left)) return Math.min(left.length, right.length) + 100;
  const leftPairs = new Set();
  const rightPairs = new Set();
  for (let index = 0; index < left.length - 1; index += 1) leftPairs.add(left.slice(index, index + 2));
  for (let index = 0; index < right.length - 1; index += 1) rightPairs.add(right.slice(index, index + 2));
  let score = 0;
  leftPairs.forEach(pair => {
    if (rightPairs.has(pair)) score += 1;
  });
  return score;
}

function bestSourceFact(candidate, sourceFacts, maxChars, excluded = new Set()) {
  const eligible = sourceFactFragments(sourceFacts).filter(fact =>
    !excluded.has(fact) && Array.from(String(fact)).length <= maxChars
  ).map(fact => ({ fact, score: sourceOverlapScore(candidate, fact) }))
    .filter(item => item.score > 0)
    .sort((left, right) => right.score - left.score || left.fact.length - right.fact.length);
  if (!eligible.length) return null;
  return eligible[0].fact;
}

function sourceFactFragments(sourceFacts) {
  return [...new Set(sourceFacts.flatMap(fact => {
    const text = String(fact || "").trim();
    if (!text) return [];
    const clauses = [
      text,
      ...text.split(/[。；;！？!?\n]/u).map(fragment => fragment.trim()).filter(Boolean),
    ];
    return clauses.flatMap(fragment => {
      const labeled = fragment.match(/^[^：:]{1,40}[：:](.+)$/u);
      const fragments = labeled && labeled[1].trim()
        ? [fragment, labeled[1].trim()]
        : [fragment];
      return fragments.flatMap(candidate => [
        candidate,
        ...candidate.split(/[、，,]/u).map(item => item.trim()).filter(Boolean),
      ]);
    });
  }))];
}

function bestSourceFragment(candidate, sourceFacts, maxChars) {
  const eligible = sourceFactFragments(sourceFacts)
    .filter(fact => Array.from(String(fact)).length <= maxChars);
  if (!eligible.length) return null;
  return eligible
    .map(fact => ({ fact, score: sourceOverlapScore(candidate, fact) }))
    .sort((left, right) => right.score - left.score || left.fact.length - right.fact.length)[0].fact;
}

function bestOverlappingSourceFragment(candidate, sourceFacts, maxChars) {
  const eligible = sourceFactFragments(sourceFacts)
    .filter(fact => Array.from(String(fact)).length <= maxChars)
    .map(fact => ({ fact, score: sourceOverlapScore(candidate, fact) }))
    .filter(item => item.score > 0)
    .sort((left, right) => right.score - left.score || left.fact.length - right.fact.length);
  return eligible.length ? eligible[0].fact : null;
}

function strictTextOrFallback(value, normalizedSources, sourceFacts, maxChars, excluded) {
  if (isHonestPlaceholder(value) || isSourceBacked(value, normalizedSources)) return value;
  return bestSourceFact(value, sourceFacts, maxChars, excluded) || "待补充";
}

function isRuntimeSourceCopy(text, runtimeSource) {
  const normalized = normalizeText(text);
  // A complete contextual phrase can be backed by the original request. A
  // bare number cannot borrow a page count or another unrelated input value.
  return (normalized.match(/\p{L}/gu) || []).length >= 2
    && normalizeText(runtimeSource).includes(normalized);
}

function sanitizeUnsupportedClaims(
  value,
  fieldPath,
  sourceFacts,
  normalizedSources,
  changes,
  slide = null,
  structuralContext = {}
) {
  if (typeof value === "string") {
    if (isHonestPlaceholder(value)) return value;
    const entry = { path: fieldPath, text: value };
    if (isVisualEnumEntry(entry, slide)) return value;
    const hasUnsupportedNumber = !isRuntimeSourceCopy(value, structuralContext.runtimeSource)
      && numberTokens(value).some(token =>
      !isNumberBackedForSlide(token, sourceFacts, slide, fieldPath)
      && !isDiagramStructuralEntry(entry, slide)
      && !isStructuralNumber(
        entry,
        token,
        slide,
        structuralContext.slideCount,
        structuralContext.eyebrowOrdinal
      )
    );
    const shortConceptBacked = /\.(?:label|title|eyebrow)$/.test(fieldPath)
      && isShortSourceConceptBacked(value, normalizedSources.join(" "));
    const hasUnsupportedPerformance = requiresPerformanceBacking(value)
      && !isSourceBacked(value, normalizedSources)
      && !shortConceptBacked;
    const hasUnsupportedTeamSize = TEAM_SIZE_RE.test(value)
      && !isSourceBacked(value, normalizedSources);
    if (hasUnsupportedNumber || hasUnsupportedPerformance || hasUnsupportedTeamSize) {
      changes.push(`${fieldPath}: replaced unsupported claim with 待补充`);
      return "待补充";
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => sanitizeUnsupportedClaims(
      item,
      `${fieldPath}.${index}`,
      sourceFacts,
      normalizedSources,
      changes,
      slide,
      structuralContext
    ));
  }
  if (!isPlainObject(value) || isMediaObject(value)) return value;
  const result = {};
  Object.entries(value).forEach(([key, item]) => {
    result[key] = sanitizeUnsupportedClaims(
      item,
      `${fieldPath}.${key}`,
      sourceFacts,
      normalizedSources,
      changes,
      slide,
      structuralContext
    );
  });
  return result;
}

function ensureGeneratedProjectMedia(image, fieldPath, changes) {
  if (!isMediaObject(image) || !GENERATED_PROJECT_SRC_RE.test(image.src.trim())) return image;
  const normalized = { ...image };
  if (normalized.origin !== "generated") {
    normalized.origin = "generated";
    changes.push(`${fieldPath}.origin: declared generated provenance`);
  }
  if (!CONCEPT_MEDIA_LABEL_RE.test(String(normalized.alt || ""))) {
    normalized.alt = "AI 概念视觉，实际项目图待补充";
    changes.push(`${fieldPath}.alt: labeled generated media as a concept placeholder`);
  }
  return normalized;
}

function projectHeadingFromSource(props, sourceText) {
  if (!/精选项目/i.test(sourceText)) return null;
  const candidate = [props.eyebrow, props.title, props.caption, props.image && props.image.src]
    .filter(Boolean)
    .join(" ");
  const categories = [
    { label: "品牌", pattern: /品牌|brand/i },
    { label: "产品", pattern: /产品|product/i },
    { label: "空间", pattern: /空间|space|spatial/i },
    { label: "文化", pattern: /文化|culture|cultural/i },
  ];
  const match = categories.find(category =>
    category.pattern.test(candidate) && category.pattern.test(sourceText)
  );
  return match ? `精选项目 · ${match.label}` : null;
}

function sourceBackedClientDomains(sourceText) {
  return [
    { title: "SaaS 领域", pattern: /\bsaas\b/i },
    { title: "消费品领域", pattern: /消费品/ },
    { title: "文化机构领域", pattern: /文化机构/ },
  ].filter(item => item.pattern.test(sourceText)).map(item => item.title);
}

function sourceBackedSectionTitle(sectionKind, sourceText) {
  const sections = {
    client: { title: "合作客户", pattern: /合作客户/ },
    "award/publication": { title: "获奖与刊载", pattern: /获奖与刊载/ },
    "team-member": { title: "团队", pattern: /团队/ },
    "future-plan": { title: "明年", pattern: /明年/ },
    process: { title: "流程", pattern: /流程/ },
  };
  const section = sections[sectionKind];
  return section && section.pattern.test(String(sourceText || ""))
    ? section.title
    : null;
}

function isNeutralSectionTitle(sectionKind, value) {
  const normalized = normalizeText(value);
  const patterns = {
    client: /^(?:合作)?客户(?:列表|概览)?$/,
    "award/publication": /^(?:获奖与刊载|奖项|刊载|荣誉)$/,
    "team-member": /^(?:团队|团队成员|team)$/,
    "future-plan": /^(?:明年|展望|下一年|future|nextyear)$/,
    process: /^(?:我们的)?(?:工作)?流程$|^输入流程标题$|^(?:process|workflow)$/,
  };
  return Boolean(normalized && patterns[sectionKind] && patterns[sectionKind].test(normalized));
}

function requireStrictSectionTitle(
  value,
  fieldPath,
  sectionKind,
  normalizedSources,
  issues
) {
  if (
    typeof value !== "string"
    || isHonestPlaceholder(value)
    || isNeutralSectionTitle(sectionKind, value)
    || isSourceBacked(value, normalizedSources)
  ) return;
  issues.push(
    `${fieldPath}: strict source-only section title is not source-backed or neutral; ` +
    "use a neutral section label, exact source wording, 待补充, or omit it"
  );
}

function sanitizeStrictSourceDeck(deck) {
  const sanitized = clone(deck);
  const changes = [];
  const truth = sanitized.truth_contract;
  const binding = runtimeSourceBinding();
  const sourceFacts = truth && Array.isArray(truth.source_facts)
    ? truth.source_facts
    : [];
  const researchFacts = truth && Array.isArray(truth.research_facts)
    ? truth.research_facts
    : [];
  // Public-research decks should be checked for unsupported numbers and
  // consequential claims, but they are not strict copy-from-user-source decks.
  if (
    !binding.strict
    || !truth
    || truth.mode !== "source_bound"
    || sourceFacts.length === 0
  ) {
    return { deck: sanitized, changes };
  }
  const claimFacts = [...sourceFacts, ...researchFacts];
  const normalizedSources = [
    ...claimFacts.map(normalizeText),
    normalizeText(binding.source_text),
  ].filter(Boolean);

  sanitized.slides.forEach((slide, slideIndex) => {
    const basePath = slidePropsPath(slide, slideIndex);
    function restoreCopy(value, fieldPath, contract) {
      if (typeof value === "string") {
        if (contract?.type !== "text" || isDiagramStructuralEntry({ path: fieldPath, text: value }, slide)) return value;
        if (isSourceBacked(value, normalizedSources)) return value;
        const original = resolveSourceCopy(value, binding.source_text);
        if (original) changes.push(`${fieldPath}: restored exact runtime source wording`);
        return original || value;
      }
      if (Array.isArray(value)) return value.map((item, index) => restoreCopy(item, `${fieldPath}.${index}`, contract?.itemShape));
      if (!isPlainObject(value) || isMediaObject(value)) return value;
      const fields = contract?.shape || contract || {};
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, restoreCopy(item, `${fieldPath}.${key}`, fields[key])]));
    }
    const props = restoreCopy(slide.props, basePath, getLayout(slide.layout_id)?.fields);
    slide.props = props;
    ["subtitle", "meta", "caption", "insight"].forEach(field => {
      const cleaned = stripPresentationDirectives(props[field]);
      if (cleaned !== props[field]) {
        props[field] = cleaned;
        changes.push(`${basePath}.${field}: removed presentation-production directive`);
      }
    });
    if (slide.layout_id === "statement-focus-v1") {
      const used = new Set();
      const replacementFacts = [...claimFacts, binding.source_text];
      const statement = strictTextOrFallback(
        props.statement,
        normalizedSources,
        replacementFacts,
        120,
        used
      );
      if (statement !== props.statement) changes.push(`${basePath}.statement: restored source-backed copy`);
      props.statement = statement;
      used.add(statement);
      const support = strictTextOrFallback(
        props.support,
        normalizedSources,
        replacementFacts,
        180,
        used
      );
      if (support !== props.support) changes.push(`${basePath}.support: restored source-backed copy`);
      props.support = support;
      props.proofs = (Array.isArray(props.proofs) ? props.proofs : []).map((proof, proofIndex) => {
        if (!proof || isHonestPlaceholder(proof.value) || isSourceBacked(proof.value, normalizedSources)) {
          return proof;
        }
        changes.push(`${basePath}.proofs.${proofIndex}.value: replaced unsupported proof`);
        return { ...proof, value: "待补充" };
      });
    } else if (slide.layout_id === "kpi-grid-v1") {
      props.items = (Array.isArray(props.items) ? props.items : []).map((item, itemIndex) => {
        const candidate = [item.label, item.value].filter(Boolean).join(" ");
        const detail = isHonestPlaceholder(item.detail)
          || isSourceBacked(item.detail, normalizedSources)
          ? item.detail
          : (bestSourceFragment(candidate, claimFacts, 90) || "");
        const valueHasUnsupportedNumber = numberTokens(item.value)
          .some(token => !isNumberBackedForSlide(
            token,
            claimFacts,
            slide,
            `${basePath}.items.${itemIndex}.value`
          ));
        const delta = !item.delta
          || isSourceBacked(item.delta, normalizedSources)
          ? item.delta
          : "";
        if (detail !== item.detail) changes.push(`${basePath}.items.${itemIndex}.detail: restored source-backed detail`);
        if (valueHasUnsupportedNumber) changes.push(`${basePath}.items.${itemIndex}.value: replaced unsupported metric`);
        if (delta !== item.delta) changes.push(`${basePath}.items.${itemIndex}.delta: removed unsupported interpretation`);
        return {
          ...item,
          value: valueHasUnsupportedNumber ? "待补充" : item.value,
          detail,
          delta,
        };
      });
      if (!isHonestPlaceholder(props.subtitle) && !isSourceBacked(props.subtitle, normalizedSources)) {
        const labels = props.items
          .map(item => item && item.label)
          .filter(label => typeof label === "string" && isSourceBacked(label, normalizedSources));
        const subtitle = labels.length >= 2 ? labels.join(" → ") : "";
        if (subtitle !== props.subtitle) {
          props.subtitle = subtitle;
          changes.push(`${basePath}.subtitle: replaced unsupported interpretation with metric sequence`);
        }
      }
    } else if (slide.layout_id === "table-data-v1") {
      if (!isHonestPlaceholder(props.subtitle) && !isSourceBacked(props.subtitle, normalizedSources)) {
        props.subtitle = "";
        changes.push(`${basePath}.subtitle: removed unsupported optional narrative`);
      }
      const columns = Array.isArray(props.columns) ? props.columns.slice() : [];
      const rows = (Array.isArray(props.rows) ? props.rows : [])
        .map(row => Array.isArray(row) ? row.slice() : []);
      const unsupportedColumns = [];
      columns.forEach((_column, columnIndex) => {
        const cells = rows.map(row => row[columnIndex]).filter(cell => cell !== undefined);
        if (
          cells.length
          && cells.every(cell => !isHonestPlaceholder(cell) && !isSourceBacked(cell, normalizedSources))
        ) {
          unsupportedColumns.push(columnIndex);
        }
      });
      const duplicateColumns = [];
      columns.forEach((_column, columnIndex) => {
        if (columnIndex === 0 || !rows.length) return;
        const duplicateOfEarlier = Array.from(
          { length: columnIndex },
          (_unused, earlierIndex) => earlierIndex
        ).some(earlierIndex => {
          const pairs = rows.map(row => [
            normalizeText(row[columnIndex]),
            normalizeText(row[earlierIndex]),
          ]);
          return pairs.some(([current]) => current)
            && pairs.every(([current, earlier]) => current === earlier);
        });
        if (duplicateOfEarlier) duplicateColumns.push(columnIndex);
      });
      const removableColumns = [
        ...unsupportedColumns,
        ...duplicateColumns.filter(index => !unsupportedColumns.includes(index)),
      ];
      const maxRemovable = Math.max(0, columns.length - 2);
      const dropped = new Set(removableColumns.slice(0, maxRemovable));
      if (dropped.size) {
        props.columns = columns.filter((_column, columnIndex) => !dropped.has(columnIndex));
        props.rows = rows.map(row => row.filter((_cell, columnIndex) => !dropped.has(columnIndex)));
        changes.push(
          `${basePath}.rows: removed ${dropped.size} unsupported or duplicate optional table column(s)`
        );
      } else {
        props.rows = rows;
      }
      props.rows = props.rows.map((row, rowIndex) => row.map((cell, cellIndex) => {
        if (isHonestPlaceholder(cell) || isSourceBacked(cell, normalizedSources)) return cell;
        const replacement = bestOverlappingSourceFragment(cell, claimFacts, 48) || "待补充";
        changes.push(`${basePath}.rows.${rowIndex}.${cellIndex}: restored source-backed cell`);
        return replacement;
      }));
    } else if (slide.layout_id === "closing-next-steps-v1") {
      if (!isHonestPlaceholder(props.subtitle) && !isSourceBacked(props.subtitle, normalizedSources)) {
        props.subtitle = "";
        changes.push(`${basePath}.subtitle: removed unsupported closing narrative`);
      }
      props.actions = (Array.isArray(props.actions) ? props.actions : []).map(
        (action, actionIndex) => {
          const label = isHonestPlaceholder(action && action.label)
            || isSourceBacked(action && action.label, normalizedSources)
            ? action.label
            : (bestOverlappingSourceFragment(action && action.label, claimFacts, 36) || "待补充");
          const detail = !action || !action.detail
            || isHonestPlaceholder(action.detail)
            || isSourceBacked(action.detail, normalizedSources)
            ? (action ? action.detail : "")
            : (bestOverlappingSourceFragment(action.detail, claimFacts, 72) || "");
          if (label !== (action && action.label)) {
            changes.push(`${basePath}.actions.${actionIndex}.label: restored source-backed action label`);
          }
          if (detail !== (action && action.detail)) {
            changes.push(`${basePath}.actions.${actionIndex}.detail: restored source-backed action detail`);
          }
          return { ...(action || {}), label, detail };
        }
      );
      if (!isHonestPlaceholder(props.contact) && !isSourceBacked(props.contact, normalizedSources)) {
        const contact = bestOverlappingSourceFragment(props.contact, claimFacts, 72) || "";
        if (contact !== props.contact) {
          props.contact = contact;
          changes.push(`${basePath}.contact: restored source-backed closing contact`);
        }
      }
    } else if (
      slide.layout_id === "project-case-study-v1"
      && isProjectCaseContext(slide)
    ) {
      const sourceHeading = projectHeadingFromSource(props, binding.source_text);
      if (!isSourceBacked(props.eyebrow, normalizedSources) && sourceHeading) {
        props.eyebrow = "精选项目";
        changes.push(`${basePath}.eyebrow: restored the requested project section label`);
      }
      const originalPositioning = props.positioning;
      props.positioning = isHonestPlaceholder(props.positioning)
        || isSourceBacked(props.positioning, normalizedSources)
        ? props.positioning
        : "待补充";
      if (props.positioning !== originalPositioning) {
        changes.push(`${basePath}.positioning: replaced unsupported project narrative`);
      }
      props.caption = isHonestPlaceholder(props.caption)
        || isSourceBacked(props.caption, normalizedSources)
        ? props.caption
        : "待补充";
      props.image = ensureGeneratedProjectMedia(props.image, `${basePath}.image`, changes);
      if (
        props.title
        && !PLACEHOLDER_RE.test(props.title)
        && !isGenericProjectTitle(props.title)
        && !isSourceBacked(props.title, normalizedSources)
      ) {
        props.title = sourceHeading
          || (isSourceBacked(props.eyebrow, normalizedSources)
            ? props.eyebrow
            : `${props.title}（待补充）`);
        changes.push(`${basePath}.title: neutralized unsupported project name`);
      }
    } else if (slide.layout_id === "timeline-horizontal-v1") {
      if (
        !isHonestPlaceholder(props.title)
        && !isNeutralSectionTitle("process", props.title)
        && !isSourceBacked(props.title, normalizedSources)
      ) {
        props.title = sourceBackedSectionTitle("process", binding.source_text) || "待补充";
        changes.push(`${basePath}.title: restored the requested process section heading`);
      }
      if (!isHonestPlaceholder(props.subtitle) && !isSourceBacked(props.subtitle, normalizedSources)) {
        props.subtitle = "";
        changes.push(`${basePath}.subtitle: removed unsupported process narrative`);
      }
      props.steps = (Array.isArray(props.steps) ? props.steps : []).map((step, stepIndex) => {
        const normalized = { ...step };
        ["title", "body"].forEach(key => {
          if (
            !isHonestPlaceholder(normalized[key])
            && !isProcessTextSourceBacked(normalized[key], binding.source_text)
          ) {
            normalized[key] = key === "body" ? "" : "待补充";
            changes.push(`${basePath}.steps.${stepIndex}.${key}: replaced unsupported process copy`);
          }
        });
        return normalized;
      });
    } else if (slide.layout_id === "cards-grid-v1") {
      const sectionHeading = [props.eyebrow, props.title, props.subtitle]
        .filter(value => typeof value === "string")
        .join(" ");
      const namedItemTitles = (Array.isArray(props.items) ? props.items : [])
        .map((item, itemIndex) => ({
          path: `${basePath}.items.${itemIndex}.title`,
          text: item && typeof item.title === "string" ? item.title : "",
        }))
        .filter(entry => entry.text);
      const sectionKind = detectNamedSectionKind(props, namedItemTitles)
        || (FUTURE_CONTEXT_RE.test(sectionHeading) ? "future-plan" : null);
      const clientDomains = sourceBackedClientDomains(binding.source_text);
      if (
        sectionKind
        && !isHonestPlaceholder(props.title)
        && !isNeutralSectionTitle(sectionKind, props.title)
        && !isSourceBacked(props.title, normalizedSources)
      ) {
        props.title = sourceBackedSectionTitle(sectionKind, binding.source_text) || "待补充";
        changes.push(`${basePath}.title: restored the requested ${sectionKind} section heading`);
      }
      if (sectionKind && !isHonestPlaceholder(props.subtitle) && !isSourceBacked(props.subtitle, normalizedSources)) {
        props.subtitle = "待补充";
        changes.push(`${basePath}.subtitle: replaced unsupported ${sectionKind} narrative`);
      }
      props.items = (Array.isArray(props.items) ? props.items : []).map((item, itemIndex) => {
        const normalized = { ...item };
        if (!isHonestPlaceholder(normalized.body) && !isSourceBacked(normalized.body, normalizedSources)) {
          normalized.body = "待补充";
          changes.push(`${basePath}.items.${itemIndex}.body: replaced unsupported card copy`);
        }
        const titleBacked = sectionKind === "client"
          ? isSourceBacked(normalized.title, normalizedSources)
            || isDomainCategoryBacked(normalized.title, normalizedSources)
          : sectionKind === "team-member"
            ? isTeamNameSourceBacked(normalized.title, binding.source_text)
            : isSourceBacked(normalized.title, normalizedSources);
        if (sectionKind && !isHonestPlaceholder(normalized.title) && !titleBacked) {
          normalized.title = sectionKind === "client" && clientDomains[itemIndex]
            ? clientDomains[itemIndex]
            : "待补充";
          changes.push(`${basePath}.items.${itemIndex}.title: replaced unsupported ${sectionKind}`);
        }
        return normalized;
      });
    }
    slide.props = sanitizeUnsupportedClaims(
      props,
      basePath,
      claimFacts,
      normalizedSources,
      changes,
      slide,
      {
        slideCount: sanitized.slides.length,
        eyebrowOrdinal: expectedEyebrowOrdinal(sanitized.slides, slideIndex),
        runtimeSource: binding.source_text,
      }
    );
  });
  return { deck: sanitized, changes };
}

function validateSourceBoundDeck(deck) {
  const issues = [];
  const warnings = [];
  const truth = deck.truth_contract;
  if (!truth || truth.mode !== "source_bound") {
    issues.push("truth_contract.mode must be source_bound for a source-bound deck");
    return { issues, warnings, sourceFactCount: 0, researchFactCount: 0, assumptionCount: 0 };
  }
  const sourceFacts = Array.isArray(truth.source_facts) ? truth.source_facts : [];
  const sourceBinding = validateSourceFactsAgainstRuntime(sourceFacts);
  warnings.push(...sourceBinding.issues);
  const researchFacts = Array.isArray(truth.research_facts) ? truth.research_facts : [];
  const researchBinding = validateResearchFactsAgainstRuntime(researchFacts);
  warnings.push(...researchBinding.issues);
  const parsedResearchFacts = researchFacts.map(parseCanonicalResearchFact);
  const entityBoundResearch = parsedResearchFacts.some(Boolean);
  if (entityBoundResearch) {
    parsedResearchFacts.forEach((parsed, index) => {
      if (!parsed) {
        warnings.push(
          `truth_contract.research_facts.${index}: research evidence must use ` +
          "entity | claim | source_type | source_url so entity-specific facts cannot " +
          "be matched across targets"
        );
      }
    });
  }
  const assumptions = Array.isArray(truth.assumptions) ? truth.assumptions : [];
  const assumptionBinding = validateAssumptionsAgainstRuntime(assumptions);
  warnings.push(...assumptionBinding.issues);
  if (!sourceFacts.length && !researchFacts.length) {
    warnings.push(
      "truth_contract has no source_facts or research_facts; capture user-provided facts " +
      "or researched facts before authoring claims"
    );
  }
  const strictSourceOnly = sourceBinding.strict && sourceFacts.length > 0;
  const hasAuthorizedAssumptions = Boolean(
    sourceBinding.available
    && sourceBinding.allows_assumptions
    && assumptions.length > 0
  );

  deck.slides.forEach((slide, index) => {
    const basePath = slidePropsPath(slide, index);
    const entries = collectTextEntries(slide.props, basePath);
    const combined = entries.map(entry => entry.text).join(" ");
    const combinedNormalized = normalizeText(combined);
    const visibleResearchEntities = entityBoundResearch
      ? new Set(
        parsedResearchFacts
          .filter(parsed =>
            parsed && parsed.entityKey && combinedNormalized.includes(parsed.entityKey)
          )
          .map(parsed => parsed.entityKey)
      )
      : new Set();
    const slideResearchFacts = visibleResearchEntities.size
      ? parsedResearchFacts
        .filter(parsed => parsed && visibleResearchEntities.has(parsed.entityKey))
        .map(parsed => parsed.raw)
      : researchFacts;
    const claimFacts = [...sourceFacts, ...slideResearchFacts];
    const normalizedFacts = claimFacts.map(normalizeText).filter(Boolean);
    const normalizedSources = [
      ...normalizedFacts,
      normalizeText(sourceBinding.source_text),
    ].filter(Boolean);
    const disclosesAssumptions = slideDisclosesAssumptions(slide);
    const hasDisclosedAuthorizedAssumptions = (
      hasAuthorizedAssumptions && disclosesAssumptions
    );

    entries.forEach(entry => {
      if (
        !entry.text.trim()
        || entry.placeholderContext
        || PLACEHOLDER_RE.test(entry.text)
        || isDiagramStructuralEntry(entry, slide)
        || isVisualEnumEntry(entry, slide)
      ) return;
      numberTokens(entry.text).forEach(token => {
        const sourceBackedNumber = isRuntimeSourceCopy(entry.text, sourceBinding.source_text) || isNumberBackedForSlide(
          token,
          claimFacts,
          slide,
          entry.path
        );
        const assumptionBackedNumber = hasDisclosedAuthorizedAssumptions
          && (
            (
              !CALENDAR_YEAR_TOKEN_RE.test(token)
              && isNumberBackedForSlide(token, assumptions, slide, entry.path)
            )
            || isChartAssumptionDataEntry(entry, slide)
          );
        if (
          !sourceBackedNumber
          && !assumptionBackedNumber
          && !isStructuralNumber(
            entry,
            token,
            slide,
            deck.slides.length,
            expectedEyebrowOrdinal(deck.slides, index)
          )
        ) {
          warnings.push(
            `${entry.path}: numeric claim ${JSON.stringify(token)} is not present in ` +
            "truth_contract.source_facts/research_facts or in user-authorized " +
            "truth_contract.assumptions " +
            "with a visible 假设/示意 disclosure on this slide"
          );
        }
      });
      if (TEAM_SIZE_RE.test(entry.text) && !isSourceBacked(entry.text, normalizedSources)) {
        warnings.push(
          `${entry.path}: team-size claim is not source-backed; use 待补充 or omit it`
        );
      }
      const shortConceptBacked = /\.(?:label|title|eyebrow)$/.test(entry.path)
        && isShortSourceConceptBacked(entry.text, sourceBinding.source_text);
      const semanticSourceBackedPerformance = !strictSourceOnly
        && isSourceSemanticPerformanceParaphrase(entry.text, sourceFacts);
      const assumptionBackedPerformance = hasDisclosedAuthorizedAssumptions
        && !SOURCE_ONLY_PERFORMANCE_RE.test(entry.text);
      if (
        requiresPerformanceBacking(entry.text)
        && !isSourceBacked(entry.text, normalizedSources)
        && !isResearchBacked(entry.text, slideResearchFacts)
        && !shortConceptBacked
        && !semanticSourceBackedPerformance
        && !assumptionBackedPerformance
      ) {
        warnings.push(
          `${entry.path}: performance/award/publication claim is not source-backed; ` +
          "use 待补充, omit it, or record user-authorized assumptions and visibly disclose them"
        );
      }
    });

    const title = typeof slide.props.title === "string" ? slide.props.title : "";
    if (
      title
      && slide.layout_id === "project-case-study-v1"
      && isProjectCaseContext(slide)
      && !PLACEHOLDER_RE.test(title)
      && !isGenericProjectTitle(title)
      && !isSourceBacked(title, normalizedSources)
    ) {
      warnings.push(
        `${basePath}.title: project name is not source-backed; use a neutral title containing 待补充`
      );
    }

    const namedItemTitles = entries.filter(entry =>
      /\.(?:items|steps)\.\d+\.title$/.test(entry.path)
    );
    const sectionHeading = [slide.props.eyebrow, slide.props.title, slide.props.subtitle]
      .filter(value => typeof value === "string")
      .join(" ");
    const sectionKind = detectNamedSectionKind(slide.props, namedItemTitles);
    const teamCapabilityContext = isTeamCapabilityContext(`${sectionHeading} ${combined}`);
    if (sectionKind) {
      if (strictSourceOnly) {
        requireStrictSectionTitle(
          slide.props.title,
          `${basePath}.title`,
          sectionKind,
          normalizedSources,
          warnings
        );
        requireStrictSourceBacked(
          slide.props.subtitle,
          `${basePath}.subtitle`,
          normalizedSources,
          warnings
        );
      }
      namedItemTitles.forEach(entry => {
        const sourceBacked = sectionKind === "team-member"
          ? isTeamNameSourceBacked(entry.text, sourceBinding.source_text)
            || (teamCapabilityContext && isTeamCapabilityTitle(entry.text))
          : isSourceBacked(entry.text, normalizedSources)
            || (
              sectionKind === "client"
              && isDomainCategoryBacked(entry.text, normalizedSources)
            );
        if (
          !entry.placeholderContext
          && !PLACEHOLDER_RE.test(entry.text)
          && !sourceBacked
        ) {
          warnings.push(
            `${entry.path}: ${sectionKind} name is not source-backed; use 待补充 or omit it`
          );
        }
      });
    }

    if (strictSourceOnly) {
      validateStrictNarrativeFields(
        slide,
        basePath,
        normalizedSources,
        warnings,
        sourceBinding.source_text
      );
    }
  });
  return {
    issues,
    warnings,
    sourceFactCount: sourceFacts.length,
    researchFactCount: researchFacts.length,
    entityBoundResearchFactCount: parsedResearchFacts.filter(Boolean).length,
    assumptionCount: assumptions.length,
    sourceBinding,
  };
}

function writeJson(filePath, value) {
  const resolved = resolveArtifactPath(filePath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const deckPath = resolveArtifactPath(opts.deck);
  const structural = validateAndNormalizeDeck(readJson(opts.deck));
  let result;
  if (!structural.ok) {
    result = {
      issues: structural.issues.map(issue => `deck-spec: ${issue}`),
      warnings: [],
      sourceFactCount: 0,
      researchFactCount: 0,
      assumptionCount: 0,
    };
  } else if (
    structural.normalized.truth_contract
    && structural.normalized.truth_contract.mode === "illustrative"
  ) {
    const sourceBinding = runtimeSourceBinding();
    const illustrativeAllowed = !sourceBinding.available || sourceBinding.allows_assumptions;
    const permissionWarning = (
      "truth_contract.mode is illustrative but the user has not explicitly permitted " +
      "invented or illustrative content"
    );
    result = {
      issues: !illustrativeAllowed ? [permissionWarning] : [],
      warnings: [
        "truth_contract.mode is illustrative; use this only when the user explicitly permits fictional or illustrative copy",
      ],
      sourceFactCount: structural.normalized.truth_contract.source_facts.length,
      researchFactCount: Array.isArray(structural.normalized.truth_contract.research_facts)
        ? structural.normalized.truth_contract.research_facts.length
        : 0,
      entityBoundResearchFactCount: 0,
      assumptionCount: structural.normalized.truth_contract.assumptions.length,
      sourceBinding: { ...sourceBinding, verified_fact_count: 0, issues: [] },
    };
  } else {
    result = validateSourceBoundDeck(structural.normalized);
  }
  const report = {
    ok: result.issues.length === 0,
    advisory: true,
    deck: deckPath,
    mode: structural.normalized && structural.normalized.truth_contract
      ? structural.normalized.truth_contract.mode
      : null,
    sourceFactCount: result.sourceFactCount,
    researchFactCount: result.researchFactCount || 0,
    entityBoundResearchFactCount: result.entityBoundResearchFactCount || 0,
    assumptionCount: result.assumptionCount || 0,
    sourceBinding: result.sourceBinding
      ? {
        available: result.sourceBinding.available,
        strict: result.sourceBinding.strict,
        allows_assumptions: result.sourceBinding.allows_assumptions,
        source_hash: result.sourceBinding.source_hash,
        verified_fact_count: result.sourceBinding.verified_fact_count,
      }
      : null,
    issues: result.issues,
    warnings: result.warnings,
  };
  if (opts.report) writeJson(opts.report, report);
  console.log(JSON.stringify(report, null, 2));
  console.log(
    `Deck truth validation: ${report.ok ? "PASS" : "FAIL"} ` +
    `(${report.sourceFactCount} source facts, ${report.researchFactCount} research facts, ` +
    `${report.assumptionCount} assumptions, ` +
    `${report.issues.length} issues, ${report.warnings.length} warnings)`
  );
  if (!report.ok) process.exit(1);
}

module.exports = {
  isNumberSourceBacked,
  sanitizeStrictSourceDeck,
  validateSourceBoundDeck,
};

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }
}
