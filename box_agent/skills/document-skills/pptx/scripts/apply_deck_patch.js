#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const {
  getLayout,
  mergeDefaults,
  readJson,
  resolveArtifactPath,
  runtimeSourceBinding,
  validateAssumptionsAgainstRuntime,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const {
  sanitizeStrictSourceDeck,
  validateSourceBoundDeck,
} = require("./validate_deck_truth.js");
const {
  expectedVisualItemContract,
} = require("./outline_layout_contract.js");
const { EDITOR_PLACEHOLDER_IMAGE, getVisualCollectionContract } = require("../layouts/registry.js");

function usage(exitCode = 2) {
  console.log("Usage: apply_deck_patch.js deck.json deck.patch.json");
  process.exit(exitCode);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function characterLength(value) {
  return Array.from(String(value == null ? "" : value).trim()).length;
}

function fitCaption(value, maxChars) {
  const text = String(value == null ? "" : value).trim();
  const characters = Array.from(text);
  if (!Number.isInteger(maxChars) || characters.length <= maxChars) return text;
  if (maxChars <= 1) return characters.slice(0, maxChars).join("");
  const clipped = characters.slice(0, maxChars - 1).join("");
  const sentenceBreak = Math.max(
    clipped.lastIndexOf("。"),
    clipped.lastIndexOf("；"),
    clipped.lastIndexOf(";"),
    clipped.lastIndexOf(".")
  );
  if (sentenceBreak >= Math.floor(maxChars * 0.6)) {
    return clipped.slice(0, sentenceBreak + 1).trim();
  }
  return `${clipped.trimEnd()}…`;
}

function extractClientBenefit(value) {
  const text = String(value == null ? "" : value).trim();
  const match = text.match(/(?:客户收益|收益|价值)\s*[：:]\s*([\s\S]+)$/u);
  return match ? match[1].trim() : "";
}

function recordChange(changes, message) {
  if (!changes.includes(message)) changes.push(message);
}

function readPatchJson(filePath, changes) {
  const resolved = resolveArtifactPath(filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`File not found: ${resolved}`);
  }
  const raw = fs.readFileSync(resolved, "utf8");
  try {
    return JSON.parse(raw);
  } catch (initialError) {
    let candidate = raw.trimEnd();
    for (let removed = 1; removed <= 8; removed += 1) {
      if (!/[}\]]$/.test(candidate)) break;
      candidate = candidate.slice(0, -1).trimEnd();
      try {
        const parsed = JSON.parse(candidate);
        if (!isPlainObject(parsed)) break;
        recordChange(
          changes,
          `patch: removed ${removed} redundant trailing JSON closer(s)`
        );
        return parsed;
      } catch (_error) {
        // Only keep trying while the invalid suffix consists of closing tokens.
      }
    }
    throw new Error(`Invalid JSON in ${resolved}: ${initialError.message}`);
  }
}

function generatedMedia(value, fallbackAlt, changes, fieldPath) {
  if (value === null || value === undefined) return value;
  const media = typeof value === "string"
    ? { src: value, alt: fallbackAlt }
    : isPlainObject(value)
      ? clone(value)
      : value;
  if (isPlainObject(media)) {
    if (media.src === undefined && isPlainObject(media.image)) {
      const nestedImage = media.image;
      ["src", "alt", "origin", "fit", "position", "treatment"].forEach(key => {
        if (media[key] === undefined && nestedImage[key] !== undefined) {
          media[key] = nestedImage[key];
        }
      });
      delete media.image;
      recordChange(changes, `${fieldPath}.image: flattened nested media object`);
    }
    if (media.src === undefined) {
      const sourceAlias = ["path", "image"].find(
        key => typeof media[key] === "string"
      );
      if (sourceAlias) {
        media.src = media[sourceAlias];
        recordChange(changes, `${fieldPath}.${sourceAlias}: mapped to src`);
      }
    }
    if (media.path !== undefined) delete media.path;
    if (media.image !== undefined) delete media.image;
    if (media.alt === undefined && media.alt_text !== undefined) {
      media.alt = media.alt_text;
      recordChange(changes, `${fieldPath}.alt_text: mapped to alt`);
    }
    if (media.alt_text !== undefined) delete media.alt_text;
    const allowed = ["src", "alt", "origin", "fit", "position", "treatment"];
    Object.keys(media).forEach(key => {
      if (allowed.includes(key)) return;
      delete media[key];
      recordChange(changes, `${fieldPath}.${key}: dropped unknown media field`);
    });
  }
  if (!isPlainObject(media) || typeof media.src !== "string") return media;
  if (/^(?:\.\/)?assets\/generated\//i.test(media.src.trim())) {
    if (media.origin !== "generated") {
      media.origin = "generated";
      recordChange(changes, `${fieldPath}.origin: inferred generated provenance from asset path`);
    }
    if (!media.alt || !String(media.alt).trim()) media.alt = fallbackAlt;
  }
  if (typeof value === "string") {
    recordChange(changes, `${fieldPath}: converted image path to a media object`);
  }
  return media;
}

function setNestedProp(target, propPath, value) {
  const parts = String(propPath || "")
    .split(".")
    .map(part => part.trim())
    .filter(Boolean);
  if (parts[0] === "props") parts.shift();
  if (!parts.length) return false;
  let current = target;
  for (const part of parts.slice(0, -1)) {
    if (!isPlainObject(current[part])) current[part] = {};
    current = current[part];
  }
  current[parts[parts.length - 1]] = value;
  return true;
}

function reconcileReadyManifestMedia(deck, deckPath, changes, options = {}) {
  const artifactRoot = path.dirname(deckPath);
  const manifestPath = options.manifestPath || path.join(
    artifactRoot,
    "assets",
    "generated",
    "manifest.json"
  );
  if (!fs.existsSync(manifestPath)) return;

  let manifest;
  try {
    manifest = readJson(manifestPath);
  } catch (_error) {
    // The final image-manifest validator owns malformed-manifest diagnostics.
    return;
  }
  const imagePlan = isPlainObject(manifest) ? manifest.image_plan : null;
  if (!Array.isArray(imagePlan)) return;

  const slidesById = new Map(deck.slides.filter(isPlainObject).map(slide => [slide.id, slide]));
  for (const entry of imagePlan) {
    if (!isPlainObject(entry)) continue;
    if (!["generate", "use_existing"].includes(entry.decision)) continue;
    const status = typeof entry.status === "string"
      ? entry.status.trim().toLowerCase()
      : "";
    if (["blocked", "failed", "error", "skipped"].includes(status)) continue;
    const outputPath = typeof entry.output_path === "string"
      ? entry.output_path.trim()
      : "";
    if (!outputPath) continue;
    const absoluteAssetPath = path.resolve(artifactRoot, outputPath);
    const relativeAssetPath = path.relative(artifactRoot, absoluteAssetPath);
    if (
      !relativeAssetPath
      || relativeAssetPath.startsWith(`..${path.sep}`)
      || relativeAssetPath === ".."
      || path.isAbsolute(relativeAssetPath)
    ) {
      continue;
    }
    try {
      const assetStat = fs.statSync(absoluteAssetPath);
      if (!assetStat.isFile() || assetStat.size <= 0) continue;
    } catch (_error) {
      continue;
    }

    const slide = slidesById.get(entry.slide_id);
    if (!slide) continue;
    const propPath = typeof entry.prop_path === "string"
      ? entry.prop_path.trim()
      : "";
    if (!propPath) continue;
    const isBackground = propPath === "background" || propPath === "slide.background";
    const normalizedPropPath = propPath.startsWith("props.")
      ? propPath.slice("props.".length)
      : propPath;
    const propParts = normalizedPropPath.split(".");
    if (propParts.some(part => !part || ["__proto__", "prototype", "constructor"].includes(part))) continue;
    if (options.onlyMissing) {
      const current = isBackground ? slide.background : propParts.reduce(
        (value, part) => value && value[part], slide.props
      );
      const currentSrc = typeof current === "string" ? current : current && current.src;
      if (typeof currentSrc === "string" && currentSrc.trim()
          && currentSrc !== EDITOR_PLACEHOLDER_IMAGE) continue;
    }
    const sourcedFromWeb = entry.resolved_via === "web" || entry.origin === "sourced";
    const origin = entry.decision === "use_existing" ? "asset" : "generated";
    const sourceTitle = isPlainObject(entry.source)
      ? String(entry.source.title || "").trim()
      : "";
    const media = generatedMedia(
      {
        src: outputPath,
        alt: sourcedFromWeb
          ? sourceTitle || "联网搜索来源的演示文稿视觉"
          : entry.decision === "use_existing"
            ? "用户提供的演示文稿视觉"
          : propPath === "background"
            ? "AI 生成的演示文稿背景概念视觉"
            : "AI 生成的演示文稿概念视觉",
        origin,
        ...(typeof entry.treatment === "string"
          ? { treatment: entry.treatment }
          : {}),
      },
      "演示文稿视觉",
      changes,
      `manifest.${slide.id}.${propPath}`
    );
    if (isBackground) {
      slide.background = media;
      recordChange(
        changes,
        `manifest.${slide.id}.${propPath}: bound ready media to slide background`
      );
      continue;
    }

    const rootProp = normalizedPropPath.split(".", 1)[0];
    const layout = getLayout(slide.layout_id);
    if (!layout || !layout.fields || !layout.fields[rootProp]) continue;
    if (!isPlainObject(slide.props)) slide.props = {};
    if (setNestedProp(slide.props, normalizedPropPath, media)) {
      recordChange(
        changes,
        `manifest.${slide.id}.${propPath}: bound ready media to slide props`
      );
    }
  }
}

function applyAlias(target, destination, aliases, changes, fieldPath) {
  if (target[destination] !== undefined) return;
  const source = aliases.find(alias => target[alias] !== undefined);
  if (!source) return;
  target[destination] = target[source];
  recordChange(changes, `${fieldPath}.${source}: mapped to ${destination}`);
}

function normalizeArrayItem(layoutId, fieldName, item, changes, fieldPath) {
  if (layoutId === "table-data-v1" && fieldName === "rows" && Array.isArray(item)) {
    return item.map((cell, columnIndex) => {
      const value = cell == null ? "" : String(cell).trim();
      if (value) return value;
      recordChange(
        changes,
        `${fieldPath}.${columnIndex}: replaced empty schedule/table cell with an em dash`
      );
      return "—";
    });
  }
  if (
    layoutId === "statement-focus-v1" &&
    fieldName === "proofs" &&
    typeof item === "string"
  ) {
    recordChange(changes, `${fieldPath}: converted proof text to an object`);
    return { value: item, label: "" };
  }
  if (!isPlainObject(item)) return item;
  const normalized = clone(item);
  if (
    ["cards-grid-v1", "quadrant-matrix-v1", "pyramid-hierarchy-v1"].includes(layoutId)
    && fieldName === "items"
  ) {
    applyAlias(normalized, "title", ["name", "label"], changes, fieldPath);
    applyAlias(normalized, "body", ["description", "text", "content"], changes, fieldPath);
  } else if (layoutId === "kpi-grid-v1" && fieldName === "items") {
    applyAlias(normalized, "detail", ["trend", "description", "body"], changes, fieldPath);
    if (normalized.unit && normalized.value !== undefined) {
      normalized.value = `${normalized.value}${normalized.unit}`;
      recordChange(changes, `${fieldPath}.unit: folded into value`);
    }
  } else if (layoutId === "architecture-layered-v1" && fieldName === "layers") {
    applyAlias(normalized, "title", ["name"], changes, fieldPath);
    applyAlias(normalized, "modules", ["items", "components"], changes, fieldPath);
  } else if (layoutId === "system-integration-v1" && fieldName === "systems") {
    applyAlias(normalized, "title", ["name", "system"], changes, fieldPath);
    applyAlias(normalized, "flow", ["description", "body", "data"], changes, fieldPath);
  } else if (layoutId === "dashboard-overview-v1" && fieldName === "items") {
    applyAlias(normalized, "title", ["name"], changes, fieldPath);
    applyAlias(normalized, "detail", ["description", "body", "focus"], changes, fieldPath);
  } else if (layoutId === "timeline-horizontal-v1" && fieldName === "steps") {
    applyAlias(normalized, "title", ["step", "name"], changes, fieldPath);
    applyAlias(normalized, "body", ["description", "text", "content"], changes, fieldPath);
  } else if (layoutId === "project-case-study-v1" && fieldName === "metrics") {
    applyAlias(normalized, "label", ["name", "title"], changes, fieldPath);
    applyAlias(normalized, "value", ["metric", "result"], changes, fieldPath);
  } else if (layoutId === "statement-focus-v1" && fieldName === "proofs") {
    applyAlias(normalized, "label", ["title", "description"], changes, fieldPath);
    applyAlias(normalized, "value", ["metric", "result"], changes, fieldPath);
  }
  return normalized;
}

function normalizeValueToContract(value, contract, layoutId, fieldName, changes, fieldPath) {
  if (
    contract.type === "text"
    && fieldName === "source"
    && typeof value === "string"
    && Number.isInteger(contract.maxChars)
    && characterLength(value) > contract.maxChars
  ) {
    const compacted = fitCaption(value, contract.maxChars);
    recordChange(
      changes,
      `${fieldPath}: compacted optional source caption to ${contract.maxChars} characters`
    );
    return compacted;
  }
  if (contract.type === "media") {
    const alt = layoutId === "project-case-study-v1"
      ? "AI 概念视觉，实际项目图待补充"
      : "AI 生成的概念视觉";
    return generatedMedia(value, alt, changes, fieldPath);
  }
  if (contract.type === "enum") {
    if (typeof value === "string" && contract.values.includes(value)) return value;
    recordChange(changes, `${fieldPath}: dropped invalid enum value ${JSON.stringify(value)}`);
    return undefined;
  }
  if (contract.type === "array" && Array.isArray(value)) {
    const normalizedItems = value.map((item, index) => {
      const itemPath = `${fieldPath}.${index}`;
      const normalizedItem = normalizeArrayItem(
        layoutId,
        fieldName,
        item,
        changes,
        itemPath
      );
      if (!isPlainObject(normalizedItem) || !isPlainObject(contract.itemShape)) {
        return normalizedItem;
      }
      const allowed = Object.keys(contract.itemShape);
      const filtered = {};
      Object.entries(normalizedItem).forEach(([key, nested]) => {
        if (!allowed.includes(key)) {
          recordChange(changes, `${itemPath}.${key}: dropped unknown item field`);
          return;
        }
        const normalizedNested = normalizeValueToContract(
          nested,
          contract.itemShape[key],
          layoutId,
          key,
          changes,
          `${itemPath}.${key}`
        );
        if (normalizedNested !== undefined) filtered[key] = normalizedNested;
      });
      Object.entries(contract.itemShape).forEach(([key, nestedContract]) => {
        if (!nestedContract.required || filtered[key] !== undefined) return;
        if (nestedContract.type === "text") {
          filtered[key] = "待补充";
          recordChange(changes, `${itemPath}.${key}: filled required text placeholder`);
        } else if (nestedContract.type === "enum" && nestedContract.default !== undefined) {
          filtered[key] = clone(nestedContract.default);
          recordChange(changes, `${itemPath}.${key}: filled required enum default`);
        }
      });
      return filtered;
    });
    const bounded = normalizedItems.slice(0, contract.maxItems);
    if (bounded.length !== normalizedItems.length) {
      recordChange(changes, `${fieldPath}: truncated to ${contract.maxItems} items`);
    }
    return bounded;
  }
  return clone(value);
}

function normalizePatchProps(slide, supplied, changes) {
  const layout = getLayout(slide.layout_id);
  if (!layout) return supplied;
  const normalized = clone(supplied);
  const basePath = `slides.${slide.id}.props`;
  if (slide.layout_id === "cover-hero-v1") {
    applyAlias(normalized, "title", ["headline"], changes, basePath);
    applyAlias(normalized, "subtitle", ["subhead"], changes, basePath);
    applyAlias(normalized, "eyebrow", ["kicker"], changes, basePath);
    applyAlias(normalized, "meta", ["caption"], changes, basePath);
    applyAlias(normalized, "hero", ["image"], changes, basePath);
  } else if (slide.layout_id === "statement-focus-v1") {
    applyAlias(normalized, "support", ["context", "subtitle"], changes, basePath);
  } else if (slide.layout_id === "project-case-study-v1") {
    applyAlias(normalized, "positioning", ["summary", "description", "subtitle"], changes, basePath);
  }
  if (
    slide.layout_id === "table-data-v1"
    && /(?:甘特|gantt)/i.test([
      slide.props && slide.props.title,
      slide.outline_intent && slide.outline_intent.title,
      slide.outline_intent && slide.outline_intent.layout,
      slide.outline_intent && slide.outline_intent.visual,
    ].filter(Boolean).join("\n"))
    && normalized.variant !== "gantt"
  ) {
    normalized.variant = "gantt";
    recordChange(changes, `${basePath}.variant: normalized to gantt from bound outline intent`);
  }
  const sourceContract = layout.fields.source;
  if (
    sourceContract
    && layout.fields.insight
    && typeof normalized.source === "string"
    && Number.isInteger(sourceContract.maxChars)
    && characterLength(normalized.source) > sourceContract.maxChars
    && (!normalized.insight || !String(normalized.insight).trim())
  ) {
    const clientBenefit = extractClientBenefit(normalized.source);
    if (clientBenefit) {
      normalized.insight = fitCaption(
        `客户收益：${clientBenefit}`,
        layout.fields.insight.maxChars
      );
      normalized.source = "";
      recordChange(
        changes,
        `${basePath}.source: moved overlong need-solution-value copy to insight`
      );
    }
  }

  const result = {};
  Object.entries(normalized).forEach(([key, value]) => {
    const contract = layout.fields[key];
    if (!contract) {
      recordChange(changes, `${basePath}.${key}: dropped unknown field for ${slide.layout_id}`);
      return;
    }
    const expectedVisualContract = expectedVisualItemContract(slide.outline_intent);
    const expectedCollection = getVisualCollectionContract(
      slide.layout_id,
      expectedVisualContract && expectedVisualContract.dimension
    );
    const expectedVisualItems = expectedVisualContract && expectedVisualContract.count;
    if (
      contract.type === "array"
      && Array.isArray(value)
      && expectedCollection
      && expectedCollection.path === key
      && Number.isInteger(contract.maxItems)
      && expectedVisualItems > contract.maxItems
      && value.length >= expectedVisualItems
    ) {
      throw new Error(
        `${basePath}.${key}: layout capacity mismatch; bound outline requires ` +
        `${expectedVisualItems} visual items, but ${slide.layout_id} supports at most ` +
        `${contract.maxItems}. Choose a compatible layout before patching`
      );
    }
    const normalizedValue = normalizeValueToContract(
      value,
      contract,
      slide.layout_id,
      key,
      changes,
      `${basePath}.${key}`
    );
    if (normalizedValue === undefined) return;
    if (contract.type === "array" && Array.isArray(normalizedValue)) {
      const bounded = normalizedValue.slice(0, contract.maxItems);
      if (bounded.length !== normalizedValue.length) {
        recordChange(changes, `${basePath}.${key}: truncated to ${contract.maxItems} items`);
      }
      const fallbacks = Array.isArray(slide.props[key]) ? slide.props[key] : [];
      let fallbackIndex = 0;
      while (bounded.length < contract.minItems && fallbackIndex < fallbacks.length) {
        bounded.push(clone(fallbacks[fallbackIndex]));
        fallbackIndex += 1;
      }
      if (bounded.length !== normalizedValue.length) {
        recordChange(changes, `${basePath}.${key}: restored required collection size`);
      }
      result[key] = bounded;
      return;
    }
    result[key] = normalizedValue;
  });
  return result;
}

function restoreBoundOutlineTitles(deck, deckPath, changes) {
  const artifactRoot = path.dirname(deckPath);
  const outlinePath = path.join(artifactRoot, "outline.json");
  const contractPath = path.join(artifactRoot, "qa", "deck_contract.json");
  if (!fs.existsSync(outlinePath) || !fs.existsSync(contractPath)) return;
  let outline;
  let contract;
  try {
    outline = readJson(outlinePath);
    contract = readJson(contractPath);
  } catch (_error) {
    return;
  }
  if (!contract || !contract.outline_binding || !Array.isArray(outline.slides)) return;
  deck.slides.forEach((slide, index) => {
    const outlinePage = Number.isInteger(slide.source_outline_page)
      ? outline.slides[slide.source_outline_page - 1]
      : outline.slides[index];
    const expectedTitle = outlinePage && typeof outlinePage.title === "string"
      ? outlinePage.title.trim()
      : "";
    const layout = getLayout(slide.layout_id);
    if (!expectedTitle || !layout || !layout.fields.title) return;
    if (slide.props.title === expectedTitle) return;
    slide.props.title = expectedTitle;
    recordChange(
      changes,
      `slides.${slide.id}.props.title: restored bound outline page title`
    );
  });
}

function omitUnsupportedOptionalResearchProofs(deck, issues, changes) {
  const truth = deck && deck.truth_contract;
  const sourceFacts = truth && Array.isArray(truth.source_facts)
    ? truth.source_facts
    : [];
  const researchFacts = truth && Array.isArray(truth.research_facts)
    ? truth.research_facts
    : [];
  if (sourceFacts.length || !researchFacts.length) return false;

  const removals = new Map();
  issues.forEach(issue => {
    const match = String(issue || "").match(
      /^slides\.([^.]+)\.props\.proofs\.(\d+)(?:\.|:)/u
    );
    if (!match) return;
    const slideId = match[1];
    const proofIndex = Number(match[2]);
    if (!Number.isInteger(proofIndex)) return;
    if (!removals.has(slideId)) removals.set(slideId, new Set());
    removals.get(slideId).add(proofIndex);
  });

  let changed = false;
  deck.slides.forEach(slide => {
    const indexes = removals.get(slide.id);
    if (!indexes || !Array.isArray(slide.props && slide.props.proofs)) return;
    [...indexes]
      .sort((left, right) => right - left)
      .forEach(index => {
        if (index < 0 || index >= slide.props.proofs.length) return;
        slide.props.proofs.splice(index, 1);
        recordChange(
          changes,
          `slides.${slide.id}.props.proofs.${index}: omitted unsupported optional research proof`
        );
        changed = true;
      });
  });
  return changed;
}

function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === "--help" || argv[0] === "-h") usage(0);
  if (argv.length !== 2) usage();

  const deckPath = resolveArtifactPath(argv[0]);
  const patchPath = resolveArtifactPath(argv[1]);
  const normalizationChanges = [];
  const deck = readJson(deckPath);
  let patch = readPatchJson(patchPath, normalizationChanges);
  if (!isPlainObject(patch)) throw new Error("Deck patch must be an object");

  const allowedTopLevel = ["title", "truth_contract", "slides"];
  let unknownTopLevel = Object.keys(patch).filter(key => !allowedTopLevel.includes(key));
  if (
    patch.slides === undefined &&
    unknownTopLevel.length > 0 &&
    unknownTopLevel.every(key => /^slide-\d+$/i.test(key))
  ) {
    const directSlides = Object.fromEntries(
      unknownTopLevel.map(slideId => [slideId, patch[slideId]])
    );
    patch = Object.fromEntries(
      Object.entries(patch).filter(([key]) => !unknownTopLevel.includes(key))
    );
    patch.slides = directSlides;
    recordChange(
      normalizationChanges,
      'patch: nested direct slide-id keys under the top-level "slides" object'
    );
    unknownTopLevel = [];
  }
  if (unknownTopLevel.length) {
    const sourceFactHint = unknownTopLevel.includes("source_facts")
      ? "; source facts are scaffold-only under truth_contract.source_facts. " +
        "For user-authorized illustrative data, patch truth_contract.assumptions instead"
      : "";
    const slideEnvelopeHint = unknownTopLevel.every(key => /^slide-\d+$/i.test(key))
      ? '; slide ids must be nested under the top-level "slides" object, for example ' +
        '{"slides":{"slide-01":{"props":{...}}}}'
      : "";
    throw new Error(
      `Unknown deck patch field(s): ${unknownTopLevel.join(", ")}` +
      `${sourceFactHint}${slideEnvelopeHint}`
    );
  }
  const sourceBinding = runtimeSourceBinding();
  const truthWarnings = [];
  if (patch.title !== undefined) deck.title = patch.title;
  if (patch.truth_contract !== undefined) {
    if (!isPlainObject(patch.truth_contract)) {
      throw new Error("truth_contract patch must be an object");
    }
    const unknownTruthFields = Object.keys(patch.truth_contract)
      .filter(key => !["mode", "source_facts", "research_facts", "assumptions"].includes(key));
    if (unknownTruthFields.length) {
      throw new Error(
        `Unknown truth_contract patch field(s): ${unknownTruthFields.join(", ")}`
      );
    }
    if (!isPlainObject(deck.truth_contract)) {
      throw new Error(
        "deck.json is missing the scaffolded truth_contract; recreate only the initial scaffold"
      );
    }
    if (patch.truth_contract.mode !== undefined) {
      recordChange(
        normalizationChanges,
        "truth_contract.mode: ignored patch mutation and preserved scaffold mode"
      );
    }
    if (patch.truth_contract.source_facts !== undefined) {
      recordChange(
        normalizationChanges,
        "truth_contract.source_facts: ignored patch mutation and preserved scaffold facts"
      );
    }
    if (patch.truth_contract.research_facts !== undefined) {
      recordChange(
        normalizationChanges,
        "truth_contract.research_facts: ignored patch mutation and preserved scaffold research facts"
      );
    }
    if (patch.truth_contract.assumptions !== undefined) {
      const assumptionBinding = validateAssumptionsAgainstRuntime(
        patch.truth_contract.assumptions
      );
      if (assumptionBinding.issues.length) {
        truthWarnings.push(...assumptionBinding.issues);
      }
      deck.truth_contract.assumptions = clone(patch.truth_contract.assumptions);
    }
  }

  const slidePatches = patch.slides === undefined ? {} : patch.slides;
  if (!isPlainObject(slidePatches)) {
    throw new Error("deck.patch.json slides must be an object keyed by existing slide id");
  }
  const slidesById = new Map(deck.slides.map(slide => [slide.id, slide]));
  const patchedSlides = [];
  for (const [slideId, slidePatch] of Object.entries(slidePatches)) {
    const slide = slidesById.get(slideId);
    if (!slide) throw new Error(`Unknown slide id in patch: ${slideId}`);
    if (!isPlainObject(slidePatch)) {
      throw new Error(`slides.${slideId}: expected object`);
    }
    const unknownSlideFields = Object.keys(slidePatch)
      .filter(key => !["props", "background"].includes(key));
    if (unknownSlideFields.length) {
      throw new Error(
        `slides.${slideId}: only props/background may be patched; rejected ` +
        unknownSlideFields.join(", ")
      );
    }
    if (slidePatch.props !== undefined) {
      if (!isPlainObject(slidePatch.props)) {
        throw new Error(`slides.${slideId}.props: expected object`);
      }
      const normalizedPatch = normalizePatchProps(
        slide,
        slidePatch.props,
        normalizationChanges
      );
      slide.props = mergeDefaults(slide.props, normalizedPatch);
    }
    if (slidePatch.background === null) delete slide.background;
    else if (slidePatch.background !== undefined) {
      slide.background = generatedMedia(
        slidePatch.background,
        "AI 生成的背景概念视觉",
        normalizationChanges,
        `slides.${slide.id}.background`
      );
    }
    patchedSlides.push(slideId);
  }

  reconcileReadyManifestMedia(deck, deckPath, normalizationChanges);

  restoreBoundOutlineTitles(deck, deckPath, normalizationChanges);

  let validation = validateAndNormalizeDeck(deck);
  if (!validation.ok) {
    throw new Error(`Patched deck is invalid:\n${validation.issues.join("\n")}`);
  }
  let truthGuard = { changes: [], warnings: [...truthWarnings] };
  const truthContract = validation.normalized.truth_contract;
  const shouldGuardSourceTruth = Boolean(
    truthContract
    && truthContract.mode === "source_bound"
    && sourceBinding.available
    && !sourceBinding.allows_assumptions
  );
  if (shouldGuardSourceTruth) {
    const sanitized = sanitizeStrictSourceDeck(validation.normalized);
    validation = validateAndNormalizeDeck(sanitized.deck);
    if (!validation.ok) {
      throw new Error(`Truth-normalized deck is invalid:\n${validation.issues.join("\n")}`);
    }
    let truthResult = validateSourceBoundDeck(validation.normalized);
    truthGuard = {
      changes: sanitized.changes,
      warnings: [
        ...truthWarnings,
        ...truthResult.issues,
        ...truthResult.warnings,
      ],
    };
    if (
      [...truthResult.issues, ...truthResult.warnings].length
      && omitUnsupportedOptionalResearchProofs(
        validation.normalized,
        [...truthResult.issues, ...truthResult.warnings],
        truthGuard.changes
      )
    ) {
      validation = validateAndNormalizeDeck(validation.normalized);
      if (!validation.ok) {
        throw new Error(`Research-proof-normalized deck is invalid:\n${validation.issues.join("\n")}`);
      }
      truthResult = validateSourceBoundDeck(validation.normalized);
      truthGuard.warnings = [
        ...truthWarnings,
        ...truthResult.issues,
        ...truthResult.warnings,
      ];
    }
  }
  fs.mkdirSync(path.dirname(deckPath), { recursive: true });
  fs.writeFileSync(deckPath, `${JSON.stringify(validation.normalized, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({
    ok: true,
    deck: deckPath,
    patch: patchPath,
    patched_slides: patchedSlides,
    normalization_changes: normalizationChanges,
    truth_guard_changes: truthGuard.changes,
    truth_guard_warnings: truthGuard.warnings,
    truth_warning_count: truthGuard.warnings.length,
    truth_mode: validation.normalized.truth_contract
      ? validation.normalized.truth_contract.mode
      : null,
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

module.exports = { reconcileReadyManifestMedia };
