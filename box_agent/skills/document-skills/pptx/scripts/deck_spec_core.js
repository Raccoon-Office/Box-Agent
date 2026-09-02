"use strict";

const fs = require("fs");
const path = require("path");

const TRUTH_TEXT_MAX_CHARACTERS = 280;

const {
  getLayout,
  getVisualCollectionContract,
  layouts,
  manifestRecord,
} = require("../layouts/registry.js");
const {
  compositionDirectionCatalog,
  compositionManifestRecord,
  createDeckDesign,
  resolveDeckDesign,
  validateAndNormalizeDeckDesign,
} = require("./composition_core.js");
const {
  validateAndNormalizeDesignContract,
} = require("./design_contract_core.js");

const SKILL_ROOT = path.resolve(__dirname, "..");
const THEMES_DIR = path.join(SKILL_ROOT, "themes");
const MANIFEST_PATH = path.join(SKILL_ROOT, "layouts", "manifest.json");
const DEFAULT_THEME_ID = "blue-professional";
const LAYOUT_ID_ALIASES = Object.freeze({
  "statement-v1": "statement-focus-v1",
  "manifesto-v1": "statement-focus-v1",
  "closing-v1": "closing-next-steps-v1",
  "closing-v2": "closing-next-steps-v1",
  "thank-you-v1": "closing-next-steps-v1",
  "bar-chart-v1": "chart-bar-v1",
  "ranking-chart-v1": "chart-bar-v1",
  "chart-v1": "chart-data-v1",
  "line-chart-v1": "chart-data-v1",
  "area-chart-v1": "chart-data-v1",
  "pie-chart-v1": "chart-data-v1",
  "donut-chart-v1": "chart-data-v1",
  "radar-chart-v1": "chart-data-v1",
  "data-table-v1": "table-data-v1",
  "comparison-table-v1": "table-data-v1",
  "heatmap-v1": "heatmap-matrix-v1",
  "risk-heatmap-v1": "heatmap-matrix-v1",
  "long-text-v1": "text-columns-v1",
  "client-logo-grid-v1": "cards-grid-v1",
  "clients-logo-grid-v1": "cards-grid-v1",
  "awards-press-v1": "cards-grid-v1",
  "team-grid-v1": "cards-grid-v1",
  "team-showcase-v1": "cards-grid-v1",
  "problem-solution-v1": "comparison-two-column-v1",
  "process-flow-v1": "timeline-horizontal-v1",
  "swimlane-v1": "swimlane-process-v1",
  "customer-journey-v1": "customer-journey-map-v1",
  "journey-map-v1": "customer-journey-map-v1",
  "maturity-ladder-v1": "maturity-model-v1",
  "root-cause-v1": "cause-tree-v1",
  "fishbone-analysis-v1": "cause-tree-v1",
  "business-model-v1": "cards-grid-v1",
  "comparison-matrix-v1": "table-data-v1",
  "funding-use-v1": "chart-data-v1",
  "architecture-diagram-v1": "technical-diagram-v1",
  "integration-map-v1": "technical-diagram-v1",
  "data-pipeline-v1": "technical-diagram-v1",
  "qualitative-dashboard-v1": "dashboard-overview-v1",
  "wide-image-v1": "image-feature-v1",
  "image-feature-story-v1": "image-feature-v1",
  "full-bleed-v1": "image-full-bleed-v1",
  "full-slide-image-v1": "image-full-bleed-v1",
  "factory-line-v1": "factory-process-line-v1",
  "legal-irac-v1": "legal-case-logic-v1",
  "real-estate-factsheet-v1": "property-factsheet-v1",
  "ecommerce-funnel-v1": "commerce-funnel-v1",
  "supply-chain-network-v1": "supply-network-v1",
});
const LAYOUT_ID_HINTS = Object.freeze([
  { keywords: ["swimlane", "swim-lane", "cross-functional-process", "role-phase"], id: "swimlane-process-v1" },
  { keywords: ["customer-journey", "user-journey", "journey-map"], id: "customer-journey-map-v1" },
  { keywords: ["maturity-model", "maturity-ladder", "capability-maturity"], id: "maturity-model-v1" },
  { keywords: ["cause-tree", "root-cause", "fishbone"], id: "cause-tree-v1" },
  { keywords: ["factory-process", "factory-line", "production-line", "shop-floor"], id: "factory-process-line-v1" },
  { keywords: ["legal-case", "legal-logic", "legal-irac", "irac"], id: "legal-case-logic-v1" },
  { keywords: ["property-facts", "property-factsheet", "site-facts", "real-estate"], id: "property-factsheet-v1" },
  { keywords: ["commerce-funnel", "ecommerce-funnel", "retail-funnel", "conversion-funnel"], id: "commerce-funnel-v1" },
  { keywords: ["supply-network", "supply-chain-network", "logistics-network", "control-tower"], id: "supply-network-v1" },
  { keywords: ["heatmap", "heat-map", "risk-heatmap", "risk-heat-map"], id: "heatmap-matrix-v1" },
  { keywords: ["architecture", "system-layer", "tech-stack", "integration", "data-flow", "system-map", "data-pipeline", "pipeline"], id: "technical-diagram-v1" },
  { keywords: ["dashboard-overview", "management-dashboard", "operations-dashboard"], id: "dashboard-overview-v1" },
  { keywords: ["full-bleed", "full-slide-image", "cinematic-image", "image-background"], id: "image-full-bleed-v1" },
  { keywords: ["wide-image", "image-feature", "large-image", "visual-feature"], id: "image-feature-v1" },
  { keywords: ["image-hero", "hero-split", "visual-split"], id: "image-hero-split-v1" },
  { keywords: ["closing", "thank-you", "thankyou", "next-step", "cta", "contact"], id: "closing-next-steps-v1" },
  { keywords: ["line-chart", "area-chart", "pie-chart", "donut-chart", "radar-chart", "time-series", "multi-series", "trend-chart"], id: "chart-data-v1" },
  { keywords: ["bar-chart", "bar-graph", "ranking", "distribution"], id: "chart-bar-v1" },
  { keywords: ["table", "matrix", "schedule"], id: "table-data-v1" },
  { keywords: ["long-text", "longform", "long-form", "text-column", "deep-dive", "detail"], id: "text-columns-v1" },
  { keywords: ["statement", "manifesto", "quote", "vision", "thesis"], id: "statement-focus-v1" },
  { keywords: ["client", "logo", "award", "press", "team", "people", "member", "card", "list"], id: "cards-grid-v1" },
  { keywords: ["project", "case-study", "portfolio"], id: "project-case-study-v1" },
  { keywords: ["timeline", "process", "workflow", "roadmap"], id: "timeline-horizontal-v1" },
  { keywords: ["kpi", "metric", "stat", "number", "data"], id: "kpi-grid-v1" },
  { keywords: ["comparison", "compare", "versus"], id: "comparison-two-column-v1" },
  { keywords: ["section", "divider", "chapter", "marker"], id: "section-marker-v1" },
  { keywords: ["cover", "title", "opening"], id: "cover-hero-v1" },
]);
const STRICT_SOURCE_REQUEST_RE = /只使用.{0,24}(?:提供|给出).{0,16}事实|仅使用.{0,24}(?:提供|给出).{0,16}事实|(?:所有|全部|任何|一切)(?:内容|事实)?[^。；;\n]{0,12}(?:禁止|不得|不要)(?:虚构|编造)|(?:禁止|不得|不要)(?:虚构|编造)(?:任何|所有|全部)?(?:内容|事实)|(?:不|勿|不要|不得|禁止)(?:补充|添加|加入|写入)[^。；;\n]{0,24}(?:未|没有)(?:提供|给出)[^。；;\n]{0,8}(?:事实|信息|内容)|(?:未|没有)(?:提供|给出)[^。；;\n]{0,8}(?:事实|信息|内容)[^。；;\n]{0,12}(?:不要|不得|不可)(?:补充|添加|编造)|provided facts only|only (?:use|using) (?:the )?(?:provided|supplied) facts|do not (?:invent|fabricate) (?:anything|any facts?|content)|no fabricated facts/i;
const ASSUMPTION_PERMISSION_RE = /(?:允许|可以|可|请)(?:使用|采用|用)?[^\n。；;]{0,24}(?:合理)?(?:假设|示意)(?:性)?数据|(?:假设|示意)(?:性)?数据[^\n。；;]{0,24}(?:可以|可|允许|接受)|(?:may|can|please|allowed to|use|using)[^.\n;]{0,40}(?:reasonable\s+)?(?:assumptions?|assumed|illustrative|hypothetical)(?:\s+(?:data|figures|values))?/i;
const ASSUMPTION_DENIAL_RE = /(?:不可以|不可|不得|不要|禁止|不允许)[^\n。；;]{0,24}(?:假设|示意)(?:性)?数据|(?:do not|don't|must not|no)[^.\n;]{0,32}(?:assumptions?|assumed|illustrative|hypothetical)(?:\s+(?:data|figures|values))?/i;

function normalizeLayoutId(layoutId) {
  const requested = String(layoutId || "").trim();
  if (!requested || layouts.some(layout => layout.id === requested)) return requested;
  if (LAYOUT_ID_ALIASES[requested]) return LAYOUT_ID_ALIASES[requested];
  const normalized = requested.toLowerCase().replace(/[_\s]+/g, "-");
  const hinted = LAYOUT_ID_HINTS.find(item =>
    item.keywords.some(keyword => normalized.includes(keyword))
  );
  return hinted ? hinted.id : requested;
}

function artifactRoot() {
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  return configured ? path.resolve(configured) : process.cwd();
}

function resolveArtifactPath(filePath) {
  if (path.isAbsolute(filePath)) return path.resolve(filePath);
  return path.resolve(artifactRoot(), filePath);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function deepClone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function mergeDefaults(defaults, supplied) {
  if (supplied === undefined) return deepClone(defaults);
  if (!isPlainObject(defaults) || !isPlainObject(supplied)) return deepClone(supplied);
  const merged = deepClone(defaults);
  for (const [key, value] of Object.entries(supplied)) {
    merged[key] = isPlainObject(value) && isPlainObject(merged[key])
      ? mergeDefaults(merged[key], value)
      : deepClone(value);
  }
  return merged;
}

function normalizeVerbatimSourceText(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    // Models commonly normalize editorial spacing around CJK words (for
    // example "面向 中小制造工厂" -> "面向中小制造工厂"). Treat whitespace as
    // presentation-only while keeping every non-whitespace character verbatim,
    // so deductions and paraphrases are still rejected.
    .replace(/\s+/g, "")
    .trim();
}

function runtimeSourceBinding() {
  const encoded = String(process.env.BOX_AGENT_SOURCE_TEXT_B64 || "").trim();
  if (!encoded) {
    return {
      available: false,
      strict: false,
      allows_assumptions: false,
      source_hash: null,
      source_text: "",
    };
  }
  let sourceText;
  try {
    sourceText = Buffer.from(encoded, "base64").toString("utf8");
  } catch (_error) {
    throw new Error("BOX_AGENT_SOURCE_TEXT_B64 is not valid base64 source text");
  }
  const strict = STRICT_SOURCE_REQUEST_RE.test(sourceText);
  return {
    available: true,
    strict,
    allows_assumptions: !strict
      && !ASSUMPTION_DENIAL_RE.test(sourceText)
      && ASSUMPTION_PERMISSION_RE.test(sourceText),
    source_hash: require("crypto").createHash("sha256").update(sourceText).digest("hex"),
    source_text: sourceText,
  };
}

function validateSourceFactsAgainstRuntime(sourceFacts) {
  const binding = runtimeSourceBinding();
  const facts = Array.isArray(sourceFacts) ? sourceFacts : [];
  if (!binding.available) {
    return { ...binding, verified_fact_count: 0, issues: [] };
  }
  const source = normalizeVerbatimSourceText(binding.source_text);
  const issues = [];
  let verifiedFactCount = 0;
  facts.forEach((fact, index) => {
    const normalizedFact = normalizeVerbatimSourceText(fact);
    if (normalizedFact && source.includes(normalizedFact)) {
      verifiedFactCount += 1;
      return;
    }
    issues.push(
      `truth_contract.source_facts.${index}: ${JSON.stringify(fact)} is not a ` +
      "contiguous phrase from the user's source text; copy the supplied wording verbatim " +
      "instead of adding a deduction or paraphrase" +
      (binding.strict
        ? ""
        : "; if this statement came from completed external research, pass it with " +
          "--research-fact instead of repeating the research or relabeling it as user input")
    );
  });
  return {
    ...binding,
    verified_fact_count: verifiedFactCount,
    issues,
  };
}

function validateResearchFactsAgainstRuntime(researchFacts) {
  const binding = runtimeSourceBinding();
  const facts = Array.isArray(researchFacts) ? researchFacts : [];
  const issues = [];
  if (facts.length && binding.available && binding.strict) {
    issues.push(
      "truth_contract.research_facts is not allowed for a strict source-only request; " +
      "use only verbatim user-provided --fact values, omit an optional claim, or use " +
      "an explicit placeholder for a required unavailable fact"
    );
  }
  return {
    ...binding,
    research_fact_count: facts.length,
    issues,
  };
}

function validateAssumptionsAgainstRuntime(assumptions) {
  const binding = runtimeSourceBinding();
  const entries = Array.isArray(assumptions) ? assumptions : [];
  const issues = [];
  if (entries.length && binding.available && !binding.allows_assumptions) {
    issues.push(
      "truth_contract.assumptions requires explicit user permission for assumed or " +
      "illustrative data; omit the assumptions and use sourced values or explicit " +
      "placeholders without pausing"
    );
  }
  return {
    ...binding,
    assumption_count: entries.length,
    issues,
  };
}

function normalizeTruthTextList(value, fieldPath, maxEntries, issues) {
  if (!Array.isArray(value)) {
    issues.push(`${fieldPath}: expected array`);
    return [];
  }
  if (value.length > maxEntries) {
    issues.push(`${fieldPath}: expected at most ${maxEntries} entries`);
  }
  const normalized = [];
  value.forEach((entry, index) => {
    if (typeof entry !== "string" || !entry.trim()) {
      issues.push(`${fieldPath}.${index}: expected non-empty text`);
      return;
    }
    if (characterLength(entry.trim()) > TRUTH_TEXT_MAX_CHARACTERS) {
      issues.push(
        `${fieldPath}.${index}: exceeds ${TRUTH_TEXT_MAX_CHARACTERS} characters`
      );
      return;
    }
    if (!normalized.includes(entry.trim())) normalized.push(entry.trim());
  });
  return normalized;
}

function validateAndNormalizeTruthContract(value, fieldPath, issues) {
  if (value === undefined) return null;
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  const allowed = ["mode", "source_facts", "research_facts", "assumptions"];
  const unknown = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknown.length) {
    issues.push(`${fieldPath}: unknown field(s): ${unknown.join(", ")}`);
  }
  const mode = value.mode === undefined ? "source_bound" : value.mode;
  if (!["source_bound", "illustrative"].includes(mode)) {
    issues.push(`${fieldPath}.mode: expected one of source_bound, illustrative`);
  }
  const normalizedFacts = normalizeTruthTextList(
    value.source_facts === undefined ? [] : value.source_facts,
    `${fieldPath}.source_facts`,
    80,
    issues
  );
  const hasResearchFacts = value.research_facts !== undefined;
  const normalizedResearchFacts = normalizeTruthTextList(
    hasResearchFacts ? value.research_facts : [],
    `${fieldPath}.research_facts`,
    80,
    issues
  );
  const normalizedAssumptions = normalizeTruthTextList(
    value.assumptions === undefined ? [] : value.assumptions,
    `${fieldPath}.assumptions`,
    40,
    issues
  );
  return {
    mode,
    source_facts: normalizedFacts,
    ...(hasResearchFacts ? { research_facts: normalizedResearchFacts } : {}),
    assumptions: normalizedAssumptions,
  };
}

function readJson(filePath) {
  const resolved = resolveArtifactPath(filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`File not found: ${resolved}`);
  }
  try {
    return JSON.parse(fs.readFileSync(resolved, "utf8"));
  } catch (error) {
    throw new Error(`Invalid JSON in ${resolved}: ${error.message}`);
  }
}

function listThemes() {
  if (!fs.existsSync(THEMES_DIR)) return [];
  const documents = fs.readdirSync(THEMES_DIR)
    .filter(name => name.endsWith(".json"))
    .sort()
    .map(name => ({ name, value: readJson(path.join(THEMES_DIR, name)) }));
  const catalogThemes = documents.flatMap(({ value }) => {
    if (Array.isArray(value)) return value;
    if (isPlainObject(value) && Array.isArray(value.themes)) return value.themes;
    return [];
  });
  const individualThemes = documents
    .map(({ value }) => value)
    .filter(theme => isPlainObject(theme) && typeof theme.id === "string" && theme.id.trim());
  const themesById = new Map();
  [...catalogThemes, ...individualThemes].forEach(theme => {
    if (!isPlainObject(theme) || typeof theme.id !== "string" || !theme.id.trim()) return;
    themesById.set(theme.id, theme);
  });
  return [...themesById.values()].sort((left, right) => left.id.localeCompare(right.id));
}

function getTheme(themeId) {
  return listThemes().find(theme => theme.id === themeId) || null;
}

function themeManifestRecord(theme) {
  return {
    id: theme.id,
    name: theme.name || theme.id,
    description: theme.description || "",
    source_reference: theme.source_reference || "builtin",
    selection: isPlainObject(theme.selection) ? deepClone(theme.selection) : {},
    palette: isPlainObject(theme.palette) ? deepClone(theme.palette) : {},
    typography: isPlainObject(theme.typography) ? deepClone(theme.typography) : {},
    shape: isPlainObject(theme.shape) ? deepClone(theme.shape) : {},
    style: isPlainObject(theme.style) ? deepClone(theme.style) : {},
    composition: compositionManifestRecord(theme),
  };
}

function characterLength(value) {
  return Array.from(String(value)).length;
}

function looksLikeMarkup(value) {
  return /<\/?[a-z][^>]*>/i.test(String(value));
}

function validateText(value, contract, fieldPath, issues) {
  if (typeof value !== "string") {
    issues.push(`${fieldPath}: expected text, got ${Array.isArray(value) ? "array" : typeof value}`);
    return;
  }
  if (contract.required && !value.trim()) {
    issues.push(`${fieldPath}: required text must not be empty`);
  }
  const length = characterLength(value.trim());
  if (Number.isInteger(contract.maxChars) && length > contract.maxChars) {
    issues.push(`${fieldPath}: ${length} characters exceeds maxChars ${contract.maxChars}`);
  }
  if (looksLikeMarkup(value)) {
    issues.push(`${fieldPath}: HTML markup is not allowed; controlled deck text is plain text`);
  }
  if (value.includes("\u0000")) {
    issues.push(`${fieldPath}: NUL characters are not allowed`);
  }
}

function validateMedia(value, contract, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(
      `${fieldPath}: expected media object like ` +
      `{"src":"assets/generated/image.png","alt":"image description"}`
    );
    return;
  }
  const unknown = Object.keys(value).filter(key =>
    !["src", "alt", "origin", "fit", "position", "treatment"].includes(key)
  );
  if (unknown.length) {
    issues.push(`${fieldPath}: unknown media field(s): ${unknown.join(", ")}`);
  }
  if (typeof value.src !== "string" || !value.src.trim()) {
    issues.push(`${fieldPath}.src: expected a non-empty image path`);
  } else {
    const src = value.src.trim();
    const isDataImage = /^data:image\/[a-z0-9.+-]+;base64,/i.test(src);
    const isRemote = /^[a-z][a-z0-9+.-]*:/i.test(src);
    const segments = src.replace(/\\/g, "/").split("/");
    if (!isDataImage && isRemote) {
      issues.push(`${fieldPath}.src: remote or executable URLs are not allowed; localize the asset first`);
    }
    if (!isDataImage && (path.isAbsolute(src) || segments.includes(".."))) {
      issues.push(`${fieldPath}.src: use an artifact-root-relative path without '..' segments`);
    }
  }
  if (value.alt !== undefined && typeof value.alt !== "string") {
    issues.push(`${fieldPath}.alt: expected text`);
  } else if (typeof value.alt === "string" && characterLength(value.alt) > 160) {
    issues.push(`${fieldPath}.alt: exceeds 160 characters`);
  }
  const mediaEnums = {
    origin: ["generated", "asset", "uploaded"],
    fit: ["cover", "contain"],
    position: ["center", "left", "right", "top", "bottom"],
    treatment: ["wash-light", "wash-dark", "none"],
  };
  Object.entries(mediaEnums).forEach(([key, values]) => {
    if (value[key] !== undefined && !values.includes(value[key])) {
      issues.push(`${fieldPath}.${key}: expected one of ${values.join(", ")}`);
    }
  });
  if (!Array.isArray(contract.allowedKinds) || !contract.allowedKinds.includes("image")) {
    issues.push(`${fieldPath}: layout contract does not allow image media`);
  }
}

function validateAndNormalizeBackground(value, fieldPath, issues) {
  if (value === undefined) return null;
  const contract = { allowedKinds: ["image"] };
  validateMedia(value, contract, fieldPath, issues);
  if (!isPlainObject(value)) return null;
  return {
    src: typeof value.src === "string" ? value.src.trim() : value.src,
    ...(value.alt === undefined ? {} : { alt: value.alt }),
    ...(value.origin === undefined ? {} : { origin: value.origin }),
    fit: value.fit || "cover",
    position: value.position || "center",
    treatment: value.treatment || "wash-light",
  };
}

function validateShape(value, shape, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return;
  }
  const unknown = Object.keys(value).filter(key => !Object.prototype.hasOwnProperty.call(shape, key));
  if (unknown.length) {
    issues.push(`${fieldPath}: unknown field(s): ${unknown.join(", ")}`);
  }
  for (const [key, contract] of Object.entries(shape)) {
    validateField(value[key], contract, `${fieldPath}.${key}`, issues);
  }
}

function validateArray(value, contract, fieldPath, issues) {
  if (!Array.isArray(value)) {
    issues.push(`${fieldPath}: expected array`);
    return;
  }
  if (value.length < contract.minItems || value.length > contract.maxItems) {
    issues.push(
      `${fieldPath}: expected ${contract.minItems}-${contract.maxItems} item(s), got ${value.length}`
    );
  }
  value.forEach((item, index) => {
    const itemPath = `${fieldPath}.${index}`;
    if (contract.itemShape && contract.itemShape.type) {
      validateField(item, contract.itemShape, itemPath, issues);
    } else {
      validateShape(item, contract.itemShape || {}, itemPath, issues);
    }
  });
}

function validateField(value, contract, fieldPath, issues) {
  const missing = value === undefined || value === null;
  if (missing) {
    if (contract.required) issues.push(`${fieldPath}: required field is missing`);
    return;
  }
  if (contract.type === "text") {
    validateText(value, contract, fieldPath, issues);
  } else if (contract.type === "enum") {
    if (typeof value !== "string" || !contract.values.includes(value)) {
      issues.push(`${fieldPath}: expected one of ${contract.values.join(", ")}`);
    }
  } else if (contract.type === "media") {
    validateMedia(value, contract, fieldPath, issues);
  } else if (contract.type === "array") {
    validateArray(value, contract, fieldPath, issues);
  } else if (contract.type === "object") {
    validateShape(value, contract.shape || {}, fieldPath, issues);
  } else if (contract.type === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      issues.push(`${fieldPath}: expected finite number`);
    } else {
      if (Number.isFinite(contract.min) && value < contract.min) {
        issues.push(`${fieldPath}: must be at least ${contract.min}`);
      }
      if (Number.isFinite(contract.max) && value > contract.max) {
        issues.push(`${fieldPath}: must be at most ${contract.max}`);
      }
    }
  } else if (contract.type === "boolean" && typeof value !== "boolean") {
    issues.push(`${fieldPath}: expected boolean`);
  } else if (!["text", "enum", "media", "array", "object", "number", "boolean"].includes(contract.type)) {
    issues.push(`${fieldPath}: unsupported contract type ${JSON.stringify(contract.type)}`);
  }
}

function validateAndNormalizeLayoutProps(value, layout, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  const normalized = mergeDefaults(layout.defaultProps || {}, value);
  const unknownProps = Object.keys(normalized)
    .filter(key => !Object.prototype.hasOwnProperty.call(layout.fields, key));
  if (unknownProps.length) {
    issues.push(
      `${fieldPath}: unknown field(s): ${unknownProps.join(", ")}; ` +
      `allowed fields for ${layout.id}: ${Object.keys(layout.fields).join(", ")}`
    );
  }
  for (const [fieldName, contract] of Object.entries(layout.fields)) {
    validateField(normalized[fieldName], contract, `${fieldPath}.${fieldName}`, issues);
  }
  return normalized;
}

function validateAndNormalizeLayoutDrafts(value, fieldPath, issues) {
  if (value === undefined) return null;
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected an object keyed by layout id`);
    return null;
  }
  const entries = Object.entries(value);
  if (entries.length > layouts.length) {
    issues.push(`${fieldPath}: expected at most ${layouts.length} saved layout draft(s)`);
  }
  const normalized = {};
  entries.forEach(([layoutId, props]) => {
    const layout = getLayout(layoutId);
    if (!layout) {
      issues.push(`${fieldPath}.${layoutId}: unknown layout`);
      return;
    }
    const draft = validateAndNormalizeLayoutProps(
      props,
      layout,
      `${fieldPath}.${layoutId}`,
      issues
    );
    if (draft) normalized[layoutId] = draft;
  });
  return Object.keys(normalized).length ? normalized : null;
}

function validateAndNormalizeOutlineIntent(value, fieldPath, issues) {
  if (value === undefined) return null;
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  const contracts = {
    title: { type: "text", maxChars: 120, required: true },
    message: { type: "text", maxChars: 500, required: true },
    layout: { type: "text", maxChars: 120, required: true },
    visual: { type: "text", maxChars: 300, required: true },
  };
  const unknown = Object.keys(value).filter(
    key => !Object.prototype.hasOwnProperty.call(contracts, key)
      && key !== "visual_item_contract"
  );
  if (unknown.length) {
    issues.push(`${fieldPath}: unknown field(s): ${unknown.join(", ")}`);
  }
  const normalized = {};
  Object.entries(contracts).forEach(([key, contract]) => {
    validateField(value[key], contract, `${fieldPath}.${key}`, issues);
    normalized[key] = typeof value[key] === "string" ? value[key].trim() : value[key];
  });
  if (value.visual_item_contract !== undefined) {
    const itemContract = value.visual_item_contract;
    if (
      !isPlainObject(itemContract)
      || !Number.isInteger(itemContract.count)
      || itemContract.count < 0
      || typeof itemContract.dimension !== "string"
      || !itemContract.dimension.trim()
    ) {
      issues.push(
        `${fieldPath}.visual_item_contract: expected {dimension, count}`
      );
    } else {
      normalized.visual_item_contract = {
        dimension: itemContract.dimension.trim(),
        count: itemContract.count,
      };
    }
  }
  return normalized;
}

function normalizeSlide(
  slide,
  normalizedProps,
  normalizedDrafts,
  normalizedBackground,
  normalizedOutlineIntent
) {
  return {
    id: slide.id,
    layout_id: slide.layout_id,
    props: normalizedProps,
    ...(slide.source_outline_page === undefined
      ? {}
      : { source_outline_page: slide.source_outline_page }),
    ...(slide.source_outline_item_range === undefined
      ? {}
      : { source_outline_item_range: { ...slide.source_outline_item_range } }),
    ...(normalizedOutlineIntent ? { outline_intent: normalizedOutlineIntent } : {}),
    ...(normalizedDrafts ? { layout_drafts: normalizedDrafts } : {}),
    ...(normalizedBackground ? { background: normalizedBackground } : {}),
  };
}

function validateChartDataProps(props, fieldPath, issues) {
  const categories = Array.isArray(props && props.categories) ? props.categories : [];
  const series = Array.isArray(props && props.series) ? props.series : [];
  series.forEach((item, seriesIndex) => {
    const values = Array.isArray(item && item.values) ? item.values : [];
    if (values.length !== categories.length) {
      issues.push(
        `${fieldPath}.series.${seriesIndex}.values: expected ${categories.length} ` +
        `value(s) to match categories, got ${values.length}`
      );
    }
    values.forEach((value, valueIndex) => {
      if (!/-?\d+(?:,\d{3})*(?:\.\d+)?/.test(String(value == null ? "" : value))) {
        issues.push(
          `${fieldPath}.series.${seriesIndex}.values.${valueIndex}: expected a numeric ` +
          "chart value; units may be included, but placeholders are not valid data. " +
          "Keep a complete factual category/series subset, move isolated metrics to " +
          "highlights or narrative fields, and never invent a missing baseline or forecast"
        );
      }
    });
  });
}

function validateTechnicalDiagramProps(props, fieldPath, issues) {
  const nodes = Array.isArray(props && props.nodes) ? props.nodes : [];
  const edges = Array.isArray(props && props.edges) ? props.edges : [];
  const nodeIds = new Set();
  const edgeIds = new Set();
  const pipelineLabels = new Map();
  nodes.forEach((node, nodeIndex) => {
    const nodeId = String(node && node.id || "");
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$/.test(nodeId)) {
      issues.push(
        `${fieldPath}.nodes.${nodeIndex}.id: use 1-32 letters, numbers, '_' or '-', starting with alphanumeric`
      );
    } else if (nodeIds.has(nodeId)) {
      issues.push(`${fieldPath}.nodes.${nodeIndex}.id: duplicate node id ${JSON.stringify(nodeId)}`);
    } else {
      nodeIds.add(nodeId);
    }
    if (props && props.diagram_kind === "pipeline") {
      const label = String(node && node.label || "").trim();
      const normalizedLabel = label.toLocaleLowerCase().replace(/\s+/g, "");
      if (normalizedLabel && pipelineLabels.has(normalizedLabel)) {
        issues.push(
          `${fieldPath}.nodes.${nodeIndex}.label: duplicate pipeline stage label ` +
          `${JSON.stringify(label)}; use unique stage names and represent feedback with an edge`
        );
      } else if (normalizedLabel) {
        pipelineLabels.set(normalizedLabel, nodeIndex);
      }
    }
  });
  edges.forEach((edge, edgeIndex) => {
    const edgeId = String(edge && edge.id || "");
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$/.test(edgeId)) {
      issues.push(
        `${fieldPath}.edges.${edgeIndex}.id: use 1-48 letters, numbers, '_' or '-', starting with alphanumeric`
      );
    } else if (edgeIds.has(edgeId)) {
      issues.push(`${fieldPath}.edges.${edgeIndex}.id: duplicate edge id ${JSON.stringify(edgeId)}`);
    } else {
      edgeIds.add(edgeId);
    }
    ["source", "target"].forEach(endpoint => {
      const nodeId = String(edge && edge[endpoint] || "");
      if (!nodeIds.has(nodeId)) {
        issues.push(
          `${fieldPath}.edges.${edgeIndex}.${endpoint}: unknown node id ${JSON.stringify(nodeId)}`
        );
      }
    });
  });
}

function normalizeAndValidateSwimlaneProps(props, fieldPath, issues) {
  const columns = Array.isArray(props && props.columns) ? props.columns : [];
  const lanes = Array.isArray(props && props.lanes) ? props.lanes : [];
  lanes.forEach((lane, laneIndex) => {
    const activities = Array.isArray(lane && lane.activities) ? lane.activities : [];
    while (activities.length < columns.length) activities.push("待补充活动");
    if (activities.length > columns.length) {
      issues.push(
        `${fieldPath}.lanes.${laneIndex}.activities: expected ${columns.length} ` +
        `activity cell(s) to match columns, got ${activities.length}`
      );
    }
  });
}

function validateAndNormalizeDeck(spec) {
  const issues = [];
  const warnings = [];
  if (!isPlainObject(spec)) {
    return { ok: false, issues: ["Deck spec must be an object"], warnings, normalized: null };
  }

  const topLevelFields = [
    "schema_version",
    "title",
    "theme_id",
    "design",
    "design_contract",
    "truth_contract",
    "slides",
  ];
  const unknownTopLevel = Object.keys(spec).filter(key => !topLevelFields.includes(key));
  if (unknownTopLevel.length) {
    issues.push(`Unknown top-level field(s): ${unknownTopLevel.join(", ")}`);
  }
  if (spec.schema_version !== 1) issues.push("schema_version must be 1");
  if (typeof spec.title !== "string" || !spec.title.trim()) {
    issues.push("title must be non-empty text");
  } else if (characterLength(spec.title.trim()) > 120) {
    issues.push("title exceeds 120 characters");
  }
  const theme = typeof spec.theme_id === "string" ? getTheme(spec.theme_id) : null;
  if (!theme) {
    issues.push(
      `Unknown theme_id: ${JSON.stringify(spec.theme_id)}; ` +
      `registered theme_ids: ${listThemes().map(item => item.id).join(", ")}`
    );
  }
  if (!Array.isArray(spec.slides)) {
    issues.push("slides must be an array");
    return { ok: false, issues, warnings, normalized: null };
  }
  if (spec.slides.length < 1 || spec.slides.length > 40) {
    issues.push(`slides must contain 1-40 entries, got ${spec.slides.length}`);
  }

  const ids = new Set();
  const normalizedSlides = [];
  const normalizedTruthContract = validateAndNormalizeTruthContract(
    spec.truth_contract,
    "truth_contract",
    issues
  );
  const normalizedDesign = theme
    ? validateAndNormalizeDeckDesign(spec.design, theme, issues, warnings)
    : null;
  const normalizedDesignContract = validateAndNormalizeDesignContract(
    spec.design_contract,
    issues
  );
  spec.slides.forEach((slide, index) => {
    const slidePath = `slides.${index}`;
    if (!isPlainObject(slide)) {
      issues.push(`${slidePath}: expected object`);
      return;
    }
    const allowedSlideFields = [
      "id",
      "layout_id",
      "props",
      "source_outline_page",
      "source_outline_item_range",
      "outline_intent",
      "layout_drafts",
      "background",
    ];
    const unknownSlideFields = Object.keys(slide).filter(key => !allowedSlideFields.includes(key));
    if (unknownSlideFields.length) {
      issues.push(`${slidePath}: unknown field(s): ${unknownSlideFields.join(", ")}`);
    }
    if (typeof slide.id !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(slide.id)) {
      issues.push(`${slidePath}.id: use 1-64 letters, numbers, '_' or '-', starting with alphanumeric`);
    } else if (ids.has(slide.id)) {
      issues.push(`${slidePath}.id: duplicate slide id ${JSON.stringify(slide.id)}`);
    } else {
      ids.add(slide.id);
    }
    const layout = typeof slide.layout_id === "string" ? getLayout(slide.layout_id) : null;
    if (!layout) {
      issues.push(
        `${slidePath}.layout_id: unknown layout ${JSON.stringify(slide.layout_id)}; ` +
        `registered layout_ids: ${layouts.map(item => item.id).join(", ")}`
      );
      return;
    }
    if (!isPlainObject(slide.props)) {
      issues.push(`${slidePath}.props: expected object`);
      return;
    }
    if (slide.source_outline_page !== undefined &&
        (!Number.isInteger(slide.source_outline_page) || slide.source_outline_page < 1)) {
      issues.push(`${slidePath}.source_outline_page: expected positive integer`);
    }
    if (slide.source_outline_item_range !== undefined) {
      const range = slide.source_outline_item_range;
      if (
        !isPlainObject(range)
        || !["start", "end", "total", "part", "parts"].every(
          key => Number.isInteger(range[key]) && range[key] >= 1
        )
        || range.start > range.end
        || range.end > range.total
        || range.part > range.parts
      ) {
        issues.push(
          `${slidePath}.source_outline_item_range: expected valid start/end/total/part/parts`
        );
      }
    }
    const normalizedProps = validateAndNormalizeLayoutProps(
      slide.props,
      layout,
      `${slidePath}.props`,
      issues
    );
    if (slide.layout_id === "chart-data-v1") {
      validateChartDataProps(normalizedProps, `${slidePath}.props`, issues);
    } else if (slide.layout_id === "technical-diagram-v1") {
      validateTechnicalDiagramProps(normalizedProps, `${slidePath}.props`, issues);
    } else if (slide.layout_id === "swimlane-process-v1") {
      normalizeAndValidateSwimlaneProps(normalizedProps, `${slidePath}.props`, issues);
    }
    const normalizedDrafts = validateAndNormalizeLayoutDrafts(
      slide.layout_drafts,
      `${slidePath}.layout_drafts`,
      issues
    );
    const normalizedBackground = validateAndNormalizeBackground(
      slide.background,
      `${slidePath}.background`,
      issues
    );
    const normalizedOutlineIntent = validateAndNormalizeOutlineIntent(
      slide.outline_intent,
      `${slidePath}.outline_intent`,
      issues
    );
    const normalizedSlide = normalizeSlide(
      slide,
      normalizedProps,
      normalizedDrafts,
      normalizedBackground,
      normalizedOutlineIntent
    );
    normalizedSlides.push(normalizedSlide);
  });

  if (normalizedDesignContract && normalizedDesignContract.slides) {
    Object.entries(normalizedDesignContract.slides).forEach(([slideId, contract]) => {
      const slide = normalizedSlides.find(item => item.id === slideId);
      if (!slide) {
        issues.push(`design_contract.slides.${slideId}: no matching deck slide`);
        return;
      }
      const layout = getLayout(slide.layout_id);
      const visualKinds = layout && Array.isArray(layout.visualKinds)
        ? layout.visualKinds
        : [];
      if (
        contract.source === "explicit"
        && !visualKinds.includes(contract.visual_kind)
      ) {
        issues.push(
          `design_contract.slides.${slideId}.visual_kind: explicit ` +
          `${JSON.stringify(contract.visual_kind)} is not supported by ${slide.layout_id}; ` +
          `supported kinds: ${visualKinds.join(", ")}`
        );
      }
      if (
        contract.source === "explicit"
        && contract.direction
        && layout
        && Array.isArray(layout.directions)
        && layout.directions.length
        && !layout.directions.includes(contract.direction)
      ) {
        issues.push(
          `design_contract.slides.${slideId}.direction: ${slide.layout_id} does not support ` +
          `${JSON.stringify(contract.direction)}`
        );
      }
      if (
        contract.source === "explicit"
        && contract.relationship
        && layout
        && Array.isArray(layout.relationships)
        && layout.relationships.length
        && !layout.relationships.includes(contract.relationship)
      ) {
        issues.push(
          `design_contract.slides.${slideId}.relationship: ${slide.layout_id} does not support ` +
          `${JSON.stringify(contract.relationship)}`
        );
      }
      const collectionContract = getVisualCollectionContract(
        slide.layout_id,
        contract.item_dimension
      );
      const collectionField = collectionContract && collectionContract.path;
      const collection = collectionField && slide.props
        ? collectionField.split(".").reduce((value, part) => (
          value && typeof value === "object" ? value[part] : undefined
        ), slide.props)
        : null;
      if (
        contract.source === "explicit"
        && Number.isInteger(contract.item_count)
        && Array.isArray(collection)
        && collection.length !== contract.item_count
      ) {
        issues.push(
          `design_contract.slides.${slideId}.item_count: explicit contract requires ` +
          `${contract.item_count}, got ${collection.length} in props.${collectionField}`
        );
      }
    });
  }

  const normalized = {
    schema_version: 1,
    title: typeof spec.title === "string" ? spec.title.trim() : "",
    theme_id: typeof spec.theme_id === "string" ? spec.theme_id : "",
    ...(normalizedDesign ? { design: normalizedDesign } : {}),
    ...(normalizedDesignContract ? { design_contract: normalizedDesignContract } : {}),
    ...(normalizedTruthContract ? { truth_contract: normalizedTruthContract } : {}),
    slides: normalizedSlides,
  };
  return { ok: issues.length === 0, issues, warnings, normalized };
}

function buildManifest() {
  return {
    schema_version: 1,
    generated_from: "layouts/registry.js + themes/*.json",
    default_theme_id: DEFAULT_THEME_ID,
    composition_directions: compositionDirectionCatalog(),
    themes: listThemes().map(themeManifestRecord),
    layouts: layouts.map(manifestRecord),
  };
}

module.exports = {
  DEFAULT_THEME_ID,
  MANIFEST_PATH,
  SKILL_ROOT,
  TRUTH_TEXT_MAX_CHARACTERS,
  buildManifest,
  createDeckDesign,
  getLayout,
  getTheme,
  listThemes,
  normalizeLayoutId,
  readJson,
  resolveArtifactPath,
  resolveDeckDesign,
  themeManifestRecord,
  mergeDefaults,
  runtimeSourceBinding,
  validateAssumptionsAgainstRuntime,
  validateResearchFactsAgainstRuntime,
  validateSourceFactsAgainstRuntime,
  validateAndNormalizeDeck,
};
